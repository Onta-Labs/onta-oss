"""ONTA-534: a ``notify`` watch snapshots from GraphStore, not retired SPARQL.

``snapshot_watch`` is the read half of a standing alert. It used to run ONE
SPARQL ``SELECT``; under the shipped Neo4j GraphStore ``NeptuneClient.query``
raises ``SparqlClientRetired`` unconditionally, the module's ``except`` turned
that into ``{}``, and :func:`diff_snapshots` reads ``{}`` as "couldn't read this
fire" → no changes. A user set a watch, got a 200, and it could never fire —
silently and indefinitely.

These tests pin both arms: the GraphStore arm (real values out of a seeded
``MemoryGraphStore`` with SPARQL wired to explode) and the residual SPARQL arm
(still consulted when the store has nothing to say). They also pin the
DIRECTION the module deliberately fails in — an unreadable snapshot yields
``{}`` and fires nothing, and a partially-readable one declines whole rather
than persisting a baseline with holes in it.

Anti-overfit: synthetic type / attribute / entity names only.
"""

from __future__ import annotations

import pytest

from infona_client.graph.client import SparqlClientRetired
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import delete_facts, insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import attr_uri, entity_uri
from infona_client.scheduling.watch import diff_snapshots, snapshot_watch

pytestmark = pytest.mark.asyncio

TENANT = "watch-tenant"
KG_NAME = "watch-kg"
KG_GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG_NAME}"
TYPES = f"{IRI_BASE}/types/"
ONTO = f"{IRI_BASE}/onto/"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Synthetic vocabulary — no warehouse / persona / benchmark nouns.
T_GADGET = "WatchGadget"
T_DEPOT = "WatchDepot"
A_TALLY = "watch_tally"
R_HOUSED_IN = "watch_housed_in"

GADGET_URI = entity_uri(T_GADGET, "g1")
DEPOT_URI = entity_uri(T_DEPOT, "d1")
TALLY_PRED = attr_uri(T_GADGET, A_TALLY)
HOUSED_PRED = f"{ONTO}{R_HOUSED_IN}"


class RetiredSparqlNeptune:
    """Stands in for the shipped client: every SPARQL read is retired."""

    def __init__(self) -> None:
        self.calls = 0

    async def query(self, sparql: str):
        self.calls += 1
        raise SparqlClientRetired(
            "SPARQL HTTP client is retired under Neo4j GraphStore (ONTA-534)."
        )


class ScriptedNeptune:
    """Residual SPARQL arm: answers the watch SELECT from a ``{key: value}`` map."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.calls = 0

    async def query(self, sparql: str):
        self.calls += 1
        return {
            "head": {"vars": ["key", "value"]},
            "results": {
                "bindings": [
                    {
                        "key": {"type": "literal", "value": k},
                        "value": {"type": "literal", "value": v},
                    }
                    for k, v in self.values.items()
                ]
            },
        }


async def _seed(store: MemoryGraphStore, *, tally: str) -> None:
    """One gadget with a literal tally and a relationship to one depot."""
    triples = [
        (DEPOT_URI, RDF_TYPE, f"{TYPES}{T_DEPOT}"),
        (DEPOT_URI, RDFS_LABEL, "Depot One"),
        (GADGET_URI, RDF_TYPE, f"{TYPES}{T_GADGET}"),
        (GADGET_URI, RDFS_LABEL, "Gadget One"),
        (GADGET_URI, TALLY_PRED, tally),
        (GADGET_URI, HOUSED_PRED, DEPOT_URI),
    ]
    await insert_facts(None, KG_GRAPH, triples, store=store)


def _cells_watch() -> dict:
    return {
        "cells": [
            {"key": "tally", "subject": GADGET_URI, "predicate": TALLY_PRED},
            {"key": "depot", "subject": GADGET_URI, "predicate": HOUSED_PRED},
        ]
    }


# --- GraphStore arm ---------------------------------------------------------


async def test_cells_snapshot_reads_store_when_sparql_is_retired():
    """The regression: on ``main`` this returned ``{}`` and the alert never fired."""
    store = MemoryGraphStore()
    await _seed(store, tally="7")
    neptune = RetiredSparqlNeptune()

    snap = await snapshot_watch(neptune, _cells_watch(), KG_GRAPH, store=store)

    assert snap["tally"] == "7"
    assert snap["depot"] == DEPOT_URI
    # The store answered, so the retired arm was never consulted.
    assert neptune.calls == 0


async def test_watched_value_change_is_detected_end_to_end():
    """Snapshot → diff → change, with SPARQL retired the whole time."""
    store = MemoryGraphStore()
    await _seed(store, tally="7")
    neptune = RetiredSparqlNeptune()

    baseline = await snapshot_watch(neptune, _cells_watch(), KG_GRAPH, store=store)
    assert diff_snapshots(None, baseline) == []  # first fire only establishes it

    # Replace the value the way the shared write path does (clear, then write).
    await delete_facts(
        None, KG_GRAPH, triples=[(GADGET_URI, TALLY_PRED, None)], store=store
    )
    await insert_facts(None, KG_GRAPH, [(GADGET_URI, TALLY_PRED, "9")], store=store)
    fresh = await snapshot_watch(neptune, _cells_watch(), KG_GRAPH, store=store)

    changes = diff_snapshots(baseline, fresh)
    assert changes == [
        {"key": "tally", "old": "7", "new": "9", "change": "changed"}
    ]


async def test_rdfs_label_cell_resolves_to_the_name_property():
    """``rdfs:label`` is stored as the Entity display name, not an ordinary prop."""
    store = MemoryGraphStore()
    await _seed(store, tally="7")
    watch = {
        "cells": [
            {"key": "title", "subject": GADGET_URI, "predicate": RDFS_LABEL}
        ]
    }

    snap = await snapshot_watch(RetiredSparqlNeptune(), watch, KG_GRAPH, store=store)

    assert snap == {"title": "Gadget One"}


# --- Residual SPARQL arm ----------------------------------------------------


async def test_sparql_arm_still_answers_when_the_store_declines():
    """A graph URI that is not a per-KG instance graph keeps the authored arm."""
    neptune = ScriptedNeptune({"tally": "7"})

    snap = await snapshot_watch(neptune, _cells_watch(), "https://example.invalid/g")

    assert snap == {"tally": "7"}
    assert neptune.calls == 1


async def test_raw_sparql_watch_never_takes_the_store_arm():
    """``watch['sparql']`` is authored SPARQL; the store must not reinterpret it."""
    store = MemoryGraphStore()
    await _seed(store, tally="7")
    neptune = ScriptedNeptune({"tally": "42"})

    snap = await snapshot_watch(
        neptune,
        {"sparql": "SELECT ?key ?value WHERE { ?key ?p ?value }"},
        KG_GRAPH,
        store=store,
    )

    assert snap == {"tally": "42"}
    assert neptune.calls == 1


# --- Preserved failure direction: never fire a false alarm -------------------


async def test_unreadable_snapshot_still_yields_no_changes():
    """A raw-SPARQL watch has no store port: it degrades to ``{}``, not an alarm."""
    neptune = RetiredSparqlNeptune()

    snap = await snapshot_watch(
        neptune,
        {"sparql": "SELECT ?key ?value WHERE { ?key ?p ?value }"},
        KG_GRAPH,
    )

    assert snap == {}
    assert diff_snapshots({"tally": "7"}, snap) == []


async def test_partial_store_read_declines_instead_of_persisting_holes():
    """One failing subject must not yield a baseline missing that subject's keys.

    A partial map would be persisted as the next baseline; the missing keys would
    then come back as ``added`` on a later fire — a false alarm one tick later.
    """
    store = MemoryGraphStore()
    await _seed(store, tally="7")
    other = entity_uri(T_GADGET, "g2")

    class HalfBrokenStore:
        def __init__(self, inner):
            self._inner = inner

        def session(self, scope):
            inner_session = self._inner.session(scope)

            class _Session:
                scope = inner_session.scope

                async def execute_template(self, name, params=None):
                    if (params or {}).get("id") == other:
                        raise RuntimeError("simulated store read failure")
                    return await inner_session.execute_template(name, params)

            return _Session()

    watch = {
        "cells": [
            {"key": "tally", "subject": GADGET_URI, "predicate": TALLY_PRED},
            {"key": "other_tally", "subject": other, "predicate": TALLY_PRED},
        ]
    }
    neptune = RetiredSparqlNeptune()

    snap = await snapshot_watch(neptune, watch, KG_GRAPH, store=HalfBrokenStore(store))

    # Declined whole → the retired SPARQL arm ran → {} → no changes reported.
    assert snap == {}
    assert neptune.calls == 1
    assert diff_snapshots({"tally": "7", "other_tally": "1"}, snap) == []


async def test_missing_entity_declines_rather_than_reporting_an_empty_snapshot():
    """An entity the store has never seen is 'nothing to say', not 'value gone'."""
    store = MemoryGraphStore()
    neptune = RetiredSparqlNeptune()

    snap = await snapshot_watch(neptune, _cells_watch(), KG_GRAPH, store=store)

    assert snap == {}
    assert diff_snapshots({"tally": "7"}, snap) == []
