"""KG export — get instance data back out (F10 / OSS launch).

``GET /graphs/{tenant}/kgs/{kg_name}/export`` returns entities as JSON or CSV.
Uses the same read path as Explorer type records (Neo4j GraphStore when
``INFONA_GRAPH_BACKEND=neo4j``, SPARQL fallback otherwise) so export never
forks a third query dialect.

Scope for OSS v1:
* one type (``type`` query param) or every type with instances
* ``format=json`` (default) or ``format=csv``
* hard cap on rows so a runaway export cannot OOM the process
"""

from __future__ import annotations

import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.explore import get_type_records
from infona_client.api.routes.knowledge_graphs import list_type_counts
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.client import NeptuneClient
from infona_client.graph.queries import is_valid_kg_name, require_valid_type_name

router = APIRouter(prefix="/graphs/{tenant}/kgs")

# Per-type page size when walking the records endpoint (server clamp is 200).
_PAGE = 200
# Hard ceiling across all types for a single export request.
_MAX_ROWS = 50_000


class ExportTypeBlock(BaseModel):
    type: str
    columns: list[str]
    rows: list[dict[str, Any]]
    total: int
    truncated: bool = False


class ExportResponse(BaseModel):
    kg: str
    format: Literal["json"] = "json"
    types: list[ExportTypeBlock]
    row_count: int
    truncated: bool = False
    note: str | None = Field(
        default=None,
        description="Human-readable caveat (e.g. row cap hit).",
    )


async def _export_type_rows(
    *,
    kg_name: str,
    type_name: str,
    max_rows: int,
    tenant: TenantContext,
    client: NeptuneClient,
) -> ExportTypeBlock:
    """Page through get_type_records until max_rows or exhaustion."""
    require_valid_type_name(type_name)
    columns: list[str] = ["name"]
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    total = 0
    truncated = False

    while len(rows) < max_rows:
        page = await get_type_records(
            kg_name=kg_name,
            type_name=type_name,
            limit=min(_PAGE, max_rows - len(rows)),
            cursor=cursor,
            tenant=tenant,
            client=client,
        )
        page_cols = list(page.get("columns") or ["name"])
        for c in page_cols:
            if c not in columns:
                columns.append(c)
        page_rows = list(page.get("rows") or [])
        total = int(page.get("total") or total)
        if not page_rows:
            break
        rows.extend(page_rows)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    if total > len(rows) or (cursor and len(rows) >= max_rows):
        truncated = True
    return ExportTypeBlock(
        type=type_name,
        columns=columns,
        rows=rows,
        total=total or len(rows),
        truncated=truncated,
    )


def _rows_to_csv(blocks: list[ExportTypeBlock]) -> str:
    """Flat CSV with a ``type`` column; union of all attribute columns."""
    col_set: list[str] = ["type", "id", "name"]
    for b in blocks:
        for c in b.columns:
            if c not in col_set:
                col_set.append(c)
        # rows may carry keys not in columns (id)
        for r in b.rows:
            for k in r.keys():
                if k not in col_set:
                    col_set.append(k)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=col_set, extrasaction="ignore")
    w.writeheader()
    for b in blocks:
        for r in b.rows:
            out = {k: "" for k in col_set}
            out["type"] = b.type
            for k, v in r.items():
                if k in out:
                    if isinstance(v, (list, dict)):
                        out[k] = str(v)
                    elif v is None:
                        out[k] = ""
                    else:
                        out[k] = v
            w.writerow(out)
    return buf.getvalue()


@router.get("/{kg_name}/export")
async def export_kg(
    kg_name: str,
    format: Literal["json", "csv"] = Query(
        "json",
        description="json (structured) or csv (flat table with a type column)",
    ),
    type: str | None = Query(
        None,
        alias="type",
        description="Optional single type name. Omit to export every type with instances.",
    ),
    limit: int = Query(
        _MAX_ROWS,
        ge=1,
        le=_MAX_ROWS,
        description=f"Max total rows across all types (hard cap {_MAX_ROWS}).",
    ),
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Export KG instance data as JSON or CSV (OSS launch F10)."""
    if not is_valid_kg_name(kg_name):
        raise HTTPException(status_code=422, detail=f"Invalid kg name: {kg_name!r}")

    if type:
        require_valid_type_name(type)
        type_names = [type]
    else:
        counts = await list_type_counts(
            kg_name=kg_name, tenant=tenant, client=client
        )
        type_names = [c.name for c in counts if getattr(c, "entity_count", 0) > 0]
        if not type_names:
            # Empty KG — still a valid export.
            type_names = []

    remaining = limit
    blocks: list[ExportTypeBlock] = []
    for tname in type_names:
        if remaining <= 0:
            break
        block = await _export_type_rows(
            kg_name=kg_name,
            type_name=tname,
            max_rows=remaining,
            tenant=tenant,
            client=client,
        )
        blocks.append(block)
        remaining -= len(block.rows)

    row_count = sum(len(b.rows) for b in blocks)
    truncated = any(b.truncated for b in blocks) or (
        type is None and remaining <= 0 and len(type_names) > len(blocks)
    )
    note = None
    if truncated:
        note = (
            f"Export hit the row cap ({limit}). "
            "Narrow with ?type=<Type> or raise limit (max "
            f"{_MAX_ROWS})."
        )

    if format == "csv":
        body = _rows_to_csv(blocks)
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{kg_name}.csv"',
                "X-Infona-Export-Rows": str(row_count),
                "X-Infona-Export-Truncated": "1" if truncated else "0",
            },
        )

    return ExportResponse(
        kg=kg_name,
        format="json",
        types=blocks,
        row_count=row_count,
        truncated=truncated,
        note=note,
    )
