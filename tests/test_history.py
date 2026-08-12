"""Tests for temporal value-history versioning (ONTA-236 / ONTA-536).

The gap: an attribute UPDATE overwrites in place (delete-old + insert-new via the
shared write path), so "which values changed, old → new, when" was unanswerable.
The fix records a dated ``old → new`` entry on every GENUINE value change —
through ``kg_writer.delete_facts`` (the shared write path), NOT a bespoke writer.

**Ported by ONTA-536.** The SPARQL companion ``…/history`` graph went out with
Neptune; the property-graph port lands ``:ValueHistory`` rows on the GraphStore
(``MemoryGraphStore`` / ``Neo4jGraphStore``). Write-side cases below seed a
current Assertion, call ``delete_facts(..., new_values=…)``, and assert on
``snapshot_value_history()`` (hermetic). The pure builder / SPARQL-reader /
injection-guard cases remain for the library helpers.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from infona_client.graph.facts import Fact
from infona_client.graph.history import (
    build_value_change_triples,
    fetch_store_assertion_history,
    fetch_value_history,
    history_graph_uri,
    lexical_value,
    value_history_query,
)
from infona_client.graph.kg_writer import delete_facts, insert_facts
from infona_client.graph.store import get_graph_store

GRAPH = "https://graph.infona.ai/graphs/t/kg/widgets"
SUBJ = "https://graph.infona.ai/entities/Widget/w1"
PRED = "https://graph.infona.ai/types/Widget/attrs/weight_kg"


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
    assert lexical_value("<https://graph.infona.ai/entities/City/SF>") == (
        "https://graph.infona.ai/entities/City/SF"
    )
    assert lexical_value("plain string") == "plain string"


# --- build_value_change_triples: only genuine changes, dated + typed ------------


def test_build_value_change_triples_records_dated_transition():
    ts = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    triples = build_value_change_triples(SUBJ, PRED, "10.0", "12.5", changed_at=ts)
    by_pred = {p: o for _s, p, o in triples}
    assert by_pred["https://graph.infona.ai/history/oldValue"] == "10.0"
    assert by_pred["https://graph.infona.ai/history/newValue"] == "12.5"
    assert by_pred["https://graph.infona.ai/history/subject"] == SUBJ
    assert by_pred["https://graph.infona.ai/history/predicate"] == PRED
    # changed_at is a TYPED xsd:dateTime so a "since" FILTER matches it.
    assert by_pred["https://graph.infona.ai/history/changedAt"] == (
        "2026-07-08T12:00:00+00:00^^http://www.w3.org/2001/XMLSchema#dateTime"
    )


def test_build_value_change_triples_noop_when_unchanged():
    """An unchanged value (even across serialization forms) is NOT a change."""
    assert build_value_change_triples(SUBJ, PRED, "12.5", "12.5", changed_at="t") == []
    # typed-literal new vs lexical old for the same value → still no change.
    typed = "12.5^^http://www.w3.org/2001/XMLSchema#float"
    assert build_value_change_triples(SUBJ, PRED, "12.5", typed, changed_at="t") == []


# --- delete_facts: value history on GraphStore (ONTA-536 hermetic) --------------


async def _seed_weight(value: str) -> None:
    """Seed Widget.weight_kg as a current Assertion on the process GraphStore."""
    await insert_facts(
        None,
        GRAPH,
        facts=[
            Fact(subject_id=SUBJ, kind="type", key="Widget"),
            Fact(subject_id=SUBJ, kind="literal", key="weight_kg", value=value),
        ],
        store=get_graph_store(),
    )


def test_delete_facts_records_change_to_history_graph(monkeypatch):
    """A predicate-scoped clear WITH a new value + history enabled → an old→new
    :ValueHistory row on the GraphStore."""

    async def run():
        monkeypatch.setenv("INFONA_VALUE_HISTORY_ENABLED", "1")
        await _seed_weight("10.0")
        await delete_facts(
            None,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
            store=get_graph_store(),
        )
        rows = get_graph_store().snapshot_value_history()
        assert rows, "an old→new version node must land on the GraphStore"
        assert any(
            r["old_value"] == "10.0" and r["new_value"] == "12.5" for r in rows
        )
        # Not an Entity property — history is a companion, not domain data.
        for ent in get_graph_store().snapshot_entities():
            props = ent.get("props") or {}
            assert "old_value" not in props and "oldValue" not in props

    asyncio.run(run())


def test_delete_facts_no_history_for_first_insert(monkeypatch):
    """No prior value (first insert) → NO change recorded."""

    async def run():
        monkeypatch.setenv("INFONA_VALUE_HISTORY_ENABLED", "1")
        # No seed — nothing to version.
        await delete_facts(
            None,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
            store=get_graph_store(),
        )
        assert get_graph_store().snapshot_value_history() == []

    asyncio.run(run())


def test_delete_facts_no_history_for_unchanged_value(monkeypatch):
    """Re-writing the SAME value records nothing (no false positive)."""

    async def run():
        monkeypatch.setenv("INFONA_VALUE_HISTORY_ENABLED", "1")
        await _seed_weight("12.5")
        await delete_facts(
            None,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
            store=get_graph_store(),
        )
        assert get_graph_store().snapshot_value_history() == []

    asyncio.run(run())


def test_delete_facts_no_history_when_disabled(monkeypatch):
    """Env gate OFF → no ValueHistory row even when a genuine change happens."""

    async def run():
        monkeypatch.delenv("INFONA_VALUE_HISTORY_ENABLED", raising=False)
        await _seed_weight("10.0")
        await delete_facts(
            None,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
            store=get_graph_store(),
        )
        assert get_graph_store().snapshot_value_history() == []

    asyncio.run(run())


def test_delete_facts_history_only_for_pairs_with_new_value(monkeypatch):
    """A pair cleared WITHOUT a declared new value is not versioned."""

    async def run():
        monkeypatch.setenv("INFONA_VALUE_HISTORY_ENABLED", "1")
        other = "https://graph.infona.ai/types/Widget/attrs/color"
        await insert_facts(
            None,
            GRAPH,
            facts=[
                Fact(subject_id=SUBJ, kind="type", key="Widget"),
                Fact(subject_id=SUBJ, kind="literal", key="weight_kg", value="10.0"),
                Fact(subject_id=SUBJ, kind="literal", key="color", value="red"),
            ],
            store=get_graph_store(),
        )
        await delete_facts(
            None,
            GRAPH,
            triples=[(SUBJ, PRED, None), (SUBJ, other, None)],
            new_values={(SUBJ, PRED): "12.5"},  # `other` has no declared new value
            store=get_graph_store(),
        )
        rows = get_graph_store().snapshot_value_history()
        assert any("weight_kg" in (r.get("predicate") or "") for r in rows)
        assert not any("color" in (r.get("predicate") or "") for r in rows)

    asyncio.run(run())


def test_delete_facts_history_best_effort(monkeypatch):
    """A history hiccup must NOT fail the update (history is a derived companion)."""

    async def run():
        monkeypatch.setenv("INFONA_VALUE_HISTORY_ENABLED", "1")
        await _seed_weight("10.0")
        store = get_graph_store()
        from infona_client.graph.scope import GraphScope

        real_session = store.session

        class _BrokenSession:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            async def write_value_history(self, **kwargs):
                raise RuntimeError("history backend down")

        def _wrap(scope: GraphScope):
            return _BrokenSession(real_session(scope))

        monkeypatch.setattr(store, "session", _wrap)
        # Must not raise.
        removed = await delete_facts(
            None,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
            store=store,
        )
        assert removed >= 0

    asyncio.run(run())


# --- Two updates → ordered old→new transitions, each dated ----------------------


def test_two_updates_yield_ordered_transitions(monkeypatch):
    """weight_kg: 10 → 12.5 → 9.0. Two delete_facts updates emit two version nodes;
    fetch_store_assertion_history reads them back as ordered old→new transitions.
    """

    async def run():
        monkeypatch.setenv("INFONA_VALUE_HISTORY_ENABLED", "1")
        store = get_graph_store()
        await _seed_weight("10.0")
        await delete_facts(
            None, GRAPH, triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"}, store=store,
        )
        # Re-seed the new current value so the next delete sees 12.5.
        await insert_facts(
            None, GRAPH,
            facts=[Fact(subject_id=SUBJ, kind="literal", key="weight_kg", value="12.5")],
            store=store,
        )
        await delete_facts(
            None, GRAPH, triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "9.0"}, store=store,
        )

        changes = await fetch_store_assertion_history(
            store, tenant_id="t", kg_name="widgets", subject=SUBJ,
        )
        assert [(c.old_value, c.new_value) for c in changes] == [
            ("10.0", "12.5"),
            ("12.5", "9.0"),
        ]
        assert all(c.changed_at for c in changes), "every transition carries a date"
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


def test_value_history_query_rejects_iri_breakout_subject():
    """A crafted subject carrying a `>` cannot break out of the <…> IRI wrapper to
    inject a GRAPH <other-tenant> block — _escape_value rejects it, so the query
    builder raises instead of emitting cross-tenant SPARQL (tenant isolation)."""
    import pytest

    victim = "https://graph.infona.ai/graphs/VICTIM/kg/secret/history"
    payload = (
        f"http://x> }} UNION {{ GRAPH <{victim}> {{ ?node "
        f"<https://graph.infona.ai/history/subject> ?s "
    )
    with pytest.raises(ValueError):
        value_history_query(GRAPH, subject=payload)
    # A `>`-bearing predicate is rejected the same way.
    with pytest.raises(ValueError):
        value_history_query(GRAPH, predicate="http://p> } GRAPH <x> { ?a ?b ?c ")
    # A legit IRI still builds fine (no false positive).
    q = value_history_query(GRAPH, subject=SUBJ, predicate=PRED)
    assert f"<{SUBJ}>" in q and f"<{PRED}>" in q


def test_history_graph_uri_is_not_an_instance_graph():
    """The companion history graph must NOT parse as a per-KG instance graph, so
    the derived-index hooks never mistake it for one."""
    from infona_client.graph.queries import parse_kg_graph_uri

    assert parse_kg_graph_uri(GRAPH) == ("t", "widgets")
    assert parse_kg_graph_uri(history_graph_uri(GRAPH)) is None
