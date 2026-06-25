"""Web-source provider protocol and registry.

A *web-source provider* turns a natural-language discovery query
("a list of models offered by OpenRouter") into a uniform table of records —
the same ``list[dict]`` shape CSV ingest produces — which the web-discovery
capability then commits through the standard ingest pipeline
(:meth:`cograph_client.resolver.schema_resolver.SchemaResolver.ingest_mapped_records`).

This is the discovery counterpart to the enrichment adapter protocol
(:mod:`cograph_client.enrichment.sources.base`). The split matters: enrichment
fills a missing ``(entity, attribute)`` cell on entities that ALREADY exist;
discovery CREATES a whole set of new entities from a query. Different I/O shape
(there is no ``entity_label`` when the rows don't exist yet), so it gets its own
protocol — but the same plugin pattern: OSS defines the seam, a downstream
(proprietary) deployment registers a paid provider at boot.

Providers self-describe their COST the same generic way adapters do (COG-123):
the planner reads ``is_paid`` / ``cost_per_call`` via :func:`provider_cost`
(defensive ``getattr`` with free defaults), so the OSS cost model never hardcodes
the name of any specific paid provider.

OSS ships with NO provider registered. The web-discovery capability degrades
gracefully when :func:`get_web_source` returns ``None`` (the same no-op pattern
the ``suggest-relationships`` action uses without a registered recommender).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class DiscoverResult:
    """The output of a discovery run — a table of records, like CSV rows.

    ``rows`` are uniform string-keyed dicts (one record each), ready to feed the
    SAME schema-inference / ``apply_mapping`` path CSV ingest uses. ``provenance``
    maps a row's natural key (or its index as a string) to the source URL the row
    was drawn from, so every committed entity can carry a ``*_source_url``
    companion. ``sources`` is the distinct set of sources consulted (for the plan
    preview). ``is_partial`` is True when the provider truncated at ``max_rows``;
    ``estimated_total`` is the provider's best guess at the full result size
    (used only to label the plan-time cost estimate, never to drive writes).
    """

    rows: list[dict[str, str]] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    is_partial: bool = False
    estimated_total: Optional[int] = None


@runtime_checkable
class WebSourceProvider(Protocol):
    """Protocol for a web-discovery provider.

    REQUIRED: ``name`` and ``discover``.

    OPTIONAL: ``is_paid`` / ``cost_per_call`` — the OSS cost signal (COG-123),
    read generically via :func:`provider_cost`. A provider that declares neither
    is treated as FREE. A paid provider opts in by setting ``is_paid = True``
    and/or a positive ``cost_per_call`` (the per-discovery-call USD cost). Either
    signal alone marks the provider paid.
    """

    name: str
    # Optional cost signal — declared for typing/documentation; defaulted to free
    # in :func:`provider_cost`.
    is_paid: bool
    cost_per_call: float

    async def discover(
        self,
        query: str,
        *,
        sample: bool,
        max_rows: int,
        hint_columns: Optional[list[str]],
        context: dict,
    ) -> DiscoverResult:
        """Find records on the web matching ``query``.

        ``sample=True`` asks for a small, representative slice (a handful of rows)
        cheap enough to drive the plan-time preview + schema inference; the full
        pull (``sample=False``) must be drawn the SAME way so the previewed schema
        matches the committed one. ``max_rows`` caps the result; ``hint_columns``
        are optional desired fields the user named; ``context`` carries
        tenant/kg/type hints the provider may use.
        """
        ...


# Module-level registry — same shape as register_adapter / register_capability.
_providers: dict[str, WebSourceProvider] = {}


def register_web_source(provider: WebSourceProvider) -> None:
    """Register (or replace) a web-source provider by name. Idempotent."""
    _providers[provider.name] = provider


def get_web_source(name: Optional[str] = None) -> Optional[WebSourceProvider]:
    """Return a provider by ``name``, or the sole registered provider when
    ``name`` is omitted and exactly one is registered, else ``None``.

    The no-name single-provider convenience lets the capability stay decoupled
    from provider names: OSS registers none (returns ``None`` → graceful
    degradation), a deployment registers exactly one paid provider and it is
    selected automatically.
    """
    if name is not None:
        return _providers.get(name)
    if len(_providers) == 1:
        return next(iter(_providers.values()))
    return None


def list_web_sources() -> list[str]:
    return list(_providers.keys())


def reset_web_sources() -> None:
    """Clear the registry. For tests."""
    _providers.clear()


def provider_cost(provider: WebSourceProvider) -> tuple[bool, float]:
    """Read a provider's declared cost signal generically (COG-123).

    Returns ``(is_paid, cost_per_call)``. Defensive ``getattr`` with free
    defaults, so a provider that declares neither attribute is treated as free.
    Paid if it sets ``is_paid = True`` OR a positive ``cost_per_call``. Never
    raises on a malformed/non-numeric ``cost_per_call``; coerces to 0.0.
    """
    try:
        cost = float(getattr(provider, "cost_per_call", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost < 0.0:
        cost = 0.0
    is_paid = bool(getattr(provider, "is_paid", False)) or cost > 0.0
    return is_paid, cost


__all__ = [
    "DiscoverResult",
    "WebSourceProvider",
    "get_web_source",
    "list_web_sources",
    "provider_cost",
    "register_web_source",
    "reset_web_sources",
]
