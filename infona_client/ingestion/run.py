"""Handoff: dlt extract → mapped-records ingest → insert_facts (ONTA-553).

One refresh_after_write at the end with the types touched. No dlt destination.
No LLM extract — rows are already structured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from infona_client.graph.kg_writer import refresh_after_write
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.ingestion.dlt_source import (
    ExtractedResource,
    extract_records,
    lookup_resource_map,
)
from infona_client.ingestion.errors import DltExtractError
from infona_client.ingestion.models import DltIngestRequest, DltResourceMap
from infona_client.ingestion.secrets import ResolvedSecrets, resolve_source_secrets
from infona_client.resolver.file_ingest_job import (
    fail_file_ingest_job,
    finish_file_ingest_job,
    mark_file_ingest_running,
    note_file_ingest_p2,
    note_file_ingest_p5,
    note_file_ingest_p6,
    open_file_ingest_job,
)
from infona_client.resolver.models import IngestResult
from infona_client.resolver.schema_resolver import SchemaResolver

_log = structlog.stdlib.get_logger("infona.ingestion.dlt")

# Match CSV row batches so ALB / memory behaviour stays identical.
_ROW_BATCH = 500


def _merge_results(parts: list[IngestResult]) -> IngestResult:
    if not parts:
        return IngestResult(rows_in=0)
    out = parts[0].model_copy(deep=True)
    for part in parts[1:]:
        out.entities_extracted += part.entities_extracted
        out.entities_resolved += part.entities_resolved
        out.triples_inserted += part.triples_inserted
        out.entities_deduplicated += part.entities_deduplicated
        out.chunks_processed += part.chunks_processed
        out.rows_in += part.rows_in
        out.rows_dropped += part.rows_dropped
        out.rows_key_merged += part.rows_key_merged
        out.rows_key_minted += part.rows_key_minted
        out.rows_key_unmatched += part.rows_key_unmatched
        out.types_created = list(dict.fromkeys(out.types_created + part.types_created))
        out.attributes_added = list(
            dict.fromkeys(out.attributes_added + part.attributes_added)
        )
        out.node_target_types = list(
            dict.fromkeys(out.node_target_types + part.node_target_types)
        )
        out.rejections.extend(part.rejections)
        out.flagged_types = list(dict.fromkeys(out.flagged_types + part.flagged_types))
        for key, count in part.drops_by_entity.items():
            out.drops_by_entity[key] = out.drops_by_entity.get(key, 0) + count
        out.free_text_attributes = list(
            dict.fromkeys(out.free_text_attributes + part.free_text_attributes)
        )
    return out


async def run_dlt_ingest(
    *,
    tenant_id: str,
    body: DltIngestRequest,
    neptune: Any,
    anthropic_key: str,
    job_store: Any = None,
    secrets: Optional[ResolvedSecrets] = None,
    store_get: Optional[Callable] = None,
    source_factory: Optional[Callable] = None,
    verdict_cache: Any = None,
) -> IngestResult:
    """Extract then ingest. Callers map errors to HTTP; this does not HTTPException."""
    spec = body.source
    if secrets is None:
        secrets = await resolve_source_secrets(spec, store_get=store_get)

    mapped_types = [m.type for m in body.map.values()]
    job = await open_file_ingest_job(
        job_store,
        tenant_id=tenant_id,
        kg_name=body.kg or "",
        content_type="dlt",
        source=f"dlt:{spec.kind}",
        type_name=mapped_types[0] if mapped_types else "DltRecord",
        attributes=list(body.map.keys())[:40],
        rows_hint=spec.limit * len(spec.resources),
    )
    await mark_file_ingest_running(job, job_store, phase="extracting")
    note_file_ingest_p2(
        job,
        action="dlt_extract",
        detail=f"kind={spec.kind} resources={len(spec.resources)} limit={spec.limit}",
    )

    try:
        extracted = extract_records(
            spec, secrets=secrets, source_factory=source_factory
        )
        if not extracted:
            raise DltExtractError(
                "extract returned no resources. Check source.resources against "
                "the REST paths / SQL table names."
            )

        from infona_client.resolver.verdict_cache import JsonVerdictCache

        cache = verdict_cache or JsonVerdictCache(Path("/tmp/infona-verdict-cache.json"))
        resolver = SchemaResolver(
            neptune=neptune, anthropic_key=anthropic_key, verdict_cache=cache
        )
        instance_graph = (
            kg_graph_uri(tenant_id, body.kg) if body.kg else tenant_graph_uri(tenant_id)
        )
        resolver._instance_graph = instance_graph

        note_file_ingest_p5(
            job,
            action="map_resources",
            detail=f"extracted={len(extracted)} mapped={len(body.map)}",
        )
        parts: list[IngestResult] = []
        for resource in extracted:
            mapping: Optional[DltResourceMap] = lookup_resource_map(
                resource.name, body.map
            )
            if mapping is None:
                _log.info(
                    "dlt_resource_unmapped_skipped",
                    resource=resource.name,
                    tenant=tenant_id,
                )
                continue
            parts.extend(
                await _ingest_resource(
                    resolver,
                    tenant_id=tenant_id,
                    resource=resource,
                    mapping=mapping,
                    instance_graph=instance_graph,
                    kind=spec.kind,
                )
            )

        if not parts:
            raise DltExtractError(
                "no extracted resource matched the map. Map keys must equal "
                "source.resources (REST path or SQL table name)."
            )

        result = _merge_results(parts)
        note_file_ingest_p6(
            job,
            action="insert_facts",
            detail=f"rows={result.rows_in} types={sorted(result.affected_types())}",
        )
        await refresh_after_write(
            neptune,
            tenant_id=tenant_id,
            kg_name=body.kg,
            affected_types=result.affected_types(),
        )
    except Exception as exc:
        await fail_file_ingest_job(job, job_store, str(exc))
        raise

    await finish_file_ingest_job(job, job_store, result=result)
    if job is not None:
        result.job_id = job.id
    return result


async def _ingest_resource(
    resolver: SchemaResolver,
    *,
    tenant_id: str,
    resource: ExtractedResource,
    mapping: DltResourceMap,
    instance_graph: str,
    kind: str,
) -> list[IngestResult]:
    if not resource.rows:
        return [IngestResult(rows_in=0)]
    source = f"dlt:{kind}:{resource.name}"
    parts: list[IngestResult] = []
    rows = resource.rows
    for i in range(0, len(rows), _ROW_BATCH):
        batch = rows[i : i + _ROW_BATCH]
        part = await resolver.ingest_structured_rows(
            batch,
            tenant_id,
            type_name=mapping.type,
            attributes=mapping.attributes,
            source=source,
            instance_graph=instance_graph,
            key_attribute=mapping.id_field,
            attributes_exhaustive=bool(mapping.attributes),
        )
        parts.append(part)
    return parts
