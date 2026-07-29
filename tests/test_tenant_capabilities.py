"""Tenant-level read/write capability (membership roles)."""

from cograph_client.auth.capabilities import (
    INVITABLE_ROLES,
    can_admin_members,
    can_write,
    capability_for_role,
    normalize_role,
)


def test_normalize_legacy_member_is_writer():
    assert normalize_role("member") == "writer"
    assert normalize_role("MEMBER") == "writer"


def test_normalize_canonical():
    assert normalize_role("owner") == "owner"
    assert normalize_role("writer") == "writer"
    assert normalize_role("reader") == "reader"


def test_capability_mapping():
    assert capability_for_role("owner") == "write"
    assert capability_for_role("writer") == "write"
    assert capability_for_role("member") == "write"
    assert capability_for_role("reader") == "read"


def test_can_write_and_admin():
    assert can_write("writer") and can_write("owner") and not can_write("reader")
    assert can_admin_members("owner") and not can_admin_members("writer")
    assert not can_admin_members("reader")


def test_invitable_roles():
    assert INVITABLE_ROLES == frozenset({"writer", "reader"})
    assert "owner" not in INVITABLE_ROLES
    assert "member" not in INVITABLE_ROLES


import asyncio

import pytest
from fastapi import HTTPException

from cograph_client.auth.access import require_tenant_write, resolve_member_role
from cograph_client.auth.api_keys import TenantContext
from cograph_client.auth.workspace_store import make_workspace_store


def _run(coro):
    # Python 3.12+: get_event_loop() no longer auto-creates a loop on MainThread.
    return asyncio.run(coro)


def test_resolve_member_role_reader_and_writer():
    store = make_workspace_store()
    _run(store.claim_workspace("cap-ws", "user_owner", "Cap"))
    _run(store.add_member("cap-ws", "user_r", "reader"))
    _run(store.add_member("cap-ws", "user_w", "writer"))
    assert _run(resolve_member_role("cap-ws", "user_owner")) == "owner"
    assert _run(resolve_member_role("cap-ws", "user_r")) == "reader"
    assert _run(resolve_member_role("cap-ws", "user_w")) == "writer"
    assert _run(resolve_member_role("cap-ws", None)) == "writer"


def test_require_tenant_write_blocks_reader():
    store = make_workspace_store()
    _run(store.claim_workspace("cap-ws2", "user_owner", "Cap2"))
    _run(store.add_member("cap-ws2", "user_r", "reader"))
    reader_ctx = TenantContext(
        tenant_id="cap-ws2", api_key="k", subject="user_r"
    )
    with pytest.raises(HTTPException) as ei:
        _run(require_tenant_write(reader_ctx))
    assert ei.value.status_code == 403

    writer_ctx = TenantContext(
        tenant_id="cap-ws2", api_key="k", subject="user_owner"
    )
    out = _run(require_tenant_write(writer_ctx))
    assert out.capability == "write" and out.role == "owner"
