"""First-class merge / split ops with receipts (ONTA-274).

Identity drift is domain reality (Facebook→Meta; mergers/spinoffs). These ops
make merge/split a DESIGNED, lineage-preserving P6 operation — NOT the banned
post-hoc merge-as-sloppy-ER-bug-fix. They orchestrate the kg_writer primitives
(``rewrite_subject`` / ``insert_facts`` / ``delete_facts`` /
``refresh_after_write``); they construct no raw SPARQL and fork no write path.

Look up those primitives via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.mutations.<name>`` keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from infona_client.graph.kg_writer import GraphDelta, build_graph_delta
from infona_client.graph.ontology_queries import INFONA_ONTO
from infona_client.graph.provenance import (
    build_merge_lineage_triples,
    build_split_triples,
    fetch_merge_lineage,
)
from infona_client.graph.queries import _escape_value
from infona_client.pipeline.mutations_policy import (
    Triple,
    _host,
    _provenance_enabled,
    _scope,
)

logger = structlog.stdlib.get_logger("infona.pipeline.mutations")

SAME_AS = f"{INFONA_ONTO}/sameAs"  # the alias/redirect INSTANCE edge (node-valued →
#                                   on onto/, never attrs/, so NL queries can see it)


@dataclass(frozen=True)
class MergeReceipt:
    """A6 receipt for a first-class :func:`merge_entities` op (sibling of
    :class:`MutationReceipt`, sharing its ``op`` + ``graph_delta`` shape).

    ``graph_delta`` is the deterministic A6 delta of the UNIFIED node's facts,
    carrying ``fan_in = {merged_fact_id: canonical_fact_id}`` — the replay-stable
    record that ``merged`` collapsed onto ``canonical``. ``same_as`` is the
    alias/``sameAs`` lineage edge written so history knows the two URIs were unified
    (and by what evidence, via provenance). ``unified_facts`` is what now resolves
    under the canonical node."""

    op: str  # "merge"
    graph_delta: GraphDelta
    canonical: str
    merged: str
    same_as: Triple
    unified_facts: tuple[Triple, ...] = ()
    merged_fact_count: int = 0


@dataclass(frozen=True)
class SplitReceipt:
    """A6 receipt for a first-class :func:`split_entity` op (sibling of
    :class:`MutationReceipt`).

    ``restored`` is the previously-merged-away URI now re-established as an
    independent node; ``restored_facts`` the facts re-attributed to it;
    ``graph_delta`` the A6 delta of those re-materialized facts; ``removed`` the
    count of canonical triples withdrawn (the merged-exclusive facts + the
    ``sameAs`` edge)."""

    op: str  # "split"
    graph_delta: GraphDelta
    canonical: str
    restored: str
    restored_facts: tuple[Triple, ...] = ()
    removed: int = 0


def _to_canonical_form(triple: Triple, merged: str, canonical: str) -> Triple:
    """A merged node's ORIGINAL triple as it appears on the canonical AFTER the
    re-key: ``merged`` in the subject or object slot becomes ``canonical`` (exactly
    what ``rewrite_subject`` does to both directions)."""
    s, p, o = triple
    return (canonical if s == merged else s, p, canonical if o == merged else o)


async def _fetch_node_triples(neptune, instance_graph: str, uri: str) -> list[Triple]:
    """Snapshot every INSTANCE triple referencing ``uri`` — as SUBJECT and as OBJECT
    — in write-convention terms, so a merge can record what belonged to the node and
    a split can restore it byte-for-byte.

    Reads RAW SPARQL JSON (not ``parse_sparql_results``, which drops datatype) so a
    typed literal round-trips exactly (ONTA-247). Only the instance graph is read, so
    companion (provenance/validity) triples are never captured. Best-effort: a read
    hiccup yields ``[]`` (the caller then has nothing to re-key/restore for that node).
    """
    from infona_client.graph.provenance import _term_from_binding

    esc = _escape_value(uri)
    out: list[Triple] = []
    try:
        raw = await neptune.query(
            f"SELECT ?p ?o WHERE {{ GRAPH <{instance_graph}> {{ {esc} ?p ?o }} }}"
        )
        for row in raw.get("results", {}).get("bindings", []):
            p = _term_from_binding(row.get("p"))
            o = _term_from_binding(row.get("o"))
            if p:
                out.append((uri, p, o))
    except Exception:  # noqa: BLE001 — snapshot read is best-effort
        logger.warning("merge_snapshot_subject_read_failed", uri=uri, exc_info=True)
    try:
        raw = await neptune.query(
            f"SELECT ?s ?p WHERE {{ GRAPH <{instance_graph}> {{ ?s ?p {esc} }} }}"
        )
        for row in raw.get("results", {}).get("bindings", []):
            s = _term_from_binding(row.get("s"))
            p = _term_from_binding(row.get("p"))
            if s and p:
                out.append((s, p, uri))
    except Exception:  # noqa: BLE001 — snapshot read is best-effort
        logger.warning("merge_snapshot_object_read_failed", uri=uri, exc_info=True)
    # Dedup, order-stable, so the snapshot + A6 delta are deterministic.
    return list(dict.fromkeys(out))


async def merge_entities(
    neptune,
    instance_graph: str,
    *,
    a: str,
    b: str,
    type_name: str,
    canonical: Optional[str] = None,
    reason: str = "",
    observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    manifest=None,
) -> MergeReceipt:
    """Unify two entities that new evidence shows are the same real-world thing —
    a first-class, lineage-preserving, EVIDENCE-DRIVEN P6 op (ONTA-274).

    ``a`` and ``b`` are the two entity URIs; ``canonical`` (defaults to ``b``) is the
    survivor and the other is merged away. ``reason`` is the driving evidence — merge
    is NEVER a silent cleanup, so it is threaded into the always-on lineage record.

    Flow (orchestration over the kg_writer primitives — no raw writes):

    1. SNAPSHOT both nodes' facts BEFORE re-keying (:func:`_fetch_node_triples`), so
       the merge is reversible.
    2. RE-POINT via :func:`kg_writer.rewrite_subject` — every triple referencing the
       merged-away URI (as subject AND object) moves onto the canonical in ONE re-key
       event (recorded as a ``rewrite`` provenance event), NOT delete+insert, so no
       fact is lost and lineage stays intact.
    3. WRITE the ``sameAs`` alias/redirect INSTANCE edge (``canonical sameAs merged``,
       on ``onto/`` so NL queries can traverse it) + the ALWAYS-ON reversible lineage
       snapshot (provenance graph), both through the shared :func:`insert_facts`.
    4. ONE :func:`refresh_after_write` with ``rewritten_subjects={merged: canonical}``
       so derived secondary indexes RE-KEY (not accrue ghost rows) — exactly the
       param that exists for this.

    Returns a :class:`MergeReceipt` whose A6 ``graph_delta`` carries
    ``fan_in={merged: canonical}``.
    """
    at = observed_at or datetime.now(timezone.utc)
    if not a or not b:
        raise ValueError("merge_entities requires two entity URIs")
    if canonical is None:
        canonical = b
    if canonical not in (a, b):
        raise ValueError(f"canonical must be one of the two merge operands, got {canonical!r}")
    merged = a if canonical == b else b
    if merged == canonical:
        raise ValueError(f"cannot merge an entity into itself ({merged!r})")

    touched = [type_name] if type_name else []

    # 1. Snapshot both sides BEFORE the re-key (reversibility depends on this).
    merged_facts = await _fetch_node_triples(neptune, instance_graph, merged)
    canonical_facts = await _fetch_node_triples(neptune, instance_graph, canonical)

    # 2. Re-point every triple referencing `merged` onto `canonical` — ONE re-key
    #    event (rewrite_subject), never delete+insert. Lineage-preserving by design.
    await _host().rewrite_subject(
        neptune, instance_graph, merged, canonical,
        touched_types=touched, reason=reason or "merge_entities",
    )

    # 3. The alias/sameAs lineage edge (instance) + the always-on reversible snapshot
    #    (provenance), both via the shared insert primitive.
    same_as: Triple = (canonical, SAME_AS, merged)
    lineage_triples = build_merge_lineage_triples(
        canonical, merged,
        merged_facts=merged_facts, canonical_facts=canonical_facts,
        graph_uri=instance_graph, reason=reason, timestamp=at, touched_types=touched,
    )
    await _host().insert_facts(
        neptune, instance_graph, [same_as], provenance_triples=lineage_triples or None,
    )

    # 4. One post-write refresh, re-keying the merged subject in derived indexes.
    scope = _scope(instance_graph, tenant_id, kg_name)
    if scope is not None:
        await _host().refresh_after_write(
            neptune, tenant_id=scope[0], kg_name=scope[1],
            affected_types=touched, rewritten_subjects={merged: canonical},
        )

    if manifest is not None:
        manifest.record_completed(ref=canonical)

    # 5. A6 receipt — the unified node's facts + fan_in recording merged → canonical.
    unified = list(
        dict.fromkeys(
            [_to_canonical_form(f, merged, canonical) for f in merged_facts + canonical_facts]
            + [same_as]
        )
    )
    graph_delta = build_graph_delta(
        instance_graph, unified, run_id=run_id, fan_in={merged: canonical}
    )
    logger.info(
        "merge_entities",
        canonical=canonical, merged=merged,
        merged_facts=len(merged_facts), unified_facts=len(unified),
    )
    return MergeReceipt(
        op="merge",
        graph_delta=graph_delta,
        canonical=canonical,
        merged=merged,
        same_as=same_as,
        unified_facts=tuple(unified),
        merged_fact_count=len(merged_facts),
    )


async def split_entity(
    neptune,
    instance_graph: str,
    *,
    canonical: str,
    merged: str,
    type_name: str,
    reason: str = "",
    partition: Optional[tuple[list[Triple], list[Triple]]] = None,
    observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    manifest=None,
) -> SplitReceipt:
    """Separate a previously-merged (or over-merged) node back into two distinct
    nodes with their facts correctly re-attributed — the clean reverse of
    :func:`merge_entities` (ONTA-274).

    Mechanism: read back the reversible lineage the merge recorded
    (:func:`fetch_merge_lineage`) to learn which facts belonged to ``merged`` vs
    ``canonical`` — OR accept an explicit ``partition = (merged_facts,
    canonical_facts)`` when the caller knows the split better than the recorded
    lineage (the over-merge case). Then, over the kg_writer primitives:

    1. RESTORE ``merged``'s own facts (idempotent :func:`insert_facts`) — its
       identity + facts return.
    2. WITHDRAW from ``canonical`` the merged-EXCLUSIVE facts (those ``merged`` had
       that ``canonical`` did not) + the ``sameAs`` edge, via :func:`delete_facts`.
       Facts the two genuinely SHARED (e.g. ``rdf:type``) stay on ``canonical``.
    3. ONE :func:`refresh_after_write`.

    The merge lineage snapshot is deliberately LEFT in the provenance graph, so
    history preserves the full merge→split story. A merge followed by a split
    therefore returns to two nodes with their facts correctly re-attributed.
    """
    at = observed_at or datetime.now(timezone.utc)
    if not canonical or not merged:
        raise ValueError("split_entity requires the canonical and merged URIs")
    touched = [type_name] if type_name else []

    # 1. Determine each side's facts — explicit partition wins, else recorded lineage.
    if partition is not None:
        merged_facts, canonical_facts = list(partition[0]), list(partition[1])
    else:
        lineage = await fetch_merge_lineage(neptune, instance_graph, canonical, merged)
        merged_facts, canonical_facts = lineage.merged_facts, lineage.canonical_facts
    if not merged_facts:
        raise ValueError(
            f"no merge lineage found to split {merged!r} out of {canonical!r}; "
            "pass an explicit partition=(merged_facts, canonical_facts)"
        )

    # 2. Restore the merged node's own facts (idempotent insert re-establishes it).
    restore = list(dict.fromkeys(merged_facts))
    await _host().insert_facts(neptune, instance_graph, restore)

    # 3. Withdraw from the canonical the merged-EXCLUSIVE facts + the sameAs edge.
    #    A fact the two genuinely shared (its canonical form is in canonical_facts)
    #    is KEPT on the canonical — only what was uniquely merged's is removed.
    canon_set = set(canonical_facts)
    to_remove: list[Triple] = [
        cf for cf in (_to_canonical_form(f, merged, canonical) for f in restore)
        if cf not in canon_set
    ]
    to_remove.append((canonical, SAME_AS, merged))
    to_remove = list(dict.fromkeys(to_remove))
    removed = await _host().delete_facts(
        neptune, instance_graph, triples=to_remove,
        touched_types=touched, reason=reason or "split_entity",
    )

    # 4. Governance split event (gated like the other events; merge lineage kept).
    if _provenance_enabled():
        split_prov = build_split_triples(
            canonical, merged, graph_uri=instance_graph,
            reason=reason, timestamp=at, touched_types=touched,
        )
        if split_prov:
            await _host().insert_facts(neptune, instance_graph, [], provenance_triples=split_prov)

    # 5. One post-write refresh for the touched type.
    scope = _scope(instance_graph, tenant_id, kg_name)
    if scope is not None:
        await _host().refresh_after_write(
            neptune, tenant_id=scope[0], kg_name=scope[1], affected_types=touched,
        )

    if manifest is not None:
        manifest.record_completed(ref=merged)

    graph_delta = build_graph_delta(instance_graph, restore, run_id=run_id)
    logger.info(
        "split_entity",
        canonical=canonical, restored=merged,
        restored_facts=len(restore), removed=removed,
    )
    return SplitReceipt(
        op="split",
        graph_delta=graph_delta,
        canonical=canonical,
        restored=merged,
        restored_facts=tuple(restore),
        removed=removed,
    )
