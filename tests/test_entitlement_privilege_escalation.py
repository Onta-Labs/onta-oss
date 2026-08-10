"""Privilege-escalation + fail-closed tests for Enhanced-layer entitlement (ONTA-398).

The point of this ticket: nothing a free / OSS client can send may raise
Enhanced visibility. ``is_entitled`` is the single predicate; the default is
False when no premium checker is registered; LayerStack silently degrades to
``(TENANT, PUBLIC)``.

Planted-violation self-test: a checker that would grant Enhanced is only
honoured when registered through the seam — never via headers / query params.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from infona_client.auth.api_keys import (
    AuthVerdict,
    TenantContext,
    _resolve_allowed,
    get_tenant,
    register_external_verifier,
)
from infona_client.graph.entitlement import (
    get_entitlement_checker,
    is_entitled,
    layer_stack_for,
    register_entitlement_checker,
)
from infona_client.graph.layers import Layer, enhanced_graph_uri
from infona_client.graph.queries import tenant_graph_uri


@pytest.fixture(autouse=True)
def _clear_seams():
    """Every test starts with a clean OSS package: no checker, no verifier."""
    register_entitlement_checker(None)
    register_external_verifier(None)
    yield
    register_entitlement_checker(None)
    register_external_verifier(None)


def _tenant(tid: str = "free", **kw) -> TenantContext:
    return TenantContext(tenant_id=tid, api_key="k", **kw)


# --------------------------------------------------------------------------- #
# OSS default + silent degradation
# --------------------------------------------------------------------------- #


def test_oss_package_never_entitled_without_plugin():
    """No premium plugin registered → every workspace is free forever."""
    assert get_entitlement_checker() is None
    assert is_entitled(_tenant("paid-looking-name")) is False
    assert is_entitled(_tenant("demo-tenant", is_operator=True)) is False
    assert is_entitled(_tenant("x", enhanced_entitled=True)) is False


def test_layer_stack_for_non_entitled_is_tenant_public_only():
    stack = layer_stack_for(_tenant("acme"))
    assert stack.layers == (Layer.TENANT, Layer.PUBLIC)
    assert Layer.ENHANCED not in stack.layers
    assert enhanced_graph_uri() not in stack.visible_graph_uris()
    assert tenant_graph_uri("acme") in stack.visible_graph_uris()


def test_layer_stack_for_entitled_includes_enhanced():
    register_entitlement_checker(lambda t: t.tenant_id == "paid")
    stack = layer_stack_for(_tenant("paid"))
    assert stack.layers == (Layer.TENANT, Layer.ENHANCED, Layer.PUBLIC)
    assert enhanced_graph_uri() in stack.visible_graph_uris()


def test_non_entitled_degrades_silently_never_errors():
    """A free workspace still gets a complete stack — just without Enhanced."""
    stack = layer_stack_for(_tenant("free"))
    # Exactly two layers, both addressable; no exception, no partial state.
    assert len(stack.layers) == 2
    assert stack.graph_uri_for(Layer.TENANT) == tenant_graph_uri("free")
    assert stack.resolve_type("Person", {Layer.PUBLIC: {"Person": "pub"}}) == (
        Layer.PUBLIC,
        "pub",
    )
    # Enhanced definition is invisible even if present in the by-layer map.
    assert (
        stack.resolve_type(
            "Person",
            {
                Layer.ENHANCED: {"Person": "secret"},
                Layer.PUBLIC: {"Person": "pub"},
            },
        )
        == (Layer.PUBLIC, "pub")
    )


# --------------------------------------------------------------------------- #
# Privilege escalation: forged header / query / deep link / client flag
# --------------------------------------------------------------------------- #


def test_forged_enhanced_entitled_flag_alone_does_not_grant():
    """``TenantContext.enhanced_entitled`` is only meaningful when a premium
    checker is registered that reads it. OSS with no plugin ignores the bit
    (the bit can only be set by a provider — but even a hand-built context
    with the bit True stays non-entitled without a checker)."""
    forged = _tenant("free", enhanced_entitled=True)
    assert is_entitled(forged) is False
    assert Layer.ENHANCED not in layer_stack_for(forged).layers


def test_forged_header_names_are_not_consulted_by_is_entitled():
    """``is_entitled`` takes only TenantContext — no Request, no headers.

    A client that sends ``X-Enhanced-Entitled: true`` / ``X-Entitled: 1`` has
    nowhere to put it: the frozen signature has a single ``tenant`` param.
    """
    sig = inspect.signature(is_entitled)
    assert list(sig.parameters) == ["tenant"]
    # And the body never reads os.environ client-style keys either by default.
    assert is_entitled(_tenant("free")) is False


def test_query_param_layer_enhanced_cannot_raise_entitlement():
    """A ``?layer=enhanced`` query string is not an input to ``is_entitled``.

    Skills / ontology routes may accept filter params for other reasons, but
    the entitlement predicate has no layer argument — so a deep link or
    query param literally cannot open Enhanced.
    """
    sig = inspect.signature(is_entitled)
    assert "layer" not in sig.parameters
    assert "request" not in sig.parameters
    # Even a tenant_id that *looks* like a layer request stays free.
    assert is_entitled(_tenant("enhanced")) is False
    assert is_entitled(_tenant("?layer=enhanced")) is False


def test_operator_bit_alone_does_not_grant_enhanced():
    """Operator (staff) and Enhanced (paid) are different axes (ONTA-234 vs 398)."""
    assert is_entitled(_tenant("staff", is_operator=True)) is False
    register_entitlement_checker(
        lambda t: bool(getattr(t, "enhanced_entitled", False))
    )
    # Operator without the enhanced bit still free.
    assert is_entitled(_tenant("staff", is_operator=True)) is False
    assert is_entitled(_tenant("paid", enhanced_entitled=True)) is True


def test_auth_verdict_entitled_tenants_resolve_per_workspace():
    """Provider-stamped entitled_tenants only flip the selected workspace."""
    ctx_paid = _resolve_allowed(
        ["paid", "free"],
        "paid",
        "k",
        subject="user_1",
        entitled_tenants=["paid"],
    )
    ctx_free = _resolve_allowed(
        ["paid", "free"],
        "free",
        "k",
        subject="user_1",
        entitled_tenants=["paid"],
    )
    assert ctx_paid.enhanced_entitled is True
    assert ctx_free.enhanced_entitled is False

    register_entitlement_checker(
        lambda t: bool(getattr(t, "enhanced_entitled", False))
    )
    assert is_entitled(ctx_paid) is True
    assert is_entitled(ctx_free) is False


def test_client_cannot_inject_entitled_tenants_via_static_key(monkeypatch):
    """Static API keys never carry enhanced_entitled (defaults False)."""
    import json

    from infona_client.config import settings

    monkeypatch.setattr(
        settings, "api_keys", json.dumps({"static-key": "acme"})
    )
    ctx = get_tenant(tenant="acme", api_key="static-key", request=None)
    assert ctx.enhanced_entitled is False
    assert is_entitled(ctx) is False


def test_raw_triples_admin_api_scopes_to_tenant_graph_only():
    """SPARQL passthrough (triples routes) only ever targets the tenant graph.

    A free client cannot reach ``graphs/global/enhanced`` through the admin
    triples API — the route hardcodes ``tenant_graph_uri(tenant.tenant_id)``.
    """
    import infona_client.api.routes.triples as triples_mod

    src = inspect.getsource(triples_mod)
    assert "tenant_graph_uri" in src
    assert "enhanced_graph_uri" not in src
    assert "global/enhanced" not in src
    # And the helper itself only ever mints the tenant namespace.
    assert tenant_graph_uri("acme") == "https://graph.onta.sh/graphs/acme"
    assert "enhanced" not in tenant_graph_uri("acme")


def test_mcp_surface_has_no_side_channel_into_entitlement():
    """MCP reaches the backend through the same routes / ``is_entitled`` seam.

    There is no MCP-local entitlement override: any future MCP tool that
    exposed Enhanced content must call ``is_entitled`` server-side. Pin that
    the frozen seam still defaults closed when the package is used alone.
    """
    assert is_entitled(_tenant("mcp-client")) is False
    stack = layer_stack_for(_tenant("mcp-client"))
    assert Layer.ENHANCED not in stack.layers


# --------------------------------------------------------------------------- #
# Flip mid-session (checker re-evaluation; no restart)
# --------------------------------------------------------------------------- #


def test_gain_and_lose_entitlement_mid_session_without_restart():
    """Register / clear / re-register the checker; next call sees the new truth."""
    t = _tenant("acme")
    assert is_entitled(t) is False

    register_entitlement_checker(lambda tenant: tenant.tenant_id == "acme")
    assert is_entitled(t) is True
    assert Layer.ENHANCED in layer_stack_for(t).layers

    # Lose entitlement: checker now denies.
    register_entitlement_checker(lambda tenant: False)
    assert is_entitled(t) is False
    assert Layer.ENHANCED not in layer_stack_for(t).layers

    # Clear entirely → OSS default.
    register_entitlement_checker(None)
    assert is_entitled(t) is False


def test_flip_via_enhanced_entitled_bit_without_restart():
    """Bit flip on a new TenantContext (next request) is enough — no restart."""
    register_entitlement_checker(
        lambda t: bool(getattr(t, "enhanced_entitled", False))
    )
    assert is_entitled(_tenant("acme", enhanced_entitled=False)) is False
    assert is_entitled(_tenant("acme", enhanced_entitled=True)) is True
    # Lose it again on the following "request".
    assert is_entitled(_tenant("acme", enhanced_entitled=False)) is False


def test_buggy_checker_fails_closed_not_open():
    def _boom(_t: TenantContext) -> bool:
        raise RuntimeError("billing provider down")

    register_entitlement_checker(_boom)
    assert is_entitled(_tenant("paid")) is False
    assert Layer.ENHANCED not in layer_stack_for(_tenant("paid")).layers


def test_auth_verdict_defaults_entitled_tenants_empty():
    """A bare AuthVerdict (legacy callers) must fail closed on entitlement."""
    v = AuthVerdict(tenants=["acme"], subject="u")
    assert list(v.entitled_tenants) == []
    ctx = _resolve_allowed(v.tenants, "acme", "k", subject=v.subject,
                           is_operator=v.is_operator,
                           entitled_tenants=v.entitled_tenants)
    assert ctx.enhanced_entitled is False


def test_unowned_tenant_still_403_regardless_of_entitlement():
    """Entitlement cannot bypass the tenant-grant check."""
    with pytest.raises(HTTPException) as ei:
        _resolve_allowed(
            ["allowed"],
            "other",
            "k",
            entitled_tenants=["other"],  # even if "entitled" for other
        )
    assert ei.value.status_code == 403
