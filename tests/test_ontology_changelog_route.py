"""Route tests for GET /graphs/{tenant}/ontology/changelog (ONTA-401).

Modeled on tests/test_history_route.py: auth + tenant scoping + injection
rejection + pagination query params.
"""

from __future__ import annotations

from infona_client.graph.ontology_changelog import (
    changelog_graph_uri_for,
    serialize_change_records,
)
from infona_client.graph.queries import tenant_graph_uri
from infona_client.models.ontology import ChangeKind, ChangeRecord


def _changelog_response(rows: list[dict[str, str]]) -> dict:
    vars_: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                vars_.append(k)
    return {
        "head": {"vars": vars_},
        "results": {
            "bindings": [
                {k: {"value": v} for k, v in row.items()} for row in rows
            ]
        },
    }


def test_changelog_route_returns_entries(client, auth_headers, mock_neptune):
    delta = serialize_change_records(
        [ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person")]
    )
    mock_neptune.query.return_value = _changelog_response(
        [
            {
                "entry": "https://graph.infona.ai/gov/log/11111111-2222-3333-4444-555555555555",
                "action": "commit_ontology",
                "subject": tenant_graph_uri("test-tenant"),
                "timestamp": "2026-07-28T12:00:00Z",
                "tenant": "test-tenant",
                "actor": "tester",
                "message": "add Person",
                "versionBefore": "aaa",
                "versionAfter": "bbb",
                "revision": "1",
                "delta": delta,
            }
        ]
    )
    resp = client.get(
        "/graphs/test-tenant/ontology/changelog",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "test-tenant"
    assert body["graph_uri"] == tenant_graph_uri("test-tenant")
    assert body["count"] == 1
    assert body["offset"] == 0
    assert body["limit"] == 100
    entry = body["entries"][0]
    assert entry["action"] == "commit_ontology"
    assert entry["revision"] == 1
    assert entry["changes"][0]["kind"] == "add_type"
    assert entry["changes"][0]["type_name"] == "Person"


def test_changelog_route_scopes_to_tenant_companion(client, auth_headers, mock_neptune):
    """TENANT ISOLATION: FROM is this tenant's companion only — never another
    tenant's graph, never the global governance changelog."""
    mock_neptune.query.return_value = _changelog_response([])
    client.get(
        "/graphs/test-tenant/ontology/changelog",
        headers=auth_headers,
    )
    sent = mock_neptune.query.await_args.args[0]
    expected = changelog_graph_uri_for(tenant_graph_uri("test-tenant"))
    assert f"FROM <{expected}>" in sent
    assert "graphs/other-tenant" not in sent
    assert "graphs/global/changelog" not in sent


def test_changelog_route_passes_filters_and_pagination(
    client, auth_headers, mock_neptune
):
    mock_neptune.query.return_value = _changelog_response([])
    cutoff = "2026-07-01T00:00:00Z"
    subj = tenant_graph_uri("test-tenant")
    resp = client.get(
        "/graphs/test-tenant/ontology/changelog",
        params={
            "since": cutoff,
            "subject": subj,
            "action": "commit_ontology",
            "limit": 10,
            "offset": 5,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 10
    assert body["offset"] == 5
    sent = mock_neptune.query.await_args.args[0]
    assert f'FILTER(?timestamp > "{cutoff}"' in sent
    assert "commit_ontology" in sent
    assert "LIMIT 10" in sent
    assert "OFFSET 5" in sent
    assert subj in sent


def test_changelog_route_rejects_injection_subject(
    client, auth_headers, mock_neptune
):
    """A subject carrying `>` that tries to inject GRAPH <other-tenant> is
    rejected at the route boundary (422) and NEVER reaches Neptune."""
    victim = "https://graph.infona.ai/graphs/VICTIM/changelog"
    payload = f"http://x> }} UNION {{ GRAPH <{victim}> {{ ?entry ?p ?o "
    resp = client.get(
        "/graphs/test-tenant/ontology/changelog",
        params={"subject": payload},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    if mock_neptune.query.await_args is not None:
        assert victim not in mock_neptune.query.await_args.args[0]


def test_changelog_route_rejects_bad_action(client, auth_headers, mock_neptune):
    resp = client.get(
        "/graphs/test-tenant/ontology/changelog",
        params={"action": 'x" } FILTER(true)'},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert mock_neptune.query.await_args is None


def test_changelog_route_requires_auth(client, mock_neptune):
    resp = client.get("/graphs/test-tenant/ontology/changelog")
    assert resp.status_code in (401, 403)


def test_changelog_route_accepts_valid_subject_iri(
    client, auth_headers, mock_neptune
):
    mock_neptune.query.return_value = _changelog_response([])
    subj = tenant_graph_uri("test-tenant")
    resp = client.get(
        "/graphs/test-tenant/ontology/changelog",
        params={"subject": subj},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    sent = mock_neptune.query.await_args.args[0]
    assert f"FROM <{changelog_graph_uri_for(subj)}>" in sent
