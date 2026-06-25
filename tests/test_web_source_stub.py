"""Tests for the dev stub web-source provider + its plugin registration."""

from __future__ import annotations

import pytest

from cograph_client.web_sources import (
    get_web_source,
    list_web_sources,
    reset_web_sources,
)
from cograph_client.web_sources.stub import StubWebSource, register


@pytest.fixture(autouse=True)
def _clean():
    reset_web_sources()
    yield
    reset_web_sources()


def test_register_installs_stub():
    register()
    assert "stub" in list_web_sources()
    assert get_web_source("stub") is not None
    # No-name selection returns the sole provider.
    assert get_web_source() is not None


async def test_openrouter_query_returns_catalogue():
    p = StubWebSource()
    res = await p.discover(
        "a list of models offered by OpenRouter",
        sample=False, max_rows=100, hint_columns=None, context={},
    )
    assert len(res.rows) >= 5
    assert {"name", "context_length"} <= set(res.rows[0].keys())
    assert res.sources == ["https://openrouter.ai/models"]
    assert res.estimated_total == len(res.rows)


async def test_sample_truncates():
    p = StubWebSource()
    res = await p.discover(
        "openrouter models", sample=True, max_rows=100, hint_columns=None, context={},
    )
    assert len(res.rows) <= 5
    assert res.is_partial is True  # sample is a slice of the full catalogue


async def test_generic_query_synthesizes_wellformed_rows():
    p = StubWebSource()
    res = await p.discover(
        "interesting things", sample=False, max_rows=100, hint_columns=None, context={},
    )
    assert res.rows
    assert {"name", "description", "url"} <= set(res.rows[0].keys())


def test_stub_is_free():
    from cograph_client.web_sources.base import provider_cost

    is_paid, cost = provider_cost(StubWebSource())
    assert is_paid is False
    assert cost == 0.0
