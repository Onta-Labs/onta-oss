"""Tests for the verdict cache."""

import json
from pathlib import Path

import pytest

from cograph_client.resolver.models import MatchVerdict
from cograph_client.resolver.verdict_cache import JsonVerdictCache, VerdictEntry


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "verdicts.json"


@pytest.fixture
def cache(cache_path):
    return JsonVerdictCache(cache_path)


@pytest.mark.asyncio
async def test_put_and_get(cache):
    entry = VerdictEntry("House", "Property", MatchVerdict.SAME, 0.97)
    await cache.put(entry)
    result = await cache.get("House", "Property")
    assert result is not None
    assert result.verdict == MatchVerdict.SAME
    assert result.confidence == 0.97


@pytest.mark.asyncio
async def test_get_missing(cache):
    result = await cache.get("House", "Property")
    assert result is None


@pytest.mark.asyncio
async def test_case_insensitive_key(cache):
    entry = VerdictEntry("House", "Property", MatchVerdict.SAME, 0.97)
    await cache.put(entry)
    result = await cache.get("house", "property")
    assert result is not None


@pytest.mark.asyncio
async def test_get_all_for_proposed(cache):
    await cache.put(VerdictEntry("House", "Property", MatchVerdict.DIFFERENT, 0.3))
    await cache.put(VerdictEntry("House", "Building", MatchVerdict.SAME, 0.96))
    results = await cache.get_all_for_proposed("House")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_persistence(cache_path):
    cache1 = JsonVerdictCache(cache_path)
    await cache1.put(VerdictEntry("A", "B", MatchVerdict.DIFFERENT, 0.4))

    cache2 = JsonVerdictCache(cache_path)
    result = await cache2.get("A", "B")
    assert result is not None
    assert result.verdict == MatchVerdict.DIFFERENT


@pytest.mark.asyncio
async def test_corrupt_file(cache_path):
    cache_path.write_text("not json")
    cache = JsonVerdictCache(cache_path)
    result = await cache.get("A", "B")
    assert result is None
