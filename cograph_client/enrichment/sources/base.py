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
from cograph_client.retrieval.cost import source_cost


@runtime_checkable
class SourceAdapter(Protocol):
    """Protocol for an enrichment source adapter.

    REQUIRED: ``name`` and ``lookup``.

    OPTIONAL: ``is_paid`` and ``cost_per_call`` are the OSS cost signal
    (COG-123). They are NOT required — the planner reads them via
    :func:`adapter_cost`, which uses ``getattr`` with safe defaults
    (``is_paid=False`` / ``cost_per_call=0.0``). A third-party or legacy
    adapter that declares neither is correctly treated as FREE; it does not
    need to carry these attributes at all. A paid adapter opts in by setting
    ``is_paid = True`` and/or ``cost_per_call`` to its per-entity-lookup USD
    cost (either signal alone marks the adapter paid).
    """

    name: str
    # Optional cost signal — see the class docstring. Declared here for typing /
    # documentation only; defaulted to free in :func:`adapter_cost`.
    is_paid: bool
    cost_per_call: float

    async def lookup(
        self, entity_label: str, attribute: str, context: dict
    ) -> list[Verdict]:
        """Resolve ``attribute`` for ``entity_label`` into zero or more verdicts.

        ``context`` is a lookup-time dict the executor populates with optional
        hints. Every key is OPTIONAL — the executor only sets a key when the job
        carries the corresponding value, so an adapter MUST treat a missing key
        as "not provided" (``context.get(key)``) and MUST NOT change behavior
        just because a key is absent. Free/legacy adapters (e.g. Wikidata) ignore
        the whole dict harmlessly. Current keys:

        - ``instructions`` (str): free-text user guidance for agentic/premium
          adapters. Absent when the job supplied none.
        - ``target_urls`` (list[str]): explicit page(s) to read values FROM, for
          URL-aware adapters (e.g. Firecrawl). Absent when the job supplied none.
        - ``entity_type`` (str): the job's canonical entity TYPE label — a bare
          declared type name in the tenant's ontology casing (e.g. ``"Restaurant"``,
          ``"Person"``, ``"Product"``), NOT a URI and NOT lowercased. Lets a
          type-aware adapter self-exclude on entities it can't serve (e.g. a
          place source skipping a Person). Absent when the job carries no type;
          adapters MUST fall back to their type-agnostic behavior when it's
          missing rather than over-excluding.
        """
        ...


_adapters: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    _adapters[adapter.name] = adapter


def get_adapter(name: str) -> Optional[SourceAdapter]:
    return _adapters.get(name)


def list_adapters() -> list[str]:
    return list(_adapters.keys())


def adapter_cost(adapter: SourceAdapter) -> tuple[bool, float]:
    """Read an adapter's declared cost signal → ``(is_paid, cost_per_call)`` (COG-123).

    Thin back-compat alias over the one shared :func:`source_cost` seam (ONTA-193
    P2); discovery, enrichment, and the fetch ladder now all delegate there so the
    cost model never forks by rail.
    """
    return source_cost(adapter)
