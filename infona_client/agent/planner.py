"""The planner — the brain behind the single agent endpoint.

One bounded LLM call classifies the user's intent (question | enrich | clean |
dedup | ontology | ambiguous); the planner then dispatches to the matching
registered capability. There is NO per-task fan-out and NO per-task endpoint —
the classifier picks ONE capability and we call its ``plan()`` (or, for a
question, answer directly). Writes happen ONLY on ``execute_plan`` (after the
user confirms a returned plan), never during ``handle``.

Contract (the single conversational surface):
  handle(message) →
    {kind:"answer",  answer, sparql, rows}        # questions, no confirm
    {kind:"clarify", question}                    # ambiguous
    {kind:"plan",    plan_id, steps:[...]}         # actions, awaiting confirm
  execute_plan(plan_id) →
    {kind:"result",  steps:[summaries]}           # the only mutating path
    # ONE-SHOT: a duplicate confirm never re-runs the steps — it replays the
    # persisted result ({kind:"result", ..., replayed:true}) once finished, or
    # errors with code:"plan_already_executing" while the first is in flight.

Plan persistence (A2, COG-124): a swappable, tenant-scoped store keyed by
plan_id, mirroring the dual-backend :class:`JobStore` pattern. ``make_plan_store``
returns a :class:`PostgresPlanStore` when ``settings.database_url`` is set
(durable, shared across ECS tasks — so a confirm→execute survives a process
restart or a different task than the one that planned), else an
:class:`InMemoryPlanStore` (zero-config default). See
:mod:`infona_client.agent.plan_store`.
"""

from __future__ import annotations

import structlog

from infona_client.agent.capabilities.dedup_cap import DedupCapability
from infona_client.agent.capabilities.enrich_cap import EnrichCapability
from infona_client.agent.capabilities.normalize_cap import NormalizeCapability
from infona_client.agent.capabilities.ontology_cap import OntologyCapability
from infona_client.agent.capabilities.query import QueryCapability
from infona_client.agent.capabilities.subscribe_cap import SubscribeCapability
from infona_client.agent.capabilities.web_research_cap import WebResearchCapability
from infona_client.agent.conversation_store import (  # noqa: F401  (re-exported)
    Turn,
    make_conversation_store,
    reset_conversation_store,
)
from infona_client.agent.kg_scope import (  # noqa: F401
    CODE_KG_MISSING,
    CTX_KG_RESOLVED,
    check_kg_scope,
    resolved_kg_note,
)
from infona_client.agent.plan_store import (  # noqa: F401  (re-exported for back-compat)
    InMemoryPlanStore,
    PlanStore,
    PostgresPlanStore,
    StoredPlan,
    claim_plan_for_execution,
    get_plan_store,
    make_plan_store,
    reset_plan_store,
)
from infona_client.agent.planner_classify import (  # noqa: F401
    _CLASSIFY_SYSTEM,
    _MAX_CLARIFY_ROUNDS,
    _ambiguous,
    _classify,
    _clean_options,
    _normalize_classification,
    _parse_classification,
)
from infona_client.agent.planner_execute import (  # noqa: F401
    _EXECUTING_STALE_S,
    _already_confirmed_response,
    _assert_confirm_may_commit,
    _assert_may_commit,
    _kg_scope_gate_for_confirm,
    _load_history,
    _new_plan_id,
    _plan_intents,
    _record_turn,
    _result_summary,
    execute_plan,
)
from infona_client.agent.planner_handle import handle, _respond  # noqa: F401
from infona_client.agent.planner_history import (  # noqa: F401
    _COMMITTED_REPLY_KINDS,
    _KG_LABEL_MAX,
    _KG_LABEL_UNSAFE_RE,
    _PROMPT_HISTORY_TURNS,
    _effective_instruction,
    _format_history,
    _kg_label,
    _open_ask_user_turns,
    _recent_window,
    _same_kg_turns,
    _turn_matches_kg,
    query_followup_turns,
)
from infona_client.agent.planner_intent import (  # noqa: F401
    _CLAUSE_BOUNDARY,
    _DEFAULT_ACTION_OPTIONS,
    _DISCOVER_IMPERATIVE_RE,
    _ENRICH_VERB_RE,
    _EXPLICIT_DISCOVERY_INTENT_RE,
    _INTENT_PLAN_ORDER,
    _INTENT_TO_CAPABILITY,
    _QUESTION_LEAD_RE,
    _REFRESH_EXISTING_RE,
    _SUBSCRIBE_ALERT_RE,
    _SUBSCRIBE_CADENCE_RE,
    _WEB_FETCH_RE,
    _WEB_INGEST_HOSTED_ONLY,
    _hosted_only_web_ingest_answer,
    _is_interrogative,
    _is_refresh_existing_request,
    _is_subscribe_request,
    _is_web_discovery_request,
    _message_has_urls,
    _url_intent,
)
from infona_client.agent.registry import (  # noqa: F401 — monkeypatch surface
    AgentContext,
    ReadOnlyMembershipError,
    get_capabilities,
    get_capability,
    mutating_step_capabilities,
    order_steps,
    register_capability,
)
from infona_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat  # noqa: F401
from infona_client.web_sources.url_extract import extract_urls  # noqa: F401

logger = structlog.stdlib.get_logger("infona.agent.planner")


def register_default_capabilities() -> None:
    """Register the OSS A1 capabilities. Import-safe + idempotent.

    Called from app startup (and tests). A downstream/proprietary deployment can
    register additional capabilities (dedup with embedding matchers, ontology
    edits) the same way — no route change. ``register_capability`` is last-write-
    wins, so calling this more than once is harmless.
    """
    normalize = NormalizeCapability()
    register_capability(QueryCapability())
    register_capability(normalize)
    register_capability(EnrichCapability(normalize=normalize))
    register_capability(DedupCapability())
    register_capability(OntologyCapability())
    # Web discovery ingest (`web_ingest` / "discover") is premium. Hosted
    # registers it via ``infona.web_sources.plugin``. OSS does not ship the
    # capability module. The intent name stays so a premium register_capability
    # is enough — no OSS import of the class.
    # Subscribe / standing alert (ONTA-235): registered in OSS so the "subscribe"
    # intent routes and the persona can set a recurring, subscribe-able alert ONCE.
    # It persists a `notify` Schedule through the shared schedule store (the same
    # store the canonical /schedules route uses); the OSS best-effort HTTP sink
    # delivers change payloads, and a premium reliable sink supersedes it via
    # register_delivery_sink.
    register_capability(SubscribeCapability())
    # Web research (ADR 0006): the read-only counterpart — answers a question from
    # the web and returns a cited answer/artifact, no KG write.
    #
    # OPEN-WEB RETRIEVAL IS OUT OF OSS SCOPE (ONTA-293, decided 2026-07-29). OSS
    # deliberately registers NO page fetcher and NO web-source provider, so this
    # capability is dormant here (no default fetcher). It is
    # still REGISTERED so the "research" intent routes and can explain itself —
    # a dormant capability that says "hand me the content instead" is a signpost,
    # not a dead button.
    #
    # The retrieval SUBSTRATE stays in OSS (`infona_client/retrieval/`): the
    # SSRF/DNS guards, HTML safety, the fetch ladder, the cost seam. That is what
    # premium and self-hosters register INTO via `register_page_fetcher` /
    # `register_web_source`, and ADR 0008 requires exactly one such substrate. A
    # deployment that wants the previous behaviour calls `register_default_fetchers()`
    # itself at boot; nothing was deleted.
    register_capability(WebResearchCapability())
