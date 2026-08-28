import importlib
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from infona_client.api.middleware import RequestLoggingMiddleware
from infona_client.api.rate_limit import limiter
from infona_client.api.routes import actions, agent, api_sources, ask, blueprint, blueprints, conversations, corrections, enrich, explore, export, extract_sources, functions, grep, health, history, ingest, ingest_dlt, jobs, knowledge_graphs, lambda_functions, normalize, ontology, operator, query, schedules, search, skills, tenants, triples, usage, user_api_sources, workspace_invites
from infona_client.config import settings
from infona_client.graph.client import NeptuneClient
from infona_client.graph.queries import InvalidGraphIdentifier
from infona_client.logging import setup_logging

logger = structlog.stdlib.get_logger("infona.app")


def _load_auth_plugin() -> None:
    """Import and invoke the configured auth plugin, if any.

    Format: "module.path:callable". The callable is invoked with no
    arguments and is expected to register an external verifier via
    infona.auth.api_keys.register_external_verifier. Failures are logged
    but do not prevent the app from starting — the app will simply fall
    back to static API key auth.
    """
    spec = settings.auth_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("auth_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("auth_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("auth_plugin_load_failed", plugin=spec, error=str(exc))


def _load_enrichment_plugin() -> None:
    """Import and invoke the configured enrichment plugin, if any.

    Format: "module.path:callable". The callable is invoked with no
    arguments and is expected to register paid source adapters via
    infona_client.enrichment.sources.base.register_adapter and override
    tier→chain mappings via infona_client.enrichment.tiers.register_tier.
    Failures are logged but do not prevent the app from starting — the
    app will simply fall back to the OSS defaults (lite tier, Wikidata).
    """
    spec = settings.enrichment_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("enrichment_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("enrichment_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("enrichment_plugin_load_failed", plugin=spec, error=str(exc))


def _load_governance_plugin() -> None:
    """Import and invoke the configured governance plugin, if any (COG-56).

    Format: "module.path:callable". The callable is invoked with no
    arguments and is expected to register a mapping-shape judge panel via
    infona_client.resolver.governance.register_governance_panel. Failures
    are logged but do not prevent the app from starting — the app simply
    falls back to the OSS default (proposals recorded pending,
    tenant-layer-only behavior).
    """
    spec = settings.governance_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("governance_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("governance_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("governance_plugin_load_failed", plugin=spec, error=str(exc))


def _load_web_source_plugin() -> None:
    """Import and invoke the configured web-source plugin, if any.

    Format: "module.path:callable". The callable is invoked with no arguments
    and is expected to register a web-discovery provider via
    infona_client.web_sources.base.register_web_source. Failures are logged but
    do not prevent startup — the "discover" intent simply stays dormant
    (plan() returns a "not enabled" message). The OSS dev stub registers via
    "infona_client.web_sources.stub:register"; a downstream deployment points
    this at its paid provider with no OSS change.
    """
    spec = settings.web_source_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("web_source_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("web_source_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("web_source_plugin_load_failed", plugin=spec, error=str(exc))


def _load_geocoder_plugin() -> None:
    """Import and invoke the configured free-text geocoder plugin, if any (ONTA-249).

    Format: "module.path:callable". The callable is invoked with no arguments and
    is expected to register a premium Geocoder via
    infona_client.spatiotemporal.geocoder.register_geocoder (e.g. a Google Places
    / Mapbox / Nominatim adapter). Failures are logged but do not prevent startup —
    without it the OSS default (a deterministic offline gazetteer) is used, so a
    bare place-name radius anchor still resolves for common places. No paid
    geocoding API is baked into OSS; premium flows premium → OSS via this seam.
    """
    spec = settings.geocoder_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("geocoder_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("geocoder_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("geocoder_plugin_load_failed", plugin=spec, error=str(exc))


def _load_api_registry_plugin() -> None:
    """Import and invoke the configured API-source-registry plugin, if any.

    Format: "module.path:callable". The callable is invoked with no arguments
    and is expected to contribute the premium "global_enhanced" catalog overlay
    via infona_client.api_registry.register_api_source_layer. Failures are
    logged but do not prevent startup — without it only the OSS "global_public"
    seed catalog is loaded (ONTA-194).
    """
    spec = settings.api_registry_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("api_registry_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("api_registry_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("api_registry_plugin_load_failed", plugin=spec, error=str(exc))


def _load_skills_plugin() -> None:
    """Import and invoke the configured type-SKILLS plugin, if any.

    Format: "module.path:callable". The callable is invoked with no arguments
    and is expected to contribute the curated Global-Enhanced skill layer via
    infona_client.skills.register_skill_layer. Failures are logged but do not
    prevent startup — without it only the OSS Global-Public seed content is
    loaded, and resolution degrades to Tenant > Public.
    """
    spec = settings.skills_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("skills_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("skills_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("skills_plugin_load_failed", plugin=spec, error=str(exc))


def _load_secrets_cipher_plugin() -> None:
    """Import and invoke the configured secret-cipher plugin, if any (ONTA-2xx).

    Format: "module.path:callable". The callable is invoked with no arguments and
    is expected to register a SecretCipher via
    infona_client.api_registry.register_secret_cipher (e.g. an AWS-KMS data-key
    cipher). Failures are logged but do not prevent startup — without it, the OSS
    default LocalAesGcmCipher (keyed by INFONA_SECRETS_KEY) is used, or, if that
    key is also unset, secret storage is disabled (fail closed).
    """
    spec = settings.secrets_cipher_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("secrets_cipher_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("secrets_cipher_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("secrets_cipher_plugin_load_failed", plugin=spec, error=str(exc))


def _load_analytics_plugin() -> None:
    """Import and invoke the configured analytics plugin, if any (ONTA-323).

    Format: "module.path:callable". The callable is invoked with no arguments
    and is expected to register a product-analytics sink via
    infona_client.analytics.register_analytics_sink (e.g. a proprietary
    hosted-analytics sink). Failures are logged but do not prevent the app from
    starting — without it the OSS default no-op sink is used, so emit() drops
    every event and OSS stays analytics-free (no third-party analytics dependency
    baked into OSS).
    """
    spec = settings.analytics_plugin.strip()
    if not spec:
        return
    if ":" not in spec:
        logger.warning("analytics_plugin_invalid_format", spec=spec)
        return
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        fn()
        logger.info("analytics_plugin_loaded", plugin=spec)
    except Exception as exc:
        logger.error("analytics_plugin_load_failed", plugin=spec, error=str(exc))


def _load_router_plugins(app: FastAPI) -> None:
    """Import and invoke the configured router plugins, if any.

    Format: comma-separated "module.path:callable" entries. Each callable is
    invoked with the FastAPI app instance and is expected to mount additional
    routers via app.include_router(...). Failures are logged per-entry but do
    not prevent the app from starting — the app simply runs with only the OSS
    routers. This is a generic plugin protocol (no proprietary coupling): it
    lets downstream deployments attach external routers (e.g. the premium
    ontology recommender).
    """
    spec = settings.router_plugins.strip()
    if not spec:
        return
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning("router_plugin_invalid_format", spec=entry)
            continue
        module_name, attr = entry.split(":", 1)
        try:
            module = importlib.import_module(module_name)
            fn = getattr(module, attr)
            fn(app)
            logger.info("router_plugin_loaded", plugin=entry)
        except Exception as exc:
            logger.error("router_plugin_load_failed", plugin=entry, error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    # Fail fast on a legacy backend selection (ONTA-527): a deploy that still
    # carries INFONA_GRAPH_BACKEND=neptune must not boot and then serve reads
    # from a store that no longer exists. graph_backend() raises GraphConfigError
    # on anything but "neo4j".
    from infona_client.graph.store import graph_backend

    logger.info("starting", graph_backend=graph_backend())
    # Vestigial SPARQL client (ONTA-527 / ONTA-534): handlers still declare
    # Depends(get_neptune_client) for residual type-hints / dual-arm archaeology.
    # Constructing it opens no connection. Under a configured GraphStore,
    # NeptuneClient.query/update/ask fail closed (SparqlClientRetired) so
    # residual arms cannot hang on decommissioned Neptune HTTP. "neptune" is
    # the path layout, not a backend selection — BACKENDS has no neo4j key.
    app.state.neptune_client = NeptuneClient(
        settings.neptune_endpoint or "http://127.0.0.1:8182",
        backend="neptune",
    )
    try:
        from infona_client.graph.store import get_graph_store

        store = get_graph_store()
        await store.bootstrap_schema()
        logger.info("neo4j_graph_store_ready")
    except Exception as exc:  # noqa: BLE001 — surface in logs; health will degrade
        logger.error("neo4j_graph_store_bootstrap_failed", error=str(exc))
    # ONTA-399: re-hydrate durable Enhanced global skills into the process
    # mirror so authored layer-B content survives restart/redeploy without
    # depending on the image's file seed. Best-effort — never blocks startup.
    try:
        from infona_client.skills import hydrate_global_skills_from_store

        n = await hydrate_global_skills_from_store()
        if n:
            logger.info("global_enhanced_skills_hydrated", count=n)
    except Exception as exc:  # noqa: BLE001 - skills hydrate must not break startup
        logger.warning("global_enhanced_skills_hydrate_failed", error=str(exc))
    # COG-136: start the in-process schedule firing loop. make_schedule_runner
    # returns None when scheduling is disabled (no database_url and not explicitly
    # enabled), so startup is unaffected when the feature is off. Failures here
    # are logged but never block the app from serving requests.
    app.state.schedule_runner = None
    try:
        from infona_client.scheduling.runner import make_schedule_runner

        runner = make_schedule_runner(app.state)
        if runner is not None:
            runner.start()
            app.state.schedule_runner = runner
            logger.info("schedule_runner_enabled")
    except Exception as exc:  # noqa: BLE001 - scheduling must not break startup
        logger.error("schedule_runner_start_failed", error=str(exc))
    # ONTA-181: seed the semantic-maintenance schedule rows (the global
    # embed-fill sweep; per-KG reconcile rows are ensured by the write hook /
    # reindex route). Only meaningful when both the semantic index AND the
    # runner are enabled — rows without a runner would never fire, so we warn
    # instead of seeding. Best-effort: a seeding hiccup must not block startup.
    try:
        from infona_client.semantic.reconciler import (
            ensure_embed_fill_schedule,
            semantic_index_enabled,
        )

        if semantic_index_enabled():
            if app.state.schedule_runner is not None:
                await ensure_embed_fill_schedule(app.state.schedule_store)
                logger.info("semantic_maintenance_schedules_seeded")
            else:
                logger.warning(
                    "semantic_index_enabled_without_scheduler",
                    hint=(
                        "INFONA_SEMANTIC_INDEX_ENABLED is set but the schedule "
                        "runner is disabled — embed-fill/reconcile will not run "
                        "(set INFONA_DATABASE_URL or INFONA_SCHEDULER_ENABLED)."
                    ),
                )
    except Exception as exc:  # noqa: BLE001 - seeding must not break startup
        logger.error("semantic_schedule_seed_failed", error=str(exc))
    yield
    runner = getattr(app.state, "schedule_runner", None)
    if runner is not None:
        try:
            await runner.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown best-effort
            logger.warning("schedule_runner_stop_failed", error=str(exc))
    # Drain any buffered usage-metering increments (flush() never raises).
    from infona_client.usage.recorder import get_usage_recorder

    await get_usage_recorder().flush()
    # Drain any buffered product-analytics events (ONTA-323). Best-effort +
    # never raises: the OSS default no-op flush does nothing; a registered
    # premium sink flushes its background batch on shutdown.
    from infona_client.analytics import flush_analytics

    flush_analytics()
    await app.state.neptune_client.close()
    logger.info("shutdown")


async def _invalid_graph_identifier_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Render an :class:`InvalidGraphIdentifier` as a 422 (ONTA-414 / 425 / 422).

    Registered on the BASE class, so ``InvalidKGName``, ``InvalidTypeName``,
    ``InvalidTenantId`` and any future member are all covered by one handler
    (Starlette resolves handlers by walking the exception's MRO). A new member
    can therefore never regress to an opaque 500 because someone forgot a
    registration.
    """
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def create_app() -> FastAPI:
    _load_auth_plugin()
    _load_enrichment_plugin()
    _load_governance_plugin()
    _load_web_source_plugin()
    _load_api_registry_plugin()
    _load_skills_plugin()
    _load_geocoder_plugin()
    _load_secrets_cipher_plugin()
    _load_analytics_plugin()
    app = FastAPI(
        title="Infona",
        description="Living Knowledge Graph Platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # ONTA-414 / 425 / 422: the URI builders validate the caller-supplied name at
    # the ONE place every route funnels through (kg_graph_uri for a KG name,
    # type_uri / attr_uri for a type or attribute name, tenant_graph_uri for the
    # workspace), so a route that takes one off a request body or a path segment
    # without its own pattern still fails closed. Map the whole family to 422 (the
    # same status a pydantic pattern violation produces) rather than letting it
    # surface as an opaque 500.
    app.add_exception_handler(
        InvalidGraphIdentifier, _invalid_graph_identifier_handler
    )
    # GraphScopeError is the GraphStore/scope fail-closed family (reserved
    # attr keys, unscoped Cypher, etc.). Surface as 422 with the real message
    # so CLI/MCP agents can self-correct instead of an opaque 500.
    from infona_client.graph.scope import GraphScopeError

    async def _graph_scope_error_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    app.add_exception_handler(GraphScopeError, _graph_scope_error_handler)
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(health.router, tags=["health"])
    app.include_router(triples.router, tags=["triples"])
    app.include_router(query.router, tags=["query"])
    app.include_router(functions.router, tags=["functions"])
    app.include_router(lambda_functions.router, tags=["lambda_functions"])
    # ONTA-236: dated old→new value-history read route (companion of the shared
    # write-path history graph delete_facts populates).
    app.include_router(history.router, tags=["history"])
    app.include_router(ask.router, tags=["ask"])
    app.include_router(ontology.router, tags=["ontology"])
    app.include_router(ingest.router, tags=["ingest"])
    # ONTA-553: POST /ingest/dlt lives in its own module so ingest.py stays
    # under the file-size ratchet. Same canonical route; no client-side fork.
    app.include_router(ingest_dlt.router, tags=["ingest"])
    app.include_router(knowledge_graphs.router, tags=["knowledge_graphs"])
    app.include_router(export.router, tags=["export"])
    app.include_router(blueprint.export_router, tags=["blueprint"])
    app.include_router(blueprint.validate_router, tags=["blueprint"])
    app.include_router(enrich.router, tags=["enrich"])
    app.include_router(jobs.router, tags=["jobs"])
    app.include_router(operator.router, tags=["operator"])
    app.include_router(actions.router, tags=["actions"])
    app.include_router(schedules.router, tags=["schedules"])
    app.include_router(explore.router, tags=["explore"])
    app.include_router(normalize.router, tags=["normalize"])
    # ONTA-281: the canonical A10 user-correction write path (webapp/CLI/MCP all
    # ride this one route — interface-convergence rule).
    app.include_router(corrections.router, tags=["corrections"])
    app.include_router(tenants.router, tags=["tenants"])
    # ONTA-227: canonical workspace membership + invite routes (web/CLI/MCP all
    # ride these — interface-convergence rule).
    app.include_router(workspace_invites.router, tags=["workspace"])
    app.include_router(agent.router, tags=["agent"])
    app.include_router(conversations.router, tags=["conversations"])
    app.include_router(usage.router, tags=["usage"])
    # ONTA-178: the canonical semantic instance search (webapp/CLI/MCP all ride
    # this one route — interface-convergence rule).
    app.include_router(search.router, tags=["search"])
    # ONTA-416: index-free literal grep over ONE KG's triples — the debugging
    # counterpart to /search (live triple scan, no derived index), a SEPARATE
    # canonical route because its contract inverts /search's on every axis
    # (see routes/grep.py). Webapp/CLI/MCP all ride this one route.
    app.include_router(grep.router, tags=["grep"])
    # ONTA-2xx: the per-tenant API source registry (webapp/CLI/MCP all ride these
    # canonical routes via the shared SDK — interface-convergence rule).
    app.include_router(api_sources.router, tags=["api_sources"])
    # User-scoped API sources: register once, visible in every workspace the
    # caller can access. Canonical /v1/me/api-sources (not under /graphs).
    app.include_router(user_api_sources.router, tags=["user_api_sources"])
    # ONTA-553/554: 3rd-party extract sources (dlt). Distinct from /api-sources
    # (curated lookup registry). Execute is POST /ingest/dlt; this family persists
    # workspace configs. Same SDK/CLI/Explorer route — no /hubspot-sync.
    app.include_router(extract_sources.router, tags=["extract_sources"])
    # Type-attached SKILLS: markdown instruction attached to an entity type,
    # consumed by LM agents (distinct from FUNCTIONS, which are type-attached
    # compute). One canonical route set for webapp/CLI/MCP.
    app.include_router(skills.router, tags=["skills"])
    # Blueprint install / inspect / uninstall / fork (INF-575 / INF-577).
    # One /graphs/{tenant}/blueprints family; CLI/MCP/Explorer ride these.
    app.include_router(blueprints.router, tags=["blueprints"])
    _register_agent_capabilities()
    _load_router_plugins(app)
    # ONTA-227: make the workspace-registry operating mode visible at startup —
    # the degraded modes (no durable store / enforcement flag off) are
    # deliberate but must never be silent.
    from infona_client.auth.workspace_store import log_workspace_registry_mode

    log_workspace_registry_mode()
    return app


def _register_agent_capabilities() -> None:
    """Register the default OSS agent capabilities (query, normalize, enrich).

    The single agent endpoint dispatches through the capability registry, so
    capabilities must be registered for it to work. Import-safe + idempotent;
    a proprietary deployment registers additional capabilities the same way a
    router/enrichment plugin does, with no route change.
    """
    try:
        from infona_client.agent.planner import register_default_capabilities

        register_default_capabilities()
        logger.info("agent_capabilities_registered")
    except Exception as exc:  # noqa: BLE001
        logger.error("agent_capability_registration_failed", error=str(exc))


app = create_app()
