"""One-shot migration: legacy provenance companions → the attr_meta namespace.

Graphs written before ONTA-262 carry per-attribute provenance companions as
ATTRIBUTE-namespace instance triples (``types/<T>/attrs/<attr>_<suffix>``) and —
where enrichment wrote them — matching ontology declarations, which is exactly
what rendered ``<attr>_provenance`` / ``<attr>_verified_at`` as sibling columns
on every schema surface. This migration moves a KG to the current convention:

1. **Re-key the instance triples** onto ``attr_meta/<T>/<attr>/<suffix>`` via
   ``kg_writer.rewrite_predicates`` (server-side ``DELETE/INSERT/WHERE``, so a
   typed ``xsd:dateTime`` stamp keeps its datatype — the ONTA-247 lesson).
2. **Purge the stale ontology declarations** of the companion pseudo-attributes
   (``ontology_queries.delete_attribute_declaration``; schema graph only).
3. **One housekeeping pass** (``refresh_after_write``) so type-stats recompute
   and the NL-planning ontology cache invalidates.

Classification is the SAME deterministic rule the read-side uses
(``predicates.companion_leaves``): a predicate ``…/attrs/<leaf>`` is a legacy
companion iff ``<leaf>`` is ``<base>_<suffix>`` (suffix ∈ source_url /
provenance / verified_at) AND the base attribute predicate ``…/attrs/<base>``
exists in the same KG — so a real attribute that merely ends in ``_provenance``
is never migrated. Idempotent: re-running on a migrated KG finds nothing.

Until a KG is migrated it still renders correctly — the read surfaces classify
legacy companions set-wise — so running this is cleanup, not a prerequisite.
Migrating retires the classifier's work for that KG and makes the instance data
match the convention (NL freshness queries then hit the attr_meta shape without
the legacy declared-attribute fallback).

CLI (mirrors ``python -m cograph_client.qc``)::

    python -m cograph_client.graph.attr_meta_migration \
        --endpoint http://127.0.0.1:3030 --tenant demo-tenant --kg physicians
    # every KG in the tenant:
    python -m cograph_client.graph.attr_meta_migration --endpoint <url> --tenant demo-tenant
    # preview without writing:
    ... --dry-run
"""

from __future__ import annotations

import structlog

from cograph_client.graph.kg_writer import refresh_after_write, rewrite_predicates
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.predicates import ATTR_META_SUFFIXES
from cograph_client.graph.provenance import attr_provenance_companion_uri
from cograph_client.graph.queries import (
    kg_graph_uri,
    parse_kg_graph_uri,
    tenant_graph_uri,
)
from cograph_client.graph.ontology_queries import delete_attribute_declaration

logger = structlog.get_logger(__name__)

_TYPES_PREFIX = "https://cograph.tech/types/"


def _split_legacy_attr_pred(p_uri: str) -> tuple[str, str] | None:
    """``types/<T>/attrs/<leaf>`` → ``(T, leaf)``; None for any other shape."""
    if not p_uri.startswith(_TYPES_PREFIX):
        return None
    rest = p_uri[len(_TYPES_PREFIX):]
    parts = rest.split("/")
    if len(parts) != 3 or parts[1] != "attrs" or not parts[0] or not parts[2]:
        return None
    return parts[0], parts[2]


def plan_migration(attr_predicates: list[str]) -> dict[str, tuple[str, str, str]]:
    """Pure classification: which of a KG's attrs/-namespace predicates are
    legacy companions, and where each one moves.

    Returns ``{old_pred: (new_pred, type_name, companion_leaf)}``. Same rule as
    ``predicates.companion_leaves``, applied per (type, leaf): ``<base>_<suffix>``
    migrates only when ``…/attrs/<base>`` is present for the SAME type.
    """
    leaves_by_type: dict[str, set[str]] = {}
    for p in attr_predicates:
        split = _split_legacy_attr_pred(p)
        if split:
            leaves_by_type.setdefault(split[0], set()).add(split[1])
    mapping: dict[str, tuple[str, str, str]] = {}
    for p in attr_predicates:
        split = _split_legacy_attr_pred(p)
        if not split:
            continue
        type_name, leaf = split
        for suffix in ATTR_META_SUFFIXES:
            tail = f"_{suffix}"
            if leaf.endswith(tail):
                base = leaf[: -len(tail)]
                if base and base in leaves_by_type.get(type_name, ()):
                    mapping[p] = (
                        attr_provenance_companion_uri(type_name, base, suffix),
                        type_name,
                        leaf,
                    )
                break
    return mapping


async def migrate_kg(
    neptune, tenant_id: str, kg_name: str, *, dry_run: bool = False
) -> dict:
    """Migrate one KG's legacy companions to attr_meta. Returns a summary dict."""
    kg_graph = kg_graph_uri(tenant_id, kg_name)
    onto_graph = tenant_graph_uri(tenant_id)

    q = (
        f"SELECT DISTINCT ?p FROM <{kg_graph}> WHERE {{\n"
        f"  ?s ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?p), "{_TYPES_PREFIX}"))\n'
        f"}}"
    )
    _, rows = parse_sparql_results(await neptune.query(q))
    attr_preds = [r.get("p", "") for r in rows if r.get("p")]
    mapping = plan_migration(attr_preds)

    summary = {
        "kg_name": kg_name,
        "companions_found": len(mapping),
        "migrated": 0,
        "declarations_purged": 0,
        "dry_run": dry_run,
        "predicates": sorted(old for old in mapping),
    }
    if not mapping or dry_run:
        return summary

    summary["migrated"] = await rewrite_predicates(
        neptune,
        kg_graph,
        {old: new for old, (new, _t, _leaf) in mapping.items()},
        reason=f"attr_meta companion migration (ONTA-262), kg={kg_name}",
    )

    # Purge the stale ontology declarations (one idempotent delete per distinct
    # companion pseudo-attribute; discovery-only KGs simply have none to purge).
    purged: set[tuple[str, str]] = set()
    for _old, (_new, type_name, leaf) in mapping.items():
        if (type_name, leaf) in purged:
            continue
        await neptune.update(delete_attribute_declaration(onto_graph, type_name, leaf))
        purged.add((type_name, leaf))
    summary["declarations_purged"] = len(purged)

    affected_types = {t for (_new, t, _leaf) in mapping.values()}
    await refresh_after_write(
        neptune,
        tenant_id=tenant_id,
        kg_name=kg_name,
        affected_types=affected_types,
    )
    logger.info("attr_meta_migration_done", **summary)
    return summary


async def migrate_tenant(
    neptune, tenant_id: str, *, dry_run: bool = False
) -> list[dict]:
    """Migrate every KG that actually has data for ``tenant_id``.

    KGs are discovered from the store (same approach as the QC audit): enumerate
    named graphs and keep those whose URI parses to this tenant via the canonical
    ``parse_kg_graph_uri`` — companion graphs (provenance, history, stats) and the
    base graph don't parse and are correctly skipped."""
    result = await neptune.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    names: list[str] = []
    for binding in result.get("results", {}).get("bindings", []):
        cell = binding.get("g")
        uri = cell.get("value", "") if isinstance(cell, dict) else ""
        parsed = parse_kg_graph_uri(uri)
        if parsed and parsed[0] == tenant_id:
            names.append(parsed[1])
    return [
        await migrate_kg(neptune, tenant_id, name, dry_run=dry_run)
        for name in sorted(names)
    ]


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio
    import json

    ap = argparse.ArgumentParser(
        prog="python -m cograph_client.graph.attr_meta_migration",
        description="Move legacy attrs/-namespace provenance companions to attr_meta.",
    )
    ap.add_argument("--endpoint", default="http://127.0.0.1:3030",
                    help="SPARQL 1.1 endpoint (local store or reachable Neptune).")
    ap.add_argument("--backend", default="neptune", choices=["neptune", "fuseki"],
                    help="Endpoint dialect. Default: neptune.")
    ap.add_argument("--tenant", required=True, help="Tenant id to migrate.")
    ap.add_argument("--kg", help="One KG name; omit to migrate every KG in the tenant.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify and report; write nothing.")
    args = ap.parse_args(argv)

    from cograph_client.graph.client import NeptuneClient

    async def _run() -> list[dict]:
        client = NeptuneClient(endpoint=args.endpoint, backend=args.backend)
        try:
            if args.kg:
                return [await migrate_kg(client, args.tenant, args.kg, dry_run=args.dry_run)]
            return await migrate_tenant(client, args.tenant, dry_run=args.dry_run)
        finally:
            await client.close()

    results = asyncio.run(_run())
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    raise SystemExit(_main())
