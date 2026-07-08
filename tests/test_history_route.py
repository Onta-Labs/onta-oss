"""Route test for the value-history read endpoint (ONTA-236).

GET /graphs/{tenant}/history?kg_name=…&since=… returns dated old→new value
transitions from the companion history graph — the queryable surface a
"which values changed this week, old → new, with a date" question reaches.
"""

from cograph_client.graph.history import history_graph_uri
from cograph_client.graph.queries import kg_graph_uri

SUBJ = "https://cograph.tech/entities/Widget/w1"
PRED = "https://cograph.tech/types/Widget/attrs/weight_kg"


def _history_response(rows):
    return {
        "head": {"vars": ["s", "p", "oldValue", "newValue", "changedAt"]},
        "results": {
            "bindings": [
                {
                    "s": {"value": s},
                    "p": {"value": p},
                    "oldValue": {"value": ov},
                    "newValue": {"value": nv},
                    "changedAt": {"value": at},
                }
                for s, p, ov, nv, at in rows
            ]
        },
    }


def test_history_route_returns_changes(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = _history_response(
        [(SUBJ, PRED, "10.0", "12.5", "2026-07-07T00:00:00+00:00")]
    )
    resp = client.get(
        "/graphs/test-tenant/history",
        params={"kg_name": "widgets"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kg_name"] == "widgets"
    assert body["count"] == 1
    change = body["changes"][0]
    assert change["old_value"] == "10.0"
    assert change["new_value"] == "12.5"
    assert change["changed_at"] == "2026-07-07T00:00:00+00:00"


def test_history_route_scopes_to_kg_history_graph(client, auth_headers, mock_neptune):
    """The route must read the companion HISTORY graph of the named KG's data
    graph — not the tenant graph, not the data graph itself."""
    mock_neptune.query.return_value = _history_response([])
    client.get(
        "/graphs/test-tenant/history",
        params={"kg_name": "widgets"},
        headers=auth_headers,
    )
    sent = mock_neptune.query.await_args.args[0]
    expected = history_graph_uri(kg_graph_uri("test-tenant", "widgets"))
    assert expected in sent


def test_history_route_passes_since_cutoff(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = _history_response([])
    cutoff = "2026-07-06T00:00:00+00:00"
    client.get(
        "/graphs/test-tenant/history",
        params={"kg_name": "widgets", "since": cutoff},
        headers=auth_headers,
    )
    sent = mock_neptune.query.await_args.args[0]
    assert f'FILTER(?changedAt > "{cutoff}"' in sent


def test_history_route_requires_kg_name(client, auth_headers, mock_neptune):
    """kg_name is required (history is per-KG) → 422 without it."""
    resp = client.get("/graphs/test-tenant/history", headers=auth_headers)
    assert resp.status_code == 422
