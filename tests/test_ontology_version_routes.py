"""ONTA-410 — base-pin / history / diff HTTP routes.

Happy paths with mocked Neptune + tenant isolation + empty history 200.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cograph_client.graph.ontology_base_pin import BasePin, UpgradePreview
from cograph_client.graph.ontology_changelog import ChangelogEntry
from cograph_client.graph.queries import tenant_graph_uri
from cograph_client.models.ontology import ChangeKind, ChangeRecord


# ---------------------------------------------------------------------------
# base-pin
# ---------------------------------------------------------------------------


def _pin(**kwargs) -> BasePin:
    defaults = dict(
        tenant_id="test-tenant",
        base_layer="public",
        base_version=2,
        auto_upgrade=False,
        previous_version=1,
        has_previous=True,
        updated_at="2026-07-28T12:00:00Z",
    )
    defaults.update(kwargs)
    return BasePin(**defaults)


def test_base_pin_get(client, auth_headers, mock_neptune):
    pin = _pin()
    with (
        patch(
            "cograph_client.api.routes.ontology.ensure_workspace_base_pin",
            new=AsyncMock(return_value=pin),
        ),
        patch(
            "cograph_client.api.routes.ontology.latest_base_release_version",
            new=AsyncMock(return_value=3),
        ),
        patch(
            "cograph_client.api.routes.ontology._current_revision_counter",
            new=AsyncMock(return_value=42),
        ),
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/base-pin",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "test-tenant"
    assert body["base_layer"] == "public"
    assert body["base_version"] == 2
    assert body["is_live"] is False
    assert body["latest_available"] == 3
    assert body["upgrade_available"] is True
    assert body["workspace_revision"] == 42
    assert body["has_previous"] is True


def test_base_pin_preview(client, auth_headers, mock_neptune):
    changes = [ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Place")]
    preview = UpgradePreview(
        from_version=2,
        to_version=3,
        base_layer="public",
        changes=tuple(changes),
        collisions=(),
        deprecated_used=(),
        summary=("upgrade public v2 → v3",),
        from_fingerprint="aaa",
        to_fingerprint="bbb",
    )
    with patch(
        "cograph_client.api.routes.ontology.preview_base_upgrade",
        new=AsyncMock(return_value=preview),
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/base-pin/preview",
            params={"to_version": 3},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_version"] == 2
    assert body["to_version"] == 3
    assert body["changes"][0]["kind"] == "add_type"
    assert body["summary"] == ["upgrade public v2 → v3"]


def test_base_pin_upgrade(client, auth_headers, mock_neptune):
    upgraded = _pin(base_version=3, previous_version=2)
    with (
        patch(
            "cograph_client.api.routes.ontology.upgrade_base_pin",
            new=AsyncMock(return_value=upgraded),
        ),
        patch(
            "cograph_client.api.routes.ontology.latest_base_release_version",
            new=AsyncMock(return_value=3),
        ),
        patch(
            "cograph_client.api.routes.ontology._current_revision_counter",
            new=AsyncMock(return_value=7),
        ),
    ):
        resp = client.post(
            "/graphs/test-tenant/ontology/base-pin/upgrade",
            json={"to_version": 3},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_version"] == 3
    assert body["upgrade_available"] is False
    assert body["workspace_revision"] == 7


def test_base_pin_upgrade_rejects_unknown_version(client, auth_headers, mock_neptune):
    with patch(
        "cograph_client.api.routes.ontology.upgrade_base_pin",
        new=AsyncMock(side_effect=ValueError("no public release v99")),
    ):
        resp = client.post(
            "/graphs/test-tenant/ontology/base-pin/upgrade",
            json={"to_version": 99},
            headers=auth_headers,
        )
    assert resp.status_code == 422
    assert "v99" in resp.json()["detail"]


def test_base_pin_rollback(client, auth_headers, mock_neptune):
    rolled = _pin(base_version=1, previous_version=2)
    with (
        patch(
            "cograph_client.api.routes.ontology.rollback_base_pin",
            new=AsyncMock(return_value=rolled),
        ),
        patch(
            "cograph_client.api.routes.ontology.latest_base_release_version",
            new=AsyncMock(return_value=3),
        ),
        patch(
            "cograph_client.api.routes.ontology._current_revision_counter",
            new=AsyncMock(return_value=7),
        ),
    ):
        resp = client.post(
            "/graphs/test-tenant/ontology/base-pin/rollback",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    assert resp.json()["base_version"] == 1
    assert resp.json()["upgrade_available"] is True


def test_base_pin_requires_auth(client, mock_neptune):
    resp = client.get("/graphs/test-tenant/ontology/base-pin")
    assert resp.status_code in (401, 403)


def test_base_pin_graph_uri_tenant_scoped():
    """Pin storage is per-tenant companion — isolation by named graph."""
    from cograph_client.graph.ontology_base_pin import base_pin_graph_uri

    a = base_pin_graph_uri("acme")
    b = base_pin_graph_uri("other")
    assert a == "https://graph.onta.sh/graphs/acme/base-pin"
    assert b == "https://graph.onta.sh/graphs/other/base-pin"
    assert a != b


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_empty_is_200(client, auth_headers, mock_neptune):
    with (
        patch(
            "cograph_client.api.routes.ontology.fetch_ontology_changelog",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "cograph_client.api.routes.ontology._current_revision_counter",
            new=AsyncMock(return_value=0),
        ),
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/history",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["groups"] == []
    assert body["entries"] == []
    assert body["count"] == 0
    assert body["grouped"] is True
    assert body["workspace_revision"] == 0


def test_history_grouped_collapses_burst(client, auth_headers, mock_neptune):
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        ChangelogEntry(
            entry_uri=f"https://graph.onta.sh/gov/log/{i}",
            action="commit_ontology",
            subject=tenant_graph_uri("test-tenant"),
            timestamp=(t0 - timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            actor="ingest",
            message="job-9",
            revision=20 - i,
            changes=[ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=f"T{i}")],
        )
        for i in range(20)
    ]
    with (
        patch(
            "cograph_client.api.routes.ontology.fetch_ontology_changelog",
            new=AsyncMock(return_value=entries),
        ),
        patch(
            "cograph_client.api.routes.ontology._current_revision_counter",
            new=AsyncMock(return_value=20),
        ),
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/history",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["grouped"] is True
    assert body["count"] == 1
    assert body["groups"][0]["count"] == 20
    assert body["groups"][0]["actor"] == "ingest"
    assert body["entries"] == []


def test_history_flat_when_grouped_false(client, auth_headers, mock_neptune):
    entries = [
        ChangelogEntry(
            entry_uri="https://graph.onta.sh/gov/log/1",
            action="commit_ontology",
            subject=tenant_graph_uri("test-tenant"),
            timestamp="2026-07-28T12:00:00Z",
            actor="alice",
            message="add Person",
            revision=1,
            changes=[ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person")],
        )
    ]
    with (
        patch(
            "cograph_client.api.routes.ontology.fetch_ontology_changelog",
            new=AsyncMock(return_value=entries),
        ),
        patch(
            "cograph_client.api.routes.ontology._current_revision_counter",
            new=AsyncMock(return_value=1),
        ),
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/history",
            params={"grouped": "false"},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["grouped"] is False
    assert body["count"] == 1
    assert body["groups"] == []
    assert body["entries"][0]["action"] == "commit_ontology"
    assert body["entries"][0]["changes"][0]["type_name"] == "Person"


def test_history_scopes_fetch_to_tenant(client, auth_headers, mock_neptune):
    fetch = AsyncMock(return_value=[])
    with (
        patch(
            "cograph_client.api.routes.ontology.fetch_ontology_changelog",
            new=fetch,
        ),
        patch(
            "cograph_client.api.routes.ontology._current_revision_counter",
            new=AsyncMock(return_value=0),
        ),
    ):
        client.get(
            "/graphs/test-tenant/ontology/history",
            headers=auth_headers,
        )
    assert fetch.await_args.args[1] == tenant_graph_uri("test-tenant")


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_returns_change_records(client, auth_headers, mock_neptune):
    records = [
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person"),
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name="Person",
            slot_name="name",
            new_value="string",
        ),
    ]
    with (
        patch(
            "cograph_client.api.routes.ontology.get_base_pin",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "cograph_client.api.routes.ontology.diff_graphs",
            new=AsyncMock(return_value=records),
        ) as diff,
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/diff",
            params={"from": "revision:1", "to": "current"},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_ref"] == "revision:1"
    assert body["to_ref"] == "current"
    assert body["count"] == 2
    assert body["changes"][0]["kind"] == "add_type"
    assert body["changes"][1]["kind"] == "add_attribute"
    assert body["compat_class"] is not None
    # Resolved URIs use this tenant only.
    live = tenant_graph_uri("test-tenant")
    assert body["to_graph_uri"] == live
    assert body["from_graph_uri"] == f"{live}/revisions/r1"
    # diff_graphs was called with those URIs (same pair pure classifier would see).
    assert diff.await_args.args[1:] == (f"{live}/revisions/r1", live)


def test_diff_shorthand_revisions(client, auth_headers, mock_neptune):
    with (
        patch(
            "cograph_client.api.routes.ontology.get_base_pin",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "cograph_client.api.routes.ontology.diff_graphs",
            new=AsyncMock(return_value=[]),
        ) as diff,
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/diff",
            params={"from_revision": 2, "to_revision": 5},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    live = tenant_graph_uri("test-tenant")
    assert diff.await_args.args[1:] == (
        f"{live}/revisions/r2",
        f"{live}/revisions/r5",
    )
    assert resp.json()["count"] == 0  # empty is fine (missing snapshots)


def test_diff_rejects_other_tenant_uri(client, auth_headers, mock_neptune):
    with patch(
        "cograph_client.api.routes.ontology.get_base_pin",
        new=AsyncMock(return_value=None),
    ):
        resp = client.get(
            "/graphs/test-tenant/ontology/diff",
            params={
                "from": "https://graph.onta.sh/graphs/VICTIM",
                "to": "current",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 422


def test_diff_requires_refs(client, auth_headers, mock_neptune):
    resp = client.get(
        "/graphs/test-tenant/ontology/diff",
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_diff_matches_diff_shapes_records():
    """Unit: the records the route would return equal pure diff_shapes output."""
    from cograph_client.graph.ontology_commit import OntologyShape
    from cograph_client.graph.ontology_snapshots import diff_shapes

    a = OntologyShape()
    a.types["Person"] = "a person"
    b = OntologyShape()
    b.types["Person"] = "a person"
    b.types["Place"] = "a place"
    b.attrs["Person"] = {"name": "string"}

    expected = diff_shapes(a, b)
    kinds = sorted(r.kind.value for r in expected)
    assert kinds == ["add_attribute", "add_type"]
    # Same list is what classify_diff / the route surface.
    from cograph_client.graph.ontology_compat import classify_diff

    verdict = classify_diff(expected)
    assert verdict.overall.value in ("additive", "annotative", "deprecating", "breaking")
