"""Execute mixin: queue the discovery job and spawn the background run.

Writes go through ``resolver.ingest`` / ``ingest_structured_rows`` →
``insert_facts``; this module only sets up the job and hands off to
``_run_discovery_inner``.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobCategory,
    JobStatus,
)
from infona_client.graph.queries import kg_graph_uri
from infona_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id
from infona_client.pipeline.manifest import RunManifest, resolve_spend_ceiling
from infona_client.pipeline.stage_trace import (
    StageProjectId,
    attach_recorder,
    open_job_stage_trace,
)
from infona_client.config import settings
from infona_client.web_sources.base import get_web_source
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_fetch import (
    _merge_registry_ensemble,
    _rebuild_registry_sources,
)
from infona_client.agent.capabilities.web_ingest_job import _fail_job
from infona_client.agent.capabilities.web_ingest_plan_enum import (
    _expand_enumeration_ensemble,
)
from infona_client.agent.capabilities.web_ingest_plan_preview import (
    _provider_context,
    _step_cost,
)
from infona_client.agent.capabilities.web_ingest_run import _run_discovery_inner


class WebIngestExecuteMixin:
    """Background-job entry for discovery ingest."""

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        p = step.params
        # URLs persisted at plan time (empty for plain query discovery). Provider
        # selection mirrors plan(): the persisted ENSEMBLE (specialized first,
        # then general — both are consulted because neither is complete alone),
        # falling back to the legacy single "provider" name, then to the
        # mode-appropriate default (for_urls=bool(urls)) for steps persisted
        # before either key existed. Names that no longer resolve are skipped.
        urls = list(p.get("urls") or [])
        web_ensemble = [
            prov
            for prov in (
                get_web_source(n)
                for n in (p.get("providers") or [])
                if isinstance(n, str) and n
            )
            if prov is not None
        ]
        if not web_ensemble:
            single = get_web_source(p.get("provider")) or get_web_source(
                for_urls=bool(urls)
            )
            web_ensemble = [single] if single is not None else []
        # ONTA-379: nested fallbacks (e.g. source_first's Tier-1 web provider)
        # are NOT separately registered — plan() lists them by name for the cost
        # card, but get_web_source can't resolve them. Re-unwrap from the resolved
        # primary so execute still consults the full enumeration ensemble.
        if p.get("subqueries"):
            web_ensemble = _expand_enumeration_ensemble(web_ensemble)
        # ONTA-194 phase 2: rebuild the registry providers from the picks the plan
        # persisted (no second LLM call) and splice them ahead of web, honoring the
        # persisted mode. A registry-only run (api_only, or a registry-only
        # deployment) proceeds even when no web provider is available.
        registry_sources, registry_mode = await _rebuild_registry_sources(
            p, ctx.tenant_id
        )
        ensemble = (
            _merge_registry_ensemble(web_ensemble, registry_sources, registry_mode)
            if registry_sources
            else web_ensemble
        )
        if not ensemble:
            raise RuntimeError("web-source provider not available at execute time")
        provider = ensemble[0]  # primary — naming + default error attribution

        query = p["query"]
        # Enumeration fan-out (ONTA-192): the plan may carry self-contained
        # sub-queries partitioning an "all X in Y and Z" ask. One discovery runs
        # per sub-query, all merged (deduped on the key attribute) into THIS one
        # job. Absent/empty → the single primary query, the classic path.
        subqueries = (
            [query]
            if urls
            else [
                q
                for q in (p.get("subqueries") or [])
                if isinstance(q, str) and q.strip()
            ]
            or [query]
        )
        attributes = p.get("attributes") or []
        # ONTA-382: exhaustive attribute set (closed allowlist). Older persisted
        # steps predate this key → treat as illustrative (open), matching the
        # pre-382 soft-extract default.
        attributes_exhaustive = bool(p.get("attributes_exhaustive"))
        # COMPREHENSIVE fetch hint persisted at plan time so the full pull uses the
        # SAME rich projection the sample did — the column projection is the stable
        # part of the preview (the discovered shape was only an estimate). Older
        # persisted steps predate this key — fall back to the named attributes so
        # they still run (graceful degradation).
        hint_columns = p.get("hint_columns") or attributes
        proposed_type = p.get("proposed_type") or "WebRecord"
        cap = int(p.get("max_rows") or _wic._DEFAULT_PLAN_CAP)
        kg_name = p.get("kg_name") or ctx.kg_name
        instance_graph = kg_graph_uri(ctx.tenant_id, kg_name) if kg_name else None
        # ONTA-268: one ontology-write lock PER JOB, shared by every per-sub-query
        # resolver built below. A fresh resolver is constructed inside the
        # sub-query loop so no two sub-queries share the resolver's per-ingest
        # state (`_instance_graph` / `_parent_of` / the TypeMatcher graph URI) —
        # the reentrancy hazard — while the shared lock serializes their ontology
        # mutations so concurrent (or future-parallelized) sub-queries can't race
        # type creation and fragment the ontology.
        # Share the process-wide ontology write lock (ONTA-403) so discovery
        # sub-queries serialize with REST / enrichment schema commits too —
        # not only with each other (ONTA-268).
        from infona_client.graph.ontology_commit import ontology_write_lock
        ontology_lock = ontology_write_lock()
        pctx = _provider_context(ctx)

        # Track the discovery as a real job so the client polls a LIVE status
        # (queued → running → applied/failed) with a result count, the platforms
        # consulted, and the run cost — instead of a synchronous "done" the
        # instant the background task is spawned. The job store is the same
        # unified store enrichment/dedupe use (injected on ctx.extras by the
        # agent route); when it's absent (a bare/test context) we degrade to the
        # previous fire-and-forget behavior so nothing breaks.
        job_store = ctx.extras.get("enrichment_job_store")
        cost_usd, cost_note = _step_cost(step)
        job: Optional[EnrichJob] = None
        if job_store is not None:
            job_id = str(uuid.uuid4())
            job = EnrichJob(
                id=job_id,
                tenant_id=ctx.tenant_id,
                kg_name=kg_name or "",
                type_name=proposed_type,
                attributes=attributes,
                tier=EnrichmentTier.lite,
                status=JobStatus.queued,
                created_at=datetime.now(timezone.utc),
                conflict_policy=ConflictPolicy.stage,
                category=JobCategory.discovery,
                cost=cost_usd,
                cost_note=cost_note,
                # A9 Run Manifest (ONTA-273): the run as a first-class object. The
                # discovery run records per-batch coverage into it and settles it to
                # a terminal state (completed / failed-with-reason) at every exit,
                # so a run halted by provider exhaustion caveats "N of M items
                # completed before halt" instead of a silent partial. run_id = the
                # job id (the EnrichJob IS the run — no separate id to mint).
                manifest=RunManifest(run_id=job_id, stage="discovery"),
                # Chat provenance: link the job to the conversation that spawned it.
                thread_id=getattr(ctx, "session_id", None),
                # Per-run HARD spend ceiling (ONTA-378): a per-turn ceiling
                # threaded from the /agent request. The resolve_spend_ceiling(...)
                # call just below reads job.spend_ceiling_usd as the explicit
                # override, so it WINS over the deployment default and bounds THIS
                # discovery run. None → deployment default (unchanged behavior).
                spend_ceiling_usd=getattr(ctx, "spend_ceiling_usd", None),
            )
            # A9 cost envelope (ONTA-282): stamp the HARD per-run spend ceiling on
            # the manifest. A per-job override wins; else the deployment default
            # (config). None/0 ⇒ unlimited (unchanged behavior). The per-batch spend
            # feed + ceiling check in _run_inner then halt the run cleanly if it
            # crosses this envelope.
            if job.manifest is not None:
                job.manifest.spend_ceiling_usd = resolve_spend_ceiling(
                    getattr(job, "spend_ceiling_usd", None),
                    settings.enrich_spend_ceiling_usd,
                )
            # Operator Job Trace (P0–P9): open the run + stamp P1 Find input.
            # try/except: observability must never block job creation.
            try:
                rec = attach_recorder(job)
                if rec is not None:
                    rec.begin(
                        StageProjectId.p0,
                        input={
                            "job_id": job.id,
                            "category": "discovery",
                            "spend_ceiling_usd": job.spend_ceiling_usd,
                        },
                    )
                    rec.action(
                        StageProjectId.p0, "create_job", detail="discovery job queued"
                    )
                    rec.begin(
                        StageProjectId.p1,
                        input={
                            "goal": (
                                query or getattr(step, "instruction", None) or ""
                            )[:500],
                            "type_name": proposed_type,
                            "attributes": attributes,
                            "kg_name": kg_name,
                            "cap": cap,
                            "subqueries": subqueries[:20],
                            "providers": [
                                getattr(pr, "name", str(pr)) for pr in ensemble[:10]
                            ],
                            # Notion contract: P1 consumes user goal (+ A8 when refresh).
                            "contract_consumes": "user goal · A8 Refresh Delta",
                            "contract_emits": "A1 Source Bundle",
                        },
                    )
                    rec.action(
                        StageProjectId.p1, "plan", detail="spec resolved; ready to search"
                    )
            except Exception:
                _wic.logger.warning(
                    "stage_trace_open_failed",
                    job_id=getattr(job, "id", None),
                    exc_info=True,
                )
            # open_job_stage_trace handles P0 begin (ONTA-388 cross-category helper).
            rec = open_job_stage_trace(
                job,
                input={
                    "job_id": job.id,
                    "category": "discovery",
                    "spend_ceiling_usd": job.spend_ceiling_usd,
                },
                action_detail="discovery job queued",
            )
            if rec is not None:
                rec.begin(
                    StageProjectId.p1,
                    input={
                        "goal": (query or getattr(step, "instruction", None) or "")[:500],
                        "type_name": proposed_type,
                        "attributes": attributes,
                        "kg_name": kg_name,
                        "cap": cap,
                        "subqueries": subqueries[:20],
                        "providers": [
                            getattr(pr, "name", str(pr)) for pr in ensemble[:10]
                        ],
                    },
                )
                rec.action(StageProjectId.p1, "plan", detail="spec resolved; ready to search")
            await job_store.create(job)

        # Thread the tracked job id into the provider context so a URL-targeted
        # provider that resumes asynchronously (e.g. a webhook-driven adapter) can
        # correlate its callback back to THIS job. Generic + optional: providers
        # that don't need it ignore the key, and it's absent when discovery runs
        # without a job store (bare/test context), so nothing depends on it.
        if job is not None:
            pctx = {**pctx, "job_id": job.id}

        # A1 Source Bundle run identity (ONTA-346): ONE run_id for the whole
        # discovery run — the tracked job id when present (the EnrichJob IS the
        # run; its manifest already keys off job.id), else a fresh uuid for a
        # bare/test context. workspace_id = the tenant (ADR 0011: pipeline code
        # says workspace_id, infra keeps tenant_id — no blanket rename).
        run_id = job.id if job is not None else str(uuid.uuid4())
        # ONTA-372 (keystone): mint ONE run-scoped ArtifactEnvelope at the P1 entry
        # and thread ITS run_id through the WHOLE discovery pipeline — the A1
        # Source Bundle (below) AND both resolver ingest paths, which key the A6
        # Graph Delta off it. Before this, the resolver minted its own unrelated
        # uuid4, so the A1 bundle's lineage and the A6 Graph Delta's lineage
        # DIVERGED and the A6 delta was effectively dead on the discovery path.
        # workspace_id = the tenant (ADR 0011). fact_id is the run's A1 root.
        run_envelope = ArtifactEnvelope(
            workspace_id=ctx.tenant_id,
            run_id=run_id,
            fact_id=derive_fact_id(run_id=run_id, stage="A1"),
        )


        async def _run() -> None:
            try:
                await asyncio.wait_for(
                    _run_discovery_inner(
                        ctx=ctx,
                        job=job,
                        job_store=job_store,
                        instance_graph=instance_graph,
                        kg_name=kg_name,
                        ensemble=ensemble,
                        subqueries=subqueries,
                        cap=cap,
                        hint_columns=hint_columns,
                        attributes=attributes,
                        attributes_exhaustive=attributes_exhaustive,
                        proposed_type=proposed_type,
                        urls=urls,
                        query=query,
                        pctx=pctx,
                        ontology_lock=ontology_lock,
                        provider=provider,
                        run_id=run_id,
                        run_envelope=run_envelope,
                    ),
                    timeout=_wic._RUN_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                _wic.logger.error(
                    "web_ingest_run_timeout",
                    query=query,
                    timeout_s=_wic._RUN_TIMEOUT_S,
                )
                await _fail_job(
                    job,
                    job_store,
                    f"Discovery timed out after {int(_wic._RUN_TIMEOUT_S)}s "
                    "(the web fetch or extraction took too long).",
                )

        _wic._spawn(_run())
        ack = {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            "title": query,
            "message": (
                f"Searching the web for “{query}” and ingesting the results "
                f"as {proposed_type} ({', '.join(attributes)}) in the background."
            ),
        }
        if job is not None:
            ack["job_id"] = job.id
            ack["job_status"] = job.status.value
        return ack
