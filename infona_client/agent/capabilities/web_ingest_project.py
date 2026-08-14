"""A1 project: chunk, screen, suppress, structural gates.

Pre-write only — never inserts. Instance mint for the suppression check
uses ``entity_uri`` (do not hand-build entity IRIs).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from infona_client.graph.ontology_queries import entity_uri
from infona_client.pipeline.a1_validators import screen_row
from infona_client.pipeline.discovery_quality import (
    alnum_identity,
    apply_discovery_quality_gate,
    catalog_identity_key,
    catalog_path_segments,
    catalog_surface_keys,
)
from infona_client.pipeline.role_membership_gate import screen_role_membership
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_fetch import SOURCE_URL_ATTR

def _chunk_rows(rows: list, size: int) -> list[list]:
    """Split ``rows`` into consecutive sub-batches of at most ``size`` (order
    preserved). ``size <= 0`` degrades to one whole chunk — never an empty split."""
    if size <= 0 or len(rows) <= size:
        return [rows] if rows else []
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _group_rows_by_source_url(rows: list) -> list[list]:
    """Partition a batch into consecutive groups that are HOMOGENEOUS in their
    ``source_url`` (order preserved), so every row in a group cites the same page.

    This is the deterministic half of the citation-binding fix. The ``source_url``
    is stamped on each row BEFORE extraction (keyed by the provider's per-record
    provenance), but the multi-type LLM extractor then re-decides which minted
    entity each field lands on — so a batch mixing rows from page A and page B can
    have A's URL copied onto an entity drawn from B (the observed mis-binding: one
    page-level URL broadcast across every model on the page). By committing one
    ``resolver.ingest`` call PER distinct source URL, an extraction can only ever
    see rows that share ONE page, so the only URL available to stamp on any entity
    it mints is that page's URL — the cross-record placement decision is taken away
    from the LLM. Rows with no ``source_url`` (free/stub providers) form their own
    group and are unaffected.

    Groups are consecutive runs, not a global regroup, so row order within a batch
    is preserved and a provider that already returns rows page-by-page pays no
    reshuffle. Returns ``[]`` for an empty batch, ``[rows]`` when every row shares
    one URL (or none carry one) — the previous single-partition behavior.
    """
    if not rows:
        return []
    groups: list[list] = []
    current: list = []
    current_key: object = object()  # sentinel: no group started yet
    for row in rows:
        # Accept either a raw dict row OR an A1 ``SourceRow`` (ONTA-371: the
        # extract loop now drives from bundle rows). A ``SourceRow`` groups by the
        # SAME per-record source_url its snapshot ``data`` carries, so grouping is
        # byte-identical whether driven from the raw batch or the bundle.
        if isinstance(row, dict):
            key = row.get(SOURCE_URL_ATTR)
        else:
            data = getattr(row, "data", None)
            key = data.get(SOURCE_URL_ATTR) if isinstance(data, dict) else None
        if not current or key == current_key:
            current.append(row)
            current_key = key
        else:
            groups.append(current)
            current = [row]
            current_key = key
    if current:
        groups.append(current)
    return groups


def _drop_suppressed_rows(
    rows: list,
    proposed_type: str,
    key_attr: str,
    suppressed_entities: set[str],
    *,
    provider: str = "",
    job_id: Optional[str] = None,
) -> list:
    """Drop discovered rows whose would-be canonical subject is on the ENTITY-level
    suppression list (ONTA-345) — the FIND-path re-acquisition guard.

    For each surviving (post-dedupe) row this computes the SAME canonical instance
    IRI the resolver would mint for it — ``entity_uri(proposed_type,
    row[key_attr])`` — and DROPS the row when that subject is entity-suppressed
    (erased / tombstoned). So an ERASED entity is never re-minted by discovery or a
    refresh (the P1 'never re-acquire erased data' rule; GDPR erasure blast
    radius). Membership is a set check against ``suppressed_entities`` — fetched
    ONCE per run (:func:`fetch_suppressed_entities`) — so this is O(1) per row, no
    per-row query. Each drop is logged (structured). A no-op returning ``rows``
    unchanged when the suppression set is empty (the common case), so the happy
    path pays only one set-emptiness check.
    """
    if not suppressed_entities or not rows:
        return rows
    kept: list = []
    for row in rows:
        raw_id = row.get(key_attr) if isinstance(row, dict) else None
        subject = entity_uri(proposed_type, str(raw_id)) if raw_id else None
        if subject is not None and subject in suppressed_entities:
            _wic.logger.info(
                "web_ingest_suppressed_entity_dropped",
                subject=subject,
                type=proposed_type,
                key=str(raw_id),
                provider=provider,
                job_id=job_id,
            )
            continue
        kept.append(row)
    return kept


def _screen_a1_rows(
    rows: list,
    key_attr: str,
    attributes: list[str],
    *,
    provider: str = "",
    job_id: Optional[str] = None,
) -> tuple[list, int, int, list[str]]:
    """A1 validators (ONTA-393): reject nav-chrome NAMES and type-invalid CELLS from
    a post-dedupe batch BEFORE the SourceBundle is built and BEFORE resolver.ingest*,
    so garbage never becomes a graph entity.

    A row whose key/name cell is chrome ("About", "Skip to content", …) is DROPPED
    whole; a real row carrying a type-invalid cell (city that is a year, website with
    no host, address that is an enrolment phrase) keeps the row but SCRUBS that cell.
    Per-row decisions come from the pure :func:`screen_row`; here we only log each
    drop and never mutate the provider's row dict (scrubbing copies).

    Returns ``(kept_rows, rows_dropped, cells_scrubbed, drop_reasons)``. A no-op
    returning ``rows`` unchanged when nothing is invalid — the happy path pays one
    screen pass, mirroring :func:`_drop_suppressed_rows`."""
    if not rows:
        return rows, 0, 0, []
    kept: list = []
    rows_dropped = 0
    cells_scrubbed = 0
    reasons: list[str] = []
    for row in rows:
        verdict = screen_row(row, key_attr, list(attributes))
        if verdict.drop_row:
            rows_dropped += 1
            reasons.append(verdict.row_reason)
            _wic.logger.info(
                "web_ingest_a1_row_dropped",
                reason=verdict.row_reason,
                key=str(row.get(key_attr)) if isinstance(row, dict) else "",
                provider=provider,
                job_id=job_id,
            )
            continue
        if verdict.scrubbed:
            # Copy before scrubbing — the provider's row (and its provenance) is
            # shared; we remove only the offending cells from OUR view of it.
            row = dict(row)
            for attr, reason in verdict.scrubbed.items():
                row.pop(attr, None)
                cells_scrubbed += 1
                reasons.append(reason)
                _wic.logger.info(
                    "web_ingest_a1_cell_scrubbed",
                    attribute=attr,
                    reason=reason,
                    provider=provider,
                    job_id=job_id,
                )
        kept.append(row)
    return kept, rows_dropped, cells_scrubbed, reasons


@dataclass
class StructuralGateResult:
    """Outcome of post-A1 structural quality gates (role membership + identity).

    Order is fixed (ONTA-465 / WS6): role-membership first, then discovery
    quality (website policy + structural near-dup / catalog-path identity).
    Pure orchestration over pure modules — no I/O, never raises.
    """

    rows: list = field(default_factory=list)
    role_drops: int = 0
    identity_merges: int = 0
    websites_scrubbed: int = 0
    reasons: list[str] = field(default_factory=list)


def _structural_identity_keys(raw_key: object) -> set[str]:
    """Catalog-path + surface identity keys for cross-batch dedupe."""
    keys: set[str] = set()
    segs = catalog_path_segments(raw_key)
    if segs:
        keys.add("cat:" + catalog_identity_key(segs))
        for sk in catalog_surface_keys(segs):
            if sk:
                keys.add("surf:" + sk)
    else:
        a = alnum_identity(raw_key)
        if a:
            keys.add("surf:" + a)
    return keys


def apply_post_a1_structural_gates(
    rows: list,
    key_attr: str,
    attributes: list[str],
    *,
    focus_type: Optional[str] = None,
    catalog_inventory: bool = False,
) -> StructuralGateResult:
    """Run role-membership then discovery-quality on a post-A1 batch.

    Call order (hard rule, plan v2):

    1. :func:`screen_role_membership` — drop role entities mistaken for instances
    2. :func:`apply_discovery_quality_gate` — website scrub + structural identity
       merge (catalog-path ↔ surface form, near-dups)

    ``catalog_inventory`` is True when this job has already seen catalog-path
    keys in an earlier batch (cross-batch brand drops for Vapi-only scrapes).

    Never raises; on internal failure returns the input rows with zero counters
    so the write path cannot sink on observability. Input rows are not mutated
    in place (both pure modules copy).
    """
    if not rows:
        return StructuralGateResult()
    reasons: list[str] = []
    role_drops = 0
    working = list(rows)
    try:
        # Only dict rows participate; non-dicts pass through (defensive).
        dict_rows = [r for r in working if isinstance(r, dict)]
        passthrough = [r for r in working if not isinstance(r, dict)]
        rv = screen_role_membership(
            dict_rows,
            key_attr=key_attr,
            focus_type=focus_type,
            catalog_inventory=catalog_inventory,
        )
        role_drops = len(rv.dropped)
        for r in rv.reasons:
            if r not in reasons and len(reasons) < 40:
                reasons.append(r)
        working = list(rv.kept) + passthrough
    except Exception:  # noqa: BLE001 — gate never sinks write
        _wic.logger.warning("web_ingest_role_membership_failed", exc_info=True)

    identity_merges = 0
    websites_scrubbed = 0
    if not working:
        return StructuralGateResult(
            rows=[],
            role_drops=role_drops,
            reasons=reasons,
        )
    try:
        qv = apply_discovery_quality_gate(
            working,
            key_attr,
            list(attributes),
        )
        working = list(qv.rows)
        identity_merges = int(qv.near_dups_merged or 0)
        websites_scrubbed = int(qv.websites_scrubbed or 0)
        for r in qv.reasons:
            if r not in reasons and len(reasons) < 40:
                reasons.append(r)
    except Exception:  # noqa: BLE001 — gate never sinks write
        _wic.logger.warning("web_ingest_quality_gate_failed", exc_info=True)

    return StructuralGateResult(
        rows=working,
        role_drops=role_drops,
        identity_merges=identity_merges,
        websites_scrubbed=websites_scrubbed,
        reasons=reasons,
    )
