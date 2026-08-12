"""SchemaResolver free-text candidacy seam (ONTA-177).

The seam lives in SchemaResolver — NOT only in the CSV resolver — so every
ingest modality that runs a schema pass produces ``textKind`` markers:

- the extract path (text/JSON ``/ingest``, web discovery) samples validated
  string values during pass 2 and decides candidacy after the write
  (``decide_text_candidacy=True``, set by ``ingest()``);
- the mapped path (one-shot CSV ``/ingest``, web fixed-mapping via
  ``ingest_mapped_records``) applies the mapping's schema-time
  ``ColumnMapping.text_kind`` verdicts — decided ONCE at schema-inference
  time, never re-decided at apply time;
- ``/ingest/csv/rows`` (client-supplied mapping, no schema pass) deliberately
  gets NEITHER: its ``_resolve_and_insert`` call leaves
  ``decide_text_candidacy`` off, keeping the route LLM-free; a
  reconciler-side default heuristic covers it later (ONTA-181).

**Ported by ONTA-527.** Two mechanical changes and one honest subtraction:

* the ingest calls now take a per-KG ``instance_graph``. ``GRAPH`` here is the
  tenant ONTOLOGY graph (where schema/marker writes belong) and used to double
  as the instance target; the property-graph writer cannot derive a
  ``(tenant, kg)`` scope from a tenant URI and fails closed, so the two are
  spelled separately;
* a marker verdict was asserted as the ``textKind`` SPARQL the seam emitted
  (``mock_neptune.update``). The seam commits an ``OntologyMutation`` now and
  emits no SPARQL, so the verdicts are captured at the commit call
  (:func:`_text_kind_verdicts`) — the same decision, one layer up from the
  transport;
* what that no longer proves is that the verdict is PERSISTED. It isn't:
  ``SET_TEXT_KIND`` is dropped on the floor by the GraphStore commit path, so
  ingest-time free-text marking is dead in production. See the strict xfail at
  the end of this file.
"""

from __future__ import annotations

import json

import pytest

import infona_client.graph.text_markers as tm
from infona_client.graph.ontology_queries import (
    TEXT_KIND_FREE_TEXT,
    TEXT_KIND_NOT_TEXT,
)
from infona_client.graph.queries import kg_graph_uri
from infona_client.models.ontology import OntologyOpKind
from infona_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    EntitySpec,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from infona_client.resolver.schema_resolver import (
    TEXT_CANDIDACY_SYSTEM,
    SchemaResolver,
)

TENANT = "test-tenant"
KG = "tickets"
#: Tenant ONTOLOGY graph — the target of schema + marker writes.
GRAPH = f"https://graph.infona.ai/graphs/{TENANT}"
#: Per-KG INSTANCE graph — the target of the fact write (ONTA-527: the
#: property-graph writer needs a (tenant, kg) scope and rejects a tenant URI).
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)


@pytest.fixture(autouse=True)
def _clean_marker_cache():
    tm.reset_for_tests()
    yield
    tm.reset_for_tests()

_PROSE = (
    "After resetting my password this morning I am redirected back to the "
    "login page in an endless loop, tried three browsers and an incognito "
    "window, cleared cookies twice, and the problem persists everywhere."
)
_SUBJECT = "Cannot log in to the billing portal after password reset"
_ADDRESS = "1420 Willow Creek Road, Springfield"


def _resolver(mock_neptune) -> SchemaResolver:
    from infona_client.resolver.verdict_cache import JsonVerdictCache

    cache = JsonVerdictCache.__new__(JsonVerdictCache)
    cache._path = None
    cache._cache = {}
    resolver = SchemaResolver(mock_neptune, "fake-key", cache)
    resolver._er_enabled = False  # ER is tested separately
    mock_neptune.batch_exists.return_value = set()
    return resolver


def _text_kind_verdicts(resolver) -> dict[tuple[str, str], str]:
    """Record every ``SET_TEXT_KIND`` verdict the seam COMMITS.

    Returns a live ``{(type, attr): kind}`` view — the ported replacement for
    scraping ``textKind`` out of the SPARQL the seam used to emit (see the
    module docstring). The real ``_commit_ontology`` still runs, so the ingest
    under test is unchanged.
    """
    seen: dict[tuple[str, str], str] = {}
    real = resolver._commit_ontology

    async def recording(graph_uri, mutations, **kwargs):
        for mut in mutations:
            if mut.op is OntologyOpKind.SET_TEXT_KIND:
                seen[(mut.type_name, mut.slot_name)] = mut.text_kind
        return await real(graph_uri, mutations, **kwargs)

    resolver._commit_ontology = recording
    return seen


def _ticket_entities(n: int = 4) -> list[ExtractedEntity]:
    return [
        ExtractedEntity(
            type_name="Ticket",
            id=f"tk-{i}",
            attributes=[
                ExtractedAttribute(name="body", value=f"{_PROSE} Case {i}.", datatype="string"),
                ExtractedAttribute(name="subject", value=f"{_SUBJECT} {i}", datatype="string"),
                ExtractedAttribute(name="site_address", value=f"{_ADDRESS} unit {i}", datatype="string"),
                ExtractedAttribute(name="code", value=f"TK-{i:03d}", datatype="string"),
                ExtractedAttribute(name="attempts", value=str(i), datatype="integer"),
            ],
        )
        for i in range(n)
    ]


async def _run_extract_path(resolver, entities, decide=True) -> IngestResult:
    extraction = ExtractionResult(entities=entities)
    result = IngestResult(entities_extracted=len(entities))
    return await resolver._resolve_and_insert(
        extraction, GRAPH, {"Ticket": ""}, {"Ticket": {}},
        "", result, {}, {}, "batch-1",
        decide_text_candidacy=decide,
        instance_graph=INSTANCE_GRAPH,
    )


class TestExtractPathSeam:
    @pytest.mark.asyncio
    async def test_auto_tier_marks_long_prose_without_llm(self, mock_neptune):
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)
        adjudications: list[dict] = []

        async def record_adjudication(candidates):
            adjudications.append(candidates)
            return set(), set()

        resolver._adjudicate_free_text = record_adjudication
        result = await _run_extract_path(resolver, _ticket_entities())

        assert result.free_text_attributes == ["Ticket.body"]
        assert verdicts == {("Ticket", "body"): TEXT_KIND_FREE_TEXT}
        # body was AUTO (unambiguous long prose); only the borderline
        # attributes went to adjudication — and NAMES only reach that layer.
        assert len(adjudications) == 1
        assert set(adjudications[0]) == {("Ticket", "subject"), ("Ticket", "site_address")}

    @pytest.mark.asyncio
    async def test_ambiguous_band_follows_adjudication_verdict(self, mock_neptune):
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)

        async def adjudicate(candidates):
            # The REASON layer judges by name: subject = prose-ish, marked;
            # site_address = structured, EXPLICITLY declined (decided NO).
            return {("Ticket", "subject")}, {("Ticket", "site_address")}

        resolver._adjudicate_free_text = adjudicate
        result = await _run_extract_path(resolver, _ticket_entities())

        assert sorted(result.free_text_attributes) == ["Ticket.body", "Ticket.subject"]
        # The LLM's decided NO is written as the durable not_text verdict
        # (ONTA-173) — not left absent (absent = never-decided would be
        # re-sampled by the reconciler forever). Non-candidates (code is
        # CODE-shaped) get NO verdict of either polarity.
        assert verdicts == {
            ("Ticket", "body"): TEXT_KIND_FREE_TEXT,
            ("Ticket", "subject"): TEXT_KIND_FREE_TEXT,
            ("Ticket", "site_address"): TEXT_KIND_NOT_TEXT,
        }

    @pytest.mark.asyncio
    async def test_undecided_candidates_get_no_marker(self, mock_neptune):
        """A candidate the LLM did not adjudicate (absent from its response)
        stays UNDECIDED: no free_text marker, no not_text marker — only a
        genuine adjudication may persist a decided-no (ONTA-173)."""
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)

        async def adjudicate(candidates):
            return {("Ticket", "subject")}, set()  # site_address unadjudicated

        resolver._adjudicate_free_text = adjudicate
        await _run_extract_path(resolver, _ticket_entities())

        assert ("Ticket", "site_address") not in verdicts
        assert ("Ticket", "subject") in verdicts  # the adjudicated one did land

    @pytest.mark.asyncio
    async def test_marker_writes_invalidate_tenant_marker_cache(self, mock_neptune):
        """FIX (ONTA-173): the marker WRITE SITE owns the cache invalidation
        (mirroring the reconciler's self-invalidation) — refresh_after_write
        no longer blanket-invalidates on every write."""
        resolver = _resolver(mock_neptune)

        async def adjudicate(candidates):
            return set(), {("Ticket", "subject"), ("Ticket", "site_address")}

        resolver._adjudicate_free_text = adjudicate
        # Pre-populate the tenant's cached marker map.
        tm._cache[TENANT] = (999999.0, {})
        await _run_extract_path(resolver, _ticket_entities())
        assert TENANT not in tm._cache  # dropped by the write site

    @pytest.mark.asyncio
    async def test_no_marker_writes_no_cache_invalidation(self, mock_neptune):
        """A schema pass that decides NOTHING (auto tier empty, adjudication
        empty) writes no markers and must NOT drop the tenant's cache — the
        60s TTL is the hot path's protection (ONTA-173 FIX-3)."""
        resolver = _resolver(mock_neptune)

        async def adjudicate(candidates):
            return set(), set()

        resolver._adjudicate_free_text = adjudicate
        entities = [
            ExtractedEntity(
                type_name="Ticket",
                id="tk-0",
                attributes=[
                    ExtractedAttribute(name="code", value="TK-000", datatype="string"),
                ],
            )
        ]
        tm._cache[TENANT] = (999999.0, {"sentinel": True})
        await _run_extract_path(resolver, entities)
        assert TENANT in tm._cache  # untouched — nothing was written

    @pytest.mark.asyncio
    async def test_off_by_default_for_mapped_rows_route(self, mock_neptune):
        """/ingest/csv/rows calls _resolve_and_insert WITHOUT the flag: no
        candidacy, no adjudication LLM call (the route's "no LLM" contract).
        Those attributes stay undecided for ONTA-181's reconciler heuristic."""
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)

        async def must_not_run(candidates):
            raise AssertionError("adjudication must not run when candidacy is off")

        resolver._adjudicate_free_text = must_not_run
        result = await _run_extract_path(resolver, _ticket_entities(), decide=False)

        assert result.free_text_attributes == []
        assert verdicts == {}

    @pytest.mark.asyncio
    async def test_marking_is_best_effort_never_fails_ingest(self, mock_neptune):
        resolver = _resolver(mock_neptune)

        async def broken(candidates):
            raise RuntimeError("LLM seam exploded")

        resolver._adjudicate_free_text = broken
        # Must not raise; the ingest result still reports the inserted facts.
        result = await _run_extract_path(resolver, _ticket_entities())
        assert result.triples_inserted > 0


class TestAdjudicationCall:
    @pytest.mark.asyncio
    async def test_parses_recorded_output_and_filters_to_candidates(
        self, mock_neptune, monkeypatch,
    ):
        resolver = _resolver(mock_neptune)
        resolver._openrouter_key = "test-key"
        prompts: list[tuple[str, str]] = []

        async def recorded_chat(key, system, user_content, **kwargs):
            prompts.append((system, user_content))
            return json.dumps({
                "attributes": [
                    {"type": "Ticket", "attribute": "subject", "free_text": True,
                     "why": "short problem prose"},
                    {"type": "Ticket", "attribute": "site_address", "free_text": False,
                     "why": "postal address"},
                    # Hallucinated candidate the classifier never proposed:
                    {"type": "Ghost", "attribute": "x", "free_text": True,
                     "why": "not offered"},
                ],
            })

        monkeypatch.setattr(
            "infona_client.resolver.schema_resolver.openrouter_chat", recorded_chat,
        )
        confirmed, declined = await resolver._adjudicate_free_text({
            ("Ticket", "subject"): [_SUBJECT],
            ("Ticket", "site_address"): [_ADDRESS],
        })
        assert confirmed == {("Ticket", "subject")}
        # free_text=false on an OFFERED candidate is a genuine decided NO;
        # the hallucinated Ghost entry is filtered from BOTH sets.
        assert declined == {("Ticket", "site_address")}
        # The REASON layer sees names + samples under the candidacy prompt.
        assert prompts[0][0] is TEXT_CANDIDACY_SYSTEM
        assert "site_address" in prompts[0][1] and _ADDRESS in prompts[0][1]

    @pytest.mark.asyncio
    async def test_llm_failure_fails_closed(self, mock_neptune, monkeypatch):
        resolver = _resolver(mock_neptune)
        resolver._openrouter_key = "test-key"

        async def broken_chat(*args, **kwargs):
            raise RuntimeError("router down")

        monkeypatch.setattr(
            "infona_client.resolver.schema_resolver.openrouter_chat", broken_chat,
        )
        out = await resolver._adjudicate_free_text({("Ticket", "subject"): [_SUBJECT]})
        # BOTH sets empty: a failure must not fabricate decided-no verdicts —
        # unmarked AND undecided, never raised (ONTA-181 gets another look).
        assert out == (set(), set())

    @pytest.mark.asyncio
    async def test_junk_json_fails_closed(self, mock_neptune, monkeypatch):
        resolver = _resolver(mock_neptune)
        resolver._openrouter_key = "test-key"

        async def junk_chat(*args, **kwargs):
            return "sorry, I cannot help with that"

        monkeypatch.setattr(
            "infona_client.resolver.schema_resolver.openrouter_chat", junk_chat,
        )
        out = await resolver._adjudicate_free_text({("Ticket", "subject"): [_SUBJECT]})
        assert out == (set(), set())


def _listing_mapping(remarks_kind: str | None = "free_text") -> CSVSchemaMapping:
    return CSVSchemaMapping(
        entity_type="Listing",
        columns=[
            ColumnMapping(column_name="mls", role=ColumnRole.TYPE_ID,
                          datatype="string", attribute_name="mls"),
            ColumnMapping(column_name="remarks", role=ColumnRole.ATTRIBUTE,
                          datatype="string", attribute_name="remarks",
                          text_kind=remarks_kind),
            ColumnMapping(column_name="address", role=ColumnRole.ATTRIBUTE,
                          datatype="string", attribute_name="address"),
        ],
    )


_LISTING_ROWS = [
    {"mls": f"M{i}", "remarks": f"Charming home near the park {i}",
     "address": f"{i} Main St"}
    for i in range(3)
]


class TestMappedPathSeam:
    @pytest.mark.asyncio
    async def test_schema_time_verdict_is_applied_at_apply_time(self, mock_neptune):
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)
        result = await resolver._ingest_mapped(
            _listing_mapping(), _LISTING_ROWS, GRAPH, {"Listing": ""}, {"Listing": {}}, "",
            instance_graph=INSTANCE_GRAPH,
        )
        assert result.free_text_attributes == ["Listing.remarks"]
        assert verdicts == {("Listing", "remarks"): TEXT_KIND_FREE_TEXT}

    @pytest.mark.asyncio
    async def test_mapping_not_text_verdict_persists_durable_marker(self, mock_neptune):
        """A mapping column carrying the decided-no ``"not_text"`` verdict
        (the REASON pass explicitly declined a TEXT-shaped column) persists
        the durable marker at apply time (ONTA-173) — and does NOT count as a
        free-text attribute in the result."""
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)
        result = await resolver._ingest_mapped(
            _listing_mapping(remarks_kind=TEXT_KIND_NOT_TEXT), _LISTING_ROWS,
            GRAPH, {"Listing": ""}, {"Listing": {}}, "",
            instance_graph=INSTANCE_GRAPH,
        )
        assert result.free_text_attributes == []
        assert verdicts == {("Listing", "remarks"): TEXT_KIND_NOT_TEXT}

    @pytest.mark.asyncio
    async def test_mapping_marker_writes_invalidate_tenant_marker_cache(
        self, mock_neptune,
    ):
        """FIX (ONTA-173): the mapped path's marker write site self-invalidates
        the tenant marker cache (refresh_after_write no longer does)."""
        resolver = _resolver(mock_neptune)
        tm._cache[TENANT] = (999999.0, {})
        await resolver._ingest_mapped(
            _listing_mapping(), _LISTING_ROWS, GRAPH, {"Listing": ""}, {"Listing": {}}, "",
            instance_graph=INSTANCE_GRAPH,
        )
        assert TENANT not in tm._cache

    @pytest.mark.asyncio
    async def test_reingest_reemits_the_same_idempotent_verdict(self, mock_neptune):
        """Re-ingesting the same mapping re-emits the identical verdict —
        idempotent under re-ingest by construction.

        Ported by ONTA-527: the old form compared the two runs' ``textKind``
        SPARQL and asserted the single-valued ``DELETE``/``INSERT`` upsert
        shape. That string is gone with the SPARQL path; idempotency now rides
        on the mutation itself (``SET_TEXT_KIND`` is an upsert op), so the two
        runs' emitted verdicts are compared instead.
        """
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)
        await resolver._ingest_mapped(
            _listing_mapping(), _LISTING_ROWS, GRAPH, {"Listing": ""}, {"Listing": {}}, "",
            instance_graph=INSTANCE_GRAPH,
        )
        first = dict(verdicts)
        verdicts.clear()
        await resolver._ingest_mapped(
            _listing_mapping(), _LISTING_ROWS, GRAPH, {"Listing": ""}, {"Listing": {}}, "",
            instance_graph=INSTANCE_GRAPH,
        )
        assert first == verdicts == {("Listing", "remarks"): TEXT_KIND_FREE_TEXT}

    @pytest.mark.asyncio
    async def test_legacy_mapping_without_verdicts_writes_no_markers(self, mock_neptune):
        """A hand-written / pre-ONTA-177 mapping carries no text_kind:
        candidacy stays UNDECIDED (no marker, no LLM) — the decided-once
        contract; ONTA-181's reconciler heuristic covers these later."""
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)

        async def must_not_run(candidates):
            raise AssertionError("mapped path must never re-adjudicate")

        resolver._adjudicate_free_text = must_not_run
        result = await resolver._ingest_mapped(
            _listing_mapping(remarks_kind=None), _LISTING_ROWS,
            GRAPH, {"Listing": ""}, {"Listing": {}}, "",
            instance_graph=INSTANCE_GRAPH,
        )
        assert result.free_text_attributes == []
        assert verdicts == {}

    @pytest.mark.asyncio
    async def test_marker_lands_on_resolved_type_and_owner_entity(self, mock_neptune):
        """Multi-entity mapping + type matching: the verdict rides on the
        OWNING entity's declared type and lands on the RESOLVED ontology type
        (the type matcher may map the declared name onto an existing type)."""
        resolver = _resolver(mock_neptune)
        verdicts = _text_kind_verdicts(resolver)

        async def resolve_to_client(entity, *args, **kwargs):
            return "Client" if entity.type_name == "Customer" else entity.type_name

        resolver._resolve_type = resolve_to_client
        mapping = CSVSchemaMapping(
            entity_type="",
            entities=[
                EntitySpec(name="customer", type_name="Customer", id_column="email"),
                EntitySpec(name="order", type_name="Order", id_column="order_id"),
            ],
            columns=[
                ColumnMapping(column_name="email", role=ColumnRole.ATTRIBUTE,
                              datatype="string", attribute_name="email",
                              entity="customer"),
                ColumnMapping(column_name="notes", role=ColumnRole.ATTRIBUTE,
                              datatype="string", attribute_name="Customer Notes",
                              entity="customer", text_kind="free_text"),
                ColumnMapping(column_name="order_id", role=ColumnRole.ATTRIBUTE,
                              datatype="string", attribute_name="order_id",
                              entity="order"),
            ],
        )
        rows = [{"email": "a@x.com", "notes": "prefers morning calls", "order_id": "O1"}]
        result = await resolver._ingest_mapped(
            mapping, rows, GRAPH, {"Order": ""}, {"Order": {}}, "",
            instance_graph=INSTANCE_GRAPH,
        )
        # Resolved type (Client, not Customer) + normalized attribute name —
        # the same type + leaf the instance facts are written under.
        assert result.free_text_attributes == ["Client.customer_notes"]
        assert verdicts == {("Client", "customer_notes"): TEXT_KIND_FREE_TEXT}


# ---------------------------------------------------------------------------
# The verdict is decided — and then dropped (ONTA-527 finding).
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (pre-dates ONTA-527, surfaced by it): every verdict "
        "the seams above commit is discarded, so ingest-time free-text marking "
        "does not work in production. "
        "graph/ontology_commit.py::_commit_ontology_graph_store — the branch "
        "taken whenever a process GraphStore exists, i.e. always — handles "
        "UPSERT_TYPE / UPSERT_ATTRIBUTE / UPSERT_RELATIONSHIP / SET_SUBCLASS "
        "and drops every other op into an `else` that logs "
        "`ontology_store_op_skipped`, SET_TEXT_KIND included; it returns with "
        "`applied` empty. graph/ontology_catalog.py has no text-kind concept "
        "at all, and the only reader (text_markers.get_free_text_map) is "
        "SPARQL. Same root cause as the strict xfail in "
        "test_semantic_reconciler.py, reached from the ingest side instead of "
        "the reconciler side. Not fixed here: it needs a text-kind port to the "
        "catalog on both sides."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_committed_text_kind_verdict_is_applied(mock_neptune):
    """``commit_ontology`` must actually APPLY a SET_TEXT_KIND mutation."""
    from infona_client.graph.ontology_commit import commit_ontology
    from infona_client.models.ontology import OntologyMutation

    result = await commit_ontology(
        mock_neptune,
        GRAPH,
        [OntologyMutation(
            op=OntologyOpKind.SET_TEXT_KIND,
            type_name="Ticket",
            slot_name="body",
            text_kind=TEXT_KIND_FREE_TEXT,
        )],
        message="ONTA-177 candidacy verdict",
    )
    assert [m.op for m in result.applied] == [OntologyOpKind.SET_TEXT_KIND]
