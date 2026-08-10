"""Structural source_constraint derivation (ONTA-459) — no brand denylists."""

from __future__ import annotations

from types import SimpleNamespace

from infona_client.pipeline.source_scope import (
    REGISTRY_NONE,
    derive_source_constraint,
    has_named_source_signal,
    merge_provider_context,
    provider_identity_tokens,
)
from infona_client.web_sources.base import provider_accepts


def _catalog_prov(
    *,
    slug: str,
    hosts: frozenset[str],
    title: str = "",
    name: str | None = None,
):
    return SimpleNamespace(
        registry_slug=slug,
        served_hosts=hosts,
        title=title or slug,
        name=name or f"api:{slug}",
        accepts=None,  # not used by derive
    )


def _openrouter():
    return _catalog_prov(
        slug="openrouter_models",
        hosts=frozenset({"openrouter.ai"}),
        title="OpenRouter Models",
    )


def _nppes():
    return _catalog_prov(
        slug="nppes",
        hosts=frozenset({"npiregistry.cms.hhs.gov"}),
        title="NPPES NPI Registry",
    )


def _general_web():
    """locate_scrape-like: no registry metadata → unconstrained."""
    return SimpleNamespace(name="locate_scrape", title="Web")


# --------------------------------------------------------------------------- #
# Signals & tokens
# --------------------------------------------------------------------------- #


def test_has_named_source_signal_structural():
    assert has_named_source_signal("List of TTS models offered by Acme") is True
    assert has_named_source_signal("available on ExampleHost") is True
    assert has_named_source_signal("List of TTS models") is False
    assert has_named_source_signal("top packages by downloads") is False  # "by" alone
    # Weak prepositions are NOT strong signals (no exclusive-none):
    assert has_named_source_signal("models from 2024") is False
    assert has_named_source_signal("physicians at Mayo Clinic") is False


def test_provider_identity_tokens_from_self_metadata_only():
    p = _openrouter()
    toks = provider_identity_tokens(p)
    assert "openrouter" in toks
    # stopwords / too-short discarded
    assert "models" not in toks or "openrouter" in toks
    assert "ai" not in toks
    assert "com" not in toks


def test_provider_identity_tokens_nppes():
    toks = provider_identity_tokens(_nppes())
    assert "nppes" in toks
    assert "npiregistry" in toks


# --------------------------------------------------------------------------- #
# derive_source_constraint
# --------------------------------------------------------------------------- #


def test_no_signal_unconstrained():
    sc = derive_source_constraint(
        "List of text-to-speech models",
        [_openrouter(), _general_web()],
    )
    assert sc == {}


def test_signal_matches_registry_via_own_tokens():
    sc = derive_source_constraint(
        "List of TTS models offered by OpenRouter",
        [_openrouter(), _nppes(), _general_web()],
    )
    assert sc.get("registry_ids") == ["openrouter_models"]
    assert "openrouter.ai" in (sc.get("hosts") or [])


def test_signal_foreign_source_excludes_all_registry():
    """Named source not in ensemble metadata → exclusive none (catalog APIs skip)."""
    sc = derive_source_constraint(
        "List of TTS models offered by Vapi",
        [_openrouter(), _general_web()],
    )
    assert sc == {"registry_ids": [REGISTRY_NONE]}


def test_signal_matches_via_host_label_only():
    # Sub-query uses host-like token that is the provider's first DNS label.
    sc = derive_source_constraint(
        "models available on openrouter catalog",
        [_openrouter()],
    )
    assert "openrouter_models" in (sc.get("registry_ids") or [])


def test_weak_preposition_unmatched_stays_unconstrained():
    """F1: 'from 2024' / 'at Mayo' must NOT exclusive-none all catalogs."""
    sc = derive_source_constraint(
        "top LLM models from 2024",
        [_openrouter(), _nppes()],
    )
    assert sc == {}
    sc2 = derive_source_constraint(
        "physicians at Mayo Clinic",
        [_openrouter(), _nppes()],
    )
    assert sc2 == {}


def test_weak_preposition_positive_match_still_binds():
    """Weak 'from OpenRouter' can still positive-match via host/slug tokens."""
    sc = derive_source_constraint(
        "models from OpenRouter",
        [_openrouter(), _nppes()],
    )
    assert sc.get("registry_ids") == ["openrouter_models"]


def test_multi_catalog_match_when_both_mentioned():
    sc = derive_source_constraint(
        "providers offered by OpenRouter and NPPES",
        [_openrouter(), _nppes()],
    )
    ids = set(sc.get("registry_ids") or [])
    assert ids == {"openrouter_models", "nppes"}


def test_short_slug_parts_do_not_false_bind():
    """F2: open_food_facts-shaped slug must not bind on bare 'open'."""
    food = _catalog_prov(
        slug="open_food_facts_search",
        hosts=frozenset({"world.openfoodfacts.org"}),
        title="Open Food Facts Search",
    )
    sc = derive_source_constraint(
        "models offered by SomeForeignPlatform with open weights",
        [food, _openrouter()],
    )
    # Strong foreign source, no token match → exclusive none (not food catalog)
    assert sc == {"registry_ids": [REGISTRY_NONE]}

# --------------------------------------------------------------------------- #
# accepts + merge
# --------------------------------------------------------------------------- #


def test_registry_none_forces_accepts_false():
    from infona_client.api_registry import (
        RegistryDiscoverySource,
        make_api_source_catalog,
    )

    cat = make_api_source_catalog()
    src = RegistryDiscoverySource(cat.get("openrouter_models"))
    assert src.accepts("q", {"source_constraint": {"registry_ids": [REGISTRY_NONE]}}) is False
    assert provider_accepts(
        src, "q", {"source_constraint": {"registry_ids": [REGISTRY_NONE]}}
    ) is False


def test_merge_provider_context_adds_constraint():
    base = {"tenant_id": "t", "kg_name": "k"}
    ctx = merge_provider_context(
        base,
        "List of models offered by OpenRouter",
        [_openrouter(), _general_web()],
    )
    assert ctx["tenant_id"] == "t"
    assert ctx["source_constraint"]["registry_ids"] == ["openrouter_models"]


def test_merge_provider_context_respects_explicit_constraint():
    base = {"source_constraint": {"registry_ids": ["nppes"]}}
    ctx = merge_provider_context(
        base,
        "List of models offered by OpenRouter",
        [_openrouter()],
    )
    # Explicit wins — do not overwrite.
    assert ctx["source_constraint"] == {"registry_ids": ["nppes"]}


def test_ensemble_openrouter_skips_foreign_named_source():
    """Incident class: OpenRouter catalog must not run for a foreign 'offered by'."""
    from infona_client.api_registry import (
        RegistryDiscoverySource,
        make_api_source_catalog,
    )

    cat = make_api_source_catalog()
    or_src = RegistryDiscoverySource(cat.get("openrouter_models"))
    ensemble = [or_src, _general_web()]
    ctx = merge_provider_context({}, "List of TTS models offered by Vapi", ensemble)
    assert provider_accepts(or_src, "List of TTS models offered by Vapi", ctx) is False
    # General web has no accepts → True
    assert provider_accepts(
        _general_web(), "List of TTS models offered by Vapi", ctx
    ) is True


def test_ensemble_openrouter_runs_for_own_named_source():
    from infona_client.api_registry import (
        RegistryDiscoverySource,
        make_api_source_catalog,
    )

    cat = make_api_source_catalog()
    or_src = RegistryDiscoverySource(cat.get("openrouter_models"))
    ensemble = [or_src, _general_web()]
    q = "List of TTS models offered by OpenRouter"
    ctx = merge_provider_context({}, q, ensemble)
    assert provider_accepts(or_src, q, ctx) is True


def test_no_incident_brand_literals_in_source_scope_module():
    """Prod module must not hardcode incident brand/platform string literals."""
    import pathlib
    import re

    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "infona_client"
        / "pipeline"
        / "source_scope.py"
    ).read_text(encoding="utf-8")
    for banned in ("ElevenLabs", "PlayHT", "Cartesia", "Vapi", "vapi"):
        assert banned not in src, f"banned brand {banned!r} in source_scope.py"


def test_spec_prompt_does_not_hardcode_model_llm_core_chips():
    """WS4: _SPEC_SYSTEM must not force Model core chips to LLM-only attrs."""
    from infona_client.agent.capabilities import web_ingest_cap as cap

    prompt = cap._SPEC_SYSTEM
    # Old overfit example removed
    assert '["provider","context_length","input_price"]' not in prompt
    assert "For Model:" not in prompt or "context_length" not in prompt.split("For Model:")[-1][:80]
    assert "do NOT" in prompt or "do not" in prompt.lower()
    assert "TTS" in prompt or "speech" in prompt or "modality" in prompt