"""Source adapter protocol and registry.

Adapters self-describe their COST so the OSS cost model stays generic (COG-123):
the agent's enrich planner sums an adapter's declared ``cost_per_call`` over the
resolved tier chain to estimate the paid spend of a job, WITHOUT knowing the name
of any specific (proprietary) paid adapter. Free adapters (e.g. Wikidata) declare
nothing and default to ``is_paid=False`` / ``cost_per_call=0.0`` — so a downstream
deployment registers a paid adapter (Exa, Parallel, …) exactly the way it does
today, plus two optional class attributes, and the cost estimate becomes honest
with no OSS code change and no hardcoded adapter names.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from cograph_client.enrichment.models import Verdict


@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    # OSS cost signal (COG-123). OPTIONAL — both default to "free" so existing
    # adapters that don't declare them are treated as zero-cost. A paid adapter
    # sets ``is_paid = True`` and ``cost_per_call`` to its per-entity-lookup USD
    # cost; the planner reads these via :func:`adapter_cost` (getattr with
    # defaults) so an adapter need not actually carry the attributes.
    is_paid: bool
    cost_per_call: float

    async def lookup(
        self, entity_label: str, attribute: str, context: dict
    ) -> list[Verdict]: ...


_adapters: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    _adapters[adapter.name] = adapter


def get_adapter(name: str) -> Optional[SourceAdapter]:
    return _adapters.get(name)


def list_adapters() -> list[str]:
    return list(_adapters.keys())


def adapter_cost(adapter: SourceAdapter) -> tuple[bool, float]:
    """Read an adapter's declared cost signal generically (COG-123).

    Returns ``(is_paid, cost_per_call)``. Reads are defensive ``getattr`` with
    free defaults, so an adapter that declares neither attribute (the OSS
    Wikidata adapter, any legacy adapter) is correctly treated as free. An
    adapter is considered paid if it explicitly sets ``is_paid = True`` OR
    declares a positive ``cost_per_call`` — so either signal alone is enough.
    Never raises on a malformed/non-numeric ``cost_per_call``; it coerces to 0.0.
    """
    try:
        cost = float(getattr(adapter, "cost_per_call", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost < 0.0:
        cost = 0.0
    is_paid = bool(getattr(adapter, "is_paid", False)) or cost > 0.0
    return is_paid, cost
