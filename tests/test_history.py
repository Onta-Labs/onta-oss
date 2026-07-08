"""Tests for temporal value-history versioning (ONTA-236).

The gap: an attribute UPDATE overwrites in place (delete-old + insert-new via the
shared write path), so "which values changed, old → new, when" was unanswerable.
The fix records a dated ``old → new`` entry in a companion history graph on every
GENUINE value change — through ``kg_writer.delete_facts`` (the shared write path),
NOT a bespoke writer.

These tests pin, on invented data (Widget.weight_kg — no price/model special-
casing), that:
  * two updates produce ordered old→new transitions, each with a changed_at date;
  * a "changed since <cutoff>" read returns only post-cutoff transitions;
  * the history write rides kg_writer.delete_facts (behavioral) and lands in the
    companion HISTORY graph via the shared batched-insert seam;
  * a first insert / an unchanged re-write records NO change (no false positives);
  * the whole mechanism is env-gated (byte-stable when off).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from cograph_client.graph.history import (
    build_value_change_triples,
    fetch_value_history,
    history_graph_uri,
    lexical_value,
    value_history_query,
)
from cograph_client.graph.kg_writer import delete_facts

GRAPH = "https://cograph.tech/graphs/t/kg/widgets"
SUBJ = "https://cograph.tech/entities/Widget/w1"
PRED = "https://cograph.tech/types/Widget/attrs/weight_kg"


def _objects_response(objects: list[tuple[str, str, str]]) -> dict:
    """A SELECT ?s ?p ?o response (for the read-before-delete current-value query)."""
    return {
        "head": {"vars": ["s", "p", "o"]},
        "results": {
            "bindings": [
                {"s": {"value": s}, "p": {"value": p}, "o": {"value": o}}
                for s, p, o in objects
            ]
        },
    }


def _count_response(n: int) -> dict:
    return {"head": {"vars": ["n"]}, "results": {"bindings": [{"n": {"value": str(n)}}]}}


# --- lexical_value: change detected/stored on the user-visible axis -------------


def test_lexical_value_strips_typed_and_uri_wrappers():
    assert lexical_value('92^^http://www.w3.org/2001/XMLSchema#integer') == "92"
    assert lexical_value("<https://cograph.tech/entities/City/SF>") == (
        "https://cograph.tech/entities/City/SF"
    )
    assert lexical_value("plain string") == "plain string"


# --- build_value_change_triples: only genuine changes, dated + typed ------------


def test_build_value_change_triples_records_dated_transition():
    ts = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    triples = build_value_change_triples(SUBJ, PRED, "10.0", "12.5", changed_at=ts)
    by_pred = {p: o for _s, p, o in triples}
    assert by_pred["https://cograph.tech/history/oldValue"] == "10.0"
    assert by_pred["https://cograph.tech/history/newValue"] == "12.5"
    assert by_pred["https://cograph.tech/history/subject"] == SUBJ
    assert by_pred["https://cograph.tech/history/predicate"] == PRED
    # changed_at is a TYPED xsd:dateTime so a "since" FILTER matches it.
    assert by_pred["https://cograph.tech/history/changedAt"] == (
        "2026-07-08T12:00:00+00:00^^http://www.w3.org/2001/XMLSchema#dateTime"
    )


def test_build_value_change_triples_noop_when_unchanged():
    """An unchanged value (even across serialization forms) is NOT a change."""
    assert build_value_change_triples(SUBJ, PRED, "12.5", "12.5", changed_at="t") == []
    # typed-literal new vs lexical old for the same value → still no change.
    typed = "12.5^^http://www.w3.org/2001/XMLSchema#float"
    assert build_value_change_triples(SUBJ, PRED, "12.5", typed, changed_at="t") == []


# --- delete_facts: value history rides the shared write path (behavioral) -------


def test_delete_facts_records_change_to_history_graph(monkeypatch):
    """A predicate-scoped clear WITH a new value + history enabled → an old→new
    version node lands in the companion HISTORY graph (not the data graph), via
    the shared batched-insert seam inside delete_facts."""

    async def run():
        monkeypatch.setenv("COGRAPH_VALUE_HISTORY_ENABLED", "1")
        neptune = AsyncMock()
        # Reads: (1) current object for history, (2) COUNT for the delete.
        neptune.query.side_effect = [
            _objects_response([(SUBJ, PRED, "10.0")]),
            _count_response(1),
        ]
        await delete_facts(
            neptune,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
        )
        stmts = [c.args[0] for c in neptune.update.await_args_list]
        hist_graph = history_graph_uri(GRAPH)
        hist_stmts = [s for s in stmts if hist_graph in s]
        assert hist_stmts, "an old→new version node must land in the history graph"
        joined = "\n".join(hist_stmts)
        assert "10.0" in joined and "12.5" in joined
        assert "oldValue" in joined and "newValue" in joined
        # The change is recorded in the HISTORY companion, never the data graph.
        data_stmts = [s for s in stmts if hist_graph not in s]
        assert not any("oldValue" in s for s in data_stmts)

    asyncio.run(run())


def test_delete_facts_no_history_for_first_insert(monkeypatch):
    """No prior value (first insert) → the read returns nothing → NO change
    recorded (a value appearing for the first time is not a change)."""

    async def run():
        monkeypatch.setenv("COGRAPH_VALUE_HISTORY_ENABLED", "1")
        neptune = AsyncMock()
        neptune.query.side_effect = [
            _objects_response([]),  # nothing there yet
            _count_response(0),
        ]
        await delete_facts(
            neptune,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
        )
        hist_graph = history_graph_uri(GRAPH)
        assert not any(
            hist_graph in c.args[0] for c in neptune.update.await_args_list
        ), "a first insert must not create a change record"

    asyncio.run(run())


def test_delete_facts_no_history_for_unchanged_value(monkeypatch):
    """Re-writing the SAME value records nothing (no false positive)."""

    async def run():
        monkeypatch.setenv("COGRAPH_VALUE_HISTORY_ENABLED", "1")
        neptune = AsyncMock()
        neptune.query.side_effect = [
            _objects_response([(SUBJ, PRED, "12.5")]),
            _count_response(1),
        ]
        await delete_facts(
            neptune,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
        )
        hist_graph = history_graph_uri(GRAPH)
        assert not any(hist_graph in c.args[0] for c in neptune.update.await_args_list)

    asyncio.run(run())


def test_delete_facts_no_history_when_disabled(monkeypatch):
    """Env gate OFF → byte-stable: no history read, no history write."""

    async def run():
        monkeypatch.delenv("COGRAPH_VALUE_HISTORY_ENABLED", raising=False)
        neptune = AsyncMock()
        neptune.query.return_value = _count_response(1)
        await delete_facts(
            neptune,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
        )
        # Only the delete COUNT query — never the current-object read.
        assert neptune.query.await_count == 1
        hist_graph = history_graph_uri(GRAPH)
        assert not any(hist_graph in c.args[0] for c in neptune.update.await_args_list)

    asyncio.run(run())


def test_delete_facts_history_only_for_pairs_with_new_value(monkeypatch):
    """A pair cleared WITHOUT a declared new value (a pure removal, not an update)
    is not read and not versioned — only declared replacements are tracked."""

    async def run():
        monkeypatch.setenv("COGRAPH_VALUE_HISTORY_ENABLED", "1")
        other = "https://cograph.tech/types/Widget/attrs/color"
        neptune = AsyncMock()
        # Only the tracked pair (SUBJ, PRED) is read for its current value.
        neptune.query.side_effect = [
            _objects_response([(SUBJ, PRED, "10.0")]),
            _count_response(2),
        ]
        await delete_facts(
            neptune,
            GRAPH,
            triples=[(SUBJ, PRED, None), (SUBJ, other, None)],
            new_values={(SUBJ, PRED): "12.5"},  # `other` has no declared new value
        )
        joined = "\n".join(
            c.args[0] for c in neptune.update.await_args_list
            if history_graph_uri(GRAPH) in c.args[0]
        )
        assert "weight_kg" in joined
        assert "color" not in joined

    asyncio.run(run())


def test_delete_facts_history_best_effort(monkeypatch):
    """A history-read hiccup must NOT fail the update (history is a derived
    companion). The delete still proceeds."""

    async def run():
        monkeypatch.setenv("COGRAPH_VALUE_HISTORY_ENABLED", "1")
        neptune = AsyncMock()
        neptune.query.side_effect = [
            RuntimeError("history backend down"),
            _count_response(1),
        ]
        # Must not raise.
        removed = await delete_facts(
            neptune,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
        )
        assert removed == 1
        # The predicate-scoped delete still ran.
        assert any(
            "DELETE" in c.args[0] and "VALUES (?s ?p)" in c.args[0]
            for c in neptune.update.await_args_list
        )

    asyncio.run(run())


# --- Two updates → ordered old→new transitions, each dated ----------------------


def test_two_updates_yield_ordered_transitions(monkeypatch):
    """weight_kg: 10 → 12.5 → 9.0. Two delete_facts updates emit two version nodes;
    fetch_value_history reads them back as ordered old→new transitions, dated."""

    async def run():
        monkeypatch.setenv("COGRAPH_VALUE_HISTORY_ENABLED", "1")

        # A stateful fake that stores history triples across the two updates and
        # replays them for the value_history_query read (oldest → newest).
        history_rows: list[dict] = []
        current = {"o": "10.0"}

        neptune = AsyncMock()

        async def _query(sparql):
            if "changedAt" in sparql and "oldValue" in sparql:  # value_history_query
                return {
                    "head": {"vars": ["s", "p", "oldValue", "newValue", "changedAt"]},
                    "results": {"bindings": history_rows},
                }
            if "?s ?p ?o" in sparql and "COUNT" not in sparql:  # current-object read
                return _objects_response([(SUBJ, PRED, current["o"])])
            return _count_response(1)

        async def _update(sparql):
            # Capture the history version node (old/new/changedAt) as a row.
            if history_graph_uri(GRAPH) in sparql:
                import re

                old = re.search(r'oldValue> "([^"]+)"', sparql)
                new = re.search(r'newValue> "([^"]+)"', sparql)
                at = re.search(r'changedAt> "([^"]+)"', sparql)
                if old and new and at:
                    history_rows.append(
                        {
                            "s": {"value": SUBJ},
                            "p": {"value": PRED},
                            "oldValue": {"value": old.group(1)},
                            "newValue": {"value": new.group(1)},
                            "changedAt": {"value": at.group(1)},
                        }
                    )

        neptune.query.side_effect = _query
        neptune.update.side_effect = _update

        # Update 1: 10.0 → 12.5
        await delete_facts(
            neptune, GRAPH, triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
        )
        current["o"] = "12.5"
        # Update 2: 12.5 → 9.0
        await delete_facts(
            neptune, GRAPH, triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "9.0"},
        )

        changes = await fetch_value_history(neptune, GRAPH, subject=SUBJ)
        assert [(c.old_value, c.new_value) for c in changes] == [
            ("10.0", "12.5"),
            ("12.5", "9.0"),
        ]
        assert all(c.changed_at for c in changes), "every transition carries a date"
        # Ordered oldest → newest (the query ORDER BY ?changedAt).
        assert changes[0].changed_at <= changes[1].changed_at

    asyncio.run(run())


# --- value_history_query: "changed since <cutoff>" ------------------------------


def test_value_history_query_since_filters_by_typed_datetime():
    """A `since` cutoff produces a TYPED xsd:dateTime FILTER (strictly after)."""
    q = value_history_query(GRAPH, since="2026-07-06T00:00:00+00:00")
    assert history_graph_uri(GRAPH) in q
    assert 'FILTER(?changedAt > "2026-07-06T00:00:00+00:00"' in q
    assert "XMLSchema#dateTime" in q
    assert "ORDER BY ?changedAt" in q


def test_fetch_value_history_since_returns_only_post_cutoff(monkeypatch):
    """End-to-end read semantics: with two transitions a week apart, a cutoff
    between them returns only the later one, old→new, dated. (The FILTER runs in
    Neptune; here we assert the query the reader sends carries the cutoff and the
    reader faithfully returns whatever rows come back.)"""

    async def run():
        last_week = datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat()
        this_week = datetime(2026, 7, 7, tzinfo=timezone.utc).isoformat()
        cutoff = datetime(2026, 7, 6, tzinfo=timezone.utc).isoformat()

        sent = {}
        neptune = AsyncMock()

        async def _query(sparql):
            sent["q"] = sparql
            # Simulate Neptune applying the FILTER: only the post-cutoff row.
            return {
                "head": {"vars": ["s", "p", "oldValue", "newValue", "changedAt"]},
                "results": {
                    "bindings": [
                        {
                            "s": {"value": SUBJ},
                            "p": {"value": PRED},
                            "oldValue": {"value": "12.5"},
                            "newValue": {"value": "9.0"},
                            "changedAt": {"value": this_week},
                        }
                    ]
                },
            }

        neptune.query.side_effect = _query
        changes = await fetch_value_history(neptune, GRAPH, subject=SUBJ, since=cutoff)
        # The reader sent the cutoff to Neptune...
        assert f'"{cutoff}"' in sent["q"]
        # ...and returned only the post-cutoff transition, old→new, dated.
        assert len(changes) == 1
        assert (changes[0].old_value, changes[0].new_value) == ("12.5", "9.0")
        assert changes[0].changed_at == this_week
        # (last_week is referenced to document the pre-cutoff row the FILTER drops.)
        assert last_week < cutoff

    asyncio.run(run())


def test_value_history_query_escapes_since_no_literal_breakout():
    """A crafted `since` cannot break out of the SPARQL literal (injection guard):
    quotes/backslashes are escaped through _escape_literal, so the closing quote
    of the FILTER literal stays intact."""
    malicious = '2026" ) } ; DROP GRAPH <x> ; SELECT * WHERE { ?a ?b ?c #'
    q = value_history_query(GRAPH, since=malicious)
    # The embedded quote is escaped (\"), so the FILTER literal is not terminated
    # early — the whole payload stays trapped inside one string literal.
    assert '2026\\"' in q
    # The unescaped breakout sequence must NOT appear verbatim.
    assert '"2026" )' not in q


def test_history_graph_uri_is_not_an_instance_graph():
    """The companion history graph must NOT parse as a per-KG instance graph, so
    the derived-index hooks never mistake it for one."""
    from cograph_client.graph.queries import parse_kg_graph_uri

    assert parse_kg_graph_uri(GRAPH) == ("t", "widgets")
    assert parse_kg_graph_uri(history_graph_uri(GRAPH)) is None
