"""Tier-router tests — the registry-first ``auto`` resolution (ONTA-360) and
the no-key heuristic fallback.

The registry-first branch is deterministic and depends only on the seed
catalog, so these run offline: a covered attribute+type resolves to ``base``
with no LLM call at all; an uncovered pair falls through to the keyless
heuristic (never ``needs_clarification``).
"""

from __future__ import annotations

import pytest

from cograph_client.enrichment.tier_router import (
    _registry_covers,
    resolve_auto_tier,
)


def test_registry_covers_fred_pair_for_domain_and_generic_types():
    # The shipped FRED seeds cover the two-stage price rail: series-id
    # resolution then price fill — for domain type names AND the all-generic
    # minted "Item" (both seeds opt in via the bare "item" coverage kind).
    for type_name in ("Commodity", "LineItem", "Item", "ingredient"):
        assert _registry_covers(["bls_series_id"], type_name) == "fred_series_search"
        assert _registry_covers(["national_avg_price"], type_name) == "fred"


def test_registry_covers_misses_uncovered_type_and_attribute():
    assert _registry_covers(["bls_series_id"], "Widget") is None
    assert _registry_covers(["favorite_color"], "Commodity") is None
    assert _registry_covers([], "Commodity") is None


@pytest.mark.asyncio
async def test_auto_tier_resolves_base_when_registry_covers_without_llm():
    # No openrouter key is passed: reaching "base" proves the registry branch
    # short-circuited BEFORE any LLM classify (which would otherwise be
    # skipped straight to the heuristic).
    d = await resolve_auto_tier(["bls_series_id"], "Item", None)
    assert d.resolved_tier == "base"
    assert d.needs_clarification is False
    assert "fred_series_search" in d.routing_note


@pytest.mark.asyncio
async def test_auto_tier_uncovered_falls_through_to_heuristic():
    d = await resolve_auto_tier(["favorite_color"], "Widget", None)
    assert d.resolved_tier in ("lite", "core")
    assert d.needs_clarification is False  # the heuristic always lands


@pytest.mark.asyncio
async def test_registry_probe_failure_never_breaks_routing(monkeypatch):
    # If the catalog probe blows up, auto-tier must fall through, not raise.
    import cograph_client.api_registry.catalog as catalog

    def boom():
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(catalog, "get_api_source_catalog", boom)
    d = await resolve_auto_tier(["bls_series_id"], "Commodity", None)
    assert d.resolved_tier in ("lite", "core")
