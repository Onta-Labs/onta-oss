"""Global-Enhanced entitlement seam — Wave 0 contract freeze for ONTA-396 / ONTA-398.

Does this workspace see the Enhanced layer?

* **OSS default:** ``False`` for every caller. Resolution degrades to
  ``Tenant > Public``, never errors (ADR 0002 §1, ``LayerStack``).
* **Premium determination** (which tenants are entitled, how billing maps, …)
  is proprietary (boundary doc §5 / §28). It plugs in here via
  :func:`register_entitlement_checker` at app startup — same plugin shape as
  :func:`~cograph_client.auth.api_keys.register_external_verifier` and the
  operator bit on :class:`~cograph_client.auth.api_keys.TenantContext`.

**Location freeze:** this module is the ONE place entitlement is decided for
the layered-ontology epic. Callers (skills routes, ONTA-397 layered reads,
future MCP tool gating) MUST call :func:`is_entitled` rather than hardcoding
``False`` or inventing a second seam. ONTA-397 and ONTA-398 both touch this
signature; freezing it here is what lets them land without colliding.

Signature (frozen):

* ``is_entitled(tenant: TenantContext) -> bool``
* ``register_entitlement_checker(checker: EntitlementChecker | None) -> None``
* ``EntitlementChecker = Callable[[TenantContext], bool]``

**Call sites that must use this predicate** (document for ONTA-397+; wire any
site that already constructs a ``LayerStack`` with ``entitled=``):

* ``api/routes/skills.py::_entitled`` — thin wrapper; every skills list /
  resolve / prompt-block path builds ``LayerStack(entitled=…)`` from it.
* ``skills/resolve.py::resolve_skills`` — takes ``entitled: bool`` from the
  route; the route is the one that calls :func:`is_entitled`.
* Any future layered ontology read (ONTA-397 ``fetch_ontology`` /
  workspace response): pass ``entitled=is_entitled(tenant)`` into
  :class:`~cograph_client.graph.layers.LayerStack`, or use
  :func:`layer_stack_for`.
* Future MCP / agent surfaces that expose Enhanced content must call
  :func:`is_entitled` server-side; client flags, ``?layer=enhanced``, and
  deep links MUST NOT raise entitlement.
"""

from __future__ import annotations

from typing import Callable, Optional

from cograph_client.auth.api_keys import TenantContext

#: Premium (or test) determination. Takes the full auth context so a provider
#: can key off ``tenant_id``, ``subject``, ``is_operator``,
#: ``enhanced_entitled``, etc. without a second lookup. Returns True only when
#: Enhanced is visible.
EntitlementChecker = Callable[[TenantContext], bool]

_checker: Optional[EntitlementChecker] = None


def register_entitlement_checker(checker: Optional[EntitlementChecker]) -> None:
    """Register (or clear, with ``None``) the Enhanced-layer entitlement checker.

    Premium code calls this once at startup. OSS deployments never do; the
    default stays ``False`` for everyone.
    """
    global _checker
    _checker = checker


def get_entitlement_checker() -> Optional[EntitlementChecker]:
    """The registered checker, if any. Exposed for tests and diagnostics."""
    return _checker


def is_entitled(tenant: TenantContext) -> bool:
    """Does this caller see the Global-Enhanced ontology layer?

    OSS answers ``False`` for everyone when no checker is registered. A
    registered checker is authoritative; its return value is coerced to bool.
    Never raises — a buggy premium checker that raises is treated as not
    entitled (fail closed), so a provider outage cannot open Enhanced to a
    non-paying workspace.

    Never consults client-supplied headers, query parameters, or path
    segments. Only the registered checker (premium: verified identity +
    env allowlist) may return True.
    """
    checker = _checker
    if checker is None:
        return False
    try:
        return bool(checker(tenant))
    except Exception:
        return False


def layer_stack_for(tenant: TenantContext):
    """Build the :class:`~cograph_client.graph.layers.LayerStack` for ``tenant``.

    THE construction helper layered reads should use (ONTA-397+). Non-entitled
    workspaces get ``(TENANT, PUBLIC)``; entitled get
    ``(TENANT, ENHANCED, PUBLIC)``. Silent total degradation — never raises
    and never invents a third stack shape.
    """
    from cograph_client.graph.layers import LayerStack
    from cograph_client.graph.queries import tenant_graph_uri

    return LayerStack(
        tenant_graph_uri=tenant_graph_uri(tenant.tenant_id),
        entitled=is_entitled(tenant),
    )
