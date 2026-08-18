"""Explain `er rebuild` merges + field conflicts (ONTA-543).

Pure helpers: given clusters + per-URI signals/props, produce the report
fields a stranger should see — winner, reason, provenance, and any
leftover unresolved conflict. Rebuild calls these; it does not invent a
second merge criterion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.pipeline.conflict import (
    DEFAULT_CONFLICT_POLICY,
    REASON_VALUE,
    FactClaim,
)
from infona_client.resolver.er.scoring import DefaultScorer
from infona_client.resolver.er.types import (
    ERConfig,
    NormalizedSignals,
    Scorer,
)


# Product fields this fixture (and any similar master-data CSV) disagrees on.
CONFLICT_FIELDS: tuple[str, ...] = ("headquarters", "credit_rating")

# Housekeeping / identity columns — never treated as field conflicts.
_SKIP_PROPS: frozenset[str] = frozenset(
    {
        "record_id",
        "record_source",
        "authority",
        "observed_at",
        "tax_id",
        "email",
        "phone",
        "name",
        "source",
        "primary_type",
        "labels",
    }
)


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    return str(value)


def _parse_authority(raw: Any) -> Optional[AuthorityLevel]:
    text = _as_str(raw).strip()
    if not text:
        return None
    try:
        return AuthorityLevel(text)
    except ValueError:
        return None


def _parse_ts(raw: Any) -> Optional[datetime]:
    text = _as_str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _props(props_by_uri: Mapping[str, Mapping[str, Any]], uri: str) -> Mapping[str, Any]:
    return props_by_uri.get(uri) or {}


def explain_merge(
    cluster: list[str],
    entities: Mapping[str, NormalizedSignals],
    config: ERConfig,
    *,
    props_by_uri: Optional[Mapping[str, Mapping[str, Any]]] = None,
    scorer: Scorer | None = None,
    triggered_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Winner / why / provenance for one auto-merge cluster."""
    from infona_client.resolver.er.rebuild import choose_canonical

    scorer = scorer or DefaultScorer()
    winner = choose_canonical(cluster, dict(entities))
    losers = [u for u in cluster if u != winner]
    at = triggered_at or datetime.now(timezone.utc)

    best_score = 0.0
    contributions: list[dict[str, Any]] = []
    win_signals = entities.get(winner, NormalizedSignals())
    for loser in losers:
        result = scorer.score(win_signals, entities.get(loser, NormalizedSignals()), config)
        if result.score >= best_score:
            best_score = result.score
            contributions = [
                {
                    "signal": c.signal,
                    "weight": c.weight,
                    "similarity": c.similarity,
                    "contribution": c.contribution,
                }
                for c in result.contributions
            ]

    props = _props(props_by_uri or {}, winner)
    provenance = {
        "source": _as_str(
            props.get("record_source")
            or props.get("source_label")
            or props.get("source")
        ),
        "observed_at": _as_str(props.get("observed_at")),
        "authority": _as_str(props.get("authority")),
    }
    return {
        "winner": winner,
        "losers": losers,
        "reason": "signal-richest",
        "score": round(best_score, 4),
        "contributions": contributions,
        "triggered_at": at.isoformat(),
        "provenance": provenance,
    }


def _claims_for_field(
    cluster: Iterable[str],
    field: str,
    props_by_uri: Mapping[str, Mapping[str, Any]],
    *,
    prefer_uri: str = "",
) -> list[FactClaim]:
    """Strongest claim per distinct value (authority > confidence > recency).

    On an unbroken trust-axis tie, prefer the merge-winner's row so the
    surviving value's provenance is the fragment that actually survived.
    """
    by_value: dict[str, FactClaim] = {}
    # Winner first so an equal-key later row cannot steal provenance.
    ordered = list(cluster)
    if prefer_uri in ordered:
        ordered.remove(prefer_uri)
        ordered.insert(0, prefer_uri)
    for uri in ordered:
        props = _props(props_by_uri, uri)
        value = _as_str(props.get(field)).strip()
        if not value:
            continue
        claim = FactClaim(
            value=value,
            authority=_parse_authority(props.get("authority")),
            observed_at=_parse_ts(props.get("observed_at")),
            source=_as_str(
                props.get("record_source")
                or props.get("source_label")
                or props.get("source")
            ),
        )
        existing = by_value.get(value)
        if existing is None or DEFAULT_CONFLICT_POLICY._key(claim) > DEFAULT_CONFLICT_POLICY._key(
            existing
        ):
            by_value[value] = claim
    return list(by_value.values())


def _claim_row(claim: FactClaim) -> dict[str, Any]:
    ts = claim.observed_at.isoformat() if claim.observed_at else ""
    return {
        "value": claim.value,
        "source": claim.source,
        "authority": claim.authority_str,
        "observed_at": ts,
    }


def explain_field_conflicts(
    cluster: list[str],
    winner: str,
    props_by_uri: Mapping[str, Mapping[str, Any]],
    *,
    fields: Iterable[str] = CONFLICT_FIELDS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split disagreeing fields into resolved (winner+why) vs unresolved.

    A disagreement whose deciding axis would be the lexical ``value``
    tiebreak is **left unresolved** — that is a silent guess, and the
    fixture's point is that we do not make it.
    """
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for field in fields:
        if field in _SKIP_PROPS:
            continue
        claims = _claims_for_field(
            cluster, field, props_by_uri, prefer_uri=winner
        )
        if len(claims) < 2:
            continue
        incoming, *existing = claims
        decision = DEFAULT_CONFLICT_POLICY.resolve(existing, incoming)
        payload = {
            "field": field,
            "entity": winner,
            "winner": _claim_row(decision.winner),
            "loser": _claim_row(decision.loser) if decision.loser else None,
            "reason": decision.reason,
            "values": [_claim_row(c) for c in claims],
        }
        payload["values"] = sorted(payload["values"], key=lambda c: c.get("source") or "")
        if decision.reason == REASON_VALUE:
            payload["flagged"] = "equal-trust sources — not silently guessed"
            unresolved.append(payload)
        else:
            resolved.append(payload)
    return resolved, unresolved


def explain_clusters(
    clusters: list[list[str]],
    entities: Mapping[str, NormalizedSignals],
    config: ERConfig,
    *,
    props_by_uri: Optional[Mapping[str, Mapping[str, Any]]] = None,
    scorer: Scorer | None = None,
    triggered_at: Optional[datetime] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Explain every merge cluster: merges + resolved + unresolved conflicts."""
    scorer = scorer or DefaultScorer()
    props_by_uri = props_by_uri or {}
    at = triggered_at or datetime.now(timezone.utc)
    merges: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for cluster in clusters:
        merge = explain_merge(
            cluster,
            entities,
            config,
            props_by_uri=props_by_uri,
            scorer=scorer,
            triggered_at=at,
        )
        merges.append(merge)
        resolved, leftover = explain_field_conflicts(
            cluster, merge["winner"], props_by_uri
        )
        conflicts.extend(resolved)
        unresolved.extend(leftover)
    merges.sort(key=lambda m: str(m.get("winner") or ""))
    conflicts.sort(key=lambda c: (str(c.get("entity") or ""), str(c.get("field") or "")))
    unresolved.sort(key=lambda c: (str(c.get("entity") or ""), str(c.get("field") or "")))
    return {"merges": merges, "conflicts": conflicts, "unresolved": unresolved}


def format_rebuild_report(report: Mapping[str, Any], kg: str) -> str:
    """CLI-shaped text a stranger should see after ``infona er rebuild``."""
    lines = [f"Rebuilding entity resolution for {kg}…"]
    types = report.get("types") or []
    for t in types:
        name = str(t.get("type") or "?").ljust(16)
        lines.append(
            f"  {name} {t.get('entities_before')} → {t.get('entities_after')}"
            f"  (−{t.get('fragments_absorbed')} fragments across "
            f"{t.get('clusters_merged')} clusters)"
        )
    merges = list(report.get("merges") or [])
    if not merges:
        for t in types:
            merges.extend(t.get("merges") or [])
    conflicts = list(report.get("conflicts") or [])
    unresolved = list(report.get("unresolved") or [])
    if not conflicts:
        for t in types:
            conflicts.extend(t.get("conflicts") or [])
    if not unresolved:
        for t in types:
            unresolved.extend(t.get("unresolved") or [])

    for merge in merges:
        lines.append("")
        lines.append(f"  merge  {merge.get('winner', '?')}")
        losers = merge.get("losers") or []
        if losers:
            lines.append(f"         losers:     {', '.join(str(u) for u in losers)}")
        lines.append(f"         reason:     {merge.get('reason', '')}")
        score = merge.get("score")
        if score is not None:
            lines.append(f"         score:      {float(score):.2f}")
        prov = merge.get("provenance") or {}
        src = prov.get("source") or ""
        when = prov.get("observed_at") or merge.get("triggered_at") or ""
        auth = prov.get("authority") or ""
        tail = f"{src} @ {when}".strip(" @")
        if auth:
            tail = f"{tail} ({auth})" if tail else f"({auth})"
        if tail:
            lines.append(f"         provenance: {tail}")

    for conflict in conflicts:
        win = conflict.get("winner") or {}
        lose = conflict.get("loser") or {}
        lines.append("")
        lines.append(f"  conflict  {conflict.get('field', '?')}")
        lines.append(f"         entity:     {conflict.get('entity', '')}")
        lines.append(
            "         winner:     "
            f"{win.get('value', '')}  ({win.get('source', '')}, "
            f"{win.get('authority', '')}, {win.get('observed_at', '')})"
        )
        if lose:
            lines.append(
                "         loser:      "
                f"{lose.get('value', '')}  ({lose.get('source', '')}, "
                f"{lose.get('authority', '')}, {lose.get('observed_at', '')})"
            )
        lines.append(f"         reason:     {conflict.get('reason', '')}")

    for item in unresolved:
        lines.append("")
        lines.append(f"  unresolved  {item.get('field', '?')}")
        lines.append(f"         entity:     {item.get('entity', '')}")
        for claim in item.get("values") or []:
            src = claim.get("source") or "?"
            lines.append(
                f"         {src}: {claim.get('value', '')} @ "
                f"{claim.get('observed_at', '')} ({claim.get('authority', '')})"
            )
        flagged = item.get("flagged") or "equal-trust sources — not silently guessed"
        lines.append(f"         flagged: {flagged}")

    total = report.get("fragments_absorbed_total")
    if total is None:
        total = sum(int(t.get("fragments_absorbed") or 0) for t in types)
    lines.append("")
    lines.append(f"Done. {total} fragments absorbed.")
    return "\n".join(lines) + "\n"
