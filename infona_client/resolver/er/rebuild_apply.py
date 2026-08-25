"""Apply resolved ``er rebuild`` field conflicts onto the canonical URI (E7).

``explain_field_conflicts`` already decides HQ winner vs unresolved
``credit_rating``. This module makes that report real: after URI merge, each
**resolved** conflict (not ``REASON_VALUE`` / leftover) is written through
``write_with_conflict_resolution`` so the loser is CLOSED, not merely narrated.

Failures must not fail the merge (log, like explain). Unresolved / equal-trust
guesses are not applied. Only rows that already carry a winner+loser claim
are sent — no hardcoded values, no guess on generic KGs missing authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

import structlog

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.graph.ontology_queries import attr_uri
from infona_client.pipeline.conflict import REASON_VALUE, FactClaim
from infona_client.pipeline.mutations import write_with_conflict_resolution

logger = structlog.stdlib.get_logger("infona.resolver.er.rebuild")


def _parse_authority(raw: Any) -> Optional[AuthorityLevel]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return AuthorityLevel(text)
    except ValueError:
        return None


def _parse_ts(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _claim_from_row(row: Mapping[str, Any] | None) -> Optional[FactClaim]:
    if not row:
        return None
    value = str(row.get("value") or "").strip()
    if not value:
        return None
    return FactClaim(
        value=value,
        authority=_parse_authority(row.get("authority")),
        observed_at=_parse_ts(row.get("observed_at")),
        source=str(row.get("source") or ""),
    )


async def apply_resolved_conflicts(
    client,
    instance_graph: str,
    type_name: str,
    extras: Mapping[str, Any],
) -> None:
    """Close the loser of each resolved explain-conflict. Never raises."""
    conflicts = extras.get("conflicts") or []
    if not conflicts:
        return
    for conflict in conflicts:
        try:
            await _apply_one(client, instance_graph, type_name, conflict)
        except Exception:  # noqa: BLE001 — apply must not fail the merge
            logger.warning(
                "er_rebuild_conflict_apply_failed",
                field=conflict.get("field"),
                entity=conflict.get("entity"),
                exc_info=True,
            )


async def _apply_one(
    client,
    instance_graph: str,
    type_name: str,
    conflict: Mapping[str, Any],
) -> None:
    if conflict.get("reason") == REASON_VALUE:
        return
    winner = _claim_from_row(conflict.get("winner"))
    loser = _claim_from_row(conflict.get("loser"))
    entity = str(conflict.get("entity") or "")
    field = str(conflict.get("field") or "")
    if winner is None or loser is None or not entity or not field:
        return
    existing: list[FactClaim] = []
    for row in conflict.get("values") or ():
        claim = _claim_from_row(row)
        if claim is None or claim.value == winner.value:
            continue
        existing.append(claim)
    if not existing:
        existing = [loser]
    await write_with_conflict_resolution(
        client,
        instance_graph,
        subject=entity,
        predicate=attr_uri(type_name, field),
        type_name=type_name,
        value=winner.value,
        authority=winner.authority,
        source=winner.source,
        observed_at=winner.observed_at,
        existing_claims=existing,
        reason=str(conflict.get("reason") or "er-rebuild"),
        refresh=False,
    )
