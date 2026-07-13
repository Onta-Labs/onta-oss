"""ONTA-270 — ontology-version stamp on A5 placement plans + P6 rejects stale plans.

The race this closes (the read-modify-write side of the SchemaResolver ontology
race, COMPLEMENTING ONTA-268's per-sub-query resolver + ontology-write lock): P5
computes a placement against ontology state T; a concurrent run advances it to
T+1 while P5 is still (slowly) extracting; P5 then applies its plan computed
against the STALE snapshot T → duplicate terms (a synonym of a type the other run
just minted) even though every stage individually looked deterministic.

Fix: `ingest()` fingerprints the ontology it read (``ontology_version``) and
stamps it onto the A5 placement plan; the apply (`_resolve_and_insert`, P6)
reconciles the stamp against the CURRENT ontology under the ontology-write lock
and, on mismatch, REJECTS the stale basis and RECOMPUTES against the new version
(`_reconcile_ontology_version`) so no duplicate term is minted.

Two layers of coverage:

1. Pure-seam unit tests on the ``ontology_version`` fingerprint (deterministic,
   order-independent, change-detecting, schema-object/plain-string parity) and on
   ``_reconcile_ontology_version`` (match = no-op; mismatch = refresh-in-place).
2. An end-to-end pyoxigraph-backed test proving the "done when": a stale plan is
   REJECTED (the duplicate type is NOT written) and the entity lands on the
   existing term — with a guard-OFF control that DOES mint the duplicate, so the
   version stamp is provably load-bearing (not vacuously passing).

Skipped where pyoxigraph is not installed (not a declared CI dep; runs locally),
matching ``tests/test_resolver_reentrancy.py``.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

import pytest

from cograph_client.graph.ontology_queries import (  # noqa: E402
    insert_type,
    ontology_version,
    type_uri,
)

# --------------------------------------------------------------------------- #
# 1. Pure fingerprint seam — no store, no pyoxigraph.
# --------------------------------------------------------------------------- #


def test_ontology_version_deterministic_and_order_independent():
    """Same ontology content → same version, regardless of dict/insertion order."""
    a = ontology_version(
        {"B": "", "A": ""},
        {"A": {"y": "string", "x": "integer"}},
        {"A": "Base"},
    )
    b = ontology_version(
        {"A": "", "B": ""},
        {"A": {"x": "integer", "y": "string"}},
        {"A": "Base"},
    )
    assert a == b
    # Stable across calls (no timestamp/nonce).
    assert a == ontology_version({"A": "", "B": ""}, {"A": {"x": "integer", "y": "string"}}, {"A": "Base"})


def test_ontology_version_changes_on_any_advance():
    """A new type, a new attribute, or a new subclass edge each shifts the version."""
    base = ontology_version({"A": ""}, {"A": {"x": "integer"}}, {})
    assert base != ontology_version({"A": "", "B": ""}, {"A": {"x": "integer"}}, {})  # new type
    assert base != ontology_version({"A": ""}, {"A": {"x": "integer", "y": "string"}}, {})  # new attr
    assert base != ontology_version({"A": ""}, {"A": {"x": "float"}}, {})  # changed range
    assert base != ontology_version({"A": ""}, {"A": {"x": "integer"}}, {"A": "Base"})  # new edge


def test_ontology_version_schema_object_and_string_parity():
    """An AttributeSchema-like object (with ``.datatype``) and a plain datatype
    string hash identically for the same (type, attr, datatype) content — so the
    resolver's in-memory shape and a raw mapping agree."""

    class _Schema:
        def __init__(self, dt: str) -> None:
            self.datatype = dt

    assert ontology_version({"A": ""}, {"A": {"x": _Schema("integer")}}) == ontology_version(
        {"A": ""}, {"A": {"x": "integer"}}
    )


def test_empty_ontology_version_matches_frozen_a5_stamp():
    """The cold-start (empty-read) fingerprint the boundary A5 fixtures freeze is a
    stable constant — a regression tripwire on the stamp value itself."""
    assert ontology_version({}, {}) == "e3b0c44298fc1c14"


# --------------------------------------------------------------------------- #
# Pyoxigraph-backed layer (real SchemaResolver over an in-process store).
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri  # noqa: E402
from cograph_client.resolver.models import (  # noqa: E402
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.type_matcher import MatchVerdict, TypeMatch  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_CLASS = "http://www.w3.org/2000/01/rdf-schema#Class"

_TENANT = "onta270"
_ONTO_GRAPH = tenant_graph_uri(_TENANT)
_KG_GRAPH = kg_graph_uri(_TENANT, "kg1")


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — the same
    shape ``tests/test_resolver_reentrancy.py`` uses."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)

    async def batch_exists(self, sparql: str) -> set[str]:
        data = await self.query(sparql)
        rows = data.get("results", {}).get("bindings", [])
        return {r["entity"]["value"] for r in rows if "entity" in r}


def _make_resolver(neptune) -> SchemaResolver:
    os.environ["COGRAPH_ER_ENABLED"] = "0"  # deterministic URIs, no signal-hash suffix
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"onta270_verdicts_{time.time_ns()}.json"
    return SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=JsonVerdictCache(cache_path),
        embedding_service=None,
    )


def _doctor_entity() -> ExtractedEntity:
    return ExtractedEntity(
        type_name="Doctor",
        id="doc1",
        attributes=[ExtractedAttribute(name="city", value="SF", datatype="string")],
    )


def _synonym_matcher(canonical: str, synonym: str):
    """A TypeMatcher.match stub modelling the real matcher's key property: it can
    only fold a proposed type onto a canonical one that is IN the candidate set it
    is shown. So ``synonym`` collapses to ``canonical`` ONLY when ``canonical`` is
    present in ``existing_types`` — exactly the difference a stale (pre-advance)
    snapshot vs a reconciled (post-advance) snapshot makes."""

    async def match(proposed, description, existing_types, **_kw):
        if proposed == synonym and canonical in existing_types:
            return TypeMatch(
                proposed=proposed, resolved=canonical,
                verdict=MatchVerdict.SAME, confidence=1.0, is_new=False,
            )
        return TypeMatch(
            proposed=proposed, resolved=proposed,
            verdict=MatchVerdict.DIFFERENT, confidence=1.0, is_new=True,
        )

    return match


async def _class_names(n: PyoxiNeptune, graph: str) -> set[str]:
    got = await n.query(
        f"SELECT ?t WHERE {{ GRAPH <{graph}> {{ ?t <{RDF_TYPE}> <{RDFS_CLASS}> }} }}"
    )
    return {b["t"]["value"] for b in got["results"]["bindings"]}


async def _instance_types(n: PyoxiNeptune, graph: str, subject_frag: str) -> set[str]:
    got = await n.query(
        f"SELECT ?s ?t WHERE {{ GRAPH <{graph}> {{ ?s <{RDF_TYPE}> ?t }} }}"
    )
    return {
        b["t"]["value"]
        for b in got["results"]["bindings"]
        if subject_frag in b["s"]["value"]
    }


# --------------------------------------------------------------------------- #
# 2. The reconcile seam directly: match = no-op, mismatch = refresh-in-place.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reconcile_detects_and_refreshes_stale_snapshot():
    n = PyoxiNeptune()
    r = _make_resolver(n)

    # A snapshot read against the EMPTY ontology, and the stamp for it.
    existing_types: dict = {}
    existing_attrs: dict = {}
    parent_of: dict = {}
    stamp = ontology_version(existing_types, existing_attrs, parent_of)

    # No advance yet → reconcile is a no-op and the snapshot is unchanged.
    current = await r._reconcile_ontology_version(
        _ONTO_GRAPH, stamp, existing_types, existing_attrs, parent_of
    )
    assert current == stamp
    assert existing_types == {}

    # A concurrent run advances the ontology (T → T+1).
    await n.update(insert_type(_ONTO_GRAPH, "Physician", ""))

    # Now reconcile against the OLD stamp detects staleness and refreshes in place.
    current2 = await r._reconcile_ontology_version(
        _ONTO_GRAPH, stamp, existing_types, existing_attrs, parent_of
    )
    assert current2 != stamp, "advanced ontology must yield a new version"
    assert "Physician" in existing_types, "stale snapshot must be refreshed to T+1"


# --------------------------------------------------------------------------- #
# 3. End-to-end: a stale plan is REJECTED (no duplicate term) — with a guard-OFF
#    control that DOES mint the duplicate (the stamp is load-bearing).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stale_plan_rejected_no_duplicate_terms():
    """P5 reads the empty ontology and stamps the plan; a concurrent run mints the
    canonical ``Physician`` DURING extraction; P6 detects the advance and
    re-resolves ``Doctor`` onto the now-visible ``Physician`` instead of minting a
    duplicate. The ontology ends with Physician only — no ``Doctor`` type."""
    n = PyoxiNeptune()
    r = _make_resolver(n)
    r._type_matcher.match = _synonym_matcher("Physician", "Doctor")

    async def concurrent_advance_then_extract(content, content_type, existing_types=None, constraint=None):
        # The T → T+1 write lands in the window AFTER ingest read the (empty)
        # ontology and BEFORE the apply — exactly the race ONTA-270 closes.
        await n.update(insert_type(_ONTO_GRAPH, "Physician", ""))
        return ExtractionResult(entities=[_doctor_entity()])

    r._extract = concurrent_advance_then_extract

    await r.ingest(
        "payload", _TENANT, content_type="text", source="p5", instance_graph=_KG_GRAPH,
    )

    classes = await _class_names(n, _ONTO_GRAPH)
    assert type_uri("Physician") in classes, "the concurrent canonical type must survive"
    assert type_uri("Doctor") not in classes, (
        "stale plan was applied verbatim — Doctor duplicate minted despite the version stamp"
    )
    # The entity landed on the existing canonical term, not a duplicate.
    inst_types = await _instance_types(n, _KG_GRAPH, "/entities/")
    assert type_uri("Physician") in inst_types
    assert type_uri("Doctor") not in inst_types


@pytest.mark.asyncio
async def test_guard_off_control_mints_the_duplicate():
    """Load-bearing control: the SAME apply with NO version stamp (the legacy
    direct-caller path) against a stale empty snapshot DOES mint the ``Doctor``
    duplicate — proving the stamp in the test above is what prevents it, not some
    incidental convergence."""
    n = PyoxiNeptune()
    r = _make_resolver(n)
    r._type_matcher.match = _synonym_matcher("Physician", "Doctor")

    # The concurrent run already advanced the ontology (Physician exists)...
    await n.update(insert_type(_ONTO_GRAPH, "Physician", ""))

    # ...but the apply runs with a STALE empty snapshot and NO stamp (guard off).
    result = IngestResult(entities_extracted=1, batch_id="ctl")
    await r._resolve_and_insert(
        ExtractionResult(entities=[_doctor_entity()]),
        _ONTO_GRAPH,
        {},  # stale existing_types (as if read before Physician landed)
        {},  # stale existing_attrs
        "ctl",
        result,
        {},  # entity_uri_map
        {},  # entity_type_map
        "ctl",
        instance_graph=_KG_GRAPH,
        parent_of={},
        ontology_version_stamp=None,  # GUARD OFF
    )

    classes = await _class_names(n, _ONTO_GRAPH)
    assert type_uri("Physician") in classes
    assert type_uri("Doctor") in classes, (
        "without the version stamp the stale plan must mint the Doctor duplicate "
        "(if this fails the control is vacuous and the e2e proves nothing)"
    )
