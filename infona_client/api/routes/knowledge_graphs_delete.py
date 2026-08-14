"""Delete a knowledge graph and all its derived state."""

from __future__ import annotations

from typing import Any

from fastapi import Depends

from infona_client.api.deps import get_neptune_client, get_schedule_store
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.queries import kg_graph_uri, kg_meta_uri, tenant_graph_uri


async def delete_kg(
    kg_name: str,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
    schedule_store=Depends(get_schedule_store),
):
    """Delete a knowledge graph and all its data.

    Store-specific purge (registry + DETACH on Neo4j; DROP GRAPH + metadata on
    SPARQL) runs first. Every derived-state eviction then runs for BOTH backends
    (ONTA-532): the Neo4j branch used to early-return after registry / DETACH /
    durable-stats and skip semantic clear, spatiotemporal clear, example bank,
    NL cache, kg_status, explore stats cache, and reconcile schedule — leaving
    stale answers and schedules for a recreated same-name KG.
    """
    from infona_client.graph.kg_registry import delete_registered_kg, neo4j_kg_registry_active

    base = tenant_graph_uri(tenant.tenant_id)
    neo4j = neo4j_kg_registry_active()

    if neo4j:
        # Registry row first; instance entity purge is best-effort via Cypher.
        await delete_registered_kg(tenant.tenant_id, kg_name)
        try:
            from infona_client.graph.store import get_graph_store

            store = get_graph_store()
            run = getattr(store, "_run", None)
            if callable(run):
                await run(
                    "MATCH (n {tenant_id: $tenant_id, kg: $kg}) DETACH DELETE n",
                    {"tenant_id": tenant.tenant_id, "kg": kg_name},
                    writing=True,
                    database=None,
                )
        except Exception:  # noqa: BLE001
            import structlog
            structlog.get_logger("infona.kg").warning(
                "neo4j_kg_instance_delete_failed", kg_name=kg_name, exc_info=True
            )
    else:
        graph = kg_graph_uri(tenant.tenant_id, kg_name)
        # The shared builder, not a fourth hand-rolled copy of this URI (ONTA-422):
        # `kg_meta_uri` is the canonical one and now validates the tenant half.
        kg_uri = kg_meta_uri(tenant.tenant_id, kg_name)

        # Drop all triples in the KG graph
        await client.update(f"DROP SILENT GRAPH <{graph}>")

        # Remove KG metadata
        await client.update(
            f"DELETE WHERE {{\n"
            f"  GRAPH <{base}> {{\n"
            f"    <{kg_uri}> ?p ?o .\n"
            f"  }}\n"
            f"}}"
        )

    # ------------------------------------------------------------------
    # Shared derived-state cleanup (ONTA-532). Runs for Neo4j AND SPARQL.
    # Do not early-return above this block.
    # ------------------------------------------------------------------

    # Drop precomputed type-stats + in-memory summary cache. The stats key is
    # derived from the KG name, so a KG recreated under the same name would
    # otherwise serve this deleted graph's stale counts. Backend-aware: on
    # Neo4j this skips SPARQL named-graph DROP and only clears cache + durable
    # row (see explore.drop_kg_stats).
    from infona_client.api.routes.explore import drop_kg_stats
    await drop_kg_stats(client, tenant.tenant_id, kg_name)

    # Purge stale examples from the example bank for this KG
    try:
        from infona_client.nlp.example_bank import get_example_bank
        bank = get_example_bank()
        if bank and bank._examples:
            before = len(bank._examples)
            bank._examples = [e for e in bank._examples if e.kg_name != kg_name]
            removed = before - len(bank._examples)
            if removed > 0:
                bank.save()
                import structlog
                structlog.get_logger("infona.kg").info(
                    "example_bank_purged", kg=kg_name, removed=removed,
                    remaining=len(bank._examples),
                )
    except Exception:
        pass  # Bank purge is best-effort, don't fail the delete

    # Clear this KG's rows from the spatio-temporal secondary index. Scoped to
    # (tenant_id, kg_name) so a sibling KG's geometry facts are untouched — the
    # whole reason the index carries a kg_name dimension. Best-effort: the
    # eventually-consistent derived index must never block the KG delete.
    try:
        from infona_client.spatiotemporal.registry import get_spatiotemporal_index
        await get_spatiotemporal_index().clear(tenant.tenant_id, kg_name=kg_name)
    except Exception:
        pass  # Derived-index cleanup is best-effort, don't fail the delete

    # Clear this KG's chunks from the SEMANTIC instance index (ONTA-181) — same
    # (tenant_id, kg_name) scoping and same best-effort contract as the
    # spatio-temporal clear above: only THIS KG's rows, never a sibling's.
    try:
        from infona_client.semantic.registry import get_semantic_index
        await get_semantic_index().clear(tenant.tenant_id, kg_name=kg_name)
    except Exception:
        pass  # Derived-index cleanup is best-effort, don't fail the delete

    # Evict the NL-planning caches for this tenant (ONTA-417). They are keyed by
    # the TENANT ontology graph, not by KG, so nothing above touches them:
    # without this, the deleted KG's cached ontology summary and its cached
    # active-type set survive the delete, and a KG recreated under the same name
    # inherits the dead one's cached scope for the rest of the TTL. Same
    # best-effort contract as the derived-index clears above.
    #
    # SCOPE, precisely. The two cache sweeps here are complete. The embedding
    # eviction is NOT: invalidate_cache pops only the IN-MEMORY embedding store,
    # and the next retrieve() reloads identical chunks from S3, which is never
    # cleared. So this does not stop a deleted KG's types from being retrieved.
    # What keeps them from displacing this graph's schema is ONTA-411, which
    # demotes them and marks them "[no instances]" because they carry no
    # instances in the graph being queried.
    #
    # TODO(ONTA-417): evicting the DECLARATIONS (and with them the S3-persisted
    # chunks) is the real fix and is deliberately not attempted here. The deleted
    # KG's types remain declared in the tenant ontology graph, so the next
    # embedding rebuild re-embeds them. Pruning them needs a real "is this type
    # still used by another KG, or was it authored by hand?" guard. Types are
    # shared tenant-wide BY DESIGN, and ONTA-258 deliberately keeps
    # declared-but-unpopulated types visible, so an unguarded prune would delete
    # user-authored schema.
    try:
        from infona_client.nlp.pipeline import NLQueryPipeline
        NLQueryPipeline.invalidate_cache(base)
    except Exception:
        pass  # Cache eviction is best-effort, don't fail the delete

    # Drop the KG's cached "this graph holds data" verdict (ONTA-453). The probe
    # caches POSITIVE verdicts for KG_STATUS_CACHE_TTL and never re-checks inside
    # it, so without this eviction a question asked in the minute after a delete
    # sails past the missing-KG guard and gets answered out of the tenant base
    # graph plus the global layers, which is exactly the confidently-wrong answer
    # that guard exists to stop. Every other derived index is evicted above; this
    # is the one that was missed.
    try:
        from infona_client.graph.kg_status import invalidate_kg_status
        invalidate_kg_status(tenant.tenant_id, kg_name)
    except Exception:
        pass  # Cache eviction is best-effort, don't fail the delete

    # Drop the KG's recurring semantic-reconcile schedule row (ONTA-181) so the
    # runner doesn't keep scanning a graph that no longer exists. Best-effort.
    try:
        from infona_client.semantic.reconciler import remove_reconcile_schedule
        await remove_reconcile_schedule(schedule_store, tenant.tenant_id, kg_name)
    except Exception:
        pass  # Schedule cleanup is best-effort, don't fail the delete

    return {"deleted": kg_name}
