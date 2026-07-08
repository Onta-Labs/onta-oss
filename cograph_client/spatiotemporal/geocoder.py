"""Free-text geocoder SEAM for the spatial fast-path (ONTA-249).

A radius question ("orthopedic surgeons within 25 miles of Irvine") needs to turn
the free-text anchor ("Irvine") into ``(lon, lat)``. Before this, the pipeline
could only resolve an anchor that already existed as a KG entity carrying a
``geo:wktLiteral`` — a bare place name never resolved, so the whole radius fast
path silently fell through.

This module is the read-path twin of the spatio-temporal index registry: a small
protocol + a process-wide registry (``register_geocoder`` / ``get_geocoder`` /
``reset_geocoder``) + a **general, deterministic, offline** OSS default. It
follows the same plugin discipline as ``register_spatiotemporal_index`` /
``register_adapter``:

* OSS ships :class:`GeoNamesGeocoder`, a pure-Python, offline, DETERMINISTIC
  geocoder backed by a bundled slice of the **GeoNames** public gazetteer
  (``cities15000`` — every city on Earth with population ≥ 15,000, ~34,000
  places, all 195 countries + every US state). It is GENERAL public reference
  data, not a curated/persona list: "Irvine" resolves because Irvine, CA is a
  real city in the public dataset, exactly like the other ~34,000 cities. No
  network, no API key, no paid dependency — tests and OSS installs work with
  zero setup and identical results every run.

  Dataset provenance (see ``data/PROVENANCE.md``):
    - Source: GeoNames — https://download.geonames.org/export/dump/cities15000.zip
    - License: Creative Commons Attribution 4.0 (CC BY 4.0)
    - Bundled form: the 7 columns we use (name, asciiname, admin1 code, country
      code, latitude, longitude, population), gzip-compressed TSV. ~34k rows,
      ~0.7 MB on disk.

* A PREMIUM geocoder (Google Places / Mapbox / a paid Nominatim tier, …) lives in
  the proprietary tree and registers itself at startup via
  :func:`register_geocoder` (loaded through the existing ``module:callable``
  plugin-spec mechanism / ``OMNIX_GEOCODER_PLUGIN``, same as the enrichment /
  governance plugins). Premium → OSS only; OSS never imports it, and no paid API
  is baked into OSS.

The seam is intentionally tiny (one async method) so a premium adapter is trivial
to implement and the fast path stays byte-stable when no geocoder resolves a name
(it just falls through to the SPARQL path, exactly as before).
"""

from __future__ import annotations

import gzip
import threading
from pathlib import Path
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


# --------------------------------------------------------------------------- #
# Normalization — deterministic + locale-independent so the same phrase always
# maps to the same key across processes and runs.
# --------------------------------------------------------------------------- #
# A tiny set of diacritic folds so a query typed without accents ("Zurich") still
# hits a dataset row stored with them ("Zürich"). Deliberately small and explicit
# — NOT a full Unicode NFKD transliteration (which would pull in edge cases and
# make behavior harder to reason about). The dataset also carries an ASCII
# "asciiname" column that we index alongside the accented name, so most accented
# places are reachable by their plain-ASCII spelling regardless.
_DIACRITIC_FOLD = str.maketrans(
    {
        "á": "a", "à": "a", "â": "a", "ä": "a", "ã": "a", "å": "a", "ā": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e", "ē": "e",
        "í": "i", "ì": "i", "î": "i", "ï": "i", "ī": "i",
        "ó": "o", "ò": "o", "ô": "o", "ö": "o", "õ": "o", "ø": "o", "ō": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u", "ū": "u",
        "ñ": "n", "ç": "c", "ß": "ss",
    }
)


def _normalize(place: str) -> str:
    """Lowercase, fold common diacritics, strip leading articles + noise.

    Deterministic + locale-independent so the same phrase always maps to the same
    key across processes and runs.
    """
    if not isinstance(place, str):
        return ""
    p = place.strip().lower().translate(_DIACRITIC_FOLD)
    for article in ("the ", "a ", "an "):
        if p.startswith(article):
            p = p[len(article):]
    # Collapse internal whitespace; drop a trailing period.
    p = " ".join(p.split())
    return p.rstrip(".")


# US state postal codes (the GeoNames US admin1 code is exactly this 2-letter
# form) → themselves, plus a few common full-name spellings a user might type in
# "City, State". General reference, not persona-specific.
_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}
_US_STATE_NAME_TO_CODE = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri",
    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx",
    "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}

# A small ISO-3166 country-name → alpha-2-code map for common "City, Country"
# queries. GeoNames stores the alpha-2 country code; a user typing a full country
# name maps through here. Not exhaustive (the alpha-2 code itself always works);
# these are the high-traffic full names so "Paris, France" resolves.
_COUNTRY_NAME_TO_CODE = {
    "united states": "us", "usa": "us", "us": "us", "united states of america": "us",
    "united kingdom": "gb", "uk": "gb", "great britain": "gb", "england": "gb",
    "britain": "gb",
    "canada": "ca", "mexico": "mx", "france": "fr", "germany": "de", "spain": "es",
    "italy": "it", "portugal": "pt", "netherlands": "nl", "belgium": "be",
    "switzerland": "ch", "austria": "at", "ireland": "ie", "sweden": "se",
    "norway": "no", "denmark": "dk", "finland": "fi", "poland": "pl",
    "greece": "gr", "turkey": "tr", "russia": "ru", "china": "cn", "japan": "jp",
    "india": "in", "brazil": "br", "argentina": "ar", "chile": "cl",
    "colombia": "co", "peru": "pe", "australia": "au", "new zealand": "nz",
    "south africa": "za", "egypt": "eg", "nigeria": "ng", "kenya": "ke",
    "south korea": "kr", "korea": "kr", "indonesia": "id", "thailand": "th",
    "vietnam": "vn", "philippines": "ph", "malaysia": "my", "singapore": "sg",
    "israel": "il", "saudi arabia": "sa", "united arab emirates": "ae",
    "uae": "ae",
}


# --------------------------------------------------------------------------- #
# The bundled GeoNames record + loader
# --------------------------------------------------------------------------- #
_DATA_DIR = Path(__file__).parent / "data"
_GEONAMES_FILE = _DATA_DIR / "geonames_cities15000.tsv.gz"


class _CityRecord:
    """One gazetteer row we keep in memory. Slots keep the ~34k-row index small."""

    __slots__ = ("lon", "lat", "population", "admin1", "country")

    def __init__(self, lon: float, lat: float, population: int, admin1: str, country: str):
        self.lon = lon
        self.lat = lat
        self.population = population
        self.admin1 = admin1  # normalized (lowercased) admin1 code (US: state code)
        self.country = country  # normalized (lowercased) alpha-2 country code


def _load_geonames(path: Path) -> dict[str, list[_CityRecord]]:
    """Parse the bundled gzip TSV into ``normalized-name -> [records]``.

    Every row is indexed under both its accented ``name`` and its plain-ASCII
    ``asciiname`` (deduped), so a query resolves whether or not the user typed
    accents. Records under a name are sorted **descending by population** so a
    bare-name lookup deterministically returns the most-populous match.
    """
    index: dict[str, list[_CityRecord]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            name, asciiname, admin1, country, lat_s, lon_s, pop_s = parts[:7]
            try:
                lat = float(lat_s)
                lon = float(lon_s)
            except (TypeError, ValueError):
                continue
            try:
                population = int(pop_s) if pop_s else 0
            except (TypeError, ValueError):
                population = 0
            rec = _CityRecord(
                lon=lon, lat=lat, population=population,
                admin1=(admin1 or "").strip().lower(),
                country=(country or "").strip().lower(),
            )
            keys = set()
            for raw_name in (name, asciiname):
                k = _normalize(raw_name)
                if k:
                    keys.add(k)
            for k in keys:
                index.setdefault(k, []).append(rec)
    # Deterministic ordering: population desc, then a stable geographic tiebreak
    # (lat, lon) so equal-population collisions never flip between builds/runs.
    for recs in index.values():
        recs.sort(key=lambda r: (-r.population, r.lat, r.lon))
    return index


class GeoNamesGeocoder:
    """General, offline, deterministic geocoder over the bundled GeoNames slice.

    Resolves a free-text place name to WGS84 ``(lon, lat)``:

      * bare name ("Irvine", "London") → the most-populous matching city;
      * ``"City, ST"`` (US state code or full state name) → the city in that
        state ("Portland, OR" vs "Portland, ME");
      * ``"City, Country"`` (alpha-2 code or common full name) → the city in that
        country ("Paris, France", "London, GB").

    Casing / whitespace / common diacritics are normalized. Returns ``None`` for
    anything it can't confidently resolve — never raises, never guesses wildly.

    The bundled dataset is a GENERAL public gazetteer (see module docstring): the
    ~34k cities are every place on Earth with population ≥ 15,000, not a curated
    list. It is loaded lazily on first ``geocode`` and cached process-wide.
    """

    # Class-level cache so the ~34k-row index is parsed at most once per process,
    # shared across every instance. Guarded for thread-safety.
    _index: Optional[dict[str, list[_CityRecord]]] = None
    _index_lock = threading.Lock()

    def __init__(self, *, data_file: Optional[Path] = None) -> None:
        self._data_file = data_file or _GEONAMES_FILE

    def _get_index(self) -> dict[str, list[_CityRecord]]:
        # Fast path: already loaded (and it's the default bundled file).
        if self._data_file == _GEONAMES_FILE and GeoNamesGeocoder._index is not None:
            return GeoNamesGeocoder._index
        with GeoNamesGeocoder._index_lock:
            if self._data_file == _GEONAMES_FILE:
                if GeoNamesGeocoder._index is None:
                    GeoNamesGeocoder._index = _load_geonames(self._data_file)
                return GeoNamesGeocoder._index
            # A non-default file (tests) — load without touching the shared cache.
            return _load_geonames(self._data_file)

    @staticmethod
    def _split_qualifier(place: str) -> tuple[str, Optional[str]]:
        """Split ``"City, Qualifier"`` into ``(city, qualifier)``.

        Only the LAST comma-separated segment is treated as the qualifier
        (state/country), so "Washington, D.C." and "San Jose, CA, USA" behave
        sensibly. Returns ``(place, None)`` when there's no comma.
        """
        if "," not in place:
            return place, None
        head, _, tail = place.rpartition(",")
        return head.strip(), tail.strip()

    def _resolve_qualifier(
        self, recs: list[_CityRecord], qualifier: str
    ) -> Optional[_CityRecord]:
        """Pick the record matching a state/country qualifier, else None."""
        q = _normalize(qualifier)
        if not q:
            return None
        # US state (code or full name).
        state_code = q if q in _US_STATE_CODES else _US_STATE_NAME_TO_CODE.get(q)
        if state_code:
            for r in recs:  # recs already sorted by population desc
                if r.country == "us" and r.admin1 == state_code:
                    return r
        # Country (alpha-2 code or common full name).
        country_code = (
            q if len(q) == 2 else None
        ) or _COUNTRY_NAME_TO_CODE.get(q)
        if country_code:
            for r in recs:
                if r.country == country_code:
                    return r
        return None

    async def geocode(self, place: str) -> Optional[tuple[float, float]]:
        if not isinstance(place, str) or not place.strip():
            return None
        try:
            index = self._get_index()
        except Exception:  # pragma: no cover - defensive: never raise into pipeline
            logger.warning("geonames_index_load_failed", exc_info=True)
            return None

        city, qualifier = self._split_qualifier(place)
        key = _normalize(city)
        recs = index.get(key)
        if not recs:
            # No comma-split hit; maybe the whole string is a plain name that
            # happened to contain a comma-like token — try the raw normalized form.
            recs = index.get(_normalize(place))
            qualifier = None
        if not recs:
            return None

        if qualifier:
            match = self._resolve_qualifier(recs, qualifier)
            if match is not None:
                return (match.lon, match.lat)
            # Qualifier given but unmatched → do NOT silently return a wrong city;
            # fall through to None so the caller degrades to the SPARQL path.
            return None

        # Bare name → most-populous match (recs sorted population desc).
        best = recs[0]
        return (best.lon, best.lat)


# --------------------------------------------------------------------------- #
# Legacy small-gazetteer geocoder — kept for tests / callers that want a tiny
# INVENTED gazetteer with no dependency on real-world coordinates.
# --------------------------------------------------------------------------- #
# A small curated gazetteer of well-known PUBLIC places (city → lon,lat, WGS84).
# General-knowledge reference data, retained only as a convenience default for
# :class:`GazetteerGeocoder`. The GENERAL OSS default is now
# :class:`GeoNamesGeocoder` (thousands of places); this stays for tests that pass
# an INVENTED gazetteer so they never depend on real coordinates.
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
    """Deterministic, offline dict-backed geocoder.

    Resolves a place name by exact (normalized) match against a small in-memory
    gazetteer. Returns ``None`` for an unknown place. Callers can pass an INVENTED
    gazetteer at construction so tests never depend on real-world coordinates
    (this is why it's retained even though :class:`GeoNamesGeocoder` is the
    general default).
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
    and fall back to :class:`GeoNamesGeocoder`. Same plugin style as
    ``register_spatiotemporal_index`` / ``register_adapter``.
    """
    global _registered
    _registered = geocoder
    logger.info(
        "geocoder_registered",
        backend=type(geocoder).__name__ if geocoder is not None else None,
    )


def get_geocoder() -> Geocoder:
    """The registered geocoder, else a lazily-built (and cached) OSS default.

    The OSS default is :class:`GeoNamesGeocoder` — a GENERAL offline gazetteer of
    ~34k public cities. A premium geocoder registered via
    :func:`register_geocoder` supersedes it.
    """
    global _default
    if _registered is not None:
        return _registered
    if _default is None:
        _default = GeoNamesGeocoder()
    return _default


def reset_geocoder() -> None:
    """Test helper — clear the registered override and the cached default.

    Does NOT drop :class:`GeoNamesGeocoder`'s parsed-index class cache (that's an
    immutable, expensive-to-rebuild artifact of the bundled file, safe to keep).
    """
    global _registered, _default
    _registered = None
    _default = None
