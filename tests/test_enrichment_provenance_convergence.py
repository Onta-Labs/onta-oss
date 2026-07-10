"""ONTA-245 — per-attribute provenance convergence + refresh-existing.

MECHANISM tests on INVENTED types/attrs (Widget/sku/color, Gadget/material/weight_kg,
Sprocket/diameter_mm/finish) across ≥2 unrelated domains — no persona token. Proves:
  * enrichment writes a PER-ATTRIBUTE `<attr>_source_url` (per-attribute, not per-record);
  * two attributes from two sources carry INDEPENDENT provenance;
  * companions are dated from the VERDICT's real source date, not write time (F1);
  * the canonical companion-provenance GRAPH is fed via the shared insert_facts seam;
  * a `verified` re-confirm ADVANCES the freshness stamp without rewriting the value (F2);
  * a REFRESH intent routes to the `verify` policy; a scoped refresh touches only the
    subset and mints nothing (F3);
  * discovery emits the IDENTICAL per-attribute companions end-to-end (cross-rail F1).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import EnrichmentExecutor, _attr_uri, _now
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
    Verdict,
)
from cograph_client.graph.provenance import provenance_graph_uri

from tests._enrichment_prov_helpers import (
    DOMAINS,
    XSD_DATETIME,
    FakeWikidata,
    all_updates,
    entities_query_response,
    make_job,
    query_router,
)


@pytest.mark.parametrize("type_name,attr,label,value,src", DOMAINS)
def test_enrichment_writes_per_attribute_source_url(type_name, attr, label, value, src, monkeypatch):
    """Enriching one attribute lands its OWN `<attr>_source_url` display companion on
    the entity (per-attribute, not per-record). Two invented domains."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [{"uri": f"https://cograph.tech/entities/{type_name}/e1", "label": label, "vals": ""}]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({(label, attr): [
                Verdict(value=value, confidence=0.95, source="wikidata", source_url=src)
            ]}),
        )
        job = make_job(type_name=type_name, attributes=[attr], policy=ConflictPolicy.overwrite)
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        writes = all_updates(neptune)
        assert _attr_uri(type_name, f"{attr}_source_url") in writes
        assert src in writes

    asyncio.run(run())


def test_two_attributes_carry_independent_sources(monkeypatch):
    """Enriching TWO attributes on the same entity from DIFFERENT sources gives each
    its own independent `<attr>_source_url` — per-attribute provenance."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [{"uri": "https://cograph.tech/entities/Widget/e1", "label": "Alpha Widget", "vals": ""}]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({
                ("Alpha Widget", "sku"): [
                    Verdict(value="WX-1", confidence=0.95, source="wikidata",
                            source_url="https://a.example/sku")
                ],
                ("Alpha Widget", "color"): [
                    Verdict(value="red", confidence=0.95, source="wikidata",
                            source_url="https://b.example/color")
                ],
            }),
        )
        job = make_job(type_name="Widget", attributes=["sku", "color"],
                       policy=ConflictPolicy.overwrite)
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        writes = all_updates(neptune)
        assert "https://a.example/sku" in writes
        assert "https://b.example/color" in writes
        assert _attr_uri("Widget", "sku_source_url") in writes
        assert _attr_uri("Widget", "color_source_url") in writes

    asyncio.run(run())


def test_companions_dated_from_verdict_not_write_time(monkeypatch):
    """The `<attr>_verified_at` companion is dated from the VERDICT's real source date
    (source_published_at), NOT the write time — so provenance shows when the source
    knew the fact (F1)."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)
    published = datetime(2021, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    async def run():
        rows = [{"uri": "https://cograph.tech/entities/Gadget/e1", "label": "Beta Gadget", "vals": ""}]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({("Beta Gadget", "material"): [
                Verdict(value="titanium", confidence=0.9, source="web",
                        source_url="https://specs.example/beta", source_published_at=published)
            ]}),
        )
        job = make_job(type_name="Gadget", attributes=["material"], policy=ConflictPolicy.overwrite)
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        writes = all_updates(neptune)
        assert "2021-06-01T12:00:00" in writes
        assert _now().year != published.year  # sanity: they really differ

    asyncio.run(run())


def test_canonical_provenance_graph_gets_confidence_and_source_date(monkeypatch):
    """With COGRAPH_PROVENANCE_ENABLED on, enrichment feeds the CANONICAL companion
    provenance GRAPH via the shared insert_facts(provenance_triples=…) seam — each
    record keyed with prov:confidence + a source-dated prov:timestamp (F1)."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)
    monkeypatch.setenv("COGRAPH_PROVENANCE_ENABLED", "1")
    published = datetime(2020, 3, 3, tzinfo=timezone.utc)

    async def run():
        graph = "https://cograph.tech/graphs/test-tenant/kg/kg"
        prov_graph = provenance_graph_uri(graph)
        rows = [{"uri": "https://cograph.tech/entities/Widget/e1", "label": "Alpha Widget", "vals": ""}]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({("Alpha Widget", "sku"): [
                Verdict(value="WX-9", confidence=0.9, source="web",
                        source_url="https://a.example/sku", source_published_at=published)
            ]}),
        )
        job = make_job(type_name="Widget", attributes=["sku"], policy=ConflictPolicy.overwrite)
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        prov_writes = [
            (c.args[0] if c.args else "") for c in neptune.update.await_args_list
            if prov_graph in (c.args[0] if c.args else "")
        ]
        assert prov_writes, "canonical provenance graph write missing"
        blob = " ".join(prov_writes)
        assert "0.9" in blob and "#float" in blob  # prov:confidence
        assert "2020-03-03" in blob  # source-dated prov:timestamp, not write time

    asyncio.run(run())


@pytest.mark.parametrize("policy", [ConflictPolicy.verify, ConflictPolicy.stage])
def test_verified_row_advances_freshness_without_rewriting_value(policy, monkeypatch):
    """F2: a re-verify (source re-confirms the existing value → `verified`) under
    verify/stage RE-STAMPS the freshness companion WITHOUT writing a duplicate
    primary value triple. Clock advances; value untouched."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        sku_pred = _attr_uri("Widget", "sku")
        rows = [{
            "uri": "https://cograph.tech/entities/Widget/e1",
            "label": "Alpha Widget",
            "vals": f"{sku_pred}::WX-1000",  # already has this value
        }]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({("Alpha Widget", "sku"): [
                Verdict(value="WX-1000", confidence=0.95, source="wikidata",
                        source_url="https://a.example/sku")
            ]}),
        )
        job = make_job(type_name="Widget", attributes=["sku"], policy=policy)
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        final = await executor._jobs.get(job.id)
        assert final.progress.verified == 1  # it was a re-verify
        writes = all_updates(neptune)
        # The freshness stamp WAS (re)written as a typed dateTime...
        assert _attr_uri("Widget", "sku_verified_at") in writes
        assert XSD_DATETIME in writes
        # ...but no NEW primary value triple was written (the value is unchanged).
        # The only occurrence of the primary predicate is inside the SELECT echoed
        # back by the fake; there is no INSERT of `<sku> "WX-1000"`.
        assert 'INSERT DATA' in writes  # a write did happen (the re-stamp)
        insert_blob = " ".join(
            (c.args[0] if c.args else "")
            for c in neptune.update.await_args_list
            if "INSERT DATA" in (c.args[0] if c.args else "")
        )
        assert f"<{sku_pred}>" not in insert_blob, "primary value must NOT be re-inserted"
        assert "_verified_at" in insert_blob

    asyncio.run(run())


def test_agent_refresh_intent_routes_to_verify_policy():
    """F3: the enrich capability routes a REFRESH intent to the `verify` conflict
    policy (advancing the clock on existing values) rather than the default `stage`
    — generic verb detection, no persona field."""
    from cograph_client.agent.capabilities.enrich_cap import (
        _default_conflict_policy,
        _looks_like_refresh,
        _refresh_conflict_policy,
    )

    for phrase in [
        "refresh the sku for all widgets",
        "re-verify the weight_kg on gadgets",
        "re-check the material values",
        "update the freshness of these records",
    ]:
        assert _looks_like_refresh(phrase), phrase
    for phrase in [
        "enrich the sku for all widgets",
        "fill in the missing color",
        "discover new gadgets from the web",
    ]:
        assert not _looks_like_refresh(phrase), phrase

    assert _refresh_conflict_policy() == ConflictPolicy.verify
    assert _default_conflict_policy() == ConflictPolicy.stage


def test_agent_replace_intent_routes_to_overwrite_policy():
    """pf10 sp-refresh-pricing: an EXPLICIT replace / update-to-current intent routes
    refresh to the `overwrite` conflict policy (REPLACE the changed value), while a
    plain refresh / re-verify STAYS `verify` (ONTA-245 default preserved) and a bare
    enrich stays `stage`. Conservative — a false-positive overwrite destroys data."""
    from cograph_client.agent.capabilities.enrich_cap import (
        _default_conflict_policy,
        _looks_like_overwrite,
        _looks_like_refresh,
        _overwrite_conflict_policy,
        _refresh_conflict_policy,
    )

    # Explicit REPLACE intent → overwrite.
    for phrase in [
        "refresh pricing so every number is current",
        "replace the stale prices with the latest",
        "update the prices to current",
        "make them current",
        "keep the address current",
        "overwrite the existing values",
        "correct the values",
        "fix outdated numbers",
        "refresh per-minute pricing and vendor-reported latency so every number "
        "is current and sourced",
    ]:
        assert _looks_like_overwrite(phrase), phrase

    # Plain refresh / re-verify / re-check / re-confirm — must STAY verify (NOT
    # overwrite), plus a few non-refresh negatives so the detector isn't over-eager.
    for phrase in [
        "re-verify the affiliations",
        "refresh the pricing",
        "re-check the numbers",
        "re-check the material values",  # "values" alone must not trigger replace
        "re-confirm the addresses",
        "update the freshness of these records",
        "refresh the current pricing",  # "current" as an adjective, not predicate
        "enrich the sku for all widgets",
        "fill in the missing color",
        "discover new gadgets from the web",
    ]:
        assert not _looks_like_overwrite(phrase), phrase

    # A plain refresh verb still routes to the ENRICH rail (refresh detection is
    # unchanged) — only the POLICY differs.
    assert _looks_like_refresh("refresh the pricing")
    assert _looks_like_refresh("re-verify the affiliations")

    assert _overwrite_conflict_policy() == ConflictPolicy.overwrite
    assert _refresh_conflict_policy() == ConflictPolicy.verify
    assert _default_conflict_policy() == ConflictPolicy.stage


def test_scoped_refresh_processes_subset_without_discovery(monkeypatch):
    """A scoped refresh (entity_uris subset + verify policy) processes ONLY the named
    subset and mints NO new entities — re-verify-existing, not re-discover."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        wk_pred = _attr_uri("Gadget", "weight_kg")
        rows = [{
            "uri": "https://cograph.tech/entities/Gadget/g1",
            "label": "Beta Gadget",
            "vals": f"{wk_pred}::3.2",
        }]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({("Beta Gadget", "weight_kg"): [
                Verdict(value="3.2", confidence=0.95, source="wikidata")
            ]}),
        )
        job = EnrichJob(
            id="job-refresh", tenant_id="test-tenant", kg_name="kg", type_name="Gadget",
            attributes=["weight_kg"], tier=EnrichmentTier.lite, status=JobStatus.queued,
            created_at=datetime.now(timezone.utc), conflict_policy=ConflictPolicy.verify,
            entity_uris=["https://cograph.tech/entities/Gadget/g1"],
        )
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        final = await executor._jobs.get(job.id)
        assert final.progress.total == 1
        assert final.progress.processed == 1
        writes = all_updates(neptune)
        assert "entities/Gadget/g2" not in writes  # nothing new minted (no discovery)

    asyncio.run(run())


async def test_discovery_writes_companions_end_to_end(tmp_path, monkeypatch):
    """End-to-end through the REAL resolver write path: with the attribute-provenance
    rail on, discovering an entity with ≥2 primitive attributes from a URL source
    emits, PER ATTRIBUTE, its own `<attr>_source_url` + typed `<attr>_verified_at`
    companion — collected into the SAME batch that flows through insert_facts. A
    discovered fact is now provenance-symmetric with an enriched one (F1 cross-rail)."""
    from cograph_client.resolver.attribute_resolver import AttributeSchema
    from cograph_client.resolver.models import (
        ExtractedAttribute,
        ExtractedEntity,
        IngestResult,
    )
    from cograph_client.resolver.schema_resolver import SchemaResolver
    from cograph_client.resolver.verdict_cache import JsonVerdictCache

    monkeypatch.setenv("COGRAPH_DISCOVERY_ATTR_PROVENANCE", "1")
    resolver = SchemaResolver(AsyncMock(), "fake-key", JsonVerdictCache(tmp_path / "c.json"))
    collected: list[tuple[str, str, str]] = []
    sprocket_uri = "https://cograph.tech/entities/Sprocket/s1"
    await resolver._resolve_and_insert_entity(
        entity=ExtractedEntity(
            type_name="Sprocket", id="s1",
            attributes=[
                ExtractedAttribute(name="diameter_mm", value="42", datatype="integer"),
                ExtractedAttribute(name="finish", value="anodized", datatype="string"),
            ],
        ),
        resolved_type="Sprocket",
        entity_uri=sprocket_uri,
        is_duplicate=False,
        graph_uri="https://cograph.tech/graphs/test-tenant/kg/kg",
        existing_types={"Sprocket": ""},
        existing_attrs={
            "Sprocket": {
                "diameter_mm": AttributeSchema("diameter_mm", "integer"),
                "finish": AttributeSchema("finish", "string"),
            },
        },
        source="https://catalog.example/sprocket/s1",
        result=IngestResult(),
        _collect_triples=collected,
    )

    preds = {p for _s, p, _o in collected}
    for attr in ("diameter_mm", "finish"):
        assert _attr_uri("Sprocket", f"{attr}_source_url") in preds, attr
        assert _attr_uri("Sprocket", f"{attr}_verified_at") in preds, attr
    assert (sprocket_uri, _attr_uri("Sprocket", "diameter_mm_source_url"),
            "https://catalog.example/sprocket/s1") in collected
    stamp = next(o for _s, p, o in collected if p == _attr_uri("Sprocket", "finish_verified_at"))
    assert stamp.endswith(f"^^{XSD_DATETIME}")


async def test_discovery_companions_off_by_default(tmp_path, monkeypatch):
    """The discovery attribute-provenance rail is OFF by default so bulk CSV ingest
    stays byte-stable — no companions unless the flag is set."""
    from cograph_client.resolver.attribute_resolver import AttributeSchema
    from cograph_client.resolver.models import (
        ExtractedAttribute,
        ExtractedEntity,
        IngestResult,
    )
    from cograph_client.resolver.schema_resolver import SchemaResolver
    from cograph_client.resolver.verdict_cache import JsonVerdictCache

    monkeypatch.delenv("COGRAPH_DISCOVERY_ATTR_PROVENANCE", raising=False)
    resolver = SchemaResolver(AsyncMock(), "fake-key", JsonVerdictCache(tmp_path / "c.json"))
    collected: list[tuple[str, str, str]] = []
    await resolver._resolve_and_insert_entity(
        entity=ExtractedEntity(
            type_name="Sprocket", id="s1",
            attributes=[ExtractedAttribute(name="finish", value="anodized", datatype="string")],
        ),
        resolved_type="Sprocket",
        entity_uri="https://cograph.tech/entities/Sprocket/s1",
        is_duplicate=False,
        graph_uri="https://cograph.tech/graphs/test-tenant/kg/kg",
        existing_types={"Sprocket": ""},
        existing_attrs={"Sprocket": {"finish": AttributeSchema("finish", "string")}},
        source="https://catalog.example/sprocket/s1",
        result=IngestResult(),
        _collect_triples=collected,
    )
    preds = {p for _s, p, _o in collected}
    assert _attr_uri("Sprocket", "finish_verified_at") not in preds
    assert _attr_uri("Sprocket", "finish_source_url") not in preds
