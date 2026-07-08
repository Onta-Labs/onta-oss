"""ONTA-249 follow-up: the GENERAL offline geocoder (GeoNamesGeocoder).

The whole point of this suite is the ANTI-OVERFITTING guarantee: the OSS default
geocoder resolves arbitrary real places because they live in a **general public
gazetteer** (GeoNames ``cities15000`` — every city on Earth with population ≥
15,000, ~34k rows, CC BY 4.0), NOT because anyone hand-seeded a curated list.

So these tests assert behavior over a BROAD, unrelated spread of real places
(every US region + several continents), plus general mechanics — "City, ST"
disambiguation, casing/whitespace/diacritic normalization, and clean misses —
rather than any single cherry-picked entry. "Irvine" is checked only as one
member of that broad sample, exactly like the other dozen unrelated cities: it
resolves because Irvine, CA is a real public-dataset city.

End-to-end, a "within N km of <real place>" query resolves the anchor through the
geocoder seam and filters entities by radius (reuses #148's spatial fast-path
scaffolding, but with a REAL anchor resolved from the bundled dataset).
"""

from __future__ import annotations

import math
import time

import pytest

from cograph_client.graph.queries import kg_graph_uri
from cograph_client.nlp.pipeline import NLQueryPipeline
from cograph_client.spatiotemporal.geocoder import (
    GeoNamesGeocoder,
    get_geocoder,
    reset_geocoder,
)
from cograph_client.spatiotemporal.memory import InMemorySpatioTemporalIndex
from cograph_client.spatiotemporal.protocol import SpatioTemporalFact
from cograph_client.spatiotemporal.registry import (
    register_spatiotemporal_index,
    reset_spatiotemporal_index,
)

TENANT = "geo-tenant"
KG = "GeoKG"


@pytest.fixture(autouse=True)
def _reset():
    reset_spatiotemporal_index()
    reset_geocoder()
    yield
    reset_spatiotemporal_index()
    reset_geocoder()


def _geo() -> GeoNamesGeocoder:
    g = get_geocoder()
    assert isinstance(g, GeoNamesGeocoder)  # confirms it's the general default
    return g


# --------------------------------------------------------------------------- #
# Anti-overfitting: a broad spread of unrelated real places must resolve to
# plausible coords. This asserts the GENERAL dataset works — not one entry.
# --------------------------------------------------------------------------- #
# (place, approx_lon, approx_lat) — hand-picked to span every US region AND
# multiple continents so no single locale/persona is favoured. Coordinates are
# the well-known city centroids; we assert within ~0.5deg of them (the exact
# GeoNames centroid differs slightly but must be in the right place on Earth).
_BROAD_SAMPLE = [
    # US, spread coast-to-coast + interior
    ("Seattle, WA", -122.33, 47.61),
    ("Denver, CO", -104.99, 39.74),
    ("Austin, TX", -97.74, 30.27),
    ("Miami, FL", -80.19, 25.77),
    ("Boston, MA", -71.06, 42.36),
    ("Irvine, CA", -117.82, 33.67),   # the eval anchor — just one of many
    ("Minneapolis, MN", -93.27, 44.98),
    # World spread — six continents
    ("Toronto", -79.38, 43.65),
    ("Mexico City", -99.13, 19.43),
    ("London, GB", -0.13, 51.51),
    ("Berlin", 13.40, 52.52),
    ("Cairo", 31.24, 30.04),
    ("Mumbai", 72.88, 19.07),
    ("Sydney", 151.21, -33.87),
    ("Buenos Aires", -58.38, -34.60),
    ("Nairobi", 36.82, -1.29),
    ("Tokyo", 139.69, 35.69),
]


@pytest.mark.parametrize("place,exp_lon,exp_lat", _BROAD_SAMPLE)
async def test_broad_sample_of_real_places_resolves(place, exp_lon, exp_lat):
    coords = await _geo().geocode(place)
    assert coords is not None, f"{place!r} should resolve from the general gazetteer"
    lon, lat = coords
    assert -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0
    assert abs(lon - exp_lon) < 0.5, f"{place}: lon {lon} far from {exp_lon}"
    assert abs(lat - exp_lat) < 0.5, f"{place}: lat {lat} far from {exp_lat}"


async def test_sample_size_confirms_dataset_is_large_and_general():
    """Sanity: the bundled dataset is a big general gazetteer, not a short list.

    Resolving a large, diverse set of cities that no one hand-added here proves
    generality. If someone swapped the bundle for a tiny curated list, this fails.
    """
    g = _geo()
    # A pile of cities NONE of which are referenced anywhere else in these tests.
    diverse = [
        "Reykjavik", "Lagos", "Jakarta", "Lima", "Warsaw", "Bogota", "Chennai",
        "Osaka", "Melbourne", "Cape Town", "Dublin", "Vienna", "Manila",
        "Santiago", "Kraków", "Portland, OR", "Fresno, CA", "Tulsa, OK",
        "Spokane, WA", "Chattanooga, TN",
    ]
    resolved = 0
    for name in diverse:
        if await g.geocode(name) is not None:
            resolved += 1
    # Expect essentially all of these well-known cities to resolve.
    assert resolved >= len(diverse) - 1, f"only {resolved}/{len(diverse)} resolved"


# --------------------------------------------------------------------------- #
# "City, ST" disambiguation + normalization
# --------------------------------------------------------------------------- #
async def test_city_state_disambiguation():
    """Same city name, different states → different coords picked by the qualifier."""
    g = _geo()
    portland_or = await g.geocode("Portland, OR")
    portland_me = await g.geocode("Portland, ME")
    assert portland_or is not None and portland_me is not None
    assert portland_or != portland_me
    # OR Portland is west (~-122 lon), ME Portland is east (~-70 lon).
    assert portland_or[0] < -120 and portland_me[0] > -75
    # Full state name resolves the same as the code.
    assert await g.geocode("Portland, Oregon") == portland_or


async def test_country_disambiguation():
    """'London, GB' vs 'London, CA' (Canada) resolve to different continents."""
    g = _geo()
    london_gb = await g.geocode("London, GB")
    london_ca = await g.geocode("London, Canada")
    assert london_gb is not None and london_ca is not None
    assert london_gb != london_ca
    # UK London near prime meridian; Canadian London near -81 lon.
    assert abs(london_gb[0]) < 1.0
    assert london_ca[0] < -80


async def test_bare_name_picks_most_populous():
    """A bare ambiguous name deterministically resolves to the biggest city.

    'London' (no qualifier) → London, England (8.9M), not London, Ontario.
    'Paris' → Paris, France, not Paris, TX. This is the deterministic default.
    """
    g = _geo()
    london = await g.geocode("London")
    london_gb = await g.geocode("London, GB")
    assert london == london_gb  # biggest London is the GB one
    paris = await g.geocode("Paris")
    paris_fr = await g.geocode("Paris, France")
    assert paris == paris_fr


async def test_casing_and_whitespace_normalization():
    g = _geo()
    canonical = await g.geocode("San Francisco")
    assert canonical is not None
    for variant in ("san francisco", "  San   Francisco  ", "SAN FRANCISCO", "San Francisco."):
        assert await g.geocode(variant) == canonical


async def test_diacritic_folding():
    """A query typed without accents still hits the accented dataset row."""
    g = _geo()
    with_accent = await g.geocode("Zürich")
    without_accent = await g.geocode("Zurich")
    assert with_accent is not None
    assert with_accent == without_accent


async def test_determinism_across_repeated_calls():
    """Same input → identical output every time (no nondeterministic tie-break)."""
    g = _geo()
    first = await g.geocode("Springfield")  # many Springfields; must be stable
    for _ in range(10):
        assert await g.geocode("Springfield") == first


# --------------------------------------------------------------------------- #
# Clean misses — unknown/garbage → None, never a crash or a wrong coord.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "Totally Invented Nowhere Place 99999",
        "asdkjfhqwoieur",
        "Irvine, ZZ",       # real city, impossible qualifier → no wrong coord
        "Irvine, Atlantis",
        ",",
        "123456",
    ],
)
async def test_unknown_or_garbage_returns_none(bad):
    assert await _geo().geocode(bad) is None


async def test_non_string_input_is_safe():
    g = _geo()
    for bad in (None, 42, 3.14, ["Irvine"], {"city": "Irvine"}):
        # geocode declares str, but must not crash on bad callers.
        assert await g.geocode(bad) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# End-to-end: "within N km of <real place>" resolves the anchor via the geocoder
# and filters entities by radius. Uses the general default (no invented gazetteer).
# --------------------------------------------------------------------------- #
def _pipe(neptune=None):
    class _NoNeptune:
        """Anchor-via-Neptune always misses → forces the free-text geocoder path."""
        async def query(self, sparql):
            return {"head": {"vars": []}, "results": {"bindings": []}}

    return NLQueryPipeline(neptune or _NoNeptune(), anthropic_key="dummy")


def _fact(uri, lon, lat, *, label, typ="Clinic"):
    return SpatioTemporalFact(
        entity_uri=uri, tenant_id=TENANT, kg_name=KG, lon=lon, lat=lat,
        valid_from=None, valid_to=None, attrs={"label": label, "type": typ},
    )


def _intent(**over):
    base = {
        "is_spatial": True, "kind": "radius",
        "anchor_lon": None, "anchor_lat": None, "anchor_description": None,
        "radius_m": None, "bbox": None, "target_type": None,
        "as_of": None, "time_from": None, "time_to": None,
    }
    base.update(over)
    return base


async def _run(pipe, question, intent_json):
    async def fake_detect(q, onto):
        return intent_json
    pipe._detect_spatial_intent = fake_detect  # type: ignore[assignment]
    dg = kg_graph_uri(TENANT, KG)
    return await pipe._try_spatial_fast_path(question, "onto", dg, {}, time.time())


def _offset_km(lon, lat, dx_km, dy_km):
    """Approx offset a WGS84 point by (dx_km east, dy_km north)."""
    dlat = dy_km / 111.0
    dlon = dx_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
    return (lon + dlon, lat + dlat)


async def test_within_radius_of_real_place_filters_by_distance():
    """The money test, with a REAL anchor: 'within 25 km of Irvine' resolves
    Irvine from the general dataset and returns only entities inside the radius.

    This is the anti-overfit e2e: nothing seeded 'irvine→coords' — Irvine comes
    from the public gazetteer, and the radius math filters real placed entities.
    """
    # Anchor Irvine, CA resolved via the DEFAULT general geocoder (not registered).
    anchor = await _geo().geocode("Irvine, CA")
    assert anchor is not None
    a_lon, a_lat = anchor

    idx = InMemorySpatioTemporalIndex()
    register_spatiotemporal_index(idx)
    # Two entities ~2 km and ~10 km from the anchor → INSIDE a 25 km radius;
    # one ~60 km away → OUTSIDE.
    near_lon, near_lat = _offset_km(a_lon, a_lat, 2.0, 0.0)
    mid_lon, mid_lat = _offset_km(a_lon, a_lat, 0.0, 10.0)
    far_lon, far_lat = _offset_km(a_lon, a_lat, 60.0, 0.0)
    await idx.upsert_many([
        _fact("e:near", near_lon, near_lat, label="Near Clinic"),
        _fact("e:mid", mid_lon, mid_lat, label="Mid Clinic"),
        _fact("e:far", far_lon, far_lat, label="Far Clinic"),
    ])

    pipe = _pipe()  # Neptune anchor lookup always misses → geocoder resolves Irvine
    res = await _run(
        pipe, "clinics within 25 km of Irvine",
        _intent(anchor_description="Irvine", radius_m=25_000, target_type="Clinic"),
    )
    assert res is not None
    assert res.sparql == ""  # answered straight from the index
    assert "Near Clinic" in res.answer
    assert "Mid Clinic" in res.answer
    assert "Far Clinic" not in res.answer  # excluded by distance


async def test_within_radius_unknown_place_falls_through():
    """A radius anchor the general geocoder can't resolve → clean fall-through."""
    idx = InMemorySpatioTemporalIndex()
    register_spatiotemporal_index(idx)
    pipe = _pipe()
    res = await _run(
        pipe, "clinics within 10 km of Xyzzynowhereville",
        _intent(anchor_description="Xyzzynowhereville", radius_m=10_000),
    )
    assert res is None  # graceful fall-through to SPARQL, not a crash
