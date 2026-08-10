"""Promotion-consent seam — refuse tenant→global writes without explicit consent (ONTA-402a).

Product rule (founder / ONTA-402 / plan §5): a workspace must never write into a
global layer (Public A or Enhanced B) through ordinary ontology mutation.
Only the **governed promotion path** may, and only with a recorded,
per-workspace consent. Default = no consent. No consent → no promotion write.

This module is the **OSS refuse-without-consent seam**:

* **Default provider always denies.** OSS deployments never promote into shared
  layers unless something registers a provider that grants.
* **Premium determination** (durable store, who/when/what audit, grant UI) plugs
  in via :func:`register_promotion_consent_provider` — same plugin shape as
  :func:`~infona_client.graph.entitlement.register_entitlement_checker` and
  :func:`register_governance_panel`. Premium code lives in ``cograph/``; this
  module never imports ``cograph.*``.

Every path that writes tenant-originated shape/type content into a global
named graph (``write_governed_type``, premium ``GlobalShapeWriter``) MUST call
:func:`require_promotion_consent` before the first ``insert_triples`` into that
graph. A structural drift guard
(``tests/test_promotion_consent_guard.py``) fails CI if a new writer reintroduces
an ungated global write.
"""

from __future__ import annotations

from typing import Optional, Protocol

import structlog

logger = structlog.stdlib.get_logger("cograph.resolver.promotion_consent")


class PromotionConsentError(PermissionError):
    """Raised when a global-layer promotion is attempted without workspace consent.

    Subclass of ``PermissionError`` so callers can distinguish "not allowed"
    from ordinary ``ValueError`` validation failures (unapproved decision,
    missing payload). The write path must not catch-and-swallow this into a
    successful write.
    """


class PromotionConsentProvider(Protocol):
    """Pluggable consent determination — premium durable store implements this.

    ``has_consent`` is async so a Postgres-backed provider can query without
    blocking the event loop. Fail closed: any exception from the provider is
    treated as no consent by :func:`has_promotion_consent`.
    """

    async def has_consent(
        self,
        tenant_id: str,
        *,
        target_layer: str = "public",
    ) -> bool:
        """True iff ``tenant_id`` has granted promotion into ``target_layer``.

        ``target_layer`` is the ontology layer value (``\"public\"`` /
        ``\"enhanced\"``). Workspace-wide grants typically cover both.
        """
        ...


class DenyAllPromotionConsent:
    """OSS default: every workspace has no consent. Zero configuration."""

    async def has_consent(
        self,
        tenant_id: str,
        *,
        target_layer: str = "public",
    ) -> bool:
        return False


_provider: Optional[PromotionConsentProvider] = None
_DENY_ALL = DenyAllPromotionConsent()


def register_promotion_consent_provider(
    provider: Optional[PromotionConsentProvider],
) -> None:
    """Register (or clear, with ``None``) the promotion-consent provider.

    Premium code calls this once at startup with a durable store. OSS
    deployments never do; the default stays deny-all.
    """
    global _provider
    _provider = provider
    logger.info(
        "promotion_consent_provider_registered",
        provider=type(provider).__name__ if provider is not None else None,
    )


def get_promotion_consent_provider() -> PromotionConsentProvider:
    """The registered provider, or the deny-all default. Exposed for tests."""
    return _provider if _provider is not None else _DENY_ALL


async def has_promotion_consent(
    tenant_id: str,
    *,
    target_layer: str = "public",
) -> bool:
    """Does this workspace have recorded consent to promote into ``target_layer``?

    Default (no provider) is False. A provider exception is treated as False
    (fail closed) so an outage cannot open the global write path.
    """
    if not tenant_id:
        return False
    provider = get_promotion_consent_provider()
    try:
        return bool(await provider.has_consent(tenant_id, target_layer=target_layer))
    except Exception:
        logger.warning(
            "promotion_consent_provider_failed",
            tenant=tenant_id,
            target_layer=target_layer,
            exc_info=True,
        )
        return False


async def require_promotion_consent(
    tenant_id: str,
    *,
    target_layer: str = "public",
    what: str = "",
) -> None:
    """Raise :class:`PromotionConsentError` unless the workspace has consent.

    Writers call this **before** any global-graph insert so the refuse path
    is structural: even a fire-and-forget async panel that forgets its own
    check still cannot write. The error message always names the rule rather
    than a hand-rolled check.
    """
    if await has_promotion_consent(tenant_id, target_layer=target_layer):
        return
    detail = f" ({what})" if what else ""
    raise PromotionConsentError(
        f"workspace {tenant_id!r} has no recorded consent to promote into "
        f"the {target_layer} layer{detail}; default is no consent "
        f"(ONTA-402a / governed promotion gate). Grant consent explicitly "
        f"before any tenant-originated shape is written into Public or Enhanced."
    )
