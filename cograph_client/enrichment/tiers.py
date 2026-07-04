"""Tier -> adapter chain registry.

Each tier is an ordered list of adapter names (e.g. ["cache", "wikidata"]).
At enrichment time, the executor walks the chain in order and stops at the
first verdict whose confidence >= confidence_min.

OSS ships with a `lite` tier (cache + wikidata). cograph (proprietary) calls
`register_tier(EnrichmentTier.base, [...])` etc. at app boot to wire paid
adapters into base/core/pro.
"""
from __future__ import annotations

from typing import Callable

from cograph_client.enrichment.models import EnrichmentTier

# Default OSS chains. cograph overrides via register_tier().
#
# ``auto`` is a META-tier (COG-124): it is resolved to a concrete tier
# (``lite``/``core``) by the tier router BEFORE a job is created, so a real chain
# is never walked for ``auto``. It is mapped to the free ``lite`` chain only as a
# defensive fallback so ``get_chain(auto)`` can never KeyError.
_DEFAULT_CHAINS: dict[EnrichmentTier, list[str]] = {
    EnrichmentTier.auto: ["wikidata"],
    EnrichmentTier.lite: ["wikidata"],
    EnrichmentTier.base: ["wikidata"],
    EnrichmentTier.core: ["wikidata"],
    EnrichmentTier.pro: ["wikidata"],
}

_chains: dict[EnrichmentTier, list[str]] = dict(_DEFAULT_CHAINS)

# Chain-prefix providers (ONTA-194 phase 3): callables that return adapter names
# that must ALWAYS LEAD every tier's chain, ahead of the tier's configured
# adapters — the mechanism by which authoritative sources (the API source
# registry's source-of-truth entries) outrank wikidata / web adapters via the
# executor's first-sufficient-verdict short-circuit. Recomputed on every
# get_chain() call so it is immune to registration order (e.g. it survives a
# later register_tier() override by the proprietary plugin). Generic: any package
# can register a lead prefix; tiers.py stays decoupled from what it leads with.
_chain_prefix_providers: list[Callable[[EnrichmentTier], list[str]]] = []


def register_chain_prefix_provider(fn: Callable[[EnrichmentTier], list[str]]) -> None:
    """Register a provider of adapter names that lead every chain. Idempotent
    per function object (registering the same callable twice is a no-op)."""
    if fn not in _chain_prefix_providers:
        _chain_prefix_providers.append(fn)


def reset_chain_prefix_providers() -> None:
    """Drop all chain-prefix providers. For tests."""
    _chain_prefix_providers.clear()


def _chain_prefix(tier: EnrichmentTier) -> list[str]:
    prefix: list[str] = []
    for fn in _chain_prefix_providers:
        try:
            names = fn(tier) or []
        except Exception:  # noqa: BLE001 - a bad provider must not break enrichment
            names = []
        for name in names:
            if name and name not in prefix:
                prefix.append(name)
    return prefix


def register_tier(tier: EnrichmentTier, adapter_chain: list[str]) -> None:
    """Override the adapter chain for a tier. Called by the cograph plugin
    at app boot. Idempotent -- last write wins.
    """
    _chains[tier] = list(adapter_chain)


def get_chain(tier: EnrichmentTier) -> list[str]:
    """Return the configured adapter chain for a tier, with any registered
    lead-prefix adapters (e.g. registry source-of-truth entries) prepended."""
    chain = list(_chains.get(tier, _DEFAULT_CHAINS[tier]))
    prefix = _chain_prefix(tier)
    if not prefix:
        return chain
    return [*prefix, *[c for c in chain if c not in prefix]]


def reset_tiers() -> None:
    """Restore defaults. For tests.

    Also drops chain-prefix providers so ``get_chain`` returns the pristine
    tier chain (a leading-prefix provider would otherwise perturb it).
    """
    _chains.clear()
    _chains.update(_DEFAULT_CHAINS)
    reset_chain_prefix_providers()
