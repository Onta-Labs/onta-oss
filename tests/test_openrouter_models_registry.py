"""OpenRouter models catalog entry — production path for model-list discovery."""

from __future__ import annotations

import pytest

from cograph_client.api_registry.catalog import make_api_source_catalog
from cograph_client.api_registry.discovery import (
    RegistryDiscoverySource,
    _enrich_provider_from_id,
)
from cograph_client.api_registry.executor import RegistryApiSource


def test_openrouter_models_in_seed_catalog():
    cat = make_api_source_catalog()
    spec = cat.get("openrouter_models")
    assert spec is not None
    assert spec.enabled
    assert spec.auth.mode.value == "none" or str(spec.auth.mode) in ("none", "AuthMode.none")
    names = {ep.name for ep in spec.endpoints}
    assert "speech" in names
    assert "list" in names
    speech = next(ep for ep in spec.endpoints if ep.name == "speech")
    assert speech.query.get("output_modalities") == "speech"
    assert speech.field_mappings.get("name") == "id"


def test_enrich_provider_from_id():
    assert _enrich_provider_from_id({"name": "mistralai/voxtral-mini-tts-2603"})[
        "provider"
    ] == "mistralai"
    # never clobber
    assert _enrich_provider_from_id(
        {"name": "mistralai/x", "provider": "kept"}
    )["provider"] == "kept"
    assert "provider" not in _enrich_provider_from_id({"name": "noslash"})


def test_registry_discovery_source_is_structured():
    cat = make_api_source_catalog()
    spec = cat.get("openrouter_models")
    src = RegistryDiscoverySource(spec, endpoint="speech")
    assert getattr(src, "structured", False) is True


@pytest.mark.asyncio
async def test_openrouter_speech_endpoint_live():
    """Live smoke against OpenRouter public API (no key)."""
    cat = make_api_source_catalog()
    spec = cat.get("openrouter_models")
    assert spec is not None
    src = RegistryDiscoverySource(spec, endpoint="speech")
    res = await src.discover(
        "TTS models on OpenRouter",
        sample=False,
        max_rows=50,
        hint_columns=["name", "provider", "modality"],
        context={},
    )
    assert not res.error, res.error
    assert len(res.rows) >= 10, f"expected ≥10 speech models, got {len(res.rows)}"
    names = {r.get("name") for r in res.rows}
    # Gold members from the public speech catalog (2026-07 dogfood)
    assert "mistralai/voxtral-mini-tts-2603" in names
    assert any(n and n.startswith("minimax/") for n in names)
    # provider derived from id
    vox = next(r for r in res.rows if r.get("name") == "mistralai/voxtral-mini-tts-2603")
    assert vox.get("provider") == "mistralai"
    # structured path eligible
    assert src.structured is True
