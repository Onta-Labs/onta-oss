"""Execute a planned enrichment as a background EnrichJob.

Owns count / subset-URI / multi-value-scope resolution helpers and
:meth:`EnrichCapability.execute`. Builds the same :class:`EnrichJob` the
``/enrich/jobs`` route builds and hands it to the shared executor.

Invariants other agents must not break:
- Look up ``_spawn`` and ``logger`` on the public ``enrich_cap`` module
  via :func:`_host` so patches keep working (a missed ``_spawn`` lookup
  is the usual hang after extract).
- This mixin does not write the graph. The executor the job drives must
  stay on ``insert_facts`` / ``refresh_after_write``; instance edges on
  ``onto/<leaf>``; entity IRIs via ``entity_uri``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from infona_client.agent.capabilities.enrich_common import _SUBSET_MAX, _host
from infona_client.agent.capabilities.enrich_cost import _coerce_tier
from infona_client.agent.capabilities.enrich_intent import (
    _default_conflict_policy,
    _overwrite_conflict_policy,
    _refresh_conflict_policy,
)
from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.enrichment.models import EnrichJob, EnrichScope, JobStatus
from infona_client.enrichment.tier_router import (
    DEFAULT_CONFIDENCE_MIN as _DEFAULT_CONFIDENCE_MIN,
)
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.pipeline.stage_trace import stamp_enrichment_job_created


class EnrichExecuteMixin:
    """Count / resolve / run the planned enrichment job."""

    async def _estimate_matched(
        self,
        ctx: AgentContext,
        type_name: str,
        scope: dict | None,
        attributes: list[str],
    ) -> tuple[Optional[int], bool]:
        """Estimate how many entities the enrich job will match.

        Reuses the executor's existing index-efficient ``count_entities`` (the
        same SELECT/COUNT path COG-112 built — no new query engine). Returns
        ``(count, exact)``: ``exact=True`` when the COUNT actually ran, else
        ``(None, False)`` so the caller falls back to a labeled estimate rather
        than reporting a misleading 0. Defensive: any executor/store error or a
        missing executor degrades to ``(None, False)`` — the plan must never fail
        on a cost estimate.
        """
        executor = ctx.extras.get("enrichment_executor")
        if executor is None or not hasattr(executor, "count_entities"):
            return None, False
        enrich_scope = None
        if scope and scope.get("predicate") and scope.get("value"):
            try:
                enrich_scope = EnrichScope(
                    predicate=scope["predicate"], value=scope["value"]
                )
            except Exception:  # noqa: BLE001 — a bad scope just means "no count"
                return None, False
        try:
            n = await executor.count_entities(
                ctx.tenant_id,
                ctx.kg_name,
                type_name,
                scope=enrich_scope,
            )
            return int(n), True
        except Exception:  # noqa: BLE001
            _host().logger.warning("agent_enrich_count_failed", exc_info=True)
            return None, False

    async def _resolve_subset_uris(
        self, ctx: AgentContext, type_name: str, subset: dict
    ) -> list[str]:
        """Resolve a ranked/specific subset to the concrete entity IRIs it names.

        Reuses the shared NL entity selector
        (:meth:`NLQueryPipeline.select_entity_uris` — GraphStore Cypher after
        ONTA-534; SPARQL execution retired) so "the 5 brokers with the most
        listings" becomes those 5 IRIs — no new query engine, no client-side
        ranking. The subset's own LIMIT is honored; ``_SUBSET_MAX`` is an outer
        safety cap so a runaway/unbounded subset can't fan out to thousands of
        paid calls. Returns ``[]`` on any failure — the caller fails closed
        rather than enriching the whole type by accident.
        """
        description = str(subset.get("description") or "").strip()
        if not description:
            return []
        raw_limit = subset.get("limit")
        lim = (
            int(raw_limit)
            if isinstance(raw_limit, (int, float))
            and not isinstance(raw_limit, bool)
            and raw_limit > 0
            else None
        )
        lim = min(lim, _SUBSET_MAX) if lim else _SUBSET_MAX

        # Lazy import: keep the heavy NL pipeline (and its anthropic client) out of
        # agent-registry import time, mirroring QueryCapability._build_pipeline.
        from infona_client.nlp.pipeline import NLQueryPipeline

        pipeline = NLQueryPipeline(ctx.neptune, ctx.anthropic_key)
        onto_graph = tenant_graph_uri(ctx.tenant_id)
        instance_graph = (
            kg_graph_uri(ctx.tenant_id, ctx.kg_name) if ctx.kg_name else onto_graph
        )
        try:
            return await pipeline.select_entity_uris(
                description, type_name, onto_graph, instance_graph, lim
            )
        except Exception:  # noqa: BLE001 — resolution must never crash planning
            _host().logger.warning("agent_enrich_subset_resolve_failed", exc_info=True)
            return []

    async def _resolve_scope_value_uris(
        self,
        ctx: AgentContext,
        type_name: str,
        predicate: str,
        values: list[str],
    ) -> list[str]:
        """Resolve a MULTI-VALUE scope (a list of scope values) to entity IRIs.

        Drives the executor's DETERMINISTIC value-IN select
        (:meth:`EnrichmentExecutor.select_scope_value_uris`) — NOT the NL LLM — so
        "refresh pricing for OpenAI, Google, Deepgram and ElevenLabs" matches the
        existing records whose ``predicate`` value is any of those names
        (case/normalization-insensitive), rather than the single crammed literal
        that matches nothing. Bounded by ``_SUBSET_MAX`` so a huge list can't fan a
        paid enrich out unboundedly. Returns ``[]`` on any failure (no executor, no
        select method, store error) so the caller fails closed.
        """
        executor = ctx.extras.get("enrichment_executor")
        if executor is None or not hasattr(executor, "select_scope_value_uris"):
            return []
        try:
            return await executor.select_scope_value_uris(
                ctx.tenant_id,
                ctx.kg_name,
                type_name,
                predicate,
                values,
                limit=_SUBSET_MAX,
            )
        except Exception:  # noqa: BLE001 — resolution must never crash planning
            _host().logger.warning(
                "agent_enrich_scope_value_resolve_failed", exc_info=True
            )
            return []

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        """Create + run an EnrichJob in the background (same as /enrich/jobs)."""
        p = step.params
        executor = ctx.extras.get("enrichment_executor")
        job_store = ctx.extras.get("enrichment_job_store")
        if executor is None or job_store is None:
            raise RuntimeError(
                "enrichment executor/job_store not available in agent context"
            )
        scope = None
        if p.get("scope") and p["scope"].get("predicate"):
            scope = EnrichScope(
                predicate=p["scope"]["predicate"], value=p["scope"]["value"]
            )
        # Explicit entity set (resolved from a ranked/specific subset at plan time);
        # the executor uses a VALUES block and lets it win over scope.
        entity_uris = p.get("entity_uris") or None
        # URL-targeted mode: the page(s) to read values FROM (set at plan time).
        # Threaded onto the job → the executor's adapter lookup context
        # (``target_urls``). Empty by default → unchanged behavior.
        source_urls = p.get("source_urls") or []
        limit = p.get("limit")
        job = EnrichJob(
            id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            kg_name=ctx.kg_name,
            type_name=p["type_name"],
            attributes=p["attributes"],
            tier=_coerce_tier(p.get("tier")),
            status=JobStatus.queued,
            created_at=datetime.now(timezone.utc),
            # Conflict-policy selection (checked most-specific first):
            #  * `overwrite`  — an EXPLICIT replace intent (pf10 sp-refresh-pricing):
            #    a changed value is REPLACED with the fresh one (+ its source stamp).
            #  * `verify`     — a plain refresh: re-confirm existing values and
            #    advance the freshness stamp WITHOUT clobbering (ONTA-245 F3 default).
            #  * `stage`      — a normal enrich: stage conflicts for review.
            conflict_policy=_overwrite_conflict_policy()
            if p.get("overwrite")
            else _refresh_conflict_policy()
            if p.get("refresh")
            else _default_conflict_policy(),
            confidence_min=float(
                p.get("confidence_min", _DEFAULT_CONFIDENCE_MIN)
                or _DEFAULT_CONFIDENCE_MIN
            ),
            scope=scope,
            entity_uris=entity_uris,
            source_urls=source_urls,
            # Carry the plan's proposed cap so the job actually honors the bound
            # surfaced to the user at plan time (COG-123). int() guards a stray
            # non-int; None leaves whole-subset behavior unchanged. bool is a
            # subclass of int, so exclude it explicitly — a stray True/False must
            # not be coerced to a 1/0 limit.
            limit=int(limit)
            if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit
            else None,
            # Chat provenance: link the job to the conversation that spawned it.
            thread_id=getattr(ctx, "session_id", None),
            # Per-run HARD spend ceiling (ONTA-378): a per-turn ceiling threaded
            # from the /agent request bounds THIS job via the executor's
            # ``resolve_spend_ceiling(...)`` override. None → deployment default.
            spend_ceiling_usd=getattr(ctx, "spend_ceiling_usd", None),
        )
        # Operator Job Trace (ONTA-387): open live P0 at create (same as /enrich/jobs).
        stamp_enrichment_job_created(job)
        await job_store.create(job)
        _host()._spawn(executor.run(job, ctx.tenant_id))
        return {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            "job_id": job.id,
            "job_status": job.status.value,
            "message": (
                f"Enriching {', '.join(job.attributes)} on {job.type_name} "
                "in the background; results will be staged for review."
            ),
        }
