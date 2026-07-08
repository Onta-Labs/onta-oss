"""Value-history read route (ONTA-236).

Exposes the companion value-history graph that ``graph/kg_writer.delete_facts``
populates on every genuine attribute-value change (gated by
``COGRAPH_VALUE_HISTORY_ENABLED``). Answers the persona question "which values
changed since <date>, old → new, with a change date" — e.g. Speko's
``sp-price-changes``: "every model whose price changed this week, old → new".

Read-only and GENERAL: it queries the same store for any attribute of any type,
with optional ``subject`` / ``predicate`` / ``since`` narrowing, so a "changed
since <cutoff>" question returns only transitions after the cutoff, each dated.
The WRITE side stays entirely on the shared write path — this route never writes.
"""

from fastapi import APIRouter, Depends, Query

from cograph_client.api.deps import get_neptune_client
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.history import fetch_value_history
from cograph_client.graph.queries import kg_graph_uri

router = APIRouter()


@router.get("/graphs/{tenant}/history")
async def get_value_history(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    kg_name: str = Query(..., description="KG whose value history to read"),
    subject: str | None = Query(
        None, description="Narrow to one entity URI (all attributes if omitted)"
    ),
    predicate: str | None = Query(
        None, description="Narrow to one attribute predicate URI"
    ),
    since: str | None = Query(
        None,
        description=(
            "ISO-8601 date/dateTime cutoff; returns only changes STRICTLY AFTER it "
            "(e.g. the start of the week for 'what changed this week')"
        ),
    ),
    limit: int = Query(1000, ge=1, le=10000),
):
    """Return dated ``old → new`` value transitions for a KG, oldest → newest.

    Each entry is ``{subject, predicate, old_value, new_value, changed_at}``. The
    history graph is the companion of the KG's data graph; a first insert (no
    prior value) and an unchanged re-write are never recorded, so every row is a
    genuine change.
    """
    graph_uri = kg_graph_uri(tenant.tenant_id, kg_name)
    changes = await fetch_value_history(
        client,
        graph_uri,
        subject=subject,
        predicate=predicate,
        since=since,
        limit=limit,
    )
    return {
        "kg_name": kg_name,
        "count": len(changes),
        "changes": [
            {
                "subject": c.subject,
                "predicate": c.predicate,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "changed_at": c.changed_at,
            }
            for c in changes
        ],
    }
