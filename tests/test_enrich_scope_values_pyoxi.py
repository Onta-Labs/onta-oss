"""Real-store (pyoxigraph) tests for the MULTI-VALUE scope resolver
(``EnrichmentExecutor.select_scope_value_uris``).

Validates the ACTUAL generated SPARQL against a genuine SPARQL engine — the
regression guard for the persona-eval refresh gap: a scoped refresh over a LIST
of values ("refresh pricing for OpenAI, Google, Deepgram and ElevenLabs") was
extracted as a single crammed literal scope
(``provided_by = "OpenAI, Google, Deepgram, ElevenLabs"``), matched 0 existing
records, premature-clarified, and the caller fell into a fresh discovery build.
The fix resolves the value SET to the concrete entity IRIs whose scope value is a
case/normalization-insensitive MEMBER of the set — matched here against real RDF.

Uses invented types/attrs/values only (no persona tokens). Skipped where
pyoxigraph is not installed (it is not a declared CI test dep); runs in local dev.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.enrichment.cache import EnrichmentCache  # noqa: E402
from cograph_client.enrichment.executor import EnrichmentExecutor  # noqa: E402
from cograph_client.enrichment.job_store import InMemoryJobStore  # noqa: E402
from cograph_client.enrichment.models import (  # noqa: E402
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
    Verdict,
)
from cograph_client.graph.ontology_queries import attr_uri, type_uri  # noqa: E402
from cograph_client.graph.provenance import attr_provenance_companion_uri  # noqa: E402
from cograph_client.graph.queries import (  # noqa: E402
    kg_graph_uri,
    tenant_graph_uri,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_SUBCLASSOF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
RDFS_DOMAIN = "http://www.w3.org/2000/01/rdf-schema#domain"
ONTO = "https://cograph.tech/onto/"
ENT = "https://cograph.tech/entities/"
TENANT, KG, TYPE = "scope-vals-pyoxi", "k1", "Widget"


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default
    matching the production backend."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


def _executor(n: PyoxiNeptune) -> EnrichmentExecutor:
    class _NoAdapter:
        name = "none"
        is_paid = False

        async def lookup(self, *a, **k):
            return []

    return EnrichmentExecutor(n, InMemoryJobStore(), EnrichmentCache(), _NoAdapter())


async def _seed_literal_scope(n: PyoxiNeptune) -> None:
    """Three Widgets with a literal ``made_by`` in DIFFERENT casing/spacing than a
    caller would type, plus the ontology declaration so the predicate resolves."""
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    made_by = attr_uri(TYPE, "made_by")
    await n.update(
        f'INSERT DATA {{ GRAPH <{onto}> {{ '
        f'<{made_by}> <{RDF_TYPE}> <{RDF_PROPERTY}> ; '
        f'<{RDFS_DOMAIN}> <{type_uri(TYPE)}> ; '
        f'<{RDFS_LABEL}> "made_by" . }} }}'
    )
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{ENT}{TYPE}/w1> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{made_by}> "acme corp" . '
        f'<{ENT}{TYPE}/w2> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{made_by}> "GLOBEX" . '
        f'<{ENT}{TYPE}/w3> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{made_by}> "Umbrella" . '
        f'}} }}'
    )


@pytest.mark.asyncio
async def test_select_scope_value_uris_matches_case_insensitive_set():
    """A LIST of values matches the existing records whose literal scope value is a
    case/normalization-insensitive MEMBER of the set — the crammed-literal bug's
    fix. "Acme Corp" (typed) matches "acme corp" (stored); "Initech" (no record)
    is simply absent; "Umbrella" is NOT in the set so it is excluded."""
    n = PyoxiNeptune()
    await _seed_literal_scope(n)
    ex = _executor(n)
    uris = await ex.select_scope_value_uris(
        TENANT, KG, TYPE, "made_by",
        ["Acme Corp", "globex", "Initech"],  # mixed casing; one absent
    )
    assert sorted(uris) == [f"{ENT}{TYPE}/w1", f"{ENT}{TYPE}/w2"], uris


@pytest.mark.asyncio
async def test_select_scope_value_uris_matches_relationship_target_label():
    """When the scope predicate is a RELATIONSHIP to a node, the value set matches
    the target node's rdfs:label (case-insensitively) — so a subset named by the
    related entity's display name resolves."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    # made_by is a RELATIONSHIP (onto/<leaf>) pointing at a Vendor node with a label.
    rel = f"{ONTO}made_by"
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{ENT}{TYPE}/w1> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{rel}> <{ENT}Vendor/v1> . '
        f'<{ENT}Vendor/v1> <{RDFS_LABEL}> "Deepgram" . '
        f'<{ENT}{TYPE}/w2> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{rel}> <{ENT}Vendor/v2> . '
        f'<{ENT}Vendor/v2> <{RDFS_LABEL}> "Cartesia" . }} }}'
    )
    ex = _executor(n)
    uris = await ex.select_scope_value_uris(
        TENANT, KG, TYPE, "made_by", ["deepgram"]  # lowercase; label is "Deepgram"
    )
    assert uris == [f"{ENT}{TYPE}/w1"], uris


@pytest.mark.asyncio
async def test_count_entities_supertype_reaches_leaf_typed_instances():
    """Fix B (real store): enriching a declared SUPERTYPE must reach instances
    minted under LEAF subtypes, via the reflexive ``a/rdfs:subClassOf*`` closure.

    Models the RCA voice-models KG: ``Model`` is a declared, instance-LESS
    supertype; discovery minted the real instances under LEAF types
    (``SpeechToTextModel``, ``RealtimeModel``) and connected them up with
    ``rdfs:subClassOf`` edges (as insert_subtype writes). A bare ``?e a <Model>``
    counted ZERO (the e5 failure); the closure counts all 3 leaf instances. The
    ``*`` is zero-or-more, so a directly-typed ``Model`` (m0 below) is ALSO counted
    (reflexive) — proving the closure did not lose direct matches."""
    n = PyoxiNeptune()
    kgg = kg_graph_uri(TENANT, KG)
    sup = type_uri("Model")
    stt = type_uri("SpeechToTextModel")
    rt = type_uri("RealtimeModel")
    await n.update(
        f"INSERT DATA {{ GRAPH <{kgg}> {{ "
        # Subtype lineage: both leaves are subClassOf Model.
        f"<{stt}> <{RDFS_SUBCLASSOF}> <{sup}> . "
        f"<{rt}> <{RDFS_SUBCLASSOF}> <{sup}> . "
        # 1 directly-typed Model (reflexive case) + 2 leaf-typed instances.
        f"<{ENT}Model/m0> <{RDF_TYPE}> <{sup}> . "
        f"<{ENT}SpeechToTextModel/s1> <{RDF_TYPE}> <{stt}> . "
        f"<{ENT}RealtimeModel/r1> <{RDF_TYPE}> <{rt}> . "
        f"}} }}"
    )
    ex = _executor(n)
    # Enriching the supertype counts the directly-typed one AND both leaves = 3.
    assert await ex.count_entities(TENANT, KG, "Model") == 3
    # A leaf type still counts exactly its own instances (no over-reach upward).
    assert await ex.count_entities(TENANT, KG, "SpeechToTextModel") == 1


@pytest.mark.asyncio
async def test_select_scope_value_uris_empty_on_no_match_and_bad_predicate():
    """No member matches → [] (caller fails closed to a clarify, not a whole-type
    enrich); an unresolvable/empty predicate → [] fast (no scan)."""
    n = PyoxiNeptune()
    await _seed_literal_scope(n)
    ex = _executor(n)
    # Values that match nothing.
    assert await ex.select_scope_value_uris(
        TENANT, KG, TYPE, "made_by", ["nobody", "nothing"]
    ) == []
    # Empty value set.
    assert await ex.select_scope_value_uris(TENANT, KG, TYPE, "made_by", []) == []


# --------------------------------------------------------------------------- #
# overwrite = true REPLACE of a conflicting value (pf10 sp-refresh-pricing)
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    """Minimal enrichment adapter returning a fixed verdict per (label, attribute).
    Named ``wikidata`` so the default ``lite`` tier chain resolves it (the executor
    registers the injected adapter by name), matching the other enrichment tests."""

    name = "wikidata"
    is_paid = False

    def __init__(self, mapping):
        self._mapping = mapping

    async def lookup(self, entity_label, attribute, job=None, context=None):
        return self._mapping.get((entity_label, attribute), [])


async def _seed_conflict_entity(n: PyoxiNeptune) -> None:
    """One Widget with an EXISTING literal ``pricing`` value + its OLD per-attribute
    provenance companions (source_url + a stale verified_at), plus the ontology
    declaration so the predicate resolves during selection."""
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    pricing = attr_uri(TYPE, "pricing")
    src = attr_uri(TYPE, "pricing_source_url")
    ver = attr_uri(TYPE, "pricing_verified_at")
    xsd_dt = "http://www.w3.org/2001/XMLSchema#dateTime"
    await n.update(
        f'INSERT DATA {{ GRAPH <{onto}> {{ '
        f'<{pricing}> <{RDF_TYPE}> <{RDF_PROPERTY}> ; '
        f'<{RDFS_DOMAIN}> <{type_uri(TYPE)}> ; <{RDFS_LABEL}> "pricing" . }} }}'
    )
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{ENT}{TYPE}/w1> <{RDF_TYPE}> <{type_uri(TYPE)}> ; '
        f'<{RDFS_LABEL}> "Acme Widget" ; '
        f'<{pricing}> "0.0100 (2023-09-01)" ; '
        f'<{src}> <https://old.example/price> ; '
        f'<{ver}> "2023-09-01T00:00:00"^^<{xsd_dt}> . }} }}'
    )


def _objects(n: PyoxiNeptune, subject: str, pred: str) -> list[str]:
    rows = n.store.query(
        f"SELECT ?o WHERE {{ GRAPH ?g {{ <{subject}> <{pred}> ?o }} }}",
        use_default_graph_as_union=True,
    )
    return sorted(str(row["o"].value) for row in rows)


async def _current_objects(n: PyoxiNeptune, subject: str, pred: str) -> set[str]:
    """The CURRENT objects of ``(subject, pred)`` — those with no CLOSED validity
    interval (the P7 "current facts" projection)."""
    from cograph_client.graph.validity import current_objects_query

    raw = await n.query(current_objects_query(kg_graph_uri(TENANT, KG), subject, pred))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


@pytest.mark.asyncio
async def test_overwrite_supersedes_conflicting_value_and_restamps_provenance():
    """pf10 sp-refresh-pricing (real store), ONTA-279: under `overwrite` a CONFLICT
    row (fresh value ≠ existing) now SUPERSEDES the stale value through the P6 write
    op — the fresh value becomes the SINGULAR CURRENT value while the stale value's
    validity interval is CLOSED (it stays in the graph as history, never
    hard-deleted, replacing the retired delete-old+insert-new path). The
    per-attribute citation is restamped to the FRESH source + verified date on the
    attr_meta namespace. This is what "every number is CURRENT and sourced"
    requires."""
    n = PyoxiNeptune()
    await _seed_conflict_entity(n)
    w1 = f"{ENT}{TYPE}/w1"
    pricing = attr_uri(TYPE, "pricing")
    src = attr_provenance_companion_uri(TYPE, "pricing", "source_url")
    ver = attr_provenance_companion_uri(TYPE, "pricing", "verified_at")

    ex = EnrichmentExecutor(
        n, InMemoryJobStore(), EnrichmentCache(),
        _FakeAdapter({("Acme Widget", "pricing"): [
            Verdict(value="0.0043 (2026-07-07)", confidence=0.95, source="fake",
                    source_url="https://new.example/price",
                    source_published_at=datetime(2026, 7, 7, tzinfo=timezone.utc))
        ]}),
    )
    job = EnrichJob(
        id="ow-1", tenant_id=TENANT, kg_name=KG, type_name=TYPE,
        attributes=["pricing"], tier=EnrichmentTier.lite, status=JobStatus.queued,
        created_at=datetime.now(timezone.utc), conflict_policy=ConflictPolicy.overwrite,
        entity_uris=[w1],
    )
    await ex._jobs.create(job)
    await ex.run(job, TENANT)

    final = await ex._jobs.get(job.id)
    assert [r.action for r in final.results] == ["conflict"]
    # SUPERSESSION: the fresh value is the SINGULAR CURRENT value; the stale value's
    # interval is CLOSED but its edge is KEPT (present in the instance graph as
    # history) — supersession never hard-deletes (ONTA-279).
    assert await _current_objects(n, w1, pricing) == {"0.0043 (2026-07-07)"}
    assert _objects(n, w1, pricing) == ["0.0043 (2026-07-07)", "0.0100 (2023-09-01)"]
    # Provenance companions restamped to the FRESH source + date on the attr_meta
    # namespace. (The stale LEGACY companions now persist rather than being cleared —
    # the retired _overwrite_clear_targets is what used to clear them; ONTA-279
    # accepts that companion accretion in exchange for lineage-preserving
    # supersession of the primary value.)
    assert _objects(n, w1, src) == ["https://new.example/price"]
    assert _objects(n, w1, ver) == ["2026-07-07T00:00:00Z"]


@pytest.mark.asyncio
async def test_verify_does_not_replace_conflicting_value():
    """The ONTA-245 contract is preserved: under `verify` the SAME conflict row does
    NOT replace the existing value (a plain re-verify never clobbers) — the stale
    value + its old provenance are retained. Only the EXPLICIT overwrite policy
    replaces (asserted above)."""
    n = PyoxiNeptune()
    await _seed_conflict_entity(n)
    w1 = f"{ENT}{TYPE}/w1"
    pricing = attr_uri(TYPE, "pricing")
    src = attr_uri(TYPE, "pricing_source_url")

    ex = EnrichmentExecutor(
        n, InMemoryJobStore(), EnrichmentCache(),
        _FakeAdapter({("Acme Widget", "pricing"): [
            Verdict(value="0.0043 (2026-07-07)", confidence=0.95, source="fake",
                    source_url="https://new.example/price",
                    source_published_at=datetime(2026, 7, 7, tzinfo=timezone.utc))
        ]}),
    )
    job = EnrichJob(
        id="vf-1", tenant_id=TENANT, kg_name=KG, type_name=TYPE,
        attributes=["pricing"], tier=EnrichmentTier.lite, status=JobStatus.queued,
        created_at=datetime.now(timezone.utc), conflict_policy=ConflictPolicy.verify,
        entity_uris=[w1],
    )
    await ex._jobs.create(job)
    await ex.run(job, TENANT)

    final = await ex._jobs.get(job.id)
    assert [r.action for r in final.results] == ["conflict"]
    # Unchanged: the existing value + its old source persist (verify never clobbers).
    assert _objects(n, w1, pricing) == ["0.0100 (2023-09-01)"]
    assert _objects(n, w1, src) == ["https://old.example/price"]


@pytest.mark.asyncio
async def test_overwrite_keeps_incumbent_and_skips_provenance_when_value_rejected():
    """Data-loss + citation-integrity guard: under `overwrite`, a conflict row whose
    FRESH value fails validation (a non-conforming primitive → no primary triple is
    written) must (1) NOT clear the incumbent — clearing-without-replacing would EMPTY
    the attribute — and (2) NOT stamp fresh `_source_url` / `_verified_at`, which
    would falsely cite, on the RETAINED old value, a source that DISAGREED with it."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    xsd_int = "http://www.w3.org/2001/XMLSchema#integer"
    stock = attr_uri(TYPE, "stock")
    stock_src = attr_uri(TYPE, "stock_source_url")
    stock_ver = attr_uri(TYPE, "stock_verified_at")
    RDFS_RANGE = "http://www.w3.org/2000/01/rdf-schema#range"
    # Declare `stock` with an INTEGER range so a non-numeric verdict is rejected.
    await n.update(
        f'INSERT DATA {{ GRAPH <{onto}> {{ '
        f'<{stock}> <{RDF_TYPE}> <{RDF_PROPERTY}> ; <{RDFS_DOMAIN}> <{type_uri(TYPE)}> ; '
        f'<{RDFS_RANGE}> <{xsd_int}> ; <{RDFS_LABEL}> "stock" . }} }}'
    )
    w1 = f"{ENT}{TYPE}/w1"
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{w1}> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{RDFS_LABEL}> "Acme Widget" ; '
        f'<{stock}> "42"^^<{xsd_int}> . }} }}'
    )
    ex = EnrichmentExecutor(
        n, InMemoryJobStore(), EnrichmentCache(),
        _FakeAdapter({("Acme Widget", "stock"): [
            # Non-numeric → validate_triple rejects it for an integer range.
            Verdict(value="lots", confidence=0.95, source="fake",
                    source_url="https://new.example/stock",
                    source_published_at=datetime(2026, 7, 7, tzinfo=timezone.utc))
        ]}),
    )
    job = EnrichJob(
        id="ow-rej", tenant_id=TENANT, kg_name=KG, type_name=TYPE,
        attributes=["stock"], tier=EnrichmentTier.lite, status=JobStatus.queued,
        created_at=datetime.now(timezone.utc), conflict_policy=ConflictPolicy.overwrite,
        entity_uris=[w1],
    )
    await ex._jobs.create(job)
    await ex.run(job, TENANT)

    final = await ex._jobs.get(job.id)
    assert [r.action for r in final.results] == ["conflict"]
    # (1) The incumbent integer is preserved — NOT emptied by a clear-without-replace.
    assert _objects(n, w1, stock) == ["42"]
    # (2) No fresh provenance was stamped onto the retained (disagreeing) value —
    # on either the legacy attribute-namespace shape or the attr_meta shape.
    assert _objects(n, w1, stock_src) == []
    assert _objects(n, w1, stock_ver) == []
    assert _objects(n, w1, attr_provenance_companion_uri(TYPE, "stock", "source_url")) == []
    assert _objects(n, w1, attr_provenance_companion_uri(TYPE, "stock", "verified_at")) == []
