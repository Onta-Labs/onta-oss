"""ONTA-277 regression: opening a validity interval for a value CLEARS any prior
closure on that value's interval node, so a re-asserted (resurrected) value becomes
genuinely current again.

The bug: a validity node is keyed by ``sha1(subject|predicate|object)``
(``graph/validity.py::_interval_uri``). CLOSING a fact only ADDS ``val:validTo``
(+ ``val:supersededBy`` + ``val:status``) to that node; RE-ASSERTING the same value
later calls ``build_open_interval_triples``, which only appends ``val:validFrom`` and
never removes the stale ``val:validTo``. Because ``current_objects_query`` treats
"the node has ANY ``val:validTo``" as closed, the resurrected value was silently
excluded from the current-facts read — the load-bearing read P7 cites.

The fix routes a ``reopen_facts=`` clear through the shared write path
(``kg_writer.insert_facts`` → ``validity.reopen_interval_update``), invoked by every
mutation op that OPENS an interval for a now-current value. These tests exercise all
three shipped surfaces where that happens plus a load-bearing + targeting control.

pyoxigraph store shim copied from tests/test_supersession.py.
"""
from __future__ import annotations

import json

import pytest

from cograph_client.api_registry.spec import AuthorityLevel
from cograph_client.graph.kg_writer import insert_facts
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.validity import (
    VAL_VALID_TO,
    _interval_uri,
    current_objects_query,
    fetch_history,
    validity_graph_uri,
)
from cograph_client.pipeline.conflict import REASON_CONFIDENCE
from cograph_client.pipeline.corrections import UserAssertion, apply_user_assertion
from cograph_client.pipeline.mutations import (
    supersede_fact,
    write_with_conflict_resolution,
)

TENANT, KG = "onta277res", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
ACME = "https://cograph.tech/entities/Company/acme"
HAS_CEO = "https://cograph.tech/onto/hasCEO"
PHONE = "https://cograph.tech/types/Company/attrs/phone"
REVENUE = "https://cograph.tech/onto/revenue"
REV_10M, REV_12M = "10000000", "12000000"


pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence the shared refresh_after_write internals (cache-invalidate / embed /
    stats recompute) so these tests isolate the reopen mechanism — exactly as
    tests/test_supersession.py does. The op STILL calls refresh_after_write."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


async def _current(n: PyoxiNeptune, subject: str, predicate: str) -> set[str]:
    """The "current facts" projection — objects with no CLOSED validity interval."""
    raw = await n.query(current_objects_query(INSTANCE_GRAPH, subject, predicate))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


async def _node_has_valid_to(
    n: PyoxiNeptune, subject: str, predicate: str, obj: str
) -> bool:
    """True iff the ``(subject, predicate, obj)`` interval node still carries a
    ``val:validTo`` (a stale closure) in the companion validity graph."""
    node = _interval_uri(subject, predicate, obj)
    val_graph = validity_graph_uri(INSTANCE_GRAPH)
    raw = await n.query(
        f"SELECT ?vt WHERE {{ GRAPH <{val_graph}> {{ <{node}> <{VAL_VALID_TO}> ?vt }} }}"
    )
    return len(raw["results"]["bindings"]) > 0


async def _seed_open_fact(n: PyoxiNeptune, subject: str, predicate: str, value: str):
    """Seed an initial current fact via the shared insert primitive (no closure)."""
    await insert_facts(
        n,
        INSTANCE_GRAPH,
        [
            (subject, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
             "https://cograph.tech/types/Company"),
            (subject, predicate, value),
        ],
    )


# --------------------------------------------------------------------------- #
# 1. supersede_fact A → B → A resurrection
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_supersede_resurrection_makes_value_current_again():
    """A → B → A: after re-asserting Alice she is current again; history still holds
    both values, with Alice's node OPEN (its prior closure cleared) and Bob's node
    CLOSED (superseded by the re-asserted Alice)."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")
    assert await _current(n, ACME, HAS_CEO) == {"Alice"}

    await supersede_fact(n, INSTANCE_GRAPH, subject=ACME, predicate=HAS_CEO,
                         new_value="Bob", type_name="Company", run_id="r1")
    assert await _current(n, ACME, HAS_CEO) == {"Bob"}

    await supersede_fact(n, INSTANCE_GRAPH, subject=ACME, predicate=HAS_CEO,
                         new_value="Alice", type_name="Company", run_id="r2")

    # The resurrection sticks: Alice is current again (pre-fix this was set()).
    assert await _current(n, ACME, HAS_CEO) == {"Alice"}

    # History still tells the full story: both values present; Alice's node ends
    # OPEN, Bob's node ends CLOSED (superseded by the re-asserted Alice).
    history = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, HAS_CEO)}
    assert set(history) == {"Alice", "Bob"}, "both values must remain queryable"
    assert history["Alice"].is_current and not history["Alice"].valid_to, "Alice re-opened"
    assert not history["Bob"].is_current and history["Bob"].valid_to, "Bob closed"
    assert history["Bob"].superseded_by, "Bob points at its replacement (the re-asserted Alice)"


# --------------------------------------------------------------------------- #
# 2. A10 user-correction revert 111 → 222 → 111 (the trust case)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a10_revert_through_corrections_path_sticks():
    """apply_user_assertion 111 → 222 → 111: the revert to the original value is the
    exact A10 trust case (a user corrects, then reverts, their own value). It must
    stick — current = {111}, not the silently-empty pre-fix result."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, PHONE, "111")
    assert await _current(n, ACME, PHONE) == {"111"}

    await apply_user_assertion(
        n, INSTANCE_GRAPH,
        UserAssertion(predicate=PHONE, value="222", subject=ACME, actor="user-42"),
        run_id="fix-1",
    )
    assert await _current(n, ACME, PHONE) == {"222"}

    receipt = await apply_user_assertion(
        n, INSTANCE_GRAPH,
        UserAssertion(predicate=PHONE, value="111", subject=ACME, actor="user-42"),
        run_id="fix-2",
    )

    # The revert sticks: the value the user restored is current (pre-fix: set()).
    assert await _current(n, ACME, PHONE) == {"111"}
    assert receipt.superseded == ((ACME, PHONE, "222"),), "the intermediate 222 is retired"

    history = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, PHONE)}
    assert set(history) == {"111", "222"}
    assert history["111"].is_current, "the reverted value is current"
    assert not history["222"].is_current, "the intermediate value is closed"


# --------------------------------------------------------------------------- #
# 3. Conflict oscillation 10M → 12M → 10M (steady-state of a P8 refresh loop)
# --------------------------------------------------------------------------- #
async def _seed_conflict_fact(
    n: PyoxiNeptune, value: str, *, authority: AuthorityLevel, confidence: float
) -> None:
    await insert_facts(
        n, INSTANCE_GRAPH,
        [(ACME, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
          "https://cograph.tech/types/Company")],
    )
    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH, subject=ACME, predicate=REVENUE, type_name="Company",
        value=value, authority=authority, confidence=confidence, source="A", run_id="seed",
    )


@pytest.mark.asyncio
async def test_conflict_oscillation_winner_matches_current():
    """10M → 12M → 10M: a conflict oscillation (the steady state of a refresh loop
    that keeps re-picking a value that previously lost). The final winner 10M must be
    both the receipt's reported winner AND the current fact — pre-fix the receipt
    said 10M won while current() returned set()."""
    n = PyoxiNeptune()
    # 1. 10M lands (authoritative, 0.8).
    await _seed_conflict_fact(n, REV_10M, authority=AuthorityLevel.authoritative, confidence=0.8)
    assert await _current(n, ACME, REVENUE) == {REV_10M}

    # 2. 12M wins on authority (source_of_truth > authoritative). 10M → deprecated.
    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH, subject=ACME, predicate=REVENUE, type_name="Company",
        value=REV_12M, authority=AuthorityLevel.source_of_truth, confidence=0.9,
        source="B", run_id="osc-2",
    )
    assert await _current(n, ACME, REVENUE) == {REV_12M}

    # 3. 10M wins AGAIN on confidence (equal authority, 0.95 > 0.9) — its interval
    #    must be re-opened (prior deprecation cleared) so it is current once more.
    receipt = await write_with_conflict_resolution(
        n, INSTANCE_GRAPH, subject=ACME, predicate=REVENUE, type_name="Company",
        value=REV_10M, authority=AuthorityLevel.source_of_truth, confidence=0.95,
        source="C", run_id="osc-3",
    )

    assert receipt.conflict is True and receipt.reason == REASON_CONFIDENCE
    assert receipt.winner == (ACME, REVENUE, REV_10M)
    # The receipt's winner and the current-facts read now AGREE (the bug was that
    # the receipt reported 10M while the current read returned set()).
    current = await _current(n, ACME, REVENUE)
    assert current == {REV_10M} == {receipt.winner[2]}
    assert receipt.deprecated == ((ACME, REVENUE, REV_12M),), "12M is the deprecated loser"

    history = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, REVENUE)}
    assert history[REV_10M].is_current and not history[REV_12M].is_current


# --------------------------------------------------------------------------- #
# 4. Load-bearing + targeting control
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reopen_clears_only_the_resurrected_nodes_closure():
    """Proves the fix is load-bearing AND targeted. After A → B → A:

    * the resurrected value's (Alice) interval node NO LONGER carries ``val:validTo``
      — clearing that stale closure is exactly what makes the current read see her
      again (remove the reopen and this assertion fails, and Alice vanishes); while
    * the OTHER value's (Bob) interval node STILL carries ``val:validTo`` — the
      reopen touches only the re-asserted value's node, never a sibling's closure."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")
    await supersede_fact(n, INSTANCE_GRAPH, subject=ACME, predicate=HAS_CEO,
                         new_value="Bob", type_name="Company", run_id="r1")

    # Mid-oscillation: Alice's node carries a closure (she was superseded by Bob).
    assert await _node_has_valid_to(n, ACME, HAS_CEO, "Alice") is True

    await supersede_fact(n, INSTANCE_GRAPH, subject=ACME, predicate=HAS_CEO,
                         new_value="Alice", type_name="Company", run_id="r2")

    # Load-bearing: Alice's closure was cleared (that is what makes her current).
    assert await _node_has_valid_to(n, ACME, HAS_CEO, "Alice") is False, (
        "the resurrected value's node must have its stale val:validTo cleared"
    )
    # Targeted: Bob's closure is untouched (only the re-asserted value is reopened).
    assert await _node_has_valid_to(n, ACME, HAS_CEO, "Bob") is True, (
        "a sibling value's closure must NOT be cleared by the reopen"
    )
    assert await _current(n, ACME, HAS_CEO) == {"Alice"}
