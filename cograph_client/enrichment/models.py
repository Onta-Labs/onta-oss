"""Pydantic models and enums for the auto-enrichment feature."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from cograph_client.pipeline.manifest import RunCoverage, RunManifest


# A safe predicate local-name: starts with a letter/underscore, then word chars
# or hyphens. This is the ONLY shape a scope predicate may take — it is later
# matched as an escaped, lower-cased string LITERAL in SPARQL (never spliced into
# an IRI), so this validator + the executor escaping together close the
# injection surface (COG-112 review fix #1/#2).
_SAFE_LOCAL_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")
# A well-formed http(s) IRI with none of the characters that could break out of
# a SPARQL ``<…>`` term (``<``, ``>``, ``"``, ``{``, ``}``, whitespace).
_SAFE_IRI_RE = re.compile(r'^https?://[^\s<>"{}]+$')


def _validate_entity_uris_field(value):
    """Reusable field validator for an optional ``entity_uris`` list: each entry
    must be a safe http(s) IRI. Returns the value unchanged or raises so the API
    rejects bad input with 422 (COG-112 review fix #1)."""
    if value is None:
        return value
    for u in value:
        if not isinstance(u, str) or not _SAFE_IRI_RE.match(u):
            raise ValueError(
                f"entity_uris entries must be http(s) IRIs without <>\"{{}} or "
                f"whitespace; got {u!r}"
            )
    return value


class EnrichmentTier(str, Enum):
    # "auto" is a meta-tier (COG-124): it is NOT a real adapter chain. The route
    # resolves it to a concrete tier (``lite`` or ``core``) via the shared tier
    # router BEFORE a job is created, so an EnrichJob always carries a concrete
    # tier. It exists only on the request as the smart default.
    auto = "auto"
    lite = "lite"
    base = "base"
    core = "core"
    pro = "pro"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    review = "review"
    applied = "applied"
    cancelled = "cancelled"
    failed = "failed"

    def is_terminal(self) -> bool:
        """True when the job has stopped doing work and will not advance on its
        own — the states a bounded ``wait_for_job`` long-poll should return on.

        ``queued`` and ``running`` are the only in-flight states: the job is
        still searching / ingesting / enriching and its progress may still
        change. Everything else is settled:

        - ``applied`` / ``failed`` / ``cancelled`` — classic done/error/stopped.
        - ``review`` — the enrichment run finished computing and is now parked
          waiting for a human's conflict decisions; it does no more work until a
          caller applies decisions, so a waiter should stop blocking and return.

        This is the single source of truth for "is this job done?" — the wait
        route, the SDK, and the MCP tool all defer to it so the terminal set can
        never drift between the interfaces.
        """
        return self not in (JobStatus.queued, JobStatus.running)


class JobCategory(str, Enum):
    """The kind of work a job performs.

    The unified Jobs page lists jobs across all categories. Existing enrichment
    jobs default to ``enrichment`` for backward compatibility. ``discovery`` is
    web-discovery ingest (the ``web_ingest`` capability): it CREATES a new set of
    records from the web rather than filling/merging existing ones.
    """

    dedupe = "dedupe"
    enrichment = "enrichment"
    reconciliation = "reconciliation"
    discovery = "discovery"


class JobTrigger(str, Enum):
    """How a job was kicked off.

    ``manual`` is a user-initiated action. ``scheduled`` is live (COG-135/136):
    the scheduler in ``cograph_client/scheduling/`` (``ScheduleRunner`` in
    ``runner.py``, cron + interval with missed-tick catch-up and a Postgres
    ``SELECT ... FOR UPDATE SKIP LOCKED`` multi-replica claim) fires due
    schedules via ``dispatch_scheduled_action``, which reuses the exact same
    action worker as the manual path — so a scheduled enrichment runs behind the
    identical confidence / conflict-policy / staging gate, only with
    ``trigger=scheduled``. It is wired into ``api/app.py`` lifespan via
    ``make_schedule_runner`` and enabled by default whenever a ``database_url``
    is configured (prod demo-tenant runs Aurora, so it is on in prod). Only
    ``webhook`` remains reserved for future automation — populated by callers,
    with no firing logic yet.
    """

    manual = "manual"
    scheduled = "scheduled"
    webhook = "webhook"


class ConflictPolicy(str, Enum):
    skip = "skip"
    verify = "verify"
    overwrite = "overwrite"
    stage = "stage"


class EnrichScope(BaseModel):
    """Value filter restricting an enrich job to a subset of a type's entities (COG-112).

    ``predicate`` is an attribute OR relationship **local-name** (e.g.
    ``haslevel``, ``title``) of the enriched ``type_name``. ``value`` is matched
    case-insensitively:

    - For a **literal attribute** the value is matched against the literal's
      string value.
    - For a **relationship to another node** (object property, e.g.
      ``haslevel → Level``) the value is matched against the target node's
      display label/name — so value ``"Manager"`` selects entities related to
      the Level node whose ``rdfs:label`` / name is "Manager". The target IRI's
      local-name is accepted as a fallback.

    The predicate is given as a **case-insensitive local-name**, so callers never
    need to know the storage namespace (attribute-URI vs ``onto/`` relationship
    form) and casing differences (``hasLevel`` vs ``haslevel``) do not matter.
    The executor resolves it against the type's ontology-declared predicates to a
    concrete instance predicate IRI before building the query — so the SELECT/
    COUNT match a predicate-indexed term instead of scanning every predicate of
    every entity (COG-112 perf fix). An unresolved predicate matches nothing
    (honest matched-0), never an unbounded scan.

    ``predicate`` is validated to a safe local-name and ``value`` to be non-empty
    (see validators below). Injection-safety is twofold: the predicate is BOTH a
    safe local-name AND resolved to an ontology-known IRI before interpolation,
    and ``value`` is only ever an escaped, lower-cased string literal — never
    spliced into an IRI — so neither can inject.
    """

    predicate: str
    value: str

    @field_validator("predicate")
    @classmethod
    def _check_predicate(cls, v: str) -> str:
        # Must be a safe local-name (non-empty, no IRI/whitespace/quote chars).
        # This is matched as an escaped string literal in SPARQL, never spliced
        # into an IRI, so it cannot inject (COG-112 review fix #1/#2/#3).
        if not isinstance(v, str) or not _SAFE_LOCAL_NAME_RE.match(v):
            raise ValueError(
                "scope.predicate must be a non-empty local-name matching "
                f"{_SAFE_LOCAL_NAME_RE.pattern} (letters/digits/_/-, no spaces "
                "or IRI characters)"
            )
        return v

    @field_validator("value")
    @classmethod
    def _check_value(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("scope.value must be non-empty")
        return v


class EnrichRequest(BaseModel):
    type_name: str
    attributes: list[str]
    # COG-124: ``auto`` is the smart default — the route resolves it to a concrete
    # tier (free Wikidata ``lite`` vs paid web ``core``) via the shared tier router
    # before creating the job, leaning paid when Wikidata is likely weak.
    tier: EnrichmentTier = EnrichmentTier.auto
    kg_name: str
    conflict_policy: ConflictPolicy = ConflictPolicy.stage
    confidence_min: float = 0.85
    limit: Optional[int] = None
    # COG-112 scoped enrichment. Both optional; default None → unchanged
    # whole-type behavior. If BOTH are set, ``entity_uris`` wins (the explicit
    # subset is the lower-level primitive and takes precedence over ``scope``).
    scope: Optional[EnrichScope] = None
    entity_uris: Optional[list[str]] = None
    # Optional enrichment knobs. Both default None → behavior is exactly as
    # today when omitted. ``instructions`` is free-text guidance threaded into
    # the adapter lookup context (agentic/premium adapters can read it; free
    # adapters like wikidata ignore it). ``sources`` overrides the adapter chain
    # (provider/adapter-name list, e.g. ["wikidata"], ["exa"]); unknown names
    # fall back gracefully (skipped with the existing one-shot warning).
    instructions: Optional[str] = None
    sources: Optional[list[str]] = None
    # URL-targeted enrichment: explicit page(s) to read the attribute values
    # FROM, instead of (or in addition to) a web search. Default None → today's
    # behavior. The route copies these onto ``EnrichJob.source_urls``; the
    # executor threads them into the adapter lookup context as ``target_urls`` so
    # a URL-aware premium adapter (e.g. Firecrawl) reads the supplied pages.
    # Free adapters (wikidata) ignore it harmlessly.
    target_urls: Optional[list[str]] = None
    # Chat provenance: the conversation/thread id this job was created from, when
    # it was kicked off from the Ask-AI chat. Default None → not chat-originated
    # (e.g. a direct API / CLI / scheduled call). The route copies it onto
    # ``EnrichJob.thread_id`` so a job is traceable back to its conversation.
    thread_id: Optional[str] = None

    _check_entity_uris = field_validator("entity_uris")(_validate_entity_uris_field)


class Verdict(BaseModel):
    """A single enrichment candidate value with provenance (ADR-0005 §5).

    Two distinct confidence signals are intentionally kept separate:

    - ``confidence`` is the CALIBRATED score. It is the only value the
      tier-chain threshold (e.g. ``confidence_min``) compares against. A
      calibrated score is meant to approximate the probability that the
      value is correct.
    - ``raw_confidence`` is an untrusted, relevance-ish signal straight from
      a source (e.g. an Exa neural relevance score). It is NEVER compared to
      a threshold; it exists only for diagnostics/debugging and as input to a
      calibration step that produces ``confidence``.

    All provenance fields are optional with a ``None`` default so legacy
    construction ``Verdict(value=..., confidence=..., source=...)`` keeps
    working unchanged.
    """

    value: str
    confidence: float
    source: str
    source_url: Optional[str] = None
    reasoning: Optional[str] = None
    raw_confidence: Optional[float] = None
    retrieved_at: Optional[datetime] = None
    source_published_at: Optional[datetime] = None
    grounding_score: Optional[float] = None
    extraction_method: Optional[str] = None
    calibration_method: Optional[str] = None
    # Source-authority level this verdict was produced under (an
    # ``api_registry.spec.AuthorityLevel`` value string, e.g. ``"source_of_truth"``),
    # threaded into P6 write-time conflict resolution (ONTA-279) so a refresh's fresh
    # value is ranked against the existing current value on the ONE shared authority
    # scale. Optional/None: a plain machine scrape carries no explicit authority and
    # the executor defaults it to a sensible non-top level (never ``user_assertion``,
    # which is minted only by the human-correction write path). A registry-backed or
    # premium adapter MAY stamp its curated ``authority_level`` here.
    authority: Optional[str] = None


RowAction = Literal["filled", "verified", "conflict", "skipped", "no_match"]


class RowResult(BaseModel):
    entity_uri: str
    attribute: str
    existing_value: Optional[str] = None
    verdict: Optional[Verdict] = None
    action: RowAction
    # The INCUMBENT value's provenance, read from its per-attribute companions
    # (`<attr>_source_url` / `<attr>_verified_at`) at selection time. Populated for
    # a CONFLICT row so the review queue can show BOTH disagreeing sources — the
    # existing source vs the proposed verdict's source (ONTA-246). Both default
    # None so a value with no prior provenance (or a non-conflict row) is unchanged.
    existing_source_url: Optional[str] = None
    existing_verified_at: Optional[str] = None


class JobProgress(BaseModel):
    total: int = 0
    processed: int = 0
    filled: int = 0
    verified: int = 0
    conflicts: int = 0
    skipped: int = 0
    # COG: a lookup that found nothing is a first-class, counted outcome — not a
    # black hole. Kept distinct from ``skipped`` (which is backward-compat).
    no_match: int = 0
    cache_hits: int = 0
    # Coarse WHAT-is-happening-now label for a running job (ONTA-238), so a poll
    # mid-run reads "ingesting" instead of a bare, uninformative status=running.
    # Discovery sets it through the run ("searching" → "ingesting" → "done", or
    # "failed" on a terminal error); enrichment/dedupe leave it "" (unchanged).
    # Purely additive: an empty phase means "no phase reported", never a state.
    phase: str = ""


ProviderStatus = Literal["ok", "no_match", "error", "skipped"]


class ApiRequestTrace(BaseModel):
    """One HTTP request a provider issued during a run — the request-level detail
    behind a ``ProviderLog``'s aggregate counters.

    Populated for **API-source (registry) providers** (``api:{slug}``), where a
    "request" is a single declarative GET the executor sent (the first page and
    every synthesized pagination page). It answers "which requests went out, with
    what payload, and how did each fare" without a log dive:

    - ``url``      — the request URL, **auth-free** (the post-redirect display URL;
      any query-key secret is applied only to the live fetch, never stored here),
      so it is safe to surface as-is.
    - ``params``   — the query parameters of that request (the GET "payload"):
      the bound search params (e.g. ``city=San Francisco``) merged with the
      pagination cursor (e.g. ``skip=200&limit=200``). Parsed from ``url`` so it
      carries no auth material.
    - ``status``   — the HTTP status code of the response (``200``, ``404`` …), or
      ``None`` when the request never got a response (DNS/connect error, blocked
      by the SSRF guard, non-JSON body).
    - ``records``  — how many records this single request returned (raw count on
      the page, pre-dedupe), so per-request yield is visible, not just the run total.
    - ``error``    — a short reason when the request failed (``HTTP 404``, a
      transport error), else ``None``.

    All fields default so an empty/older ``ProviderLog`` is unaffected. Web-search
    providers and enrichment adapters leave ``requests`` empty today; only the
    registry executor emits these.
    """

    url: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    status: Optional[int] = None
    records: int = 0
    error: Optional[str] = None


class ProviderLog(BaseModel):
    """Per-provider activity record for ONE job run — what each provider we used
    (enrichment adapter or web-source) actually did, surfaced in the run-detail
    view so a user can see which providers were consulted and how they fared.

    ``provider`` is the adapter / web-source name (``wikidata``, ``exa``,
    ``perplexity``, the discovery provider name, …). The counters are cumulative
    over the run:

    - ``attempts``   — live lookups issued to the provider (cache hits excluded).
    - ``matches``    — lookups that yielded a usable result (a sufficiently
      confident verdict for enrichment; a discovered record for web discovery).
    - ``no_match``   — lookups that ran but found nothing usable.
    - ``errors`` / ``timeouts`` — failed / timed-out lookups.
    - ``cache_hits`` — answers served from the enrichment cache (no live call).

    ``status`` is a coarse roll-up for the UI pill: ``ok`` (produced at least one
    usable result), ``no_match`` (ran but found nothing), ``error`` (every
    attempt failed), or ``skipped`` (named but never reachable — e.g. an
    unregistered adapter). ``last_error`` carries a representative message for
    the error/timeout case so the user sees *why* a provider failed without
    leaving the page.

    All fields default to zero/None so this is purely additive — existing job
    construction is unchanged and a run that records nothing simply has an empty
    ``provider_logs`` list.
    """

    provider: str
    status: ProviderStatus = "ok"
    attempts: int = 0
    matches: int = 0
    no_match: int = 0
    errors: int = 0
    timeouts: int = 0
    cache_hits: int = 0
    last_error: Optional[str] = None
    # Request-level trace: the individual HTTP requests this provider issued
    # during the run (URL, query-param payload, HTTP status, records fetched).
    # Populated for API-source registry providers; empty for web-search providers
    # and enrichment adapters. Capped at the accumulation site so the persisted
    # job stays bounded; default empty so existing job construction is unchanged.
    requests: list[ApiRequestTrace] = Field(default_factory=list)


class JobErrorItem(BaseModel):
    """One aggregated entry in a job's error summary.

    Groups a provider failure mode (``error`` / ``timeout`` / ``missing``) — or a
    fatal ``job``-level error — with a representative ``message`` and how many
    times that failure occurred over the run, so the run-detail view can show "a
    summary of the potential errors" instead of forcing a log dive.
    """

    provider: Optional[str] = None
    kind: Literal["error", "timeout", "missing", "job"] = "error"
    message: str
    count: int = 1


class EnrichJob(BaseModel):
    id: str
    tenant_id: str
    kg_name: str
    type_name: str
    attributes: list[str]
    tier: EnrichmentTier
    status: JobStatus
    progress: JobProgress = Field(default_factory=JobProgress)
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    conflict_policy: ConflictPolicy
    confidence_min: float = 0.85
    error: Optional[str] = None
    limit: Optional[int] = None
    results: list[RowResult] = Field(default_factory=list)
    # COG-112 scoped enrichment. Both optional / default None so existing
    # enrichment-job construction keeps working unchanged (whole-type behavior).
    # If both are set, ``entity_uris`` wins (see EnrichRequest).
    scope: Optional[EnrichScope] = None
    entity_uris: Optional[list[str]] = None
    # Optional enrichment knobs (mirror EnrichRequest). Both default None so
    # existing enrichment-job construction keeps working unchanged. They carry
    # into the executor: ``instructions`` is folded into the adapter lookup
    # context (and the cache key), ``sources`` overrides the adapter chain.
    instructions: Optional[str] = None
    sources: Optional[list[str]] = None
    # URL-targeted enrichment: the explicit page(s) to read attribute values
    # FROM (mirrors EnrichRequest.target_urls). Empty by default → today's
    # behavior. The executor threads these into the adapter lookup context as
    # ``target_urls`` so a URL-aware premium adapter (e.g. Firecrawl) reads the
    # supplied pages; free adapters ignore it harmlessly.
    source_urls: list[str] = Field(default_factory=list)
    # COG-101: unified-jobs fields. All optional with safe defaults so existing
    # enrichment-job construction keeps working unchanged.
    category: JobCategory = JobCategory.enrichment
    trigger: JobTrigger = JobTrigger.manual
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    cost: Optional[float] = None
    cost_note: Optional[str] = None
    # Optional HARD per-run spend ceiling (USD) — the A9 cost envelope (ONTA-282).
    # ``None`` ⇒ fall back to the deployment default (config
    # ``enrich_spend_ceiling_usd``); ``0`` ⇒ unlimited. When the effective ceiling
    # is > 0, the run HALTS CLEANLY once cumulative attributable spend reaches it
    # (terminal ``failed`` + cost-envelope reason + honest partial coverage on the
    # manifest). Optional / default ``None`` so every existing job is unchanged.
    spend_ceiling_usd: Optional[float] = None
    # Discovery/web-ingest summary fields (COG — realtime job status). Both
    # optional with safe defaults so enrichment/dedupe job construction is
    # unchanged. ``result_count`` is the headline "how many records were found"
    # number (entities resolved); ``platforms`` are the web sources/providers
    # consulted during the run (e.g. the provider name + distinct source hosts),
    # surfaced in the job-details view.
    result_count: Optional[int] = None
    platforms: Optional[list[str]] = None
    # Observability (run-detail view): a per-provider activity log for whatever
    # providers this run used, and an aggregated summary of the errors hit. Both
    # optional with empty defaults so existing job construction is unchanged; the
    # enrichment executor and the web-discovery capability populate them, and the
    # job-detail route serializes them verbatim for the UI.
    provider_logs: list[ProviderLog] = Field(default_factory=list)
    error_summary: list[JobErrorItem] = Field(default_factory=list)
    # A9 Run Manifest (ONTA-273): the run as a first-class object — per-item
    # status, drops, retries, spend-to-date, and the terminal halt reason (e.g.
    # provider exhaustion on a 402/sustained-429). Optional / default None so
    # every existing job (and older persisted jobs) is unchanged; the discovery
    # capability and the enrichment executor populate it, and it rides in the same
    # jsonb payload the job already persists (no schema migration). Its
    # ``coverage()`` view is what lets a partially-completed run HONESTLY caveat
    # "N of M items completed before halt" instead of a silent partial/success.
    manifest: Optional[RunManifest] = None
    # Chat provenance: the conversation/thread id this job was created from (when
    # kicked off from the Ask-AI chat). Optional / default None so every other
    # writer (direct API, CLI, scheduled runs) is unchanged. Echoed in the job
    # summary + detail so a job can be traced back to its conversation.
    thread_id: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    tenant_id: str
    kg_name: str
    type_name: str
    attributes: list[str]
    tier: EnrichmentTier
    status: JobStatus
    progress: JobProgress
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    conflict_policy: ConflictPolicy
    confidence_min: float = 0.85
    error: Optional[str] = None
    # COG-101: unified-jobs fields.
    category: JobCategory = JobCategory.enrichment
    trigger: JobTrigger = JobTrigger.manual
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    cost: Optional[float] = None
    cost_note: Optional[str] = None
    # Discovery/web-ingest summary fields (see EnrichJob). Optional so the
    # summary of an enrichment/dedupe job is unchanged.
    result_count: Optional[int] = None
    platforms: Optional[list[str]] = None
    # Derived 0-100 completion percentage from progress.processed/total.
    progress_pct: int = 0
    # A9 Run Manifest coverage view (ONTA-273): the one-line "N of M items
    # completed; K dropped; <halt reason>" the Jobs list renders so a partially
    # completed / halted run is honest at a glance, not just in the detail view.
    # Optional / default None so a job with no manifest is unchanged.
    coverage: Optional[RunCoverage] = None
    # Chat provenance (see EnrichJob.thread_id). Optional so a non-chat job's
    # summary is unchanged.
    thread_id: Optional[str] = None


ReviewDecision = Literal["accept", "reject", "skip"]


class ConflictReview(BaseModel):
    entity_uri: str
    attribute: str
    existing_value: str
    proposed: Verdict
    decision: Optional[ReviewDecision] = None
    # The incumbent value's provenance (source + as-of), so the review surface can
    # show BOTH disagreeing sources — the existing one vs the proposed verdict's —
    # not just the new proposal (ONTA-246). Both default None (no prior provenance
    # recorded) so existing construction / older jobs are unaffected.
    existing_source_url: Optional[str] = None
    existing_verified_at: Optional[str] = None


def _progress_pct(progress: JobProgress) -> int:
    """Derive a 0-100 completion percentage from processed/total.

    Returns 0 when total is unknown (0) to avoid division-by-zero; clamps to
    [0, 100] so a stray over-count can never report >100.
    """
    if not progress.total:
        return 0
    pct = round(progress.processed / progress.total * 100)
    return max(0, min(100, pct))


def job_to_summary(job: EnrichJob) -> JobSummary:
    return JobSummary(
        id=job.id,
        tenant_id=job.tenant_id,
        kg_name=job.kg_name,
        type_name=job.type_name,
        attributes=job.attributes,
        tier=job.tier,
        status=job.status,
        progress=job.progress,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        conflict_policy=job.conflict_policy,
        confidence_min=job.confidence_min,
        error=job.error,
        category=job.category,
        trigger=job.trigger,
        last_run=job.last_run,
        next_run=job.next_run,
        cost=job.cost,
        cost_note=job.cost_note,
        result_count=job.result_count,
        platforms=job.platforms,
        progress_pct=_progress_pct(job.progress),
        coverage=job.manifest.coverage() if job.manifest is not None else None,
        thread_id=job.thread_id,
    )
