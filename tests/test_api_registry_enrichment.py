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
    # NPPES + FRED have enrich_from params; geonames/openfoodfacts/clinicaltrials
    # (seed) do not, so they aren't registered as enrichment adapters. Order is by
    # authority rank then slug: nppes (source_of_truth) leads fred (authoritative).
    names = register_registry_enrichment(make_api_source_catalog())
    assert names == ["api:nppes", "api:fred"]


def test_chain_prefix_survives_a_later_register_tier_override():
    register_registry_enrichment(make_api_source_catalog())
    # The proprietary plugin would override tiers AFTER registration; the prefix
    # is recomputed per get_chain(), so the registry still leads.
    register_tier(EnrichmentTier.core, ["wikidata", "exa", "perplexity"])
    chain = get_chain(EnrichmentTier.core)
    assert chain == ["api:nppes", "api:fred", "wikidata", "exa", "perplexity"]


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
        register_tier(EnrichmentTier.lite, ["webfake"])   # get_chain -> [api:nppes, api:fred, webfake]
        assert get_chain(EnrichmentTier.lite) == ["api:nppes", "api:fred", "webfake"]

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


# --------------------------------------------------------------------------- #
# enrich_from: attribute:<attr> — bind a param from another entity attribute
# (the ID-keyed-API recipe: e.g. a resolved bls_series_id feeds a FRED price
# lookup). Two enrichment steps: step 1 resolves the key attribute, step 2
# reads it here.
# --------------------------------------------------------------------------- #
def _attr_binding_spec() -> ApiSourceSpec:
    return ApiSourceSpec.from_dict({
        "slug": "fredlike", "title": "FRED-like", "base_url": "https://api.x.test",
        "auth": {"mode": "none"}, "authority_level": "authoritative",
        "coverage": {"entity_kinds": ["ingredient"], "attributes": ["national_avg_price"]},
        "endpoints": [{
            "name": "obs", "path": "/obs", "method": "GET",
            "params": [{"name": "series_id", "target": "series_id",
                        "enrich_from": "attribute:bls_series_id"}],
            "result_path": "observations",
            "field_mappings": {"national_avg_price": "value"},
            "pagination": {"style": "none"},
            "smoke_bindings": {"series_id": "APU0000712311"},
        }],
    })


def test_attribute_enrich_from_is_accepted():
    # The dynamic attribute:<attr> recipe validates (was rejected before the feature).
    assert not validate_spec(_attr_binding_spec())


def test_malformed_attribute_enrich_from_is_rejected():
    for bad in ("attribute:", "attribute:bad!", "attribute: x"):
        d = {"slug": "x", "title": "X", "base_url": "https://api.x.test", "auth": {"mode": "none"},
             "endpoints": [{"name": "s", "path": "/s", "field_mappings": {"id": "id"},
                            "params": [{"name": "q", "target": "q", "enrich_from": bad}],
                            "pagination": {"style": "none"}}]}
        errs = validate_spec(ApiSourceSpec.from_dict(d))
        assert any("enrich_from" in e for e in errs), f"{bad!r} should be rejected"


def test_attribute_binding_resolves_from_entity_attrs():
    spec = _attr_binding_spec()
    adapter = RegistrySourceAdapter(spec)
    ep = spec.endpoint()
    # The series id comes from the entity's attribute, NOT the label.
    bindings = adapter._build_bindings(ep, "Roma tomatoes", {"bls_series_id": "APU0000712311"})
    assert bindings == {"series_id": "APU0000712311"}
    # Missing/empty attribute -> no binding -> graceful no-op (chain falls through).
    assert adapter._build_bindings(ep, "Roma tomatoes", {}) == {}
    assert adapter._build_bindings(ep, "Roma tomatoes", {"bls_series_id": ""}) == {}


def test_attribute_binding_flows_through_lookup_context():
    spec = _attr_binding_spec()

    class _Result:
        dormant = False
        error = None
        rows = [{"national_avg_price": "2.489"}]
        sources = ["https://api.x.test/obs?series_id=APU0000712311"]
        provenance: dict = {}

    class _FakeExec:
        def __init__(self):
            self.bindings = None

        async def execute(self, spec_, bindings, **kw):
            self.bindings = bindings
            return _Result()

    fake = _FakeExec()
    adapter = RegistrySourceAdapter(spec, executor=fake)

    async def run():
        ctx = {"entity_type": "ingredient",
               "entity_attributes": {"bls_series_id": "APU0000712311"}}
        verdicts = await adapter.lookup("Roma tomatoes", "national_avg_price", ctx)
        # The executor received the attribute-derived binding.
        assert fake.bindings == {"series_id": "APU0000712311"}
        assert len(verdicts) == 1
        assert verdicts[0].value == "2.489"
        assert verdicts[0].source == "api:fredlike"
        # No entity_attributes in context -> binding empty -> lookup no-ops.
        verdicts2 = await adapter.lookup("Roma tomatoes", "national_avg_price", {"entity_type": "ingredient"})
        assert verdicts2 == []

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# binding_source_attributes: which leaves the executor must pre-load
# --------------------------------------------------------------------------- #
def test_binding_source_attributes_reports_attribute_recipe_leaves():
    adapter = RegistrySourceAdapter(_attr_binding_spec())
    assert adapter.binding_source_attributes == frozenset({"bls_series_id"})


def test_binding_source_attributes_empty_for_label_only_bindings():
    # The nppes seed binds only FROM the entity label (entity_name_first/last),
    # never from another attribute -> no binding-source leaves to pre-load.
    nppes = RegistrySourceAdapter(make_api_source_catalog().get("nppes"))
    assert nppes.binding_source_attributes == frozenset()


# --------------------------------------------------------------------------- #
# Executor: pre-load entity_attributes for the attribute:<attr> recipe
# --------------------------------------------------------------------------- #
class _DummyAdapter:
    """Minimal 4th-arg (wikidata) stand-in for building an EnrichmentExecutor."""

    name = "dummy"

    async def lookup(self, *a, **k):  # pragma: no cover - never consulted here
        return []


def test_load_binding_attrs_parses_leaves_scoped_to_uris():
    async def run():
        e1 = "https://cograph.tech/entities/ingredient/roma_tomatoes"
        bls_uri = "https://cograph.tech/types/ingredient/attrs/bls_series_id"
        captured = {}

        def _query(sparql):
            captured["sparql"] = sparql
            return {"head": {"vars": ["e", "vals"]},
                    "results": {"bindings": [{
                        "e": {"type": "uri", "value": e1},
                        "vals": {"type": "literal", "value": f"{bls_uri}::APU0000712311"}}]}}

        neptune = AsyncMock()
        neptune.query.side_effect = _query
        ex = EnrichmentExecutor(neptune, InMemoryJobStore(), EnrichmentCache(), _DummyAdapter())
        out = await ex._load_binding_attrs(
            "https://omnix.dev/graphs/test", [e1], "ingredient", {"bls_series_id"})
        assert out == {e1: {"bls_series_id": "APU0000712311"}}
        # Scoped to exactly the passed URI + the concrete leaf predicate IRI.
        assert e1 in captured["sparql"]
        assert bls_uri in captured["sparql"]

    asyncio.run(run())


def test_load_binding_attrs_is_graceful_on_query_error():
    async def run():
        neptune = AsyncMock()
        neptune.query.side_effect = RuntimeError("neptune down")
        ex = EnrichmentExecutor(neptune, InMemoryJobStore(), EnrichmentCache(), _DummyAdapter())
        out = await ex._load_binding_attrs(
            "https://omnix.dev/graphs/test",
            ["https://cograph.tech/entities/ingredient/x"], "ingredient", {"bls_series_id"})
        assert out == {}

    asyncio.run(run())


def test_lookup_chain_threads_entity_attributes_into_context():
    async def run():
        class _SpyAdapter:
            def __init__(self):
                self.name = "spy"
                self.seen_ctx = None

            async def lookup(self, entity_label, attribute, context):
                self.seen_ctx = dict(context)
                return [Verdict(value="v", confidence=0.99, source="spy")]

        spy = _SpyAdapter()
        register_adapter(spy)
        ex = EnrichmentExecutor(AsyncMock(), InMemoryJobStore(), EnrichmentCache(), _DummyAdapter())
        job = EnrichJob(
            id="j", tenant_id="t", kg_name="kg", type_name="ingredient",
            attributes=["national_avg_price"], tier=EnrichmentTier.lite,
            status=JobStatus.queued, created_at=datetime.now(timezone.utc),
            conflict_policy=ConflictPolicy.stage, confidence_min=0.85,
        )
        # With bind attrs -> context carries entity_attributes.
        verdicts = await ex._lookup_chain(
            "Roma tomatoes", "national_avg_price", ["spy"], job, set(), 0.85,
            entity_attrs={"bls_series_id": "APU0000712311"})
        assert verdicts and verdicts[0].value == "v"
        assert spy.seen_ctx.get("entity_attributes") == {"bls_series_id": "APU0000712311"}
        # Without bind attrs (different label avoids the verdict cache) -> the key
        # is absent, so the call shape is unchanged for every non-binding adapter.
        spy.seen_ctx = None
        await ex._lookup_chain(
            "Green beans", "national_avg_price", ["spy"], job, set(), 0.85,
            entity_attrs=None)
        assert "entity_attributes" not in spy.seen_ctx

    asyncio.run(run())


def test_attribute_binding_flows_end_to_end_through_executor():
    async def run():
        seen = {}

        def handler(req):
            from urllib.parse import parse_qs, urlparse
            seen.update({k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()})
            return httpx.Response(200, json={"observations": [{"value": "2.489"}]})

        # A FRED-like source_of_truth entry that fills national_avg_price by
        # binding its request param FROM the entity's own bls_series_id attribute.
        spec = _attr_binding_spec()
        adapter = RegistrySourceAdapter(
            spec, executor=RegistryApiSource(transport=httpx.MockTransport(handler)))
        register_adapter(adapter)                       # name == "api:fredlike"
        register_tier(EnrichmentTier.lite, ["api:fredlike"])
        assert get_chain(EnrichmentTier.lite) == ["api:fredlike"]

        entity_uri = "https://cograph.tech/entities/ingredient/roma_tomatoes"
        bls_uri = "https://cograph.tech/types/ingredient/attrs/bls_series_id"

        def _query(sparql):
            if "bls_series_id" in sparql:
                # The separate _load_binding_attrs read: the entity's key attr.
                return {"head": {"vars": ["e", "vals"]},
                        "results": {"bindings": [{
                            "e": {"type": "uri", "value": entity_uri},
                            "vals": {"type": "literal", "value": f"{bls_uri}::APU0000712311"}}]}}
            if "?nameAttr" in sparql:
                # The target-attr entity SELECT: national_avg_price is still EMPTY.
                return {"head": {"vars": ["e", "label", "nameAttr", "vals"]},
                        "results": {"bindings": [{
                            "e": {"type": "uri", "value": entity_uri},
                            "label": {"type": "literal", "value": "Roma tomatoes"},
                            "vals": {"type": "literal", "value": ""}}]}}
            # Ontology / strategy / write-path reads fail-open to empty.
            return {"head": {"vars": []}, "results": {"bindings": []}}

        neptune = AsyncMock()
        neptune.query.side_effect = _query
        neptune.update.return_value = None

        store = InMemoryJobStore()
        job = EnrichJob(
            id="job-fred", tenant_id="test-tenant", kg_name="kg",
            type_name="ingredient", attributes=["national_avg_price"],
            tier=EnrichmentTier.lite, status=JobStatus.queued,
            created_at=datetime.now(timezone.utc),
            conflict_policy=ConflictPolicy.stage, confidence_min=0.85,
        )
        await store.create(job)
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), adapter)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.progress.filled == 1
        # The attribute binding reached the actual HTTP request param — proof the
        # executor pre-loaded entity_attributes and threaded it into lookup().
        assert seen.get("series_id") == "APU0000712311"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# PascalCase entity-type ↔ snake_case coverage-kind matching. Auto-ontology mints
# PascalCase type names ("LineItem"); a registry entry declares snake_case
# coverage kinds ("line_item"). The tokenizer must split camelCase so the two
# reduce to the same word set — otherwise the source is silently skipped for
# exactly those multi-word type names (the demo hit this: a "LineItem" type never
# reached the FRED price source, only single-word "Commodity"/"Ingredient" did).
# --------------------------------------------------------------------------- #
def _priced_item_spec(kinds):
    return ApiSourceSpec.from_dict({
        "slug": "priced", "title": "Priced", "base_url": "https://api.x.test",
        "auth": {"mode": "none"}, "authority_level": "authoritative",
        "coverage": {"entity_kinds": kinds, "attributes": ["national_avg_price"]},
        "endpoints": [{
            "name": "obs", "path": "/obs", "method": "GET",
            "params": [{"name": "series_id", "target": "series_id",
                        "enrich_from": "attribute:bls_series_id"}],
            "result_path": "observations",
            "field_mappings": {"national_avg_price": "value"},
            "pagination": {"style": "none"},
        }],
    })


def test_tokens_split_camelcase():
    from cograph_client.api_registry.enrichment import _tokens
    # PascalCase / acronym-prefixed names split into the same words as snake_case.
    assert _tokens("LineItem") == {"line", "item"}
    assert _tokens("line_item") == {"line", "item"}
    assert _tokens("BLSItem") == {"bls", "item"}
    assert _tokens("FoodItem") == {"food", "item"}
    # Single-word names are unaffected.
    assert _tokens("Ingredient") == {"ingredient"}
    assert _tokens("NPI") == {"npi"}


def test_pascalcase_type_matches_snakecase_coverage_kind():
    adapter = RegistrySourceAdapter(_priced_item_spec(["line_item", "food_item"]))
    # Regression: the old tokenizer left "LineItem" as one token {"lineitem"} and
    # never overlapped "line_item".
    assert adapter._type_matches("LineItem") is True
    assert adapter._type_matches("FoodItem") is True
    assert adapter._type_matches("line_item") is True  # snake_case still matches
    # The generic-stopword guard is intact: a bare "Item" shares only the generic
    # "item" token with "line_item" and must NOT match (no spurious API call).
    assert adapter._type_matches("Item") is False


@pytest.mark.asyncio
async def test_pascalcase_type_fires_lookup_end_to_end():
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        return httpx.Response(200, json={"observations": [{"value": "2.154"}]})

    ex = RegistryApiSource(transport=httpx.MockTransport(handler))
    adapter = RegistrySourceAdapter(_priced_item_spec(["line_item"]), executor=ex)
    v = await adapter.lookup(
        "Roma tomatoes", "national_avg_price",
        {"entity_type": "LineItem", "entity_attributes": {"bls_series_id": "APU0000712311"}},
    )
    assert seen["n"] == 1                    # the PascalCase type reached the API
    assert v and v[0].value == "2.154"
