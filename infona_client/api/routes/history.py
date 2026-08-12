"""Value-history read route (ONTA-236).

Exposes the companion value-history graph that ``graph/kg_writer.delete_facts``
populates on every genuine attribute-value change (gated by
``INFONA_VALUE_HISTORY_ENABLED``). Answers the persona question "which values
changed since <date>, old → new, with a change date" — e.g. Speko's
``sp-price-changes``: "every model whose price changed this week, old → new".

Read-only and GENERAL: it queries the same store for any attribute of any type,
with optional ``subject`` / ``predicate`` / ``since`` narrowing, so a "changed
since <cutoff>" question returns only transitions after the cutoff, each dated.
The WRITE side stays entirely on the shared write path — this route never writes.

**Backing store (ADR 0013 / ONTA-527):** Assertion provenance via
:func:`fetch_store_assertion_history` / ``rdfs_helpers.session_assertion_history``
— current facts plus ``verified_at`` as ``changed_at``. The SPARQL companion
``…/history`` graph, which carried full temporal ``old → new`` transitions, went
out with the Neptune backend; the property-graph ValueHistory port is deferred,
so ``old_value`` is empty on every row today. Prefer ``subject=`` for scoped
reads.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Query

from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.history import fetch_store_assertion_history
from infona_client.graph.store import GraphConfigError, get_optional_graph_store

router = APIRouter()

# A well-formed absolute IRI for the subject/predicate narrowing filters: an
# ``http(s)://`` scheme with NO IRIREF-forbidden character. This is the route-
# boundary belt to _escape_value's suspenders (defense in depth): a crafted
# ``subject``/``predicate`` carrying a ``>`` (which would break out of the ``<…>``
# wrapper and inject a ``GRAPH <other-tenant>`` block — a cross-tenant read) is
# rejected here with a 422 before it ever reaches the query builder.
_ABS_IRI_RE = re.compile(r'^https?://[^\s<>"{}|\^`\\\x00-\x20]+$')


def _require_abs_iri(name: str, value: str | None) -> None:
    if value is not None and not _ABS_IRI_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"{name} must be a well-formed absolute http(s) IRI",
        )


@router.get("/graphs/{tenant}/history")
async def get_value_history(
    tenant: TenantContext = Depends(get_tenant),
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
    """Return dated value entries for a KG, oldest → newest.

    Each entry is ``{subject, predicate, old_value, new_value, changed_at}``,
    sourced from Assertion provenance in the property-graph store.
    """
    # Tenant isolation: reject a malformed subject/predicate at the boundary so an
    # injection payload can never reach the query builder (defense in depth with
    # _escape_value, which also rejects). The graph is scoped to the authenticated
    # tenant via kg_graph_uri, so a valid narrow can only ever read this tenant.
    _require_abs_iri("subject", subject)
    _require_abs_iri("predicate", predicate)

    try:
        store = get_optional_graph_store()
    except GraphConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    changes = await fetch_store_assertion_history(
        store,
        tenant_id=tenant.tenant_id,
        kg_name=kg_name,
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
