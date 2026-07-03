"""Tests for the dev stub web-source provider + its plugin registration.

Also covers the generic query-KIND routing seam (ONTA-190): a provider can declare
``query_kinds`` and be reached by :func:`get_web_source_for_kind` WITHOUT becoming
the general no-name default. All generic — no concrete provider name appears here.
"""

from __future__ import annotations

import pytest

from cograph_client.web_sources import (
    DiscoverResult,
    get_web_source,
    get_web_source_for_kind,
    has_kind_specialized_provider,
    list_web_sources,
    register_web_source,
    reset_web_sources,
)
from cograph_client.web_sources.stub import StubWebSource, register


@pytest.fixture(autouse=True)
def _clean():
    reset_web_sources()
    yield
    reset_web_sources()


class _KindProvider:
    """A minimal provider declaring a generic query kind (no real network)."""

    def __init__(self, name: str, kinds: frozenset[str], *, url_only: bool = False):
        self.name = name
        self.query_kinds = kinds
        if url_only:
            self.url_only = True

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        return DiscoverResult(rows=[{"name": query}])


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


# --- query-kind routing (ONTA-190) ------------------------------------------


def test_get_web_source_for_kind_returns_specialized_provider():
    """A provider declaring a kind in query_kinds is returned for that kind, and
    None for a kind it does not declare."""
    register_web_source(_KindProvider("place_src", frozenset({"place"})))
    assert get_web_source_for_kind("place").name == "place_src"
    assert get_web_source_for_kind("event") is None
    assert get_web_source_for_kind("") is None


def test_kind_provider_is_not_the_general_default():
    """A kind-specialized provider registered ALONGSIDE a general query provider
    does NOT hijack the no-name default: get_web_source() still returns the general
    provider, while the specialized one is reachable by kind (and by name)."""
    register_web_source(StubWebSource())  # general provider (no query_kinds)
    register_web_source(_KindProvider("place_src", frozenset({"place"})))

    # No-name default → the general provider, NOT the place-specialized one.
    assert get_web_source().name == "stub"
    # The specialized provider is still reachable by kind and by name.
    assert get_web_source_for_kind("place").name == "place_src"
    assert get_web_source("place_src").name == "place_src"


def test_kind_provider_alone_is_not_a_default_query_provider():
    """With ONLY a kind-specialized provider registered, the no-name query default
    stays dormant for a general query (it is not counted as the single default) —
    but it is still reachable by its kind. Guards the ONTA-190 rule that a place
    source never becomes the general default and never answers non-place queries."""
    register_web_source(_KindProvider("place_src", frozenset({"place"})))
    # Not selected as the general default (would answer non-place queries).
    assert get_web_source() is None
    # But routable by kind.
    assert get_web_source_for_kind("place").name == "place_src"


def test_get_web_source_for_kind_skips_url_only_provider():
    """Kind routing is for query discovery: a url_only provider is skipped even if
    it (oddly) declared a kind."""
    register_web_source(
        _KindProvider("weird", frozenset({"place"}), url_only=True)
    )
    assert get_web_source_for_kind("place") is None


def test_has_kind_specialized_provider():
    """The availability helper reports whether ANY registered provider is
    kind-specialized — so plan() can offer discovery in a place-only deployment
    (kind provider registered, no general default) rather than refusing outright."""
    # Empty registry / only a general provider → no kind specialization.
    assert has_kind_specialized_provider() is False
    register_web_source(StubWebSource())  # general, no query_kinds
    assert has_kind_specialized_provider() is False
    # Add a kind-specialized provider → now True.
    register_web_source(_KindProvider("place_src", frozenset({"place"})))
    assert has_kind_specialized_provider() is True


def test_has_kind_specialized_provider_ignores_url_only():
    """A url_only provider that declared a kind does NOT count as a query-discovery
    kind specialization (kind routing is query discovery, not URL extraction)."""
    register_web_source(
        _KindProvider("weird", frozenset({"place"}), url_only=True)
    )
    assert has_kind_specialized_provider() is False
