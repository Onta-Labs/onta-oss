"""Tier -> adapter chain registry.

Each tier is an ordered list of adapter names (e.g. ["cache", "wikidata"]).
At enrichment time, the executor walks the chain in order and stops at the
first verdict whose confidence >= confidence_min.

OSS ships with a `lite` tier (cache + wikidata). cograph (proprietary) calls
`register_tier(EnrichmentTier.base, [...])` etc. at app boot to wire paid
adapters into base/core/pro.
"""
from __future__ import annotations

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


def register_tier(tier: EnrichmentTier, adapter_chain: list[str]) -> None:
    """Override the adapter chain for a tier. Called by the cograph plugin
    at app boot. Idempotent -- last write wins.
    """
    _chains[tier] = list(adapter_chain)


def get_chain(tier: EnrichmentTier) -> list[str]:
    """Return the configured adapter chain for a tier."""
    return list(_chains.get(tier, _DEFAULT_CHAINS[tier]))


def reset_tiers() -> None:
    """Restore defaults. For tests."""
    _chains.clear()
    _chains.update(_DEFAULT_CHAINS)
