"""One path for running a SAVED extract source (ONTA-555).

Both the manual ``POST /extract-sources/{slug}/run`` route and the scheduler's
``extract`` action land here, so a scheduled read is byte-for-byte the same
work as pressing Run: same request body construction, same secret resolution,
same :func:`~infona_client.ingestion.run.run_dlt_ingest` handoff onto
``ingest_structured_rows`` → ``insert_facts`` → ``refresh_after_write``.

Entitlement is decided by the ROUTES (``require_hosted_extract`` with the real
:class:`TenantContext`), not here: a fired schedule has no auth context, and
synthesizing one would mis-answer the premium checker — it reads a
Clerk-stamped bit off the verified context, so a synthetic context would look
un-entitled for exactly the workspaces that are paying. Creating or editing a
schedule goes through the gated routes; a lapsed workspace is handled by
removing the source, not by silently failing its fires.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from infona_client.config import settings
from infona_client.ingestion.extract_source_store import (
    make_extract_source_store,
    secret_store_slug,
)
from infona_client.ingestion.models import (
    DltExtractSource,
    DltIngestRequest,
    RunExtractSourceRequest,
)
from infona_client.ingestion.store_secrets import tenant_store_getter
from infona_client.resolver.models import IngestResult

_log = structlog.stdlib.get_logger("infona.ingestion.saved_run")


class ExtractNotRunnable(ValueError):
    """The saved config cannot be run yet (unmapped resources, disabled, gone).

    Callers map this to HTTP; the scheduler logs and skips.
    """


def build_run_request(
    source: DltExtractSource, override: Optional[RunExtractSourceRequest] = None
) -> DltIngestRequest:
    """Build the frozen ``POST /ingest/dlt`` body from a saved config.

    Rewrites the stored logical secret names (``token`` / ``dsn``) into
    ``store:dlt:{slug}/<logical>`` refs so the run resolves them out of the
    per-tenant cipher store.
    """
    spec = source.source.model_copy(deep=True)
    if override and override.limit is not None:
        spec = spec.model_copy(update={"limit": override.limit})
    auth = spec.auth
    if auth and auth.secret_ref and not auth.secret_ref.startswith(("env:", "store:")):
        spec = spec.model_copy(
            update={
                "auth": auth.model_copy(
                    update={
                        "secret_ref": f"store:{secret_store_slug(source.slug)}/{auth.secret_ref}",
                        "token": None,
                    }
                )
            }
        )
    if spec.dsn and spec.dsn == "dsn":
        spec = spec.model_copy(
            update={"dsn": f"store:{secret_store_slug(source.slug)}/dsn"}
        )
    kg = source.kg
    if override and override.kg:
        kg = override.kg
    if not source.map:
        raise ExtractNotRunnable(
            "map is required before run — set each resource's ontology type + id_field"
        )
    return DltIngestRequest(source=spec, map=source.map, kg=kg)


async def run_saved_extract(
    *,
    tenant_id: str,
    source: DltExtractSource,
    neptune: Any,
    job_store: Any = None,
    override: Optional[RunExtractSourceRequest] = None,
) -> IngestResult:
    """Extract + ingest one saved source. Raises; callers map to HTTP / logs."""
    from infona_client.ingestion.run import run_dlt_ingest

    body = build_run_request(source, override)
    return await run_dlt_ingest(
        tenant_id=tenant_id,
        body=body,
        neptune=neptune,
        anthropic_key=settings.anthropic_api_key,
        job_store=job_store,
        store_get=tenant_store_getter(tenant_id),
    )


async def run_scheduled_extract(
    schedule, *, neptune: Any, job_store: Any = None
) -> Optional[IngestResult]:
    """Fire one due ``extract`` schedule (see ``scheduling/models.py``).

    Returns ``None`` when the schedule points at a source that is gone or
    disabled — a stale row must not raise and stop the sweep.
    """
    slug = str((schedule.params or {}).get("extract_slug") or "").strip()
    if not slug:
        _log.warning("extract_schedule_missing_slug", schedule_id=schedule.id)
        return None
    row = await make_extract_source_store().get(schedule.tenant_id, slug)
    if row is None or not row.source.enabled:
        _log.info(
            "extract_schedule_skipped",
            schedule_id=schedule.id,
            tenant=schedule.tenant_id,
            slug=slug,
            reason="missing" if row is None else "disabled",
        )
        return None
    # No kg override: the SAVED SOURCE decides where rows land, exactly as it
    # does for a manual run. The schedule row's ``kg_name`` is descriptive (it
    # labels the fired job) and would go stale the moment someone re-pointed the
    # source at another graph — following it would make a scheduled read write
    # somewhere the Read now button does not.
    result = await run_saved_extract(
        tenant_id=schedule.tenant_id,
        source=row.source,
        neptune=neptune,
        job_store=job_store,
    )
    _log.info(
        "extract_schedule_fired",
        schedule_id=schedule.id,
        tenant=schedule.tenant_id,
        slug=slug,
        rows=result.rows_in,
        triples=result.triples_inserted,
    )
    return result
