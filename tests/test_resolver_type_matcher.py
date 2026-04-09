"""Tests for the type matcher."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnix.resolver.models import MatchVerdict
from omnix.resolver.type_matcher import TypeMatcher
from omnix.resolver.verdict_cache import JsonVerdictCache, VerdictEntry


@pytest.fixture
def mock_cache(tmp_path):
    return JsonVerdictCache(tmp_path / "test-verdicts.json")


@pytest.fixture
def mock_anthropic():
    return AsyncMock()


@pytest.fixture
def matcher(mock_anthropic, mock_cache):
    return TypeMatcher(mock_anthropic, mock_cache)


def _make_llm_response(verdict: str, matched_type: str | None, confidence: float):
    """Helper to create a mock Anthropic response."""
    mock = AsyncMock()
    content_block = MagicMock()
    content_block.text = json.dumps({
        "verdict": verdict,
        "matched_type": matched_type,
        "confidence": confidence,
        "reasoning": "test",
    })
    mock.content = [content_block]
    return mock


@pytest.mark.asyncio
async def test_auto_new_empty_ontology(matcher):
    result = await matcher.match("Property", "A real estate property", {})
    assert result.verdict == MatchVerdict.DIFFERENT
    assert result.is_new is True
    assert result.resolved == "Property"


@pytest.mark.asyncio
async def test_high_confidence_same(matcher, mock_anthropic):
    mock_anthropic.messages.create.return_value = _make_llm_response("SAME", "Property", 0.98)
    result = await matcher.match("House", "A house", {"Property": "A real estate property"})
    assert result.verdict == MatchVerdict.SAME
    assert result.resolved == "Property"
    assert result.is_new is False


@pytest.mark.asyncio
async def test_low_confidence_different(matcher, mock_anthropic):
    mock_anthropic.messages.create.return_value = _make_llm_response("DIFFERENT", None, 0.3)
    result = await matcher.match("Vehicle", "A car", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.DIFFERENT
    assert result.is_new is True
    assert result.resolved == "Vehicle"


@pytest.mark.asyncio
async def test_high_confidence_subtype(matcher, mock_anthropic):
    mock_anthropic.messages.create.return_value = _make_llm_response("SUBTYPE", "Property", 0.97)
    result = await matcher.match("Condo", "A condominium unit", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.SUBTYPE
    assert result.is_new is True
    assert result.parent_type == "Property"


@pytest.mark.asyncio
async def test_cached_verdict_reused(matcher, mock_cache, mock_anthropic):
    await mock_cache.put(VerdictEntry("House", "Property", MatchVerdict.SAME, 0.97))
    result = await matcher.match("House", "", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.SAME
    assert result.resolved == "Property"
    # LLM should NOT have been called
    mock_anthropic.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_ambiguous_triggers_judges(matcher, mock_anthropic):
    # First call returns ambiguous match
    initial = _make_llm_response("SAME", "Property", 0.75)
    # Judge calls return majority SAME
    judge = _make_llm_response("SAME", None, 0.8)
    mock_anthropic.messages.create.side_effect = [initial, judge, judge, judge]

    result = await matcher.match("Residence", "", {"Property": "Real estate"})
    assert result.verdict == MatchVerdict.SAME
    assert mock_anthropic.messages.create.call_count == 4  # 1 initial + 3 judges
