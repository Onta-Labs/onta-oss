"""Enrichment-rail registry adapter + authority precedence (ONTA-194, phase 3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from cograph_client.api_registry import (
    ApiSourceSpec,
    RegistryApiSource,
    RegistrySourceAdapter,
    make_api_source_catalog,
    register_registry_enrichment,
    reset_registry_enrichment,
)
from cograph_client.api_registry.catalog import reset_api_source_layers
from cograph_client.api_registry.spec import validate_spec
from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import EnrichmentExecutor
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
    Verdict,
)
from cograph_client.enrichment.sources.base import register_adapter
from cograph_client.enrichment.tiers import (
    get_chain,
    register_tier,
    reset_chain_prefix_providers,
    reset_tiers,
)
from cograph_client.retrieval import safety as safety_mod


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(safety_mod, "_resolve_ips", lambda host: ["93.184.216.34"])
    reset_api_source_layers()
    reset_tiers()
    reset_chain_prefix_providers()
    reset_registry_enrichment()
    yield
    reset_api_source_layers()
    reset_tiers()
    reset_chain_prefix_providers()
    reset_registry_enrichment()


# A canned NPPES record for "Jane Smith".
_NPPES_REC = {
    "result_count": 1,
    "results": [{
        "number": "1234567893",
        "basic": {"first_name": "JANE", "last_name": "SMITH", "credential": "MD"},
        "taxonomies": [{"desc": "Internal Medicine, Cardiovascular Disease"}],
        "addresses": [{"city": "BOSTON", "state": "MA", "telephone_number": "617-555-0100"}],
    }],
}


def _nppes_adapter(handler=None):
    cat = make_api_source_catalog()
    spec = cat.get("nppes")
    if handler is None:
        def handler(req):
            return httpx.Response(200, json=_NPPES_REC)
    ex = RegistryApiSource(transport=httpx.MockTransport(handler))
    return RegistrySourceAdapter(spec, executor=ex)


# --------------------------------------------------------------------------- #
# Adapter unit behavior
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_adapter_fills_npi_with_source_of_truth_confidence():
    v = await _nppes_adapter().lookup("Jane Smith", "npi", {"entity_type": "Physician"})
    assert len(v) == 1
    assert v[0].value == "1234567893"
    assert v[0].confidence == 0.95           # source_of_truth
    assert v[0].source == "api:nppes"
    assert v[0].source_url.startswith("https://npiregistry.cms.hhs.gov/api/")


@pytest.mark.asyncio
async def test_adapter_derives_first_last_name_bindings():
    seen = {}

    def handler(req):
        from urllib.parse import parse_qs, urlparse
        seen.update({k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()})
        return httpx.Response(200, json=_NPPES_REC)

    await _nppes_adapter(handler).lookup("Jane Smith", "phone", {"entity_type": "Doctor"})
    assert seen["first_name"] == "Jane"
    assert seen["last_name"] == "Smith"


@pytest.mark.asyncio
async def test_adapter_self_gates_on_uncoverable_attribute():
    v = await _nppes_adapter().lookup("Jane Smith", "favorite_color", {"entity_type": "Physician"})
    assert v == []


@pytest.mark.asyncio
async def test_adapter_self_gates_on_wrong_entity_type():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=_NPPES_REC)

    v = await _nppes_adapter(handler).lookup("Acme Corp", "npi", {"entity_type": "Company"})
    assert v == []
    assert calls["n"] == 0  # never hit the API for a type it can't serve


@pytest.mark.asyncio
async def test_generic_organization_type_does_not_match_via_stopword():
    # "Organization" shares only the generic "organization" token with
    # "health_organization" — it must NOT match (no spurious API call).
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=_NPPES_REC)

    v = await _nppes_adapter(handler).lookup("Acme Corporation", "phone", {"entity_type": "Organization"})
    assert v == []
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_adapter_missing_entity_type_still_answers_covered_attribute():
    # ONTA-191: don't over-exclude when the type is absent.
    v = await _nppes_adapter().lookup("Jane Smith", "npi", {})
    assert v and v[0].value == "1234567893"


@pytest.mark.asyncio
async def test_adapter_empty_result_returns_no_verdict():
    def handler(req):
        return httpx.Response(200, json={"result_count": 0, "results": []})

    assert await _nppes_adapter(handler).lookup("Nobody Here", "npi", {"entity_type": "Physician"}) == []


@pytest.mark.asyncio
async def test_adapter_never_raises_on_transport_error():
    def handler(req):
        raise httpx.ConnectError("boom")

    assert await _nppes_adapter(handler).lookup("Jane Smith", "npi", {"entity_type": "Physician"}) == []


@pytest.mark.asyncio
async def test_adapter_dormant_entry_returns_no_verdict(monkeypatch):
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    spec = ApiSourceSpec.from_dict({
        "slug": "paid_dir", "title": "Paid", "base_url": "https://api.paid.test",
        "auth": {"mode": "api_key_query", "key_env": "DEMO_TOKEN", "query_key": "token"},
        "entitlement": "paid",
        "endpoints": [{"name": "s", "path": "/s", "result_path": "results",
                       "field_mappings": {"id": "id"},
                       "params": [{"name": "q", "target": "q", "enrich_from": "entity_name"}],
                       "pagination": {"style": "none"}}],
    })
    ad = RegistrySourceAdapter(spec, executor=RegistryApiSource(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"results": []}))))
    assert await ad.lookup("Acme", "id", {}) == []


# --------------------------------------------------------------------------- #
# Chain wiring / authority ordering
# --------------------------------------------------------------------------- #
def test_register_registry_enrichment_leads_chain():
    names = register_registry_enrichment(make_api_source_catalog())
    assert "api:nppes" in names
    chain = get_chain(EnrichmentTier.core)
    assert chain[0] == "api:nppes"   # registry leads
    assert "wikidata" in chain


def test_only_enrich_ready_entries_are_registered():
    # NPPES has enrich_from params; geonames/openfoodfacts/clinicaltrials (seed)
    # do not, so they aren't registered as enrichment adapters.
    names = register_registry_enrichment(make_api_source_catalog())
    assert names == ["api:nppes"]


def test_chain_prefix_survives_a_later_register_tier_override():
    register_registry_enrichment(make_api_source_catalog())
    # The proprietary plugin would override tiers AFTER registration; the prefix
    # is recomputed per get_chain(), so the registry still leads.
    register_tier(EnrichmentTier.core, ["wikidata", "exa", "perplexity"])
    chain = get_chain(EnrichmentTier.core)
    assert chain == ["api:nppes", "wikidata", "exa", "perplexity"]


# --------------------------------------------------------------------------- #
# Authority-precedence E2E through the real executor
# --------------------------------------------------------------------------- #
def _physician_neptune():
    rows = [{"uri": "https://cograph.tech/entities/Physician/p1", "label": "Jane Smith", "vals": ""}]
    bindings = [{
        "e": {"type": "uri", "value": rows[0]["uri"]},
        "label": {"type": "literal", "value": rows[0]["label"]},
        "vals": {"type": "literal", "value": rows[0]["vals"]},
    }]
    neptune = AsyncMock()
    neptune.query.return_value = {"head": {"vars": ["e", "label", "vals"]},
                                  "results": {"bindings": bindings}}
    neptune.update.return_value = None
    return neptune


class _WebFake:
    """A web adapter that would fill npi with a WRONG value — must lose to NPPES."""

    def __init__(self):
        self.name = "webfake"
        self.calls = []

    async def lookup(self, entity_label, attribute, context):
        self.calls.append((entity_label, attribute))
        return [Verdict(value="0000000000", confidence=0.9, source="webfake")]


def test_registry_verdict_outranks_web_adapter():
    async def run():
        # NPPES leads the chain; a web adapter sits behind it.
        register_registry_enrichment(make_api_source_catalog(),
                                     executor=RegistryApiSource(transport=httpx.MockTransport(
                                         lambda r: httpx.Response(200, json=_NPPES_REC))))
        web = _WebFake()
        register_adapter(web)
        register_tier(EnrichmentTier.lite, ["webfake"])   # get_chain -> [api:nppes, webfake]
        assert get_chain(EnrichmentTier.lite) == ["api:nppes", "webfake"]

        neptune = _physician_neptune()
        store = InMemoryJobStore()
        job = EnrichJob(
            id="job-nppes", tenant_id="test-tenant", kg_name="kg",
            type_name="Physician", attributes=["npi"], tier=EnrichmentTier.lite,
            status=JobStatus.queued, created_at=datetime.now(timezone.utc),
            conflict_policy=ConflictPolicy.stage, confidence_min=0.85,
        )
        await store.create(job)
        # wikidata_adapter is irrelevant here (not in the chain); pass the web fake.
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), web)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.progress.filled == 1
        # The web adapter was NEVER consulted — NPPES (source-of-truth) short-
        # circuited the chain, so the authoritative NPI won.
        assert web.calls == []

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Spec validation for the enrich_from recipe
# --------------------------------------------------------------------------- #
def test_invalid_enrich_from_is_rejected():
    d = {
        "slug": "x", "title": "X", "base_url": "https://api.x.test", "auth": {"mode": "none"},
        "endpoints": [{"name": "s", "path": "/s", "field_mappings": {"id": "id"},
                       "params": [{"name": "q", "target": "q", "enrich_from": "bogus"}],
                       "pagination": {"style": "none"}}],
    }
    errs = validate_spec(ApiSourceSpec.from_dict(d))
    assert any("enrich_from" in e for e in errs)


def test_nppes_seed_enrich_from_roundtrips():
    spec = make_api_source_catalog().get("nppes")
    assert not validate_spec(spec)
    rebuilt = ApiSourceSpec.from_dict(spec.to_dict())
    fn = {p.name: p.enrich_from for p in rebuilt.endpoint().params if p.enrich_from}
    assert fn == {"first_name": "entity_name_first", "last_name": "entity_name_last"}
