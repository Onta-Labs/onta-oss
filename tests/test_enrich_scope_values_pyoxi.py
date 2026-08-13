"""GraphStore tests for the MULTI-VALUE scope resolver
(``EnrichmentExecutor.select_scope_value_uris``).

ONTA-527: this used to validate generated SPARQL against pyoxigraph. Production
is Neo4j-only; the resolver now filters GraphStore entity props. Same
persona-eval contract: a scoped refresh over a SET of values matches members
of the set, not a crammed literal.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.executor import EnrichmentExecutor
from infona_client.enrichment.job_store import InMemoryJobStore
from infona_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
    Verdict,
)
from infona_client.graph.ontology_queries import attr_uri
from infona_client.graph.store import get_graph_store
from tests._enrichment_prov_helpers import seed_enrich_entities

ENT = "https://graph.infona.ai/entities/"
TENANT, KG, TYPE = "scope-vals-gs", "k1", "Widget"


class _NoAdapter:
    name = "none"
    is_paid = False

    async def lookup(self, *a, **k):
        return []


def _executor() -> EnrichmentExecutor:
    from unittest.mock import AsyncMock

    n = AsyncMock()
    n.query.side_effect = AssertionError("enrich must not SPARQL")
    return EnrichmentExecutor(n, InMemoryJobStore(), EnrichmentCache(), _NoAdapter())


async def _seed_literal_scope() -> None:
    made_by = attr_uri(TYPE, "made_by")
    await seed_enrich_entities(
        TYPE,
        [
            {"uri": f"{ENT}{TYPE}/w1", "label": "w1", "vals": f"{made_by}::acme corp"},
            {"uri": f"{ENT}{TYPE}/w2", "label": "w2", "vals": f"{made_by}::GLOBEX"},
            {"uri": f"{ENT}{TYPE}/w3", "label": "w3", "vals": f"{made_by}::Umbrella"},
        ],
        tenant_id=TENANT,
        kg_name=KG,
    )


@pytest.mark.asyncio
async def test_select_scope_value_uris_matches_case_insensitive_set():
    await _seed_literal_scope()
    uris = await _executor().select_scope_value_uris(
        TENANT, KG, TYPE, "made_by",
        ["Acme Corp", "globex", "Initech"],
    )
    assert sorted(uris) == [f"{ENT}{TYPE}/w1", f"{ENT}{TYPE}/w2"], uris


@pytest.mark.asyncio
async def test_select_scope_value_uris_matches_literal_prop():
    """A scope predicate stored as an entity prop matches case-insensitively."""
    made_by = attr_uri(TYPE, "made_by")
    await seed_enrich_entities(
        TYPE,
        [
            {"uri": f"{ENT}{TYPE}/w1", "label": "w1", "vals": f"{made_by}::Deepgram"},
            {"uri": f"{ENT}{TYPE}/w2", "label": "w2", "vals": f"{made_by}::Cartesia"},
        ],
        tenant_id=TENANT,
        kg_name=KG,
    )
    uris = await _executor().select_scope_value_uris(
        TENANT, KG, TYPE, "made_by", ["deepgram"]
    )
    assert uris == [f"{ENT}{TYPE}/w1"], uris


@pytest.mark.asyncio
async def test_count_entities_lists_primary_type():
    await seed_enrich_entities(
        "Model",
        [
            {"uri": f"{ENT}Model/m0", "label": "m0"},
            {"uri": f"{ENT}Model/m1", "label": "m1"},
            {"uri": f"{ENT}Model/m2", "label": "m2"},
        ],
        tenant_id=TENANT,
        kg_name=KG,
    )
    ex = _executor()
    assert await ex.count_entities(TENANT, KG, "Model") == 3
    assert await ex.count_entities(TENANT, KG, "SpeechToTextModel") == 0


@pytest.mark.asyncio
async def test_select_scope_value_uris_empty_on_no_match_and_bad_predicate():
    await _seed_literal_scope()
    ex = _executor()
    assert await ex.select_scope_value_uris(
        TENANT, KG, TYPE, "made_by", ["nobody", "nothing"]
    ) == []
    assert await ex.select_scope_value_uris(TENANT, KG, TYPE, "made_by", []) == []


class _FakeAdapter:
    name = "wikidata"
    is_paid = False

    def __init__(self, mapping):
        self._mapping = mapping

    async def lookup(self, entity_label, attribute, context=None, job=None):
        return self._mapping.get((entity_label, attribute), [])


@pytest.mark.asyncio
async def test_overwrite_writes_fresh_value_to_store():
    pricing = attr_uri(TYPE, "pricing")
    w1 = f"{ENT}{TYPE}/w1"
    await seed_enrich_entities(
        TYPE,
        [{
            "uri": w1,
            "label": "Acme Widget",
            "vals": f'{pricing}::0.0100 (2023-09-01)',
        }],
        tenant_id=TENANT,
        kg_name=KG,
    )
    from unittest.mock import AsyncMock

    n = AsyncMock()
    n.query.side_effect = AssertionError("enrich must not SPARQL")
    ex = EnrichmentExecutor(
        n, InMemoryJobStore(), EnrichmentCache(),
        _FakeAdapter({("Acme Widget", "pricing"): [
            Verdict(
                value="0.0043 (2026-07-07)",
                confidence=0.95,
                source="fake",
                source_url="https://new.example/price",
                source_published_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
            )
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
    assert final.status != JobStatus.failed, final.error
    store = get_graph_store()
    props = next(r["props"] for r in store.snapshot_entities() if r["id"] == w1)
    got = props.get("pricing")
    assert "0.0043 (2026-07-07)" in (got if isinstance(got, list) else [got])


@pytest.mark.asyncio
async def test_verify_does_not_replace_conflicting_value():
    pricing = attr_uri(TYPE, "pricing")
    w1 = f"{ENT}{TYPE}/w1"
    await seed_enrich_entities(
        TYPE,
        [{
            "uri": w1,
            "label": "Acme Widget",
            "vals": f'{pricing}::0.0100 (2023-09-01)',
        }],
        tenant_id=TENANT,
        kg_name=KG,
    )
    from unittest.mock import AsyncMock

    n = AsyncMock()
    n.query.side_effect = AssertionError("enrich must not SPARQL")
    ex = EnrichmentExecutor(
        n, InMemoryJobStore(), EnrichmentCache(),
        _FakeAdapter({("Acme Widget", "pricing"): [
            Verdict(
                value="0.0043 (2026-07-07)",
                confidence=0.95,
                source="fake",
                source_url="https://new.example/price",
                source_published_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
            )
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
    store = get_graph_store()
    props = next(r["props"] for r in store.snapshot_entities() if r["id"] == w1)
    assert props.get("pricing") == "0.0100 (2023-09-01)"
