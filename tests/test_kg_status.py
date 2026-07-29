"""Unit tests for the shared KG existence/emptiness probe (ONTA-413).

The probe is what lets every read rail tell three states apart that a bare
SPARQL query cannot: missing KG, registered-but-empty KG, and a KG with data
whose query simply matched nothing. Its caching rules are load-bearing, so they
are pinned here rather than only exercised through the routes.
"""

from __future__ import annotations

import pytest

from cograph_client.graph.kg_status import (
    KG_EMPTY,
    KG_MISSING,
    KG_OK,
    empty_kg_message,
    invalidate_kg_status,
    kg_data_status,
    list_kg_names,
    missing_kg_message,
)

TENANT = "t-probe"


class ProbeNeptune:
    def __init__(self, *, registered: bool, has_data: bool, names=()):
        self.registered = registered
        self.has_data = has_data
        self.names = list(names)
        self.asks: list[str] = []
        self.queries: list[str] = []

    async def ask(self, sparql: str) -> bool:
        self.asks.append(sparql)
        return self.registered if "/kg_name>" in sparql else self.has_data

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        return {
            "head": {"vars": ["name"]},
            "results": {
                "bindings": [
                    {"name": {"type": "literal", "value": n}} for n in self.names
                ]
            },
        }


@pytest.fixture(autouse=True)
def _clean_cache():
    invalidate_kg_status(TENANT)
    yield
    invalidate_kg_status(TENANT)


@pytest.mark.asyncio
async def test_missing_when_neither_registered_nor_populated():
    n = ProbeNeptune(registered=False, has_data=False)
    assert await kg_data_status(n, TENANT, "nope") == KG_MISSING


@pytest.mark.asyncio
async def test_empty_when_registered_but_no_triples():
    n = ProbeNeptune(registered=True, has_data=False)
    assert await kg_data_status(n, TENANT, "fresh") == KG_EMPTY


@pytest.mark.asyncio
async def test_ok_when_populated():
    n = ProbeNeptune(registered=True, has_data=True)
    assert await kg_data_status(n, TENANT, "imdb") == KG_OK


@pytest.mark.asyncio
async def test_populated_but_unregistered_is_ok_not_missing():
    """Legacy graphs predate ``ensure_kg_registered``; data wins over the record."""
    n = ProbeNeptune(registered=False, has_data=True)
    assert await kg_data_status(n, TENANT, "legacy") == KG_OK


@pytest.mark.asyncio
async def test_probe_costs_exactly_two_asks():
    n = ProbeNeptune(registered=True, has_data=True)
    await kg_data_status(n, TENANT, "imdb")
    assert len(n.asks) == 2
    # And neither is a COUNT scan (knowledge_graphs._live_triple_count is
    # explicitly forbidden on this hot path).
    assert not any("COUNT" in q for q in n.asks)


@pytest.mark.asyncio
async def test_positive_verdict_is_cached():
    n = ProbeNeptune(registered=True, has_data=True)
    assert await kg_data_status(n, TENANT, "imdb") == KG_OK
    assert await kg_data_status(n, TENANT, "imdb") == KG_OK
    assert len(n.asks) == 2  # second call served from cache


@pytest.mark.asyncio
async def test_missing_verdict_is_never_cached():
    """create-KG-then-immediately-ask is the flow a cached negative would break."""
    n = ProbeNeptune(registered=False, has_data=False)
    assert await kg_data_status(n, TENANT, "brand-new") == KG_MISSING
    # ... the caller creates + ingests ...
    n.registered = True
    n.has_data = True
    assert await kg_data_status(n, TENANT, "brand-new") == KG_OK


@pytest.mark.asyncio
async def test_empty_verdict_is_never_cached():
    """ingest-then-immediately-ask must see the data on the very next turn."""
    n = ProbeNeptune(registered=True, has_data=False)
    assert await kg_data_status(n, TENANT, "fresh") == KG_EMPTY
    n.has_data = True
    assert await kg_data_status(n, TENANT, "fresh") == KG_OK


@pytest.mark.asyncio
async def test_probe_failure_fails_open():
    """A transient backend error must never become "your graph does not exist"."""

    class Broken(ProbeNeptune):
        async def ask(self, sparql: str) -> bool:
            raise RuntimeError("throttled")

    assert await kg_data_status(Broken(registered=True, has_data=True), TENANT, "x") == KG_OK


@pytest.mark.asyncio
async def test_blank_kg_name_short_circuits_to_ok():
    n = ProbeNeptune(registered=False, has_data=False)
    assert await kg_data_status(n, TENANT, "") == KG_OK
    assert n.asks == []


@pytest.mark.asyncio
async def test_invalid_kg_name_raises_before_any_query():
    """ONTA-414: fail closed rather than interpolate a hostile name into SPARQL."""
    from cograph_client.graph.queries import InvalidKGName

    n = ProbeNeptune(registered=True, has_data=True)
    with pytest.raises(InvalidKGName):
        await kg_data_status(n, TENANT, "kg> FROM <https://cograph.tech/graphs/victim")
    assert n.asks == []


@pytest.mark.asyncio
async def test_list_kg_names_projects_names_only():
    n = ProbeNeptune(registered=True, has_data=True, names=["imdb", "events", "imdb"])
    assert await list_kg_names(n, TENANT) == ["imdb", "events"]
    assert "LIMIT" in n.queries[0]
    assert "kg_triple_count" not in n.queries[0]


@pytest.mark.asyncio
async def test_list_kg_names_is_best_effort():
    class Broken(ProbeNeptune):
        async def query(self, sparql: str) -> dict:
            raise RuntimeError("down")

    assert await list_kg_names(Broken(registered=True, has_data=True), TENANT) == []


def test_messages_name_the_kg_and_the_alternatives():
    msg = missing_kg_message("typo", ["imdb", "events"])
    assert "typo" in msg and "imdb" in msg and "events" in msg
    assert "imdb" not in missing_kg_message("typo", [])
    assert "typo" in missing_kg_message("typo", [])
    assert "fresh" in empty_kg_message("fresh")
