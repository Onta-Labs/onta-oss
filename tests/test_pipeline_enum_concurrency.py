"""COG-58: enum-discovery concurrency is bounded.

`_fetch_ontology` discovers enumerated values by firing one COUNT(DISTINCT)
query per attribute + per relationship. An unbounded ``asyncio.gather`` meant a
wide table (hundreds of columns → hundreds of attributes) launched O(columns)
simultaneous queries, throttling serverless Neptune. The discovery is now
gated by a semaphore; these tests assert the in-flight query count never
exceeds the cap, while staying genuinely concurrent (not serialized).
"""

import asyncio

import pytest

from cograph_client.nlp import pipeline as pl
from cograph_client.nlp.pipeline import NLQueryPipeline


def _ontology_raw(n_types: int, attrs_per_type: int) -> dict:
    """A full-ontology query result in SPARQL JSON format, with string
    attributes (so the low-cardinality value-fetch phase also runs)."""
    bindings = []
    for t in range(n_types):
        for a in range(attrs_per_type):
            bindings.append({
                "typeLabel": {"value": f"Type{t}"},
                "attrLabel": {"value": f"attr{a}"},
                "range": {"value": "http://www.w3.org/2001/XMLSchema#string"},
            })
    return {
        "head": {"vars": ["typeLabel", "attrLabel", "range", "funcName"]},
        "results": {"bindings": bindings},
    }


class _ConcurrencyTrackingNeptune:
    """Stub Neptune whose ``query`` records peak concurrent in-flight calls for
    the enum-discovery queries (COUNT/value fetches)."""

    def __init__(self, ontology_raw: dict):
        self._ontology = ontology_raw
        self.current = 0
        self.peak = 0

    async def _tracked(self, payload):
        self.current += 1
        self.peak = max(self.peak, self.current)
        # Hold the slot open long enough for siblings to pile up if uncapped.
        await asyncio.sleep(0.01)
        self.current -= 1
        return payload

    async def query(self, q: str):
        if "COUNT(DISTINCT" in q:
            return await self._tracked(
                {"head": {"vars": ["cnt"]},
                 "results": {"bindings": [{"cnt": {"value": "3"}}]}}
            )
        if "SELECT DISTINCT ?val" in q:
            return await self._tracked(
                {"head": {"vars": ["val"]},
                 "results": {"bindings": [
                     {"val": {"value": "a"}}, {"val": {"value": "b"}},
                 ]}}
            )
        # The full-ontology query (not concurrency-tracked).
        return self._ontology


@pytest.mark.asyncio
async def test_enum_discovery_concurrency_is_capped(monkeypatch):
    monkeypatch.setattr(pl, "MAX_ENUM_DISCOVERY_CONCURRENCY", 4)
    pl._ontology_cache.clear()
    # 10 types × 6 attrs = 60 attribute queries — far above the cap of 4, so an
    # unbounded gather would show a peak of ~60.
    neptune = _ConcurrencyTrackingNeptune(_ontology_raw(10, 6))
    pipeline = NLQueryPipeline(neptune, anthropic_key="test")

    summary = await pipeline._fetch_ontology("g", instance_graph="g")

    assert "Type0" in summary  # discovery ran and produced a summary
    assert neptune.peak <= 4, f"peak {neptune.peak} exceeded the cap"
    assert neptune.peak > 1, "discovery should still run concurrently, not serially"


@pytest.mark.asyncio
async def test_enum_discovery_peak_independent_of_column_count(monkeypatch):
    """The whole point of COG-58: doubling the attribute count must NOT raise
    peak concurrency."""
    monkeypatch.setattr(pl, "MAX_ENUM_DISCOVERY_CONCURRENCY", 5)

    async def peak_for(n_types: int, attrs: int) -> int:
        pl._ontology_cache.clear()
        neptune = _ConcurrencyTrackingNeptune(_ontology_raw(n_types, attrs))
        pipeline = NLQueryPipeline(neptune, anthropic_key="test")
        await pipeline._fetch_ontology("g", instance_graph="g")
        return neptune.peak

    small = await peak_for(5, 4)    # 20 attrs
    large = await peak_for(20, 10)  # 200 attrs
    assert small <= 5 and large <= 5
