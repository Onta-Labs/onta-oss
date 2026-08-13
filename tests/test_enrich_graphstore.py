"""ONTA-534 residual: enrich plan + entity select must not SPARQL.

Production Ask Onta ("I wanna enrich clinical trials with their lead sponsor
and latest status") failed because ``_list_types`` still called
``NeptuneClient.query`` → ``SparqlClientRetired`` → empty type list → empty
plan → "couldn't determine the specifics" when Explorer had no type selected.

Anti-overfit: synthetic type/attr names plus the exact user phrasing as a
regression fixture.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from infona_client.agent import planner as planner_mod
from infona_client.agent.capabilities.enrich_cap import (
    EnrichCapability,
    _list_types,
    _parse_enrich_instruction,
)
from infona_client.agent.planner import register_default_capabilities, reset_plan_store
from infona_client.agent.registry import AgentContext, reset_capabilities
from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.executor import EnrichmentExecutor
from infona_client.enrichment.job_store import InMemoryJobStore
from infona_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
)
from infona_client.enrichment.strategy import list_declared_types
from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests

TENANT = "gt-demo"
KG = "label-compliance"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
TYPE_CT = "ClinicalTrial"
ATTR_SPONSOR = "lead_sponsor"
ATTR_STATUS = "latest_status"
USER_MSG = (
    "I wanna enrich clinical trials with their lead sponsor and latest status"
)


def _retired_client() -> NeptuneClient:
    """Real NeptuneClient instance (no HTTP) — production type-check shape."""
    c = NeptuneClient.__new__(NeptuneClient)
    c._endpoint = "http://unused.invalid"
    c._allow_http = False

    async def boom(*_a, **_k):
        raise AssertionError("enrich path must not call SPARQL under GraphStore")

    c.query = boom  # type: ignore[method-assign]
    c.update = boom  # type: ignore[method-assign]
    return c


def _store():
    s = MemoryGraphStore()
    configure_graph_store(s)
    return s


async def _seed_catalog(store: MemoryGraphStore, *, with_attrs: bool = False) -> None:
    await upsert_type(
        store=store,
        name=TYPE_CT,
        description="a trial",
        layer="tenant",
        tenant_id=TENANT,
    )
    if with_attrs:
        await upsert_attribute(
            store=store,
            type_name=TYPE_CT,
            attr_name=ATTR_SPONSOR,
            datatype="string",
            layer="tenant",
            tenant_id=TENANT,
        )
        await upsert_attribute(
            store=store,
            type_name=TYPE_CT,
            attr_name=ATTR_STATUS,
            datatype="string",
            layer="tenant",
            tenant_id=TENANT,
        )


async def _seed_entities(store: MemoryGraphStore) -> list[str]:
    e1 = entity_uri(TYPE_CT, "nct1")
    e2 = entity_uri(TYPE_CT, "nct2")
    rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    rdfs_label = "http://www.w3.org/2000/01/rdf-schema#label"
    type_iri = f"{IRI_BASE}/types/{TYPE_CT}"
    await insert_facts(
        None,
        GRAPH,
        [
            (e1, rdf_type, type_iri),
            (e1, rdfs_label, "Trial One"),
            (e2, rdf_type, type_iri),
            (e2, rdfs_label, "Trial Two"),
        ],
        store=store,
    )
    return [e1, e2]


def test_parse_enrich_with_their_multi_attrs():
    """Deterministic fallback must not treat the TYPE as the attribute."""
    parsed = _parse_enrich_instruction(USER_MSG)
    assert parsed["attributes"] == [ATTR_SPONSOR, ATTR_STATUS]
    # Verb-adjacent capture ("clinical trials") is the old bug.
    assert "clinical_trials" not in parsed["attributes"]
    assert "clinical" not in parsed["attributes"]


def test_parse_enrich_with_their_single_attr():
    parsed = _parse_enrich_instruction("enrich brokers with their websites")
    assert parsed["attributes"] == ["websites"]


def test_list_types_uses_catalog_when_store_configured():
    store = _store()
    try:

        async def run():
            await _seed_catalog(store)
            ctx = AgentContext(
                tenant_id=TENANT,
                kg_name=KG,
                neptune=_retired_client(),
                type_name=None,
            )
            names = await _list_types(ctx)
            assert TYPE_CT in names

        asyncio.run(run())
    finally:
        asyncio.run(store.close())
        reset_graph_store_for_tests()


def test_list_declared_types_uses_catalog_for_real_client():
    store = _store()
    try:

        async def run():
            await _seed_catalog(store)
            names = await list_declared_types(_retired_client(), TENANT)
            assert TYPE_CT in names

        asyncio.run(run())
    finally:
        asyncio.run(store.close())
        reset_graph_store_for_tests()


def test_enrich_plan_user_phrasing_no_selected_type():
    """The 2026-08-13 Explorer screenshot: no type selected, ClinicalTrial named
    in prose, SPARQL retired. Must plan, not return []."""
    store = _store()
    try:

        async def run():
            await _seed_catalog(store)
            ctx = AgentContext(
                tenant_id=TENANT,
                kg_name=KG,
                neptune=_retired_client(),
                type_name=None,
                openrouter_key="",  # force deterministic parser
            )
            steps = await EnrichCapability().plan(ctx, USER_MSG)
            assert steps, "plan() returned [] — type/attr extract failed"
            step = steps[-1]
            assert step.capability == "enrich"
            assert step.params["type_name"] == TYPE_CT
            assert ATTR_SPONSOR in step.params["attributes"]
            assert ATTR_STATUS in step.params["attributes"]

        asyncio.run(run())
    finally:
        asyncio.run(store.close())
        reset_graph_store_for_tests()


def test_agent_http_cli_path_plans_without_selected_type(monkeypatch):
    """Same canonical ``POST /agent`` the CLI / Explorer / MCP hit.

    No ``type_name`` in context (Explorer home). Classifier forced to enrich.
    Real SPARQL-retired client. Must return a plan, not the clarify the
    screenshot showed.
    """
    from fastapi.testclient import TestClient

    from infona_client.api.app import create_app
    from infona_client.graph.store import get_graph_store

    async def fake_classify(*_a, **_k):
        return {"intents": ["enrich"], "clarify": "", "options": []}

    monkeypatch.setattr(planner_mod, "_classify", fake_classify)

    reset_capabilities()
    reset_plan_store()
    register_default_capabilities()

    http_tenant = "test-tenant"
    store = get_graph_store()

    async def seed():
        await upsert_type(
            store=store,
            name=TYPE_CT,
            description="a trial",
            layer="tenant",
            tenant_id=http_tenant,
        )
        await store.kg_registry_upsert(
            http_tenant, KG, description="", triple_count=1
        )

    asyncio.run(seed())

    app = create_app()
    # Lifespan overwrites state.neptune_client — inject AFTER TestClient
    # (same as tests/conftest.py).
    client = TestClient(app)
    app.state.neptune_client = _retired_client()
    r = client.post(
        f"/graphs/{http_tenant}/agent",
        json={
            "message": USER_MSG,
            "context": {"kg_name": KG},
        },
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("kind") == "plan", body
    steps = body.get("steps") or []
    assert steps
    params = steps[-1]["params"]
    assert params["type_name"] == TYPE_CT
    assert ATTR_SPONSOR in params["attributes"]
    assert ATTR_STATUS in params["attributes"]

    reset_capabilities()
    reset_plan_store()


def test_executor_selects_entities_from_graphstore_without_sparql():
    store = _store()
    try:

        async def run():
            await _seed_catalog(store)
            uris = await _seed_entities(store)
            jobs = InMemoryJobStore()
            job = EnrichJob(
                id="job-gs",
                tenant_id=TENANT,
                kg_name=KG,
                type_name=TYPE_CT,
                attributes=[ATTR_SPONSOR],
                tier=EnrichmentTier.lite,
                status=JobStatus.queued,
                created_at=datetime.now(timezone.utc),
                conflict_policy=ConflictPolicy.skip,
            )
            await jobs.create(job)
            adapter = AsyncMock()
            adapter.name = "wikidata"
            adapter.lookup = AsyncMock(return_value=[])
            executor = EnrichmentExecutor(
                _retired_client(), jobs, EnrichmentCache(), adapter
            )
            await executor.run(job, TENANT)
            final = await jobs.get(job.id)
            assert final is not None
            assert final.status != JobStatus.failed, final.error
            assert final.progress.total == len(uris) * 1

        asyncio.run(run())
    finally:
        asyncio.run(store.close())
        reset_graph_store_for_tests()


def test_executor_empty_type_completes_zero_not_failed():
    """Store select returning [] (type exists, no instances) must complete
    with total=0 — not fall through to SPARQL and fail the job."""
    store = _store()
    try:

        async def run():
            await _seed_catalog(store)
            jobs = InMemoryJobStore()
            job = EnrichJob(
                id="job-empty",
                tenant_id=TENANT,
                kg_name=KG,
                type_name=TYPE_CT,
                attributes=[ATTR_SPONSOR],
                tier=EnrichmentTier.lite,
                status=JobStatus.queued,
                created_at=datetime.now(timezone.utc),
                conflict_policy=ConflictPolicy.skip,
            )
            await jobs.create(job)
            adapter = AsyncMock()
            adapter.name = "wikidata"
            adapter.lookup = AsyncMock(return_value=[])
            executor = EnrichmentExecutor(
                _retired_client(), jobs, EnrichmentCache(), adapter
            )
            await executor.run(job, TENANT)
            final = await jobs.get(job.id)
            assert final is not None
            assert final.status != JobStatus.failed, final.error
            assert final.progress.total == 0

        asyncio.run(run())
    finally:
        asyncio.run(store.close())
        reset_graph_store_for_tests()
