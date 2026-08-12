"""Per-fact provenance substrate tests (ADR 0002 §4, COG-38).

Covers the encoding helpers (deterministic statement ids, metadata-node
triples), the reader round-trip from a mocked SPARQL response, and the
resolver wiring: INFONA_PROVENANCE_ENABLED off (default) must be
byte-identical to pre-COG-38 behavior; on, statement-metadata triples are
emitted to the companion provenance graph alongside the attribute triples.

All mocked — no live Neptune, no LLM, no network. Env is only touched via
patch.dict / monkeypatch (auto-restored), never process-globally.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from infona_client.graph.client import NeptuneClient
from infona_client.graph.ontology_queries import attr_uri
from infona_client.graph.provenance import (
    EVENT_REWRITE,
    EVENT_TOMBSTONE,
    PROV_EVENT,
    PROV_NS,
    PROV_OBJECT,
    PROV_PREDICATE,
    PROV_REASON,
    PROV_REWRITTEN_TO,
    PROV_SUBJECT,
    build_provenance_triples,
    build_rewrite_triples,
    build_tombstone_triples,
    fetch_provenance,
    provenance_graph_uri,
    provenance_query,
    statement_id,
)
from infona_client.resolver.attribute_resolver import AttributeSchema
from infona_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from infona_client.resolver.schema_resolver import SchemaResolver


RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
FIXED_TS = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

SUBJ = "https://graph.infona.ai/entities/Guest/g1"
# ONTA-527: the write path derives tenant+kg from the instance graph, so a
# bare "g" placeholder cannot be scoped. Use a real per-KG URI.
KG_GRAPH_URI = "https://graph.infona.ai/graphs/test-tenant/kg/crm"

PRED = attr_uri("Guest", "email")
OBJ = "alice@example.com"

# Per-KG instance graph. The write path derives the tenant/kg scope from this
# URI, so a bare tenant graph (`…/graphs/t1`) is a GraphScopeError, not a write.
INGEST_GRAPH = "https://graph.infona.ai/graphs/t1/kg/k"


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    return client


def _make_resolver(mock_neptune, provenance: bool) -> SchemaResolver:
    verdict_path = Path(tempfile.mkdtemp()) / "verdicts.json"
    from infona_client.resolver.verdict_cache import JsonVerdictCache

    env = {
        "ANTHROPIC_API_KEY": "test-key",
        "OPENROUTER_API_KEY": "test-or-key",
        "INFONA_ER_ENABLED": "0",
    }
    if provenance:
        env["INFONA_PROVENANCE_ENABLED"] = "1"
    with patch.dict("os.environ", env):
        return SchemaResolver(
            neptune=mock_neptune,
            anthropic_key="test-key",
            verdict_cache=JsonVerdictCache(verdict_path),
        )


def _update_sparql(mock_neptune) -> list[str]:
    return [c.args[0] for c in mock_neptune.update.call_args_list]


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def test_statement_id_deterministic():
    assert statement_id(SUBJ, PRED, OBJ) == statement_id(SUBJ, PRED, OBJ)
    assert statement_id(SUBJ, PRED, OBJ) != statement_id(SUBJ, PRED, "bob@example.com")


def test_build_provenance_triples_fields():
    triples = build_provenance_triples(
        SUBJ, PRED, OBJ, source="crm.csv", confidence=0.9,
        timestamp=FIXED_TS, graph_uri="https://graph.infona.ai/graphs/t1",
    )
    nodes = {s for (s, _, _) in triples}
    assert len(nodes) == 1, "all metadata triples share one statement node"
    node = nodes.pop()
    assert node.startswith(f"{PROV_NS}stmt/")

    by_pred = {p: o for (_, p, o) in triples}
    assert by_pred[f"{PROV_NS}subject"] == SUBJ
    assert by_pred[f"{PROV_NS}predicate"] == PRED
    assert by_pred[f"{PROV_NS}object"] == OBJ
    assert by_pred[f"{PROV_NS}statement"] == statement_id(SUBJ, PRED, OBJ)
    assert by_pred[f"{PROV_NS}source"] == "crm.csv"
    assert by_pred[f"{PROV_NS}confidence"] == "0.9^^http://www.w3.org/2001/XMLSchema#float"
    assert by_pred[f"{PROV_NS}timestamp"] == (
        "2026-06-09T12:00:00+00:00^^http://www.w3.org/2001/XMLSchema#dateTime"
    )
    assert by_pred[f"{PROV_NS}graph"] == "https://graph.infona.ai/graphs/t1"


def test_assertion_node_distinct_per_source_same_statement_id():
    """Two sources asserting the same fact get separate metadata nodes (no
    cross-products on read) but share the fact's statement id."""
    a = build_provenance_triples(SUBJ, PRED, OBJ, source="crm.csv", timestamp=FIXED_TS)
    b = build_provenance_triples(SUBJ, PRED, OBJ, source="loyalty.csv", timestamp=FIXED_TS)
    assert a[0][0] != b[0][0]
    sid = statement_id(SUBJ, PRED, OBJ)
    assert (a[0][0], f"{PROV_NS}statement", sid) in a
    assert (b[0][0], f"{PROV_NS}statement", sid) in b


def test_build_accepts_iso_string_timestamp():
    triples = build_provenance_triples(
        SUBJ, PRED, OBJ, source="s", timestamp="2026-01-01T00:00:00+00:00",
    )
    by_pred = {p: o for (_, p, o) in triples}
    assert by_pred[f"{PROV_NS}timestamp"].startswith("2026-01-01T00:00:00+00:00^^")


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_confidence_out_of_range_raises(bad):
    with pytest.raises(ValueError):
        build_provenance_triples(SUBJ, PRED, OBJ, source="s", confidence=bad, timestamp=FIXED_TS)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_provenance_query_scopes_to_companion_graph():
    q = provenance_query("https://graph.infona.ai/graphs/t1", SUBJ)
    assert "FROM <https://graph.infona.ai/graphs/t1/provenance>" in q
    assert f"<{SUBJ}>" in q
    assert "FILTER" not in q  # no predicate narrowing by default

    narrowed = provenance_query("https://graph.infona.ai/graphs/t1", SUBJ, predicate=PRED)
    assert f"FILTER(?p = <{PRED}>)" in narrowed


@pytest.mark.asyncio
async def test_fetch_provenance_round_trips_mocked_response(mock_neptune):
    """Reader parses a standard SPARQL JSON response into ProvenanceRecords."""
    def binding(p, o, source, conf, ts):
        return {
            "p": {"type": "uri", "value": p},
            "o": {"type": "literal", "value": o},
            "stmt": {"type": "literal", "value": statement_id(SUBJ, p, o)},
            "source": {"type": "literal", "value": source},
            "confidence": {"type": "literal", "value": conf},
            "timestamp": {"type": "literal", "value": ts},
            "graph": {"type": "uri", "value": "https://graph.infona.ai/graphs/t1"},
        }

    mock_neptune.query.return_value = {
        "head": {"vars": ["p", "o", "stmt", "source", "confidence", "timestamp", "graph"]},
        "results": {"bindings": [
            binding(PRED, OBJ, "crm.csv", "0.97", "2026-06-09T12:00:00+00:00"),
            binding(PRED, OBJ, "loyalty.csv", "not-a-float", "2026-06-08T00:00:00+00:00"),
        ]},
    }

    records = await fetch_provenance(mock_neptune, "https://graph.infona.ai/graphs/t1", SUBJ)
    assert len(records) == 2
    first = records[0]
    assert first.subject == SUBJ
    assert first.predicate == PRED
    assert first.obj == OBJ
    assert first.source == "crm.csv"
    assert first.confidence == 0.97
    assert first.timestamp == "2026-06-09T12:00:00+00:00"
    assert first.statement_id == statement_id(SUBJ, PRED, OBJ)
    assert first.graph == "https://graph.infona.ai/graphs/t1"
    # Malformed confidence degrades to 1.0 instead of failing the read.
    assert records[1].confidence == 1.0


# ---------------------------------------------------------------------------
# Resolver wiring — flag off (regression) / flag on
# ---------------------------------------------------------------------------


def _guest_entity() -> ExtractedEntity:
    return ExtractedEntity(
        type_name="Guest", id="g1",
        attributes=[ExtractedAttribute(name="email", value=OBJ, datatype="string")],
    )


# email pre-registered on Guest so no EXTEND write happens — any update call
# in these tests is attributable to provenance alone.
EXISTING_ATTRS = {"Guest": {"email": AttributeSchema(name="email", datatype="string")}}


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_regression(mock_neptune):
    """Default (flag unset): zero extra Neptune calls, zero provenance triples —
    the pre-COG-38 output exactly."""
    resolver = _make_resolver(mock_neptune, provenance=False)
    collected: list[tuple[str, str, str]] = []
    result = IngestResult(entities_extracted=1)

    await resolver._resolve_and_insert_entity(
        _guest_entity(), "Guest", SUBJ, is_duplicate=False,
        graph_uri=KG_GRAPH_URI, existing_types={"Guest": ""}, existing_attrs=dict(EXISTING_ATTRS),
        source="crm.csv", result=result, _collect_triples=collected,
    )

    assert mock_neptune.update.call_count == 0
    assert all(PROV_NS not in s and PROV_NS not in o for (s, _, o) in collected)
    # The classic triple set is intact: rdf:type, label, attribute, ingested_at, source.
    preds = [p for (_, p, _) in collected]
    assert preds == [
        RDF_TYPE,
        "http://www.w3.org/2000/01/rdf-schema#label",
        PRED,
        "https://graph.infona.ai/onto/ingested_at",
        "https://graph.infona.ai/onto/source",
    ]


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "PRODUCT BUG (Neo4j port, same defect ONTA-527 pinned for enrichment): the CANONICAL companion-provenance graph is never written. kg_writer.insert_facts ignores its provenance_triples argument — the named-graph write went out with the SPARQL path — while the resolver still builds the payload. The :ProvEvent substitute is not equivalent: it carries no confidence, no statement id, and ts = write time rather than the asserted-at instant, so 'who asserted this value, with what confidence, as of when' is unrecoverable. Flip this green when the companion port lands."
    ),
    strict=True,
)
async def test_flag_on_emits_provenance_to_companion_graph(mock_neptune):
    """Flag on: one INSERT into <graph>/provenance carrying the statement node
    with the correct deterministic statement id; the instance-triple collector
    is unchanged."""
    resolver = _make_resolver(mock_neptune, provenance=True)
    collected: list[tuple[str, str, str]] = []
    result = IngestResult(entities_extracted=1)

    await resolver._resolve_and_insert_entity(
        _guest_entity(), "Guest", SUBJ, is_duplicate=False,
        graph_uri=KG_GRAPH_URI, existing_types={"Guest": ""}, existing_attrs=dict(EXISTING_ATTRS),
        source="crm.csv", result=result, _collect_triples=collected,
    )

    sparql_calls = _update_sparql(mock_neptune)
    assert len(sparql_calls) == 1, "exactly one provenance INSERT"
    sparql = sparql_calls[0]
    assert f"GRAPH <{provenance_graph_uri(KG_GRAPH_URI)}>" in sparql
    assert statement_id(SUBJ, PRED, OBJ) in sparql
    assert "crm.csv" in sparql
    assert f"<{PROV_NS}confidence>" in sparql and "1.0" in sparql
    # Instance triples are untouched: no provenance leaked into the collector.
    assert all(PROV_NS not in s and PROV_NS not in o for (s, _, o) in collected)
    assert (SUBJ, PRED, OBJ) in collected


@pytest.mark.asyncio
async def test_flag_on_no_attributes_no_provenance_insert(mock_neptune):
    """Flag on but the entity asserts no attributes: nothing to record."""
    resolver = _make_resolver(mock_neptune, provenance=True)
    entity = ExtractedEntity(type_name="Guest", id="g2")
    result = IngestResult(entities_extracted=1)

    await resolver._resolve_and_insert_entity(
        entity, "Guest", "https://graph.infona.ai/entities/Guest/g2", is_duplicate=False,
        graph_uri=KG_GRAPH_URI, existing_types={"Guest": ""}, existing_attrs={"Guest": {}},
        source="crm.csv", result=result, _collect_triples=[],
    )
    assert mock_neptune.update.call_count == 0


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "PRODUCT BUG (Neo4j port, same defect ONTA-527 pinned for enrichment): the CANONICAL companion-provenance graph is never written. kg_writer.insert_facts ignores its provenance_triples argument — the named-graph write went out with the SPARQL path — while the resolver still builds the payload. The :ProvEvent substitute is not equivalent: it carries no confidence, no statement id, and ts = write time rather than the asserted-at instant, so 'who asserted this value, with what confidence, as of when' is unrecoverable. Flip this green when the companion port lands."
    ),
    strict=True,
)
async def test_flag_on_entity_reference_attribute_gets_provenance(mock_neptune):
    """Entity-valued attributes (datatype = ontology type) are assertions too — and
    their provenance is keyed on the ``onto/<leaf>`` INSTANCE edge (where the
    relationship is actually written), NOT the ``attrs/<leaf>`` declaration
    predicate."""
    resolver = _make_resolver(mock_neptune, provenance=True)
    entity = ExtractedEntity(
        type_name="Guest", id="g3",
        attributes=[ExtractedAttribute(name="stays_at", value="Hotel Zed", datatype="Hotel")],
    )
    existing_attrs = {
        "Guest": {"stays_at": AttributeSchema(name="stays_at", datatype="Hotel")},
        "Hotel": {},
    }
    result = IngestResult(entities_extracted=1)

    await resolver._resolve_and_insert_entity(
        entity, "Guest", "https://graph.infona.ai/entities/Guest/g3", is_duplicate=False,
        graph_uri=KG_GRAPH_URI, existing_types={"Guest": "", "Hotel": ""}, existing_attrs=existing_attrs,
        source="pms", result=result, _collect_triples=[],
    )

    sparql = " || ".join(_update_sparql(mock_neptune))
    assert f"GRAPH <{provenance_graph_uri(KG_GRAPH_URI)}>" in sparql
    target = "https://graph.infona.ai/entities/Hotel/Hotel_Zed"
    # The promotion branch writes the relationship instance edge on onto/<leaf> (the
    # NL-queryable predicate); provenance describes that real edge, so the statement
    # id hashes the onto/<leaf> predicate — not attr_uri (the ontology declaration).
    sid = statement_id(
        "https://graph.infona.ai/entities/Guest/g3",
        "https://graph.infona.ai/onto/stays_at", target,
    )
    assert sid in sparql


# ---------------------------------------------------------------------------
# Batched provenance writes on the fast path (COG-46)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_on_collector_defers_provenance_zero_per_entity_inserts(mock_neptune):
    """Fast path: with a _collect_provenance collector supplied, entity
    processing makes ZERO Neptune update calls — the statement-metadata
    triples accumulate in the collector for one batched flush by the caller,
    and they are the exact same triples the per-entity path would write."""
    resolver = _make_resolver(mock_neptune, provenance=True)
    prov: list[tuple[str, str, str]] = []
    result = IngestResult(entities_extracted=1)

    await resolver._resolve_and_insert_entity(
        _guest_entity(), "Guest", SUBJ, is_duplicate=False,
        graph_uri=KG_GRAPH_URI, existing_types={"Guest": ""}, existing_attrs=dict(EXISTING_ATTRS),
        source="crm.csv", result=result, _collect_triples=[], _collect_provenance=prov,
    )

    assert mock_neptune.update.call_count == 0, "no per-entity provenance INSERT"
    by_pred = {p: o for (_, p, o) in prov}
    assert by_pred[f"{PROV_NS}statement"] == statement_id(SUBJ, PRED, OBJ)
    assert by_pred[f"{PROV_NS}source"] == "crm.csv"


async def _ingest_two_guests(resolver, mock_neptune) -> None:
    """Two-entity ingest through the real ``_resolve_and_insert`` pipeline.

    The graph is a per-KG URI (``…/graphs/t1/kg/k``): the write path derives the
    tenant/kg scope from it, and a bare tenant URI now raises ``GraphScopeError``
    rather than silently writing to a tenant-wide graph.
    """
    mock_neptune.batch_exists.return_value = set()
    extraction = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Guest", id="g1",
                attributes=[ExtractedAttribute(name="email", value="alice@example.com", datatype="string")],
            ),
            ExtractedEntity(
                type_name="Guest", id="g2",
                attributes=[ExtractedAttribute(name="email", value="bob@example.com", datatype="string")],
            ),
        ],
        relationships=[],
        source_text="",
    )
    await resolver._resolve_and_insert(
        extraction, INGEST_GRAPH, {"Guest": ""},
        {"Guest": {"email": AttributeSchema(name="email", datatype="string")}},
        "crm.csv", IngestResult(entities_extracted=2), {}, {}, "batch-1",
    )


@pytest.mark.asyncio
async def test_multi_entity_ingest_flushes_one_batched_provenance_write(
    mock_neptune, monkeypatch
):
    """End-to-end through _resolve_and_insert: a multi-entity ingest makes ONE
    call to the shared write path carrying every entity's statement metadata —
    not one awaited write per entity.

    **Ported by ONTA-527.** The flush used to be an INSERT into the companion
    provenance graph, so the batching was countable in the emitted SPARQL text.
    The write runs through ``GraphStore`` now and emits no SPARQL at all, so the
    batching is asserted at the seam where it actually happens: a single
    ``insert_facts`` call whose ``provenance_triples`` carries BOTH entities'
    statement metadata, with the per-entity-write negative unchanged. Whether
    that payload then LANDS anywhere is a separate question — see the xfail
    below, which is the half that did not survive the cutover.
    """
    import infona_client.resolver.schema_resolver as schema_resolver_mod

    resolver = _make_resolver(mock_neptune, provenance=True)

    calls: list[tuple[tuple, dict]] = []
    real_insert_facts = schema_resolver_mod.insert_facts

    async def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return await real_insert_facts(*args, **kwargs)

    monkeypatch.setattr(schema_resolver_mod, "insert_facts", _spy)

    await _ingest_two_guests(resolver, mock_neptune)

    assert len(calls) == 1, "ONE batched write per ingest, not one per entity"
    (_neptune, graph_uri, instance_triples), kwargs = calls[0]
    assert graph_uri == INGEST_GRAPH
    prov_triples = kwargs["provenance_triples"]
    sid1 = statement_id(
        "https://graph.infona.ai/entities/Guest/g1", PRED, "alice@example.com",
    )
    sid2 = statement_id(
        "https://graph.infona.ai/entities/Guest/g2", PRED, "bob@example.com",
    )
    prov_objects = {o for (_s, _p, o) in prov_triples}
    assert sid1 in prov_objects and sid2 in prov_objects
    # Instance triples ride the same single call, provenance-free.
    assert instance_triples
    assert all(PROV_NS not in s and PROV_NS not in o for (s, _p, o) in instance_triples)
    # And nothing is written per entity — the whole ingest awaits no SPARQL.
    assert mock_neptune.update.call_count == 0


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (ONTA-527): the per-fact provenance record is no longer "
        "recoverable after a write. graph/kg_writer.py::insert_facts DROPS its "
        "provenance_triples payload — the ADR 0002 §4 statement metadata that "
        "graph/provenance.py::build_provenance_triples produced (prov:source, "
        "prov:confidence, prov:timestamp, prov:statement) went to a companion "
        "named graph, and the SPARQL write of that graph is gone. The "
        "property-graph substitute, graph/pg_ops.py::create_prov_event, records "
        "only that an assert happened: it carries no confidence, no statement id, "
        "and its `source` comes from the Fact, which is None for an ordinary "
        "attribute value (ingest carries the source as a SEPARATE onto/source "
        "triple), so 'who asserted this value' is unrecoverable for exactly the "
        "domain facts provenance exists to attribute."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_ingested_fact_provenance_is_recoverable_after_the_write(
    mock_neptune, monkeypatch
):
    """After an ingest with provenance ON, the source that asserted a value fact
    must be recoverable from the store."""
    from infona_client.graph.store import get_graph_store

    monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")
    resolver = _make_resolver(mock_neptune, provenance=True)
    await _ingest_two_guests(resolver, mock_neptune)

    events = [
        e
        for e in get_graph_store().snapshot_prov()
        if e["subject_id"] == "https://graph.infona.ai/entities/Guest/g1"
        and e["object_repr"] == "alice@example.com"
    ]
    assert events, "no provenance record for the ingested value"
    assert events[0]["source"] == "crm.csv"


# --- Removal / rename events (ADR 0007): tombstone + rewrite builders ----------

_GRAPH = "https://graph.infona.ai/graphs/t/kg/k"
_FIXED = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _by_node(triples):
    nodes = {}
    for s, p, o in triples:
        nodes.setdefault(s, {}).setdefault(p, []).append(o)
    return nodes


def test_build_tombstone_triples_for_subject():
    subj = "https://graph.infona.ai/entities/E/1"
    triples = build_tombstone_triples(
        subjects=[subj],
        graph_uri=_GRAPH,
        reason="orphan sweep",
        timestamp=_FIXED,
        touched_types=["https://graph.infona.ai/types/Language"],
    )
    assert triples, "a subject removal must emit a tombstone event"
    node = triples[0][0]
    assert node.startswith(f"{PROV_NS}event/")
    fields = _by_node(triples)[node]
    assert fields[PROV_EVENT] == [EVENT_TOMBSTONE]
    assert fields[PROV_SUBJECT] == [subj]
    assert fields[PROV_REASON] == ["orphan sweep"]
    assert any("affectedType" in p for p, _ in ((k, v) for k in fields for v in fields[k]))


def test_build_tombstone_triples_predicate_scoped_has_no_object():
    # object=None → predicate-scoped removal: prov:predicate present, prov:object absent.
    triples = build_tombstone_triples(
        triples=[("urn:e", "urn:p", None)], graph_uri=_GRAPH, reason="lambda re-invoke",
        timestamp=_FIXED,
    )
    fields = _by_node(triples)[triples[0][0]]
    assert fields[PROV_EVENT] == [EVENT_TOMBSTONE]
    assert fields[PROV_PREDICATE] == ["urn:p"]
    assert PROV_OBJECT not in fields


def test_build_tombstone_triples_concrete_triple_records_object():
    triples = build_tombstone_triples(
        triples=[("urn:e", "urn:p", "the-old-value")], graph_uri=_GRAPH, timestamp=_FIXED,
    )
    fields = _by_node(triples)[triples[0][0]]
    assert fields[PROV_OBJECT] == ["the-old-value"]


def test_build_rewrite_triples_maps_old_to_new():
    triples = build_rewrite_triples(
        "urn:loser", "urn:canon", graph_uri=_GRAPH, reason="er-merge", timestamp=_FIXED,
    )
    fields = _by_node(triples)[triples[0][0]]
    assert fields[PROV_EVENT] == [EVENT_REWRITE]
    assert fields[PROV_SUBJECT] == ["urn:loser"]
    assert fields[PROV_REWRITTEN_TO] == ["urn:canon"]
    assert fields[PROV_REASON] == ["er-merge"]


def test_tombstone_events_are_deterministic_for_fixed_timestamp():
    a = build_tombstone_triples(subjects=["urn:e"], graph_uri=_GRAPH, timestamp=_FIXED)
    b = build_tombstone_triples(subjects=["urn:e"], graph_uri=_GRAPH, timestamp=_FIXED)
    assert a == b  # same fact + timestamp → same event node (idempotent)
