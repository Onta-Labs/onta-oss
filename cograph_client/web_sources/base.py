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

from cograph_client.retrieval.cost import source_cost


@dataclass
class DiscoverResult:
    """The output of a discovery run — a table of records, like CSV rows.

    ``rows`` are uniform string-keyed dicts (one record each), ready to feed the
    SAME schema-inference / ``apply_mapping`` path CSV ingest uses. ``provenance``
    maps each row to the source URL it was drawn from, so every committed entity
    can carry a per-record ``source_url`` citation (the discovery counterpart to
    enrichment's ``<attr>_source_url``). The map is keyed by the row's natural
    name, falling back to its index as a string — i.e. ``{r.get("name", str(i)):
    url}``, the convention all bundled adapters + the stub follow. The web-ingest
    capability resolves a row's URL by that same key (name, then positional
    index — see ``web_ingest_cap._row_source_url``), so an index-keyed provider
    resolves too; populate it however your source allows. ``sources`` is the
    distinct set of sources consulted (for the plan preview). ``is_partial`` is
    True when the provider truncated at ``max_rows``; ``estimated_total`` is the
    provider's best guess at the full result size (used only to label the
    plan-time cost estimate, never to drive writes). It may be an UPPER bound —
    e.g. a source-first provider counts the located catalogue before projecting
    it to the query — and providers with no total signal should leave it None
    (unknown) rather than echo a sample-capped row count.

    ``error`` is set when the provider FAILED to reach or read a source (timeout,
    non-200, blocked page) as opposed to reaching it and finding no records. Zero
    rows with ``error=None`` means "read the page(s), nothing to extract"; zero
    rows with ``error`` set means "couldn't read the page(s)". The capability
    uses this to give an honest, source-appropriate message instead of a generic
    "found nothing on the web" — the two cases warrant different user advice.
    """

    rows: list[dict[str, str]] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    is_partial: bool = False
    estimated_total: Optional[int] = None
    error: Optional[str] = None


@runtime_checkable
class WebSourceProvider(Protocol):
    """Protocol for a web-discovery provider.

    REQUIRED: ``name`` and ``discover``.

    OPTIONAL: ``is_paid`` / ``cost_per_call`` — the OSS cost signal (COG-123),
    read generically via :func:`provider_cost`. A provider that declares neither
    is treated as FREE. A paid provider opts in by setting ``is_paid = True``
    and/or a positive ``cost_per_call``. Either signal alone marks the provider
    paid.

    ``cost_per_call`` is the USD cost of ONE paid REQUEST. A provider that FANS OUT
    a discovery run across several paginated requests (e.g. a Text-Search API that
    returns ~20 records per request) also declares ``rows_per_call: int`` — the
    number of records ONE paid request yields — so the plan-time cost estimate can
    price the whole run as ``cost_per_call × ceil(rows / rows_per_call)`` instead of
    a single call. Read DEFENSIVELY via ``getattr(provider, "rows_per_call", 0)``;
    unset / ``0`` means "one paid call per run" (the backward-compatible default for
    a provider whose run is a single billed call), so existing providers are
    unaffected. Generic: OSS prices ANY paginating paid provider from these two
    numbers without knowing the provider.

    OPTIONAL (URL-targeted extraction): two boolean attributes, both read
    DEFENSIVELY elsewhere via ``getattr(provider, ..., False)`` so a provider that
    declares neither stays a plain query-discovery provider:

    - ``supports_urls: bool`` — the provider can extract records from explicit
      URLs (passed to :meth:`discover` via the ``urls`` kwarg) instead of (or in
      addition to) web-searching for ``query``. :func:`get_web_source` with
      ``for_urls=True`` selects the first provider that sets this.
    - ``url_only: bool`` — the provider ONLY does URL-targeted extraction and is
      never used for plain query discovery. Such a provider is SKIPPED when
      :func:`get_web_source` picks a default query provider, so registering it
      alongside a query provider leaves the no-arg query default unaffected.

    OPTIONAL (query-KIND specialization): ``query_kinds: frozenset[str]`` — the
    generic query CATEGORIES this provider specializes in (default empty). A
    provider that only shines on a particular shape of query declares the kinds it
    handles (e.g. a place/location source sets ``frozenset({"place"})``), and the
    planner routes a query classified into that kind to it via
    :func:`get_web_source_for_kind`, WITHOUT the provider becoming the general
    default (it is never counted as the single no-name query default). Read
    DEFENSIVELY via ``getattr(provider, "query_kinds", frozenset())`` so a provider
    that declares nothing stays a plain, kind-agnostic query provider. The kind
    vocabulary is generic and provider-independent: OSS knows the CATEGORY (a place
    query), never which concrete provider answers it — the same way the enrichment
    tier router decides lite/core without naming an adapter.
    """

    name: str
    # Optional cost signal — declared for typing/documentation; defaulted to free
    # in :func:`provider_cost`. ``rows_per_call`` is the records one paid request
    # yields, so a paginating paid run is priced as cost_per_call × the number of
    # requests; read defensively (default 0 → one paid call per run).
    is_paid: bool
    cost_per_call: float
    rows_per_call: int
    # Optional URL-extraction capability flags — declared for typing/docs; read
    # defensively (default False) wherever a provider is selected/invoked.
    supports_urls: bool
    url_only: bool
    # Optional query-kind specialization — the generic query categories this
    # provider handles; declared for typing/docs, read defensively (default empty)
    # wherever kind-routing selects a provider.
    query_kinds: frozenset[str]

    async def discover(
        self,
        query: str,
        *,
        sample: bool,
        max_rows: int,
        hint_columns: Optional[list[str]],
        context: dict,
        urls: Optional[list[str]] = None,
    ) -> DiscoverResult:
        """Find records on the web matching ``query``.

        ``sample=True`` asks for a small, representative slice (a handful of rows)
        cheap enough to drive the plan-time preview + schema inference; the full
        pull (``sample=False``) must be drawn the SAME way so the previewed schema
        matches the committed one. ``max_rows`` caps the result; ``hint_columns``
        are optional desired fields the user named; ``context`` carries
        tenant/kg/type hints the provider may use.

        ``urls`` is an OPTIONAL list of explicit pages to extract records FROM. When
        non-empty, a URL-capable provider (``supports_urls=True``) EXTRACTS records
        from those pages instead of web-searching for ``query`` (``query`` may
        still carry "what to pull from these pages"). In URL mode the returned
        :class:`DiscoverResult` shape is UNCHANGED, but ``sources`` is the input
        URLs and ``provenance`` maps each row's natural key to the URL it came
        from. A query-only provider may ignore ``urls``.
        """
        ...


# Module-level registry — same shape as register_adapter / register_capability.
_providers: dict[str, WebSourceProvider] = {}


def register_web_source(provider: WebSourceProvider) -> None:
    """Register (or replace) a web-source provider by name. Idempotent."""
    _providers[provider.name] = provider


def get_web_source(
    name: Optional[str] = None, *, for_urls: bool = False
) -> Optional[WebSourceProvider]:
    """Return a provider by ``name``, or select one for the requested mode.

    With ``name`` given, returns that provider (or ``None``). Otherwise selection
    tolerates specialized providers registered alongside the one general default —
    a URL-only extractor and/or a query-KIND-specialized provider:

    - ``for_urls=True`` → the first provider that declares ``supports_urls`` (the
      URL-targeted extractor), or ``None`` if none does.
    - query mode (default) → the sole GENERAL query provider: NOT ``url_only`` and
      NOT kind-specialized (empty ``query_kinds``). A kind-specialized provider is
      reached via :func:`get_web_source_for_kind`, never as the no-name default, so
      it can't hijack queries outside its kind. If no single general provider
      exists it falls back to the lone registered provider (the backward-compatible
      single-provider convenience).

    The no-name conveniences keep the capability decoupled from provider names:
    OSS registers none (returns ``None`` → graceful degradation), a deployment
    registers exactly one general query provider and it is selected automatically;
    adding a ``url_only`` extractor or a kind-specialized provider alongside it does
    not disturb the query default.
    """
    if name is not None:
        return _providers.get(name)
    candidates = list(_providers.values())
    if for_urls:
        for p in candidates:
            if getattr(p, "supports_urls", False):
                return p
        return None
    # Query mode: the single GENERAL query provider — skip url_only extractors AND
    # kind-specialized providers (those are reached by name / by kind, never as the
    # no-name default, so a place source can't answer non-place queries).
    q = [
        p
        for p in candidates
        if not getattr(p, "url_only", False)
        and not getattr(p, "query_kinds", frozenset())
    ]
    if len(q) == 1:
        return q[0]
    # Backward-compatible lone-provider convenience — but only for a GENERAL
    # provider. A lone kind-specialized (or url_only) provider must NOT become the
    # no-name default, or a place source would answer non-place queries.
    if len(_providers) == 1 and q:
        return q[0]
    return None


def get_web_source_for_kind(kind: str) -> Optional[WebSourceProvider]:
    """Return a registered provider that SPECIALIZES in query ``kind``, or ``None``.

    A provider opts into a kind by listing it in ``query_kinds`` (read defensively,
    default empty), so a query the planner has classified into a generic category
    (e.g. ``"place"`` for a location/business-finding query) can be routed to the
    provider built for that shape. Returns the first match in registration order; a
    ``url_only`` provider is skipped (kind routing is for query discovery, not URL
    extraction). ``None`` when no registered provider declares the kind — the caller
    then falls back to the general default, so this is a pure no-op without a
    specialized provider registered.

    Generic by design: OSS routes by the CATEGORY, never by a concrete provider
    name — a downstream deployment registers the provider that answers the kind (the
    same decoupling :func:`get_web_source` keeps for the default query provider)."""
    if not kind:
        return None
    for p in _providers.values():
        if getattr(p, "url_only", False):
            continue
        kinds = getattr(p, "query_kinds", frozenset())
        if kinds and kind in kinds:
            return p
    return None


def has_kind_specialized_provider() -> bool:
    """True if ANY registered provider declares a non-empty ``query_kinds``.

    Lets a caller decide web discovery is AVAILABLE — even when no GENERAL query
    provider is registered — as long as some query can still be served by routing
    it to a kind-specialized provider (a place-only deployment). Generic: it
    reports only that *some* kind specialization exists, never which provider or
    which kind. ``url_only`` providers don't count (kind routing is query
    discovery, not URL extraction)."""
    return any(
        getattr(p, "query_kinds", frozenset()) and not getattr(p, "url_only", False)
        for p in _providers.values()
    )


def list_web_sources() -> list[str]:
    return list(_providers.keys())


def reset_web_sources() -> None:
    """Clear the registry. For tests."""
    _providers.clear()


def provider_cost(provider: WebSourceProvider) -> tuple[bool, float]:
    """Read a provider's declared cost signal → ``(is_paid, cost_per_call)`` (COG-123).

    Thin back-compat alias over the one shared :func:`source_cost` seam (ONTA-193
    P2); discovery, enrichment, and the fetch ladder now all delegate there so the
    cost model never forks by rail.
    """
    return source_cost(provider)


__all__ = [
    "DiscoverResult",
    "WebSourceProvider",
    "get_web_source",
    "get_web_source_for_kind",
    "has_kind_specialized_provider",
    "list_web_sources",
    "provider_cost",
    "register_web_source",
    "reset_web_sources",
]
