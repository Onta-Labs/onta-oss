"""Tests for the type matcher."""

import json
from unittest.mock import AsyncMock

import pytest

from cograph_client.resolver.models import MatchVerdict
from cograph_client.resolver.type_matcher import TypeMatcher
from cograph_client.resolver.verdict_cache import JsonVerdictCache, VerdictEntry


@pytest.fixture
def mock_cache(tmp_path):
    return JsonVerdictCache(tmp_path / "test-verdicts.json")


@pytest.fixture
def fake_chat(monkeypatch):
    """Patch the OpenRouter helper the matcher calls; returns the mock so tests
    set its return_value / side_effect and assert call counts."""
    mock = AsyncMock()
    monkeypatch.setattr("cograph_client.resolver.type_matcher.openrouter_chat", mock)
    return mock


@pytest.fixture
def matcher(mock_cache):
    return TypeMatcher("test-openrouter-key", mock_cache)


def _verdict_json(verdict: str, matched_type: str | None, confidence: float) -> str:
    """The raw JSON string the router helper returns for a verdict."""
    return json.dumps({
        "verdict": verdict,
        "matched_type": matched_type,
        "confidence": confidence,
        "reasoning": "test",
    })


@pytest.mark.asyncio
async def test_auto_new_empty_ontology(matcher):
    result = await matcher.match("Property", "A real estate property", {})
    assert result.verdict == MatchVerdict.DIFFERENT
    assert result.is_new is True
    assert result.resolved == "Property"


@pytest.mark.asyncio
async def test_high_confidence_same(matcher, fake_chat):
    fake_chat.return_value = _verdict_json("SAME", "Property", 0.98)
    result = await matcher.match("House", "A house", {"Property": "A real estate property"})
    assert result.verdict == MatchVerdict.SAME
    assert result.resolved == "Property"
    assert result.is_new is False


@pytest.mark.asyncio
async def test_low_confidence_different(matcher, fake_chat):
    fake_chat.return_value = _verdict_json("DIFFERENT", None, 0.3)
    result = await matcher.match("Vehicle", "A car", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.DIFFERENT
    assert result.is_new is True
    assert result.resolved == "Vehicle"


@pytest.mark.asyncio
async def test_high_confidence_subtype(matcher, fake_chat):
    fake_chat.return_value = _verdict_json("SUBTYPE", "Property", 0.97)
    result = await matcher.match("Condo", "A condominium unit", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.SUBTYPE
    assert result.is_new is True
    assert result.parent_type == "Property"


@pytest.mark.asyncio
async def test_cached_verdict_reused(matcher, mock_cache, fake_chat):
    await mock_cache.put(VerdictEntry("House", "Property", MatchVerdict.SAME, 0.97))
    result = await matcher.match("House", "", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.SAME
    assert result.resolved == "Property"
    # LLM should NOT have been called
    fake_chat.assert_not_called()


@pytest.mark.asyncio
async def test_ambiguous_triggers_judges(matcher, fake_chat):
    # First call returns ambiguous match; judges return majority SAME.
    initial = _verdict_json("SAME", "Property", 0.75)
    judge = _verdict_json("SAME", None, 0.8)
    fake_chat.side_effect = [initial, judge, judge, judge]

    result = await matcher.match("Residence", "", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.SAME
    assert fake_chat.call_count == 4  # 1 initial + 3 judges
