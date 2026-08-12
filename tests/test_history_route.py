"""Route test for the value-history read endpoint (ONTA-236).

GET /graphs/{tenant}/history?kg_name=…&since=… returns dated value entries for
a KG — the queryable surface a "which values changed this week, with a date"
question reaches.

**Ported by ONTA-527.** These cases used to stub ``neptune.query`` with a
companion-``…/history``-graph SELECT result and then assert on the SPARQL TEXT
the route emitted (``history_graph_uri(...) in sent``, ``FILTER(?changedAt > …)``
in sent). The route reads Assertion provenance through
``graph/history.py::fetch_store_assertion_history`` now and emits no SPARQL, so
those assertions tested a builder that no longer runs. They are replaced by
assertions on OBSERVABLE behaviour over a seeded store: the scoping check seeds
a SIBLING KG and asserts its rows are absent (stronger than "the query text
named the right graph"), and the ``since`` check seeds a pre- and a post-cutoff
row and asserts only the later one comes back.

**Ported by ONTA-536:** ``old_value`` is recovered from ``:ValueHistory`` rows
written by ``delete_facts`` under ``INFONA_VALUE_HISTORY_ENABLED``.
"""

import asyncio
import os

import pytest

from infona_client.graph.facts import Fact
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import delete_facts, insert_facts
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import get_optional_graph_store

TENANT = "test-tenant"
KG = "widgets"
SIBLING_KG = "gadgets"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
SIBLING_GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{SIBLING_KG}"

SUBJ = entity_uri("Widget", "w1")
OTHER_SUBJ = entity_uri("Widget", "w2")
SIBLING_SUBJ = entity_uri("Gadget", "g1")

LAST_WEEK = "2026-07-01T00:00:00+00:00"
THIS_WEEK = "2026-07-07T00:00:00+00:00"


def _seed(graph: str, facts: list[Fact]) -> None:
    """Write ``facts`` into ``graph`` through the shared write path."""
    store = get_optional_graph_store()
    asyncio.run(insert_facts(None, graph, facts=facts, store=store))


def _widget(subject: str, weight: str, *, verified_at: str) -> list[Fact]:
    return [
        Fact(subject_id=subject, kind="type", key="Widget"),
        Fact(
            subject_id=subject,
            kind="literal",
            key="weight_kg",
            value=weight,
            verified_at=verified_at,
        ),
    ]


def _get(client, auth_headers, **params):
    return client.get(
        f"/graphs/{TENANT}/history",
        params={"kg_name": KG, **params},
        headers=auth_headers,
    )


def test_history_route_returns_changes(client, auth_headers, mock_neptune, monkeypatch):
    """The old→new transition the endpoint was built to answer (ONTA-536)."""
    monkeypatch.setenv("INFONA_VALUE_HISTORY_ENABLED", "1")
    store = get_optional_graph_store()
    # Seed 10.0, then record a 10.0 → 12.5 transition via delete_facts.
    _seed(GRAPH, _widget(SUBJ, "10.0", verified_at=LAST_WEEK))
    PRED = f"{IRI_BASE}/types/Widget/attrs/weight_kg"
    asyncio.run(
        delete_facts(
            None,
            GRAPH,
            triples=[(SUBJ, PRED, None)],
            new_values={(SUBJ, PRED): "12.5"},
            store=store,
        )
    )
    # Land the new current value with THIS_WEEK stamp (Assertion provenance).
    _seed(GRAPH, _widget(SUBJ, "12.5", verified_at=THIS_WEEK))
    resp = _get(client, auth_headers, subject=SUBJ)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kg_name"] == KG
    change = next(c for c in body["changes"] if c["new_value"] == "12.5")
    assert change["old_value"] == "10.0"
    assert change["changed_at"]  # dated transition
    mock_neptune.query.assert_not_called()


def test_history_route_returns_dated_current_values(client, auth_headers, mock_neptune):
    """The half that DID survive the port: a dated entry per current value."""
    _seed(GRAPH, _widget(SUBJ, "12.5", verified_at=THIS_WEEK))
    resp = _get(client, auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kg_name"] == KG
    assert body["count"] >= 1
    change = next(c for c in body["changes"] if c["new_value"] == "12.5")
    assert change["subject"] == SUBJ
    assert "weight_kg" in change["predicate"]
    assert change["changed_at"] == THIS_WEEK
    mock_neptune.query.assert_not_called()


def test_history_route_scopes_to_the_named_kg(client, auth_headers, mock_neptune):
    """A sibling KG in the SAME workspace holds a row; it must not leak.

    This is the assertion the old ``history_graph_uri(...) in sent`` string
    check stood in for, made against data instead of query text.
    """
    _seed(GRAPH, _widget(SUBJ, "12.5", verified_at=THIS_WEEK))
    _seed(
        SIBLING_GRAPH,
        [
            Fact(subject_id=SIBLING_SUBJ, kind="type", key="Gadget"),
            Fact(
                subject_id=SIBLING_SUBJ,
                kind="literal",
                key="weight_kg",
                value="99.9",
                verified_at=THIS_WEEK,
            ),
        ],
    )
    resp = _get(client, auth_headers)
    assert resp.status_code == 200, resp.text
    subjects = {c["subject"] for c in resp.json()["changes"]}
    assert SUBJ in subjects
    assert SIBLING_SUBJ not in subjects
    mock_neptune.query.assert_not_called()


def test_history_route_passes_since_cutoff(client, auth_headers, mock_neptune):
    """`since` returns only entries STRICTLY AFTER the cutoff."""
    _seed(GRAPH, _widget(SUBJ, "12.5", verified_at=THIS_WEEK))
    _seed(GRAPH, _widget(OTHER_SUBJ, "8.0", verified_at=LAST_WEEK))

    cutoff = "2026-07-06T00:00:00+00:00"
    resp = _get(client, auth_headers, since=cutoff)
    assert resp.status_code == 200, resp.text
    changes = resp.json()["changes"]
    values = {c["new_value"] for c in changes}
    assert "12.5" in values
    assert "8.0" not in values
    assert all(c["changed_at"] > cutoff for c in changes if c["changed_at"])

    # Without the cutoff both are visible, so the filter above is not vacuous.
    both = {c["new_value"] for c in _get(client, auth_headers).json()["changes"]}
    assert {"12.5", "8.0"} <= both
    mock_neptune.query.assert_not_called()


def test_history_route_requires_kg_name(client, auth_headers, mock_neptune):
    """kg_name is required (history is per-KG) → 422 without it."""
    resp = client.get("/graphs/test-tenant/history", headers=auth_headers)
    assert resp.status_code == 422


def test_history_route_rejects_injection_subject(client, auth_headers, mock_neptune):
    """TENANT ISOLATION: a subject carrying a `>` that tries to inject a
    GRAPH <other-tenant> block is rejected at the route boundary (422) and NEVER
    reaches Neptune — so it cannot read another tenant's history."""
    victim = "https://graph.infona.ai/graphs/VICTIM/kg/secret/history"
    payload = f"http://x> }} UNION {{ GRAPH <{victim}> {{ ?node ?p ?o "
    resp = client.get(
        "/graphs/test-tenant/history",
        params={"kg_name": "widgets", "subject": payload},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    # The malicious query must never have been sent to Neptune.
    if mock_neptune.query.await_args is not None:
        assert victim not in mock_neptune.query.await_args.args[0]


def test_history_route_rejects_injection_predicate(client, auth_headers, mock_neptune):
    """Same boundary rejection for a `>`-bearing predicate."""
    resp = client.get(
        "/graphs/test-tenant/history",
        params={
            "kg_name": "widgets",
            "predicate": "http://p> } GRAPH <x> { ?a ?b ?c ",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_history_route_accepts_valid_iri_subject(client, auth_headers, mock_neptune):
    """A well-formed absolute IRI subject is accepted (no false positive) and
    narrows the read to THAT subject, inside the caller's own workspace.

    Ported from a SPARQL-text assertion (``<SUBJ>`` and the history graph both
    appear in ``sent``) to the behaviour it stood for: the named subject's rows
    come back and a peer subject in the same KG does not.
    """
    _seed(GRAPH, _widget(SUBJ, "12.5", verified_at=THIS_WEEK))
    _seed(GRAPH, _widget(OTHER_SUBJ, "8.0", verified_at=THIS_WEEK))

    resp = _get(client, auth_headers, subject=SUBJ)
    assert resp.status_code == 200, resp.text
    subjects = {c["subject"] for c in resp.json()["changes"]}
    assert subjects == {SUBJ}
    mock_neptune.query.assert_not_called()


def test_history_route_neo4j_uses_assertion_store(
    client, auth_headers, mock_neptune, monkeypatch
):
    """When INFONA_GRAPH_BACKEND=neo4j, history lists Assertion provenance
    via GraphStore — Neptune SPARQL is not called."""
    import asyncio

    from infona_client.graph.facts import Fact
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.ontology_queries import entity_uri
    from infona_client.graph.store import (
        configure_graph_store,
        reset_graph_store_for_tests,
    )

    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    store = MemoryGraphStore()
    configure_graph_store(store)
    try:
        alice = entity_uri("Widget", "w1")
        graph = f"{IRI_BASE}/graphs/test-tenant/kg/widgets"

        async def _seed():
            await insert_facts(
                None,
                graph,
                facts=[
                    Fact(subject_id=alice, kind="type", key="Widget"),
                    Fact(
                        subject_id=alice,
                        kind="literal",
                        key="weight_kg",
                        value="12.5",
                        verified_at="2026-07-07T00:00:00+00:00",
                    ),
                ],
                store=store,
            )

        asyncio.run(_seed())
        resp = client.get(
            "/graphs/test-tenant/history",
            params={"kg_name": "widgets", "subject": alice},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] >= 1
        weights = [c for c in body["changes"] if c["new_value"] == "12.5"]
        assert weights
        assert weights[0]["changed_at"] == "2026-07-07T00:00:00+00:00"
        mock_neptune.query.assert_not_called()
    finally:
        asyncio.run(store.close())
        reset_graph_store_for_tests()
