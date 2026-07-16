"""ONTA-347: persist the ORIGINAL surface form alongside the canonical value.

When the A3 clean stage COERCES or CANONICALIZES a value (``CleanOutcome.TRANSFORMED``,
``raw_value != clean_value``), the writer persists only the canonical value as the
attribute; the original surface form the source actually carried survives only in a
log. P4 (Verify, next wave) must compare the canonical value against evidence and
needs the original preserved IN THE GRAPH. This ticket persists it as a per-attribute
SURFACE-FORM companion on the ``attr_meta/`` namespace — structurally invisible to
every user surface (``is_internal_predicate``) yet queryable — minted via the shared
``attr_provenance_companion_uri`` minter and threaded into the SAME shared write path
(``insert_facts``) as the canonical value.

Three layers:

1. Pure builders (no store): ``build_surface_form_companion`` /
   ``surface_form_companion_triples`` emit the companion ONLY on transform, and the
   companion predicate is ``is_internal_predicate`` (invisible to Explorer /
   type-stats).
2. A4 (``validate_triple``): the returned ``ValidatedTriple`` carries the companion
   when — and only when — A3 transformed the value (both the conformed-canonicalized
   OK case and the coerced case), and NEVER without a ``type_name``.
3. THE acceptance bar over an in-process pyoxigraph store: ingest a datetime
   ``"12/31/2020"`` through the REAL ``SchemaResolver`` and assert the graph stores
   BOTH the canonical ISO value AND a queryable ``attr_meta`` surface-form companion
   holding the ORIGINAL ``"12/31/2020"``; that the companion flows through
   ``insert_facts`` (the shared write path); that it is ``is_internal_predicate``;
   that a re-ingest is idempotent (no duplicate companion); and the LOAD-BEARING
   control that a value needing NO coercion emits NO companion.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import time

import pytest

from cograph_client.graph.predicates import ATTR_META_NS, is_internal_predicate
from cograph_client.graph.provenance import (
    SURFACE_FORM_SUFFIX,
    attr_provenance_companion_uri,
    build_surface_form_companion,
)
from cograph_client.normalization.clean import (
    clean_value,
    surface_form_companion_triples,
)
from cograph_client.resolver.models import RejectedValue, ValidatedTriple
from cograph_client.resolver.validator import validate_triple

SUBJ = "https://cograph.tech/entities/Event/e1"


# --------------------------------------------------------------------------- #
# 1. Pure builders — the companion is emitted ONLY on transform, and is internal
# --------------------------------------------------------------------------- #
def test_surface_form_suffix_and_uri_shape():
    """The companion mints on the attr_meta namespace via the SHARED minter — never a
    hand-built IRI (write-path/entity-uri convergence)."""
    uri = attr_provenance_companion_uri("Event", "start_date", SURFACE_FORM_SUFFIX)
    assert uri == f"{ATTR_META_NS}Event/start_date/surface_form"
    assert build_surface_form_companion(SUBJ, "Event", "start_date", "12/31/2020") == [
        (SUBJ, uri, "12/31/2020")
    ]


def test_surface_form_companion_predicate_is_internal():
    """The whole attr_meta namespace is is_internal_predicate — so the companion is
    excluded from Explorer chips/columns + type-stats while staying queryable."""
    uri = attr_provenance_companion_uri("Event", "start_date", SURFACE_FORM_SUFFIX)
    assert is_internal_predicate(uri) is True
    # Even asserted as a relationship (defensive) it stays hidden — companions are
    # always literal-valued metadata.
    assert is_internal_predicate(uri, is_relationship=True) is True


def test_builder_omits_when_any_component_empty():
    """Nothing to preserve → no triple (so a caller can unconditionally extend)."""
    assert build_surface_form_companion("", "Event", "a", "v") == []
    assert build_surface_form_companion(SUBJ, "", "a", "v") == []
    assert build_surface_form_companion(SUBJ, "Event", "", "v") == []
    assert build_surface_form_companion(SUBJ, "Event", "a", "") == []


def test_clean_helper_emits_only_on_transform():
    """surface_form_companion_triples fires on TRANSFORMED (coerce OR canonicalize),
    and is silent on PASSED (verbatim) and DROPPED (nothing written)."""
    uri = attr_provenance_companion_uri("Event", "start_date", SURFACE_FORM_SUFFIX)

    # TRANSFORMED (conformed-but-canonicalized): datetime "12/31/2020" -> ISO.
    canon = clean_value("12/31/2020", "datetime", entity_id="e1", attribute="start_date")
    assert canon.raw_value == "12/31/2020" and canon.clean_value != "12/31/2020"
    assert surface_form_companion_triples(canon, subject=SUBJ, type_name="Event") == [
        (SUBJ, uri, "12/31/2020")
    ]

    # TRANSFORMED (coerced): boolean "yes" -> "true".
    coerced = clean_value("yes", "boolean", entity_id="e1", attribute="flag")
    furi = attr_provenance_companion_uri("Event", "flag", SURFACE_FORM_SUFFIX)
    assert surface_form_companion_triples(coerced, subject=SUBJ, type_name="Event") == [
        (SUBJ, furi, "yes")
    ]

    # PASSED (verbatim string) → no companion.
    passed = clean_value("Launch", "string", entity_id="e1", attribute="name")
    assert surface_form_companion_triples(passed, subject=SUBJ, type_name="Event") == []

    # DROPPED (uncoercible integer) → no companion.
    dropped = clean_value("notanumber", "integer", entity_id="e1", attribute="n")
    assert surface_form_companion_triples(dropped, subject=SUBJ, type_name="Event") == []


# --------------------------------------------------------------------------- #
# 2. A4 (validate_triple) carries the companion — only on transform + with a type
# --------------------------------------------------------------------------- #
def test_validate_triple_carries_companion_for_canonicalized_datetime():
    """A conforming-but-canonicalized value is A3 TRANSFORMED / A4 OK — the companion
    is present even though ``original_value`` is not set on the OK branch."""
    v = validate_triple(
        SUBJ, "p", "12/31/2020", "datetime",
        entity_id="e1", attribute_name="start_date", type_name="Event",
    )
    assert isinstance(v, ValidatedTriple)
    assert v.object.startswith("2020-12-31T00:00:00")  # canonical ISO, typed
    assert v.original_value is None  # OK branch does not set it
    uri = attr_provenance_companion_uri("Event", "start_date", SURFACE_FORM_SUFFIX)
    assert v.surface_form_companion == (SUBJ, uri, "12/31/2020")


def test_validate_triple_carries_companion_for_coerced_value():
    v = validate_triple(
        SUBJ, "p", "yes", "boolean",
        entity_id="e1", attribute_name="flag", type_name="Event",
    )
    assert isinstance(v, ValidatedTriple)
    assert v.object.startswith("true")
    uri = attr_provenance_companion_uri("Event", "flag", SURFACE_FORM_SUFFIX)
    assert v.surface_form_companion == (SUBJ, uri, "yes")


def test_validate_triple_no_companion_without_type_name():
    """qc/boundary + enrichment call validate_triple WITHOUT type_name — no companion,
    so A4 output (and the frozen a4/a5 fixtures) is byte-identical to pre-347."""
    v = validate_triple(SUBJ, "p", "12/31/2020", "datetime", attribute_name="start_date")
    assert isinstance(v, ValidatedTriple) and v.surface_form_companion is None


def test_validate_triple_no_companion_for_verbatim_value():
    v = validate_triple(
        SUBJ, "p", "Launch", "string",
        attribute_name="name", type_name="Event",
    )
    assert isinstance(v, ValidatedTriple) and v.surface_form_companion is None


def test_validate_triple_dropped_has_no_companion():
    v = validate_triple(
        SUBJ, "p", "notanumber", "integer",
        attribute_name="n", type_name="Event",
    )
    assert isinstance(v, RejectedValue)


# --------------------------------------------------------------------------- #
# 3. End-to-end over a pyoxigraph store (the acceptance bar)
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.ontology_queries import attr_uri, entity_uri  # noqa: E402
from cograph_client.graph.queries import kg_graph_uri  # noqa: E402
from cograph_client.resolver.models import (  # noqa: E402
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402

TENANT = "onta347"
KG = "events"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
SRC = "https://example.com/events"
MARKER = "EVENTS_PAYLOAD"

# One entity with a datetime attribute that A3 CANONICALIZES ("12/31/2020" -> ISO)
# and a short verbatim string attribute (the load-bearing no-coercion control).
EXTRACTION = ExtractionResult(
    entities=[
        ExtractedEntity(
            type_name="Event",
            id="e1",
            attributes=[
                ExtractedAttribute(name="start_date", value="12/31/2020", datatype="datetime"),
                ExtractedAttribute(name="status", value="active", datatype="string"),
            ],
        ),
    ],
)

ENTITY = entity_uri("Event", "e1")
START_PRED = attr_uri("Event", "start_date")
START_COMPANION = attr_provenance_companion_uri("Event", "start_date", SURFACE_FORM_SUFFIX)
STATUS_COMPANION = attr_provenance_companion_uri("Event", "status", SURFACE_FORM_SUFFIX)


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query/update/batch_exists returning SPARQL-1.1 JSON, union-of-named-graphs
    default (as tests/test_fact_id_replay.py + test_conflict_policy.py use)."""

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


@pytest.fixture(autouse=True)
def _er_disabled(monkeypatch):
    """Deterministic URIs (no signal-hash suffixes) — ER off, restored at teardown."""
    monkeypatch.setenv("COGRAPH_ER_ENABLED", "0")


def _make_resolver(neptune) -> SchemaResolver:
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"sf_verdicts_{time.time_ns()}.json"
    resolver = SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=JsonVerdictCache(cache_path),
        embedding_service=None,
    )

    async def _fake_extract(content, content_type, existing_types=None, constraint=None):
        await asyncio.sleep(0)
        return EXTRACTION

    async def _no_adjudicate(candidates):
        # The AMBIGUOUS free-text tier is the only LLM call on the ingest path;
        # short values never reach it, but stub it so the test is hermetic regardless.
        return set(), set()

    resolver._extract = _fake_extract
    resolver._adjudicate_free_text = _no_adjudicate
    return resolver


async def _objs(neptune, subject: str, predicate: str) -> list[str]:
    data = await neptune.query(
        f"SELECT ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{subject}> <{predicate}> ?o }} }}"
    )
    return [b["o"]["value"] for b in data["results"]["bindings"]]


async def _count(neptune, subject: str, predicate: str) -> int:
    data = await neptune.query(
        f"SELECT (COUNT(?o) AS ?n) WHERE {{ GRAPH <{INSTANCE_GRAPH}> "
        f"{{ <{subject}> <{predicate}> ?o }} }}"
    )
    return int(data["results"]["bindings"][0]["n"]["value"])


async def _ingest(neptune) -> None:
    resolver = _make_resolver(neptune)
    await resolver.ingest(
        MARKER, TENANT, content_type="text", source=SRC, instance_graph=INSTANCE_GRAPH,
    )


@pytest.mark.asyncio
async def test_surface_form_companion_persisted_and_flows_through_insert_facts(monkeypatch):
    """THE acceptance bar. Ingesting a canonicalized datetime persists BOTH the
    canonical ISO value (as the attribute) AND a queryable attr_meta surface-form
    companion holding the ORIGINAL "12/31/2020" — and that companion flows through the
    shared write path (insert_facts)."""
    import cograph_client.resolver.schema_resolver as sr

    captured: list[tuple[str, str, str]] = []
    real_insert = sr.insert_facts

    async def _spy(neptune, instance_graph, instance_triples, **kwargs):
        captured.extend(instance_triples)
        return await real_insert(neptune, instance_graph, instance_triples, **kwargs)

    monkeypatch.setattr(sr, "insert_facts", _spy)

    n = PyoxiNeptune()
    await _ingest(n)

    # (a) The attribute stores the CANONICAL ISO value (typed dateTime).
    canonical = await _objs(n, ENTITY, START_PRED)
    assert len(canonical) == 1 and canonical[0].startswith("2020-12-31T00:00:00"), canonical

    # (b) A queryable attr_meta companion holds the ORIGINAL surface form.
    surface = await _objs(n, ENTITY, START_COMPANION)
    assert surface == ["12/31/2020"], surface

    # (c) The companion flowed through the SHARED write path (insert_facts) — not a
    #     bespoke writer — in the SAME call as the canonical value. ``captured`` holds
    #     the raw instance_triples: the companion is a plain string, the canonical
    #     object the TYPED dateTime literal.
    assert (ENTITY, START_COMPANION, "12/31/2020") in captured
    assert any(
        s == ENTITY and p == START_PRED and o.startswith("2020-12-31T00:00:00")
        for (s, p, o) in captured
    ), "the canonical value must ride the SAME insert_facts call as its companion"


@pytest.mark.asyncio
async def test_companion_is_internal_and_control_emits_nothing():
    """The companion is is_internal_predicate (excluded from type-stats / Explorer
    columns), and the LOAD-BEARING control: a value needing NO coercion ("active")
    emits NO surface-form companion."""
    n = PyoxiNeptune()
    await _ingest(n)

    # The companion predicate is structurally invisible to every user surface.
    assert is_internal_predicate(START_COMPANION) is True

    # CONTROL: the verbatim string attribute wrote its value but NO surface-form
    # companion (nothing was transformed, so there is no divergence to record).
    assert await _objs(n, ENTITY, attr_uri("Event", "status")) == ["active"]
    assert await _objs(n, ENTITY, STATUS_COMPANION) == []


@pytest.mark.asyncio
async def test_reingest_is_idempotent_no_duplicate_companion():
    """A re-ingest of the same row adds no duplicate surface-form companion (the
    companion triple is deterministic → the store dedups it)."""
    n = PyoxiNeptune()
    await _ingest(n)
    assert await _count(n, ENTITY, START_COMPANION) == 1

    await _ingest(n)  # replay into the same store
    assert await _count(n, ENTITY, START_COMPANION) == 1, "companion must not duplicate"
    # The canonical value is likewise single.
    assert await _count(n, ENTITY, START_PRED) == 1
