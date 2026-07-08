"""ONTA-249: geo-radius filtering — "within N km of PLACE".

The radius/bbox engine was fully built but (a) gated OFF by default and (b) could
only resolve an anchor that already existed as a KG entity with a geo:wktLiteral —
a bare place NAME never resolved, so the whole radius path fell through and the
user had to enumerate cities by hand.

Fix: spatial routing is now a supported, default-ENABLED path, and a free-text
geocoder SEAM (cograph_client.spatiotemporal.geocoder) turns a place name into
coords. OSS ships a deterministic offline gazetteer default; a premium geocoder
registers over it. These tests use an INVENTED gazetteer + invented entities so
nothing overfits: "within N km of <invented place>" returns entities inside the
radius and excludes those outside.
"""

from __future__ import annotations

import time

import pytest

from cograph_client.graph.queries import kg_graph_uri
from cograph_client.nlp.pipeline import NLQueryPipeline
from cograph_client.spatiotemporal.geocoder import (
    GazetteerGeocoder,
    GeoNamesGeocoder,
    get_geocoder,
    register_geocoder,
    reset_geocoder,
)
from cograph_client.spatiotemporal.memory import InMemorySpatioTemporalIndex
from cograph_client.spatiotemporal.protocol import SpatioTemporalFact
from cograph_client.spatiotemporal.registry import (
    register_spatiotemporal_index,
    reset_spatiotemporal_index,
)

TENANT = "inv-tenant"
KG = "InventedKG"

# Stand-in for a PREMIUM geocoder registered via the module:callable plugin hook.
_INVENTED_PREMIUM = GazetteerGeocoder({"Plugin Place": (1.0, 2.0)})


def _register_invented_geocoder():
    register_geocoder(_INVENTED_PREMIUM)

# An INVENTED gazetteer — a fictional place at a chosen point, plus a far-away one.
# ~0.01deg lon at this latitude ≈ 1.1 km; 1 deg lat ≈ 111 km. Coordinates chosen so
# the radius math is unambiguous, but the NAMES are invented (no persona tokens).
ZORP_CENTER = (10.0000, 45.0000)          # "Zorptown"
FARTOWN = (20.0000, 45.0000)              # "Fartown" — ~785 km east, far outside


@pytest.fixture(autouse=True)
def _reset():
    reset_spatiotemporal_index()
    reset_geocoder()
    yield
    reset_spatiotemporal_index()
    reset_geocoder()


def _pipe(neptune=None):
    class _NoNeptune:
        """Anchor-via-Neptune always misses, forcing the geocoder path."""
        async def query(self, sparql):
            return {"head": {"vars": []}, "results": {"bindings": []}}

    return NLQueryPipeline(neptune or _NoNeptune(), anthropic_key="dummy")


def _fact(uri, lon, lat, *, label, typ="Facility"):
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


# ------------------------------------------------------------------- geocoder seam
async def test_default_geocoder_is_general_and_deterministic():
    # The OSS default is now the GENERAL GeoNames gazetteer (~34k public cities),
    # not the tiny hand-list — a bare place name resolves because it's a real
    # place in public data, not because anyone seeded it.
    g = get_geocoder()
    assert isinstance(g, GeoNamesGeocoder)
    # Deterministic: two calls, identical result; unknown place → None.
    a = await g.geocode("San Francisco")
    b = await g.geocode("san francisco")
    assert a == b is not None
    assert await g.geocode("Totally Invented Nowhere Place") is None


async def test_register_geocoder_overrides_default():
    invented = GazetteerGeocoder({"Zorptown": ZORP_CENTER})
    register_geocoder(invented)
    assert get_geocoder() is invented
    assert await get_geocoder().geocode("Zorptown") == ZORP_CENTER
    reset_geocoder()
    assert isinstance(get_geocoder(), GeoNamesGeocoder)  # back to general default


# ----------------------------------------------------------- radius over place name
async def test_within_radius_of_place_name_filters_by_distance():
    """The money test: "within N km of <invented place>" resolves the anchor via
    the geocoder and returns ONLY entities inside the radius."""
    register_geocoder(GazetteerGeocoder({"Zorptown": ZORP_CENTER, "Fartown": FARTOWN}))
    idx = InMemorySpatioTemporalIndex()
    register_spatiotemporal_index(idx)
    await idx.upsert_many([
        # ~1.1 km east of Zorptown → INSIDE a 25 km radius
        _fact("e:near", ZORP_CENTER[0] + 0.014, ZORP_CENTER[1], label="Near Facility"),
        # right on the anchor → INSIDE
        _fact("e:onspot", ZORP_CENTER[0], ZORP_CENTER[1], label="On Spot"),
        # Fartown, ~785 km away → OUTSIDE
        _fact("e:far", FARTOWN[0], FARTOWN[1], label="Far Facility"),
    ])
    pipe = _pipe()
    # 25 miles ≈ 40233 m; the intent detector reports metres. Use 25 km = 25000 m.
    res = await _run(
        pipe, "facilities within 25 km of Zorptown",
        _intent(anchor_description="Zorptown", radius_m=25_000, target_type="Facility"),
    )
    assert res is not None
    assert res.sparql == ""  # answered from the index, no SPARQL
    assert "Near Facility" in res.answer
    assert "On Spot" in res.answer
    assert "Far Facility" not in res.answer  # excluded by distance


async def test_radius_place_name_no_kg_entity_still_resolves():
    """Even with NO KG entity for the anchor (Neptune returns nothing), the
    place name resolves through the geocoder — the pre-fix silent fall-through."""
    register_geocoder(GazetteerGeocoder({"Zorptown": ZORP_CENTER}))
    idx = InMemorySpatioTemporalIndex()
    register_spatiotemporal_index(idx)
    await idx.upsert_many([
        _fact("e:in", ZORP_CENTER[0] + 0.01, ZORP_CENTER[1], label="In Range"),
    ])
    pipe = _pipe()  # Neptune anchor lookup always misses
    res = await _run(
        pipe, "facilities within 10 km of Zorptown",
        _intent(anchor_description="Zorptown", radius_m=10_000),
    )
    assert res is not None and "In Range" in res.answer


async def test_unknown_place_falls_through_to_sparql():
    """A place the geocoder can't resolve → None → fall through (no crash)."""
    register_geocoder(GazetteerGeocoder({"Zorptown": ZORP_CENTER}))
    idx = InMemorySpatioTemporalIndex()
    register_spatiotemporal_index(idx)
    pipe = _pipe()
    res = await _run(
        pipe, "facilities within 10 km of Blorptopia",
        _intent(anchor_description="Blorptopia", radius_m=10_000),
    )
    assert res is None  # graceful fall-through, not an exception


def test_premium_geocoder_loads_via_plugin_spec(monkeypatch):
    """A premium geocoder registers through the standard module:callable startup
    hook (mirrors _load_enrichment_plugin), so no paid API is baked into OSS."""
    from cograph_client.api import app as app_mod
    from cograph_client.spatiotemporal.geocoder import get_geocoder

    # Point the plugin spec at THIS module's registrar (stands in for premium).
    monkeypatch.setattr(
        app_mod.settings, "geocoder_plugin",
        "tests.test_ask_geo_radius:_register_invented_geocoder", raising=False,
    )
    app_mod._load_geocoder_plugin()
    assert get_geocoder() is _INVENTED_PREMIUM


async def test_kg_entity_anchor_preferred_over_geocoder():
    """When a KG entity carries the anchor geometry, that wins over the geocoder
    (anchor pinned to the tenant's own data)."""
    class KGNeptune:
        async def query(self, sparql):
            if "wktLiteral" in sparql:
                lon, lat = ZORP_CENTER
                return {"head": {"vars": ["wkt"]},
                        "results": {"bindings": [{"wkt": {"value": f"POINT({lon} {lat})"}}]}}
            return {"head": {"vars": []}, "results": {"bindings": []}}

    # Register a geocoder that would resolve the name to a DIFFERENT, far point;
    # if the KG-entity path is preferred, that far point is never used.
    register_geocoder(GazetteerGeocoder({"Zorptown": FARTOWN}))
    idx = InMemorySpatioTemporalIndex()
    register_spatiotemporal_index(idx)
    await idx.upsert_many([
        _fact("e:near", ZORP_CENTER[0] + 0.01, ZORP_CENTER[1], label="Near KG Anchor"),
    ])
    pipe = _pipe(KGNeptune())
    res = await _run(
        pipe, "facilities within 5 km of Zorptown",
        _intent(anchor_description="Zorptown", radius_m=5_000),
    )
    # Resolved from the KG entity (ZORP_CENTER), so the nearby facility is inside.
    assert res is not None and "Near KG Anchor" in res.answer
