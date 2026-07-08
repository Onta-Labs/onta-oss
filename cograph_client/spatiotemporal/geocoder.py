"""Free-text geocoder SEAM for the spatial fast-path (ONTA-249).

A radius question ("orthopedic surgeons within 25 km of Irvine") needs to turn the
free-text anchor ("Irvine") into ``(lon, lat)``. Before this, the pipeline could
only resolve an anchor that already existed as a KG entity carrying a
``geo:wktLiteral`` — a bare place name never resolved, so the whole radius fast
path silently fell through.

This module is the read-path twin of the spatio-temporal index registry: a small
protocol + a process-wide registry (``register_geocoder`` / ``get_geocoder`` /
``reset_geocoder``) + a **deterministic** OSS default. It follows the same plugin
discipline as ``register_spatiotemporal_index`` / ``register_adapter``:

* OSS ships :class:`GazetteerGeocoder`, a pure-Python, offline, DETERMINISTIC
  lookup over a small curated gazetteer of well-known public places (plus any
  caller-supplied entries). No network, no API key, no paid dependency — so tests
  and OSS installs work with zero setup and identical results every run.
* A PREMIUM geocoder (Google Places / Mapbox / Nominatim, …) lives in the
  proprietary tree and registers itself at startup via :func:`register_geocoder`
  (loaded through the existing ``module:callable`` plugin-spec mechanism, same as
  the enrichment / governance plugins). Premium → OSS only; OSS never imports it.

The seam is intentionally tiny (one async method) so a premium adapter is trivial
to implement and the fast path stays byte-stable when no geocoder resolves a name
(it just falls through to the SPARQL path, exactly as before).
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

import structlog

logger = structlog.stdlib.get_logger("cograph.spatiotemporal.geocoder")


@runtime_checkable
class Geocoder(Protocol):
    """Turn a free-text place name into WGS84 ``(lon, lat)``.

    Implementations MUST be side-effect-free and return ``None`` for anything they
    can't confidently resolve (the caller then falls through to the non-geocoded
    path) — never raise into the query pipeline, never guess wildly.
    """

    async def geocode(self, place: str) -> Optional[tuple[float, float]]:
        ...


def _normalize(place: str) -> str:
    """Lowercase, strip articles/punctuation-ish noise for gazetteer matching.

    Deterministic + locale-independent so the same phrase always maps to the same
    key across processes and runs.
    """
    if not isinstance(place, str):
        return ""
    p = place.strip().lower()
    for article in ("the ", "a ", "an "):
        if p.startswith(article):
            p = p[len(article):]
    # Collapse internal whitespace; drop a trailing period.
    p = " ".join(p.split())
    return p.rstrip(".")


# A small curated gazetteer of well-known PUBLIC places (city/landmark → lon,lat,
# WGS84). This is general-knowledge reference data, not proprietary — it exists so
# the OSS default resolves common anchors offline and deterministically. It is NOT
# meant to be exhaustive; a premium geocoder registered via register_geocoder
# supersedes it for full coverage. Keys are pre-normalized via _normalize.
_DEFAULT_GAZETTEER: dict[str, tuple[float, float]] = {
    # (lon, lat)
    "san francisco": (-122.4194, 37.7749),
    "new york": (-74.0060, 40.7128),
    "los angeles": (-118.2437, 34.0522),
    "chicago": (-87.6298, 41.8781),
    "london": (-0.1276, 51.5074),
    "paris": (2.3522, 48.8566),
    "berlin": (13.4050, 52.5200),
    "tokyo": (139.6917, 35.6895),
    "sydney": (151.2093, -33.8688),
    "toronto": (-79.3832, 43.6532),
}


class GazetteerGeocoder:
    """Deterministic, offline gazetteer geocoder — the registered OSS default.

    Resolves a place name by exact (normalized) match against its gazetteer.
    Returns ``None`` for an unknown place. Callers can extend the gazetteer at
    construction (``extra=...``) — tests pass an INVENTED gazetteer so they never
    depend on real-world coordinates.
    """

    def __init__(
        self,
        gazetteer: Optional[dict[str, tuple[float, float]]] = None,
        *,
        extra: Optional[dict[str, tuple[float, float]]] = None,
    ) -> None:
        base = dict(_DEFAULT_GAZETTEER if gazetteer is None else gazetteer)
        if extra:
            base.update(extra)
        # Re-key through _normalize so callers can pass human-readable keys.
        self._gaz: dict[str, tuple[float, float]] = {
            _normalize(k): v for k, v in base.items()
        }

    async def geocode(self, place: str) -> Optional[tuple[float, float]]:
        key = _normalize(place)
        if not key:
            return None
        return self._gaz.get(key)


_registered: Optional[Geocoder] = None
_default: Optional[Geocoder] = None


def register_geocoder(geocoder: Optional[Geocoder]) -> None:
    """Register (or clear, with ``None``) the process-wide geocoder.

    A premium/alternate geocoder calls this at startup; OSS deployments never do
    and fall back to :class:`GazetteerGeocoder`. Same plugin style as
    ``register_spatiotemporal_index`` / ``register_adapter``.
    """
    global _registered
    _registered = geocoder
    logger.info(
        "geocoder_registered",
        backend=type(geocoder).__name__ if geocoder is not None else None,
    )


def get_geocoder() -> Geocoder:
    """The registered geocoder, else a lazily-built (and cached) OSS default."""
    global _default
    if _registered is not None:
        return _registered
    if _default is None:
        _default = GazetteerGeocoder()
    return _default


def reset_geocoder() -> None:
    """Test helper — clear both the registered override and the cached default."""
    global _registered, _default
    _registered = None
    _default = None
