"""Tests for the record-and-replay fetch cache (:mod:`cograph_client.retrieval.cache`).

All offline: the "delegate" is an in-memory fake :class:`PageFetcher` whose call
count we assert, and every test writes into an isolated ``tmp_path`` cache dir. No
real socket, no real cache directory. Covers the four modes, the round-trip
fidelity of every :class:`FetchedPage` field, corrupt-file tolerance, key
namespacing per rung, and the ``default_ladder`` integration.
"""

from __future__ import annotations

import json

import pytest

from cograph_client.retrieval import (
    FetchedPage,
    default_ladder,
    fetcher_cost,
    register_default_fetchers,
    reset_page_fetchers,
)
from cograph_client.retrieval.cache import (
    CACHE_MODE_ENV,
    CachingPageFetcher,
    FetchCache,
    FetchCacheMode,
    current_mode,
    default_cache_dir,
    maybe_wrap_ladder,
    reset_fetch_cache_registry,
    resolve_mode,
)


class FakeFetcher:
    """An in-memory rung whose ``fetch`` count we assert. Never touches the network."""

    def __init__(self, name="static", tier=0, *, page=None, is_paid=False, cost=0.0):
        self.name = name
        self.tier = tier
        self.is_paid = is_paid
        self.cost_per_call = cost
        self.calls = 0
        self._page = page

    async def fetch(self, url: str, *, want: str = "") -> FetchedPage:
        self.calls += 1
        if self._page is not None:
            return self._page
        return FetchedPage(
            url=url, text=f"scraped {url} want={want}", title="T", tier=self.name, ok=True
        )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Start each test with caching OFF and a fresh store registry; individual tests
    # opt into a mode. Prevents an ambient COGRAPH_FETCH_CACHE from leaking in.
    monkeypatch.delenv(CACHE_MODE_ENV, raising=False)
    monkeypatch.delenv("COGRAPH_FETCH_CACHE_DIR", raising=False)
    reset_fetch_cache_registry()
    reset_page_fetchers()
    yield
    reset_fetch_cache_registry()
    reset_page_fetchers()


def _cache(tmp_path):
    return FetchCache(tmp_path / "fc")


# --- mode resolution ---------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("record", FetchCacheMode.RECORD),
        ("auto", FetchCacheMode.RECORD),
        ("replay", FetchCacheMode.REPLAY),
        ("off", FetchCacheMode.OFF),
        ("", FetchCacheMode.OFF),
        (None, FetchCacheMode.OFF),
        ("garbage", FetchCacheMode.OFF),
        ("RECORD", FetchCacheMode.RECORD),  # case-insensitive
    ],
)
def test_resolve_mode(raw, expected):
    assert resolve_mode(raw) is expected


def test_current_mode_reads_env(monkeypatch):
    monkeypatch.setenv(CACHE_MODE_ENV, "replay")
    assert current_mode() is FetchCacheMode.REPLAY
    monkeypatch.delenv(CACHE_MODE_ENV)
    assert current_mode() is FetchCacheMode.OFF


def test_default_cache_dir_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "my-cache"
    monkeypatch.setenv("COGRAPH_FETCH_CACHE_DIR", str(custom))
    assert default_cache_dir() == custom
    monkeypatch.delenv("COGRAPH_FETCH_CACHE_DIR")
    # Falls back to a stable temp path (not the repo).
    assert default_cache_dir().name == "cograph-fetch-cache"


# --- record mode: scrape once, replay after ----------------------------------- #


async def test_record_mode_scrapes_once_then_replays(tmp_path):
    delegate = FakeFetcher()
    fetcher = CachingPageFetcher(delegate, cache=_cache(tmp_path), mode=FetchCacheMode.RECORD)

    first = await fetcher.fetch("https://example.test/a", want="rows")
    assert delegate.calls == 1  # miss → delegate fetched
    assert first.ok and first.text == "scraped https://example.test/a want=rows"
    assert fetcher.misses == 1 and fetcher.stores == 1 and fetcher.hits == 0

    second = await fetcher.fetch("https://example.test/a", want="rows")
    assert delegate.calls == 1  # HIT → delegate NOT called again
    assert fetcher.hits == 1
    # byte-identical round-trip of every field
    assert second == first
    assert (second.url, second.text, second.title, second.tier, second.ok,
            second.error, second.truncated) == (
        first.url, first.text, first.title, first.tier, first.ok,
        first.error, first.truncated)


async def test_record_hit_survives_a_fresh_fetcher_instance(tmp_path):
    """The cache is on DISK: a brand-new wrapper (simulating a later eval run) over
    the same dir replays without calling its delegate."""
    cache = _cache(tmp_path)
    d1 = FakeFetcher()
    await CachingPageFetcher(d1, cache=cache, mode=FetchCacheMode.RECORD).fetch(
        "https://example.test/x"
    )
    assert d1.calls == 1

    d2 = FakeFetcher()  # a fresh delegate, as in a separate process/run
    page = await CachingPageFetcher(d2, cache=cache, mode=FetchCacheMode.RECORD).fetch(
        "https://example.test/x"
    )
    assert d2.calls == 0  # served entirely from disk
    assert page.text == "scraped https://example.test/x want="


# --- replay mode: never hits the network -------------------------------------- #


async def test_replay_hit_replays_without_delegate(tmp_path):
    cache = _cache(tmp_path)
    rec = FakeFetcher()
    recorded = await CachingPageFetcher(rec, cache=cache, mode=FetchCacheMode.RECORD).fetch(
        "https://example.test/r"
    )

    replay_delegate = FakeFetcher()
    replayed = await CachingPageFetcher(
        replay_delegate, cache=cache, mode=FetchCacheMode.REPLAY
    ).fetch("https://example.test/r")
    assert replay_delegate.calls == 0  # no network in replay
    assert replayed == recorded


async def test_replay_miss_is_explicit_and_never_calls_delegate(tmp_path):
    delegate = FakeFetcher()
    fetcher = CachingPageFetcher(delegate, cache=_cache(tmp_path), mode=FetchCacheMode.REPLAY)
    page = await fetcher.fetch("https://example.test/never-recorded", want="q")
    assert delegate.calls == 0  # crucial: a replay miss does NOT scrape
    assert page.ok is False
    assert page.error and "replay MISS" in page.error
    assert "never-recorded" in page.error
    assert fetcher.misses == 1 and fetcher.stores == 0


# --- off mode: no caching, delegate always called ----------------------------- #


async def test_off_mode_is_pure_passthrough(tmp_path):
    cache = _cache(tmp_path)
    delegate = FakeFetcher()
    fetcher = CachingPageFetcher(delegate, cache=cache, mode=FetchCacheMode.OFF)

    for _ in range(3):
        await fetcher.fetch("https://example.test/o")
    assert delegate.calls == 3  # every call hits the delegate
    assert fetcher.hits == 0 and fetcher.misses == 0 and fetcher.stores == 0
    # nothing was written to disk
    assert not (cache.directory.exists() and any(cache.directory.iterdir()))


# --- FetchedPage round-trips exactly (all fields) ----------------------------- #


@pytest.mark.parametrize(
    "page",
    [
        FetchedPage(url="https://a.test/1", text="body", title="Ti", tier="static",
                    ok=True, error=None, truncated=False),
        FetchedPage(url="https://a.test/2", text="x" * 5000, title="", tier="render",
                    ok=True, error=None, truncated=True),
        FetchedPage(url="https://a.test/3", text="", title="", tier="static",
                    ok=False, error="HTTP 404", truncated=False),
        FetchedPage(url="https://uni.test/☃", text="héllo — ünïcode 日本語", title="T★",
                    tier="static", ok=True, truncated=False),
    ],
)
def test_fetchedpage_roundtrips_exactly(tmp_path, page):
    cache = _cache(tmp_path)
    key = cache.key(name="static", tier=0, url=page.url, want="")
    cache.put(key, page)
    loaded = cache.get(key)
    assert loaded == page
    # field-by-field, so a missed field can't hide behind __eq__
    for field in ("url", "text", "title", "tier", "ok", "error", "truncated"):
        assert getattr(loaded, field) == getattr(page, field), field


# --- corrupt / missing cache file → treated as a miss ------------------------- #


def test_missing_file_is_a_miss(tmp_path):
    cache = _cache(tmp_path)
    assert cache.get(cache.key(name="static", tier=0, url="https://none.test", want="")) is None


def test_corrupt_json_is_a_miss_not_a_crash(tmp_path):
    cache = _cache(tmp_path)
    key = cache.key(name="static", tier=0, url="https://c.test", want="")
    cache.directory.mkdir(parents=True, exist_ok=True)
    (cache.directory / f"{key}.json").write_text("{ this is not valid json ")
    assert cache.get(key) is None  # tolerated → miss


def test_wrong_schema_version_is_a_miss(tmp_path):
    cache = _cache(tmp_path)
    key = cache.key(name="static", tier=0, url="https://v.test", want="")
    cache.directory.mkdir(parents=True, exist_ok=True)
    (cache.directory / f"{key}.json").write_text(
        json.dumps({"_cache_version": 999, "page": {"url": "https://v.test"}})
    )
    assert cache.get(key) is None


def test_bad_page_shape_is_a_miss(tmp_path):
    cache = _cache(tmp_path)
    key = cache.key(name="static", tier=0, url="https://s.test", want="")
    cache.directory.mkdir(parents=True, exist_ok=True)
    # page missing the required `url` field → reconstruction fails → miss
    (cache.directory / f"{key}.json").write_text(
        json.dumps({"_cache_version": 1, "page": {"text": "orphan"}})
    )
    assert cache.get(key) is None


async def test_record_over_corrupt_file_recovers(tmp_path):
    """A corrupt entry must not wedge record mode — the miss re-scrapes and overwrites."""
    cache = _cache(tmp_path)
    delegate = FakeFetcher()
    fetcher = CachingPageFetcher(delegate, cache=cache, mode=FetchCacheMode.RECORD)
    key = cache.key(name="static", tier=0, url="https://rc.test", want="")
    cache.directory.mkdir(parents=True, exist_ok=True)
    (cache.directory / f"{key}.json").write_text("garbage{")

    page = await fetcher.fetch("https://rc.test")
    assert delegate.calls == 1 and page.ok
    # subsequent read now hits the freshly-written entry
    assert cache.get(key) is not None


# --- key namespacing: url, want, and rung identity all matter ----------------- #


def test_key_varies_by_url_want_and_rung(tmp_path):
    cache = _cache(tmp_path)
    base = cache.key(name="static", tier=0, url="https://k.test", want="a")
    assert base != cache.key(name="static", tier=0, url="https://k.test", want="b")
    assert base != cache.key(name="static", tier=0, url="https://OTHER.test", want="a")
    # a cheap static hit must not masquerade as an escalated render result
    assert base != cache.key(name="render", tier=2, url="https://k.test", want="a")
    # stable across calls
    assert base == cache.key(name="static", tier=0, url="https://k.test", want="a")


async def test_static_and_render_rungs_cache_independently(tmp_path):
    cache = _cache(tmp_path)
    static = FakeFetcher(name="static", tier=0)
    render = FakeFetcher(name="render", tier=2)
    wrapped_static = CachingPageFetcher(static, cache=cache, mode=FetchCacheMode.RECORD)
    wrapped_render = CachingPageFetcher(render, cache=cache, mode=FetchCacheMode.RECORD)

    await wrapped_static.fetch("https://same.test")
    # render for the SAME url is a distinct key → still a miss → its delegate runs
    await wrapped_render.fetch("https://same.test")
    assert static.calls == 1 and render.calls == 1


# --- wrapper is a well-behaved PageFetcher ------------------------------------ #


def test_wrapper_mirrors_delegate_identity_and_is_free():
    delegate = FakeFetcher(name="render", tier=2, is_paid=True, cost=0.02)
    wrapped = CachingPageFetcher(delegate, mode=FetchCacheMode.RECORD)
    assert wrapped.name == "render" and wrapped.tier == 2
    assert wrapped.delegate is delegate
    # A cache read is free — the cost seam prices the rung (and a HIT) at ~zero.
    assert fetcher_cost(wrapped) == (False, 0.0)


def test_wrapper_satisfies_pagefetcher_protocol():
    from cograph_client.retrieval import PageFetcher

    assert isinstance(CachingPageFetcher(FakeFetcher()), PageFetcher)


async def test_off_mode_only_caches_ok_pages(tmp_path):
    """A failed fetch under record mode is NOT pinned (so a later run re-attempts)."""
    cache = _cache(tmp_path)
    fail = FakeFetcher(page=FetchedPage(url="https://f.test", ok=False, error="boom"))
    fetcher = CachingPageFetcher(fail, cache=cache, mode=FetchCacheMode.RECORD)
    page = await fetcher.fetch("https://f.test")
    assert page.ok is False and fetcher.stores == 0
    assert cache.get(cache.key(name="static", tier=0, url="https://f.test", want="")) is None


# --- default_ladder integration ---------------------------------------------- #


def test_default_ladder_off_is_unchanged(monkeypatch):
    """OFF (the default): the ladder is returned unwrapped — byte-identical to today."""
    monkeypatch.delenv(CACHE_MODE_ENV, raising=False)
    register_default_fetchers()
    ladder = default_ladder()
    assert all(not isinstance(f, CachingPageFetcher) for f in ladder)


def test_default_ladder_record_wraps_each_rung(monkeypatch, tmp_path):
    monkeypatch.setenv(CACHE_MODE_ENV, "record")
    monkeypatch.setenv("COGRAPH_FETCH_CACHE_DIR", str(tmp_path / "ld"))
    register_default_fetchers()
    ladder = default_ladder()
    assert ladder and all(isinstance(f, CachingPageFetcher) for f in ladder)
    # names/tiers preserved so escalation ordering is untouched
    assert [f.name for f in ladder] == ["static"]


def test_maybe_wrap_ladder_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv(CACHE_MODE_ENV, "record")
    monkeypatch.setenv("COGRAPH_FETCH_CACHE_DIR", str(tmp_path / "idem"))
    rungs = [FakeFetcher(name="static", tier=0)]
    once = maybe_wrap_ladder(rungs)
    twice = maybe_wrap_ladder(once)
    assert all(isinstance(f, CachingPageFetcher) for f in twice)
    # no double-wrapping: the already-wrapped rung is passed through unchanged
    assert twice[0] is once[0]


def test_maybe_wrap_ladder_off_returns_same_list(monkeypatch):
    monkeypatch.delenv(CACHE_MODE_ENV, raising=False)
    rungs = [FakeFetcher()]
    assert maybe_wrap_ladder(rungs) is rungs
