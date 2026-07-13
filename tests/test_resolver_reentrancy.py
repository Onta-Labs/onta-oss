"""REAL-resolver reentrancy tests (ONTA-268).

The known SchemaResolver non-reentrancy: one shared resolver kept the live target
graph + parent map on the INSTANCE (``self._instance_graph`` / ``self._parent_of``,
set at the top of ``ingest`` and read deep on the write path), so two ``ingest``
calls interleaving on ONE shared resolver clobbered each other's target — data
leaked across workspaces (``qc/isolation.py::check_isolation`` catches it) and
type creation raced.

``tests/test_qc_isolation.py`` models the hazard with a FAKE resolver and only
OBSERVES the leak. THIS file exercises the REAL ``SchemaResolver`` end-to-end over
an in-process pyoxigraph store, with the LLM extraction stubbed (no API key in CI),
and PROVES the fix:

1. ``test_real_shared_resolver_interleaved_is_isolated`` — a SINGLE shared real
   resolver runs two ``ingest`` calls INTERLEAVED (``asyncio.gather``), each into
   its own workspace. ``check_isolation`` returns ``[]``. This is the load-bearing
   flip: it FAILS on the pre-ONTA-268 code (shared ``self._instance_graph``
   clobbers → cross_workspace_fact) and PASSES after the call-local state fix.
2. ``test_real_perrun_resolvers_interleaved_is_isolated`` — the production shape:
   one resolver PER sub-query, sharing one ontology-write lock, interleaved. Clean.
3. ``test_concurrent_ingest_ontology_equals_serial`` — concurrent ingest of a
   multi-payload set produces the SAME ontology (types + subClassOf + attributes)
   as serial ingest, and the shared ontology-write lock is provably mutually
   exclusive (max 1 holder) where an un-serialized control genuinely overlaps —
   i.e. the lock prevents type-creation races.

Skipped where pyoxigraph is not installed (not a declared CI dep; runs locally).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
import time

import pytest

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri  # noqa: E402
from cograph_client.qc.isolation import (  # noqa: E402
    WorkspaceScope,
    check_isolation,
    format_isolation,
)
from cograph_client.resolver.models import (  # noqa: E402
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402

RDFS_SUBCLASSOF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
TYPES = "https://cograph.tech/types/"

# Two distinct workspaces (distinct tenants → distinct base graphs), each with its
# own provenance source and its own KG target — the "two-workspace" contract the
# isolation checker keys off.
WS_A = WorkspaceScope(tenant="reent-alpha", source="run:alpha", kg="providers", label="alpha")
WS_B = WorkspaceScope(tenant="reent-beta", source="run:beta", kg="hospitals", label="beta")


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update()/batch_exists() returning SPARQL-1.1 JSON, union-of-named-
    graphs default (matches production + the other qc/resolver pyoxi tests)."""

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


def _make_resolver(neptune, *, ontology_lock=None) -> SchemaResolver:
    """A REAL SchemaResolver wired the way `qc.scenario._make_resolver` does: a
    fresh per-run verdict cache, no embeddings. ER disabled so URI minting stays
    deterministic (no signal-hash suffixes) and the isolation attribution is
    unambiguous. Optionally shares an ontology-write lock across resolvers."""
    os.environ["COGRAPH_ER_ENABLED"] = "0"
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"reent_verdicts_{time.time_ns()}.json"
    return SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=JsonVerdictCache(cache_path),
        embedding_service=None,
        ontology_lock=ontology_lock,
    )


# --------------------------------------------------------------------------- #
# Deterministic extraction stub (no LLM). Maps content marker -> entities, with a
# real await point so two interleaved ingests actually overlap on a shared store.
# --------------------------------------------------------------------------- #
def _stub_extract(entities_by_marker: dict[str, ExtractionResult]):
    async def fake_extract(content, content_type, existing_types=None, constraint=None):
        await asyncio.sleep(0)  # yield: the sibling ingest interleaves HERE
        return entities_by_marker[content]

    return fake_extract


def _entity(type_name: str, eid: str, *, parent_chain=None, city="SF") -> ExtractedEntity:
    return ExtractedEntity(
        type_name=type_name,
        id=eid,
        parent_chain=list(parent_chain or []),
        attributes=[ExtractedAttribute(name="city", value=city, datatype="string")],
    )


# Per-workspace payloads: distinct types + ids so an entity attributes unambiguously.
_A_MARKER, _B_MARKER = "ALPHA_PAYLOAD", "BETA_PAYLOAD"
_A_EXTRACTION = ExtractionResult(entities=[_entity("Physician", "alpha_p1")])
_B_EXTRACTION = ExtractionResult(entities=[_entity("Hospital", "beta_h1")])


async def _count(n: PyoxiNeptune, graph: str) -> int:
    got = await n.query(f"SELECT (COUNT(*) AS ?c) WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}")
    return int(got["results"]["bindings"][0]["c"]["value"])


# --------------------------------------------------------------------------- #
# 1. The load-bearing flip: a SINGLE shared real resolver, interleaved, is clean.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_real_shared_resolver_interleaved_is_isolated():
    """ONE shared real ``SchemaResolver`` runs two ``ingest`` calls interleaved via
    ``asyncio.gather``, each into its OWN workspace/instance_graph. Post-ONTA-268
    (call-local ``_instance_graph`` / ``_parent_of``) the run is perfectly
    isolated. Pre-fix this FAILS: the shared ``self._instance_graph`` set by the
    second ingest clobbers the first mid-flight, misdirecting alpha's triples into
    beta's graph (cross_workspace_fact)."""
    n = PyoxiNeptune()
    resolver = _make_resolver(n)
    resolver._extract = _stub_extract({_A_MARKER: _A_EXTRACTION, _B_MARKER: _B_EXTRACTION})

    await asyncio.gather(
        resolver.ingest(
            _A_MARKER, WS_A.tenant, content_type="text",
            source=WS_A.source, instance_graph=WS_A.instance_graph,
        ),
        resolver.ingest(
            _B_MARKER, WS_B.tenant, content_type="text",
            source=WS_B.source, instance_graph=WS_B.instance_graph,
        ),
    )

    violations = await check_isolation(n, [WS_A, WS_B])
    assert violations == [], format_isolation(violations)
    # Not vacuous: each workspace's data actually landed in ITS OWN graph.
    assert await _count(n, WS_A.instance_graph) > 0
    assert await _count(n, WS_B.instance_graph) > 0


# --------------------------------------------------------------------------- #
# 2. Production shape: per-sub-query resolvers sharing one lock, interleaved.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_real_perrun_resolvers_interleaved_is_isolated():
    """The web_ingest_cap pattern: one resolver PER sub-query, all sharing a single
    ontology-write lock, interleaved over one store. Each writes only its own
    workspace's graphs → zero cross-workspace leakage."""
    n = PyoxiNeptune()
    lock = asyncio.Lock()
    ra, rb = _make_resolver(n, ontology_lock=lock), _make_resolver(n, ontology_lock=lock)
    assert ra._ontology_lock is rb._ontology_lock  # one shared lock across resolvers
    ra._extract = _stub_extract({_A_MARKER: _A_EXTRACTION})
    rb._extract = _stub_extract({_B_MARKER: _B_EXTRACTION})

    await asyncio.gather(
        ra.ingest(
            _A_MARKER, WS_A.tenant, content_type="text",
            source=WS_A.source, instance_graph=WS_A.instance_graph,
        ),
        rb.ingest(
            _B_MARKER, WS_B.tenant, content_type="text",
            source=WS_B.source, instance_graph=WS_B.instance_graph,
        ),
    )

    violations = await check_isolation(n, [WS_A, WS_B])
    assert violations == [], format_isolation(violations)


# --------------------------------------------------------------------------- #
# 3. Concurrent ingest ontology == serial ingest ontology (no type-creation race),
#    and the shared ontology-write lock is provably mutually exclusive.
# --------------------------------------------------------------------------- #
class _InstrumentedLock:
    """Wraps (or bypasses) an ``asyncio.Lock`` while recording the max number of
    tasks INSIDE the critical section at once. ``exclusive=True`` = a real lock
    (expect max 1). ``exclusive=False`` = a null lock (no mutual exclusion, so a
    genuinely-overlapping critical section shows max >= 2)."""

    def __init__(self, *, exclusive: bool = True):
        self._lock = asyncio.Lock() if exclusive else None
        self.current = 0
        self.max_concurrent = 0
        self.entries = 0

    async def __aenter__(self):
        if self._lock is not None:
            await self._lock.acquire()
        self.current += 1
        self.entries += 1
        self.max_concurrent = max(self.max_concurrent, self.current)
        return self

    async def __aexit__(self, *exc):
        self.current -= 1
        if self._lock is not None:
            self._lock.release()
        return False


# A subtype lineage so the ontology has both a type and a subClassOf edge; a single
# tenant (ontology graph shared) — the discovery "several sub-queries, one KG" shape.
_ONTO_TENANT = "reent-onto"
_ONTO_GRAPH = tenant_graph_uri(_ONTO_TENANT)
_ONTO_KG = kg_graph_uri(_ONTO_TENANT, "kg1")
_P1_MARKER, _P2_MARKER = "P1", "P2"
_P1 = ExtractionResult(entities=[_entity("Cardiologist", "c1", parent_chain=["Physician"])])
_P2 = ExtractionResult(entities=[_entity("Pediatrician", "p1", parent_chain=["Physician"])])


async def _matcher_yield_new(*args, **kwargs):
    """Fake TypeMatcher.match: yields (a real await point INSIDE _resolve_type's
    locked region) then returns DIFFERENT (a brand-new top-level type). The yield
    is what lets two un-serialized _resolve_type calls overlap."""
    from cograph_client.resolver.type_matcher import MatchVerdict, TypeMatch

    await asyncio.sleep(0)
    proposed = args[0]
    return TypeMatch(
        proposed=proposed, resolved=proposed,
        verdict=MatchVerdict.DIFFERENT, confidence=1.0, is_new=True,
    )


async def _read_ontology(n: PyoxiNeptune) -> tuple[set, set]:
    """(minted type URIs, subClassOf edges) in the tenant ONTOLOGY (base) graph."""
    types_res = await n.query(
        f"SELECT ?t WHERE {{ GRAPH <{_ONTO_GRAPH}> {{ "
        f"?t <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        f"<http://www.w3.org/2000/01/rdf-schema#Class> }} }}"
    )
    types = {b["t"]["value"] for b in types_res["results"]["bindings"]}
    sub_res = await n.query(
        f"SELECT ?c ?p WHERE {{ GRAPH <{_ONTO_GRAPH}> {{ ?c <{RDFS_SUBCLASSOF}> ?p }} }}"
    )
    subs = {(b["c"]["value"], b["p"]["value"]) for b in sub_res["results"]["bindings"]}
    return types, subs


async def _ingest_both(n, lock, markers) -> None:
    ra, rb = _make_resolver(n, ontology_lock=lock), _make_resolver(n, ontology_lock=lock)
    for r in (ra, rb):
        r._extract = _stub_extract({_P1_MARKER: _P1, _P2_MARKER: _P2})
        r._type_matcher.match = _matcher_yield_new
    if markers == "concurrent":
        await asyncio.gather(
            ra.ingest(_P1_MARKER, _ONTO_TENANT, content_type="text", source="p1", instance_graph=_ONTO_KG),
            rb.ingest(_P2_MARKER, _ONTO_TENANT, content_type="text", source="p2", instance_graph=_ONTO_KG),
        )
    else:  # serial
        await ra.ingest(_P1_MARKER, _ONTO_TENANT, content_type="text", source="p1", instance_graph=_ONTO_KG)
        await rb.ingest(_P2_MARKER, _ONTO_TENANT, content_type="text", source="p2", instance_graph=_ONTO_KG)


@pytest.mark.asyncio
async def test_concurrent_ingest_ontology_equals_serial():
    """Concurrent sub-query ingest produces the SAME ontology as serial ingest, and
    the shared ontology-write lock serializes the type-creation critical section.

    - SERIAL baseline (fresh store): ingest both payloads sequentially.
    - CONCURRENT (fresh store): ingest both interleaved, sharing one REAL lock.
      The minted types + subClassOf edges MUST equal the serial baseline.
    - The real shared lock is mutually exclusive (max 1 holder), whereas a NULL-lock
      control genuinely overlaps (max >= 2) — proving the critical section really
      interleaves and the lock is load-bearing (not vacuously exclusive)."""
    # Serial baseline.
    n_serial = PyoxiNeptune()
    await _ingest_both(n_serial, asyncio.Lock(), "serial")
    serial_types, serial_subs = await _read_ontology(n_serial)

    # Concurrent with the REAL shared lock.
    n_conc = PyoxiNeptune()
    real_lock = _InstrumentedLock(exclusive=True)
    await _ingest_both(n_conc, real_lock, "concurrent")
    conc_types, conc_subs = await _read_ontology(n_conc)

    # No type-creation race: concurrent ontology == serial ontology.
    assert conc_types == serial_types, (conc_types, serial_types)
    assert conc_subs == serial_subs, (conc_subs, serial_subs)
    # Sanity: the lineage really was built (Physician + both leaves + edges).
    assert f"{TYPES}Physician" in serial_types
    assert (f"{TYPES}Cardiologist", f"{TYPES}Physician") in serial_subs
    assert (f"{TYPES}Pediatrician", f"{TYPES}Physician") in serial_subs

    # The real lock admitted at most ONE task into the ontology critical section.
    assert real_lock.entries >= 2, "the ontology-write lock was never entered"
    assert real_lock.max_concurrent == 1, (
        f"ontology mutations overlapped under the real lock (max={real_lock.max_concurrent})"
    )

    # Control: with a NULL lock the SAME interleaving genuinely overlaps (max >= 2),
    # proving the critical section really races and the real lock is what serializes.
    n_null = PyoxiNeptune()
    null_lock = _InstrumentedLock(exclusive=False)
    await _ingest_both(n_null, null_lock, "concurrent")
    assert null_lock.max_concurrent >= 2, (
        "expected the un-serialized critical section to overlap; if it didn't, the "
        "lock's serialization can't be demonstrated (scheduling changed?)"
    )
    # Even un-serialized the graph converges (idempotent writes), so the leak the
    # lock guards is torn/duplicated ontology writes, not divergent final triples.
    null_types, null_subs = await _read_ontology(n_null)
    assert null_types == serial_types and null_subs == serial_subs
