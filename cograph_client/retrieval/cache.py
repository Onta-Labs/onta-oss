"""Record-and-replay CACHE for page fetches — a wrapping rung of the fetch ladder.

Discovery, enrichment, and research all consult the web through the fetch ladder
(:mod:`cograph_client.retrieval.fetch`). Re-running an eval re-scrapes the SAME
URLs every time, burning paid Firecrawl/render credits and making runs
non-deterministic. This module adds a **persistent record-and-replay cache** that
scrapes each ``(rung, url, want)`` exactly ONCE and replays the real scraped
:class:`FetchedPage` from disk on every subsequent run — repeat cost → ~0, and the
eval becomes reproducible.

This is NOT an LLM mock: it never fabricates page content. On a MISS it delegates
to the REAL underlying fetcher and stores exactly what came back; on a HIT it
replays that stored bytes-faithful page with no network call.

How it hooks the ladder
-----------------------
:class:`CachingPageFetcher` is itself a :class:`PageFetcher`, so it lives IN the
substrate rather than being a bespoke fetch. It WRAPS a single delegate rung and
mirrors that rung's ``name`` / ``tier``, so the ladder the harness walks looks
identical — same rungs, same order, same escalation — the wrapper just intercepts
each ``fetch`` first and falls through to the delegate on a miss. The whole ladder
is wrapped rung-for-rung by :func:`maybe_wrap_ladder`, which
:func:`cograph_client.retrieval.fetch.default_ladder` calls on the way out. Keying
by the delegate's identity as well as ``url`` + ``want`` keeps a cheap ``static``
hit from masquerading as an escalated ``render`` result, so each rung records its
own once-scraped page.

Modes (env ``COGRAPH_FETCH_CACHE``)
-----------------------------------
* ``off`` (default) — no caching; :func:`maybe_wrap_ladder` returns the ladder
  UNCHANGED, so production behaviour is byte-identical to no cache at all.
* ``record`` / ``auto`` — HIT replays; MISS fetches through the delegate, stores
  the page, and returns it. The steady state after one warm run.
* ``replay`` — HIT replays; MISS returns an explicit, typed "not recorded"
  :class:`FetchedPage` (``ok=False``) and NEVER touches the network, so a replay
  run can never silently hit the web on an un-recorded URL.

Cache dir is ``COGRAPH_FETCH_CACHE_DIR`` (default: a stable ``cograph-fetch-cache``
dir under the system temp path, so it persists across runs). A corrupt or missing
cache file is treated as a MISS, never a crash.

Cost seam: the cache rung declares itself FREE (``is_paid=False``,
``cost_per_call=0.0``) because a disk replay costs nothing — so on a HIT
:func:`cograph_client.retrieval.fetcher_cost` reads ~zero. On a ``record`` MISS the
delegate still incurs its real (once-only) cost; that call is attributed to the
free cache rung in a harness trace, an acceptable trade since caching is opt-in and
only enabled for evals. The cache never fetches or resolves DNS itself — the
SSRF/HTML-safety guards stay entirely inside the delegate it wraps.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

from cograph_client.retrieval.types import FetchedPage

if TYPE_CHECKING:  # avoid a runtime import cycle with fetch.py
    from cograph_client.retrieval.fetch import PageFetcher

logger = structlog.stdlib.get_logger("cograph.retrieval.cache")

#: Env var selecting the cache mode (``off`` / ``record`` / ``replay`` / ``auto``).
CACHE_MODE_ENV = "COGRAPH_FETCH_CACHE"
#: Env var overriding the on-disk cache directory.
CACHE_DIR_ENV = "COGRAPH_FETCH_CACHE_DIR"

#: Bumped if the on-disk record format changes incompatibly. A file written by a
#: different version is treated as a MISS (re-recorded), never a crash.
_CACHE_VERSION = 1

#: Only successful pages are cached: a transient failure (timeout, 5xx, blocked)
#: must not be pinned into the cache under ``record`` and then replayed forever —
#: leaving it uncached lets the next ``record`` run re-attempt it. In ``replay`` an
#: un-recorded URL surfaces the explicit "not recorded" miss regardless.
_CACHE_ONLY_OK = True


class FetchCacheMode(enum.Enum):
    """What the fetch cache does on a request (mirrors the ``COGRAPH_FETCH_CACHE``
    env). ``auto`` is an alias for :attr:`RECORD`."""

    OFF = "off"
    RECORD = "record"
    REPLAY = "replay"

    @property
    def enabled(self) -> bool:
        """True when the cache is active (anything but :attr:`OFF`)."""
        return self is not FetchCacheMode.OFF


def resolve_mode(raw: Optional[str]) -> FetchCacheMode:
    """Map a raw env string → :class:`FetchCacheMode`.

    ``auto`` → :attr:`FetchCacheMode.RECORD`. Empty / unset / unrecognised →
    :attr:`FetchCacheMode.OFF` (fail safe: an unknown value never silently starts
    caching or blocking the network)."""
    val = (raw or "").strip().lower()
    if val in ("record", "auto"):
        return FetchCacheMode.RECORD
    if val == "replay":
        return FetchCacheMode.REPLAY
    if val in ("", "off", "0", "false", "no", "none"):
        return FetchCacheMode.OFF
    logger.warning("fetch_cache_unknown_mode", value=val, defaulting_to="off")
    return FetchCacheMode.OFF


def current_mode() -> FetchCacheMode:
    """The cache mode from the environment right now."""
    return resolve_mode(os.environ.get(CACHE_MODE_ENV))


def default_cache_dir() -> Path:
    """The on-disk cache directory: ``COGRAPH_FETCH_CACHE_DIR`` or a stable
    ``cograph-fetch-cache`` folder under the system temp dir (persists across runs
    so record → replay works, but stays out of the repo/product data)."""
    override = os.environ.get(CACHE_DIR_ENV)
    if override and override.strip():
        return Path(override.strip())
    return Path(tempfile.gettempdir()) / "cograph-fetch-cache"


# --- The on-disk store -------------------------------------------------------- #

# The FetchedPage fields we persist. Derived from the dataclass so a new field is
# round-tripped automatically; loads filter to these so a stale file with a
# removed/extra field degrades to a MISS instead of raising.
_PAGE_FIELDS = tuple(f.name for f in dataclasses.fields(FetchedPage))


def _page_to_dict(page: FetchedPage) -> dict:
    return {name: getattr(page, name) for name in _PAGE_FIELDS}


def _page_from_dict(data: dict) -> FetchedPage:
    """Rebuild a :class:`FetchedPage`, keeping only known fields. Raises
    ``TypeError``/``KeyError`` if the payload is missing a required field or is not
    a mapping — the caller treats that as a corrupt entry (miss)."""
    kwargs = {name: data[name] for name in _PAGE_FIELDS if name in data}
    return FetchedPage(**kwargs)


class FetchCache:
    """A directory of one-JSON-file-per-entry page records.

    Every method is corruption-tolerant: a missing dir, unreadable file, truncated
    JSON, or schema mismatch reads as a MISS (``get`` → ``None``) rather than
    raising, so a partially-written or hand-edited cache never breaks a run."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def key(self, *, name: str, tier: int, url: str, want: str) -> str:
        """Stable content-addressed key for a ``(rung, url, want)``.

        Namespaced by the delegate's ``name``/``tier`` so a cheap ``static`` hit
        can't be replayed in place of an escalated ``render`` result for the same
        URL. NUL-delimited so no field boundary can be forged by crafted input."""
        raw = "\0".join((str(name), str(tier), str(url), str(want)))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _path_for(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> Optional[FetchedPage]:
        """Return the cached page for ``key``, or ``None`` on miss/corrupt/mismatch."""
        path = self._path_for(key)
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):  # unreadable / truncated / invalid JSON
            logger.warning("fetch_cache_corrupt_read", path=str(path))
            return None
        if not isinstance(payload, dict) or payload.get("_cache_version") != _CACHE_VERSION:
            return None
        page_data = payload.get("page")
        if not isinstance(page_data, dict):
            return None
        try:
            return _page_from_dict(page_data)
        except (TypeError, KeyError):
            logger.warning("fetch_cache_corrupt_shape", path=str(path))
            return None

    def put(self, key: str, page: FetchedPage, *, key_inputs: Optional[dict] = None) -> None:
        """Persist ``page`` under ``key`` atomically (temp file + ``os.replace``) so a
        concurrent reader never sees a half-written file. Storage errors are logged,
        not raised — a cache write must never break the fetch it is caching."""
        path = self._path_for(key)
        payload = {
            "_cache_version": _CACHE_VERSION,
            "key_inputs": key_inputs or {},  # human-readable provenance for debugging
            "page": _page_to_dict(page),
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.directory), prefix=f"{key}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp_name, path)
            except BaseException:
                # Clean up the temp file on any failure so a crashed write leaves
                # no orphan behind.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except Exception as exc:
            # Any failure to PERSIST (an OSError, or a serialization error such as a
            # UnicodeEncodeError from a page whose text carries a lone surrogate) must
            # NEVER break the fetch it is caching — the PageFetcher contract is that
            # fetch never raises. Log and move on; the (still-good) page is returned
            # uncached, so the next record run re-attempts. BaseException
            # (KeyboardInterrupt / SystemExit) intentionally still propagates.
            logger.warning("fetch_cache_write_failed", path=str(path), error=str(exc)[:200])


# One shared store per resolved directory, so all wrapped rungs in a process reuse
# the same on-disk cache without re-statting the dir on every fetch.
_caches: dict[str, FetchCache] = {}


def get_default_cache() -> FetchCache:
    """Process-wide :class:`FetchCache` for the currently-configured directory."""
    directory = default_cache_dir()
    dkey = str(directory)
    cache = _caches.get(dkey)
    if cache is None:
        cache = FetchCache(directory)
        _caches[dkey] = cache
    return cache


def reset_fetch_cache_registry() -> None:
    """Drop the in-memory store registry (NOT the on-disk files). For tests."""
    _caches.clear()


# --- The wrapping rung -------------------------------------------------------- #


class CachingPageFetcher:
    """A :class:`PageFetcher` that records-and-replays a delegate rung's fetches.

    Mirrors the delegate's ``name`` / ``tier`` so it is a drop-in ladder rung. On
    ``fetch`` it consults the persistent cache first (HIT → replay, no network) and
    only falls through to the delegate on a MISS (``record``) — or returns an
    explicit typed miss without any network at all (``replay``). Declares itself
    FREE for the cost seam; see the module docstring for the cost trade-off.

    Never raises: it upholds the ``PageFetcher`` contract that a failed fetch comes
    back as ``FetchedPage(ok=False, error=...)``.
    """

    # Cost seam: a cache replay is a disk read, i.e. free. fetcher_cost() reads
    # these, so a HIT (and the whole cache rung) prices at ~zero.
    is_paid = False
    cost_per_call = 0.0

    def __init__(
        self,
        delegate: "PageFetcher",
        *,
        cache: Optional[FetchCache] = None,
        mode: Optional[FetchCacheMode] = None,
    ) -> None:
        self._delegate = delegate
        self._cache = cache
        # None → resolve the mode from the environment at each fetch (lets a test
        # or a long-lived process pick up a mode change); an explicit mode pins it.
        self._mode = mode
        self.name = str(getattr(delegate, "name", "cached"))
        try:
            self.tier = int(getattr(delegate, "tier", 0))
        except (TypeError, ValueError):
            self.tier = 0
        # Lightweight counters for observability / tests.
        self.hits = 0
        self.misses = 0
        self.stores = 0

    @property
    def delegate(self) -> "PageFetcher":
        return self._delegate

    def _resolved_mode(self) -> FetchCacheMode:
        return self._mode if self._mode is not None else current_mode()

    def _resolved_cache(self) -> FetchCache:
        return self._cache if self._cache is not None else get_default_cache()

    async def fetch(self, url: str, *, want: str = "") -> FetchedPage:
        mode = self._resolved_mode()
        if not mode.enabled:
            # OFF: pure pass-through, no cache touch — behaviour is identical to
            # having no cache rung at all.
            return await self._delegate.fetch(url, want=want)

        cache = self._resolved_cache()
        key = cache.key(name=self.name, tier=self.tier, url=url, want=want)

        hit = cache.get(key)
        if hit is not None:
            self.hits += 1
            return hit

        self.misses += 1
        if mode is FetchCacheMode.REPLAY:
            # Explicit, network-free miss — a replay run NEVER silently scrapes an
            # un-recorded URL.
            return FetchedPage(
                url=url,
                tier=self.name,
                ok=False,
                error=(
                    f"fetch-cache replay MISS: {url!r} (want={want!r}) not recorded "
                    f"under {CACHE_MODE_ENV}=replay — record it first"
                ),
            )

        # RECORD: fetch through the real rung, then persist the result.
        page = await self._delegate.fetch(url, want=want)
        if page is not None and (page.ok or not _CACHE_ONLY_OK):
            cache.put(
                key,
                page,
                key_inputs={"name": self.name, "tier": self.tier, "url": url, "want": want},
            )
            self.stores += 1
        return page


def maybe_wrap_ladder(fetchers: list) -> list:
    """Wrap each rung in :class:`CachingPageFetcher` when caching is enabled.

    When the mode is :attr:`FetchCacheMode.OFF` the ladder is returned UNCHANGED
    (same list, same objects), so the default/production path is byte-identical to
    having no cache. When enabled, every rung is wrapped with the resolved mode +
    shared cache pinned, so a whole ladder walk agrees on one mode even if the env
    flips mid-run. An already-wrapped rung is left as-is (idempotent — never
    double-wrap)."""
    mode = current_mode()
    if not mode.enabled:
        return fetchers
    cache = get_default_cache()
    return [
        f if isinstance(f, CachingPageFetcher)
        else CachingPageFetcher(f, cache=cache, mode=mode)
        for f in fetchers
    ]


__all__ = [
    "CACHE_DIR_ENV",
    "CACHE_MODE_ENV",
    "CachingPageFetcher",
    "FetchCache",
    "FetchCacheMode",
    "current_mode",
    "default_cache_dir",
    "get_default_cache",
    "maybe_wrap_ladder",
    "reset_fetch_cache_registry",
    "resolve_mode",
]
