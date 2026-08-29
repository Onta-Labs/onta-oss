"""First-run answer: one supported question on the tenant graph (INF-593).

Matching is the Clinical Trials seed's first supported question (Phase 3
obesity recruiting). ``question`` only overrides the echoed prompt text —
it does not select a different matcher.
"""

from __future__ import annotations

from typing import Any, Mapping

from infona_client.blueprint.models import REAL_NCT_RE, BlueprintManifest
from infona_client.graph.assertion_model import property_uri
from infona_client.graph.ontology_queries import type_uri
from infona_client.graph.rdfs_helpers import (
    session_entities_of_type,
    session_literal_values,
    session_object_values,
)
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import get_graph_store

_RECRUITING = frozenset({"RECRUITING", "ENROLLING_BY_INVITATION"})
DEFAULT_QUESTION = "Which Phase 3 trials for obesity are currently recruiting?"


def default_supported_question(manifest: BlueprintManifest) -> str:
    if manifest.examples:
        return manifest.examples[0].question
    return DEFAULT_QUESTION


def _lit(values: list[Any]) -> str:
    return str(values[0]).strip() if values else ""


async def _trial_card(session: Any, subject: str) -> dict[str, Any]:
    nct = _lit(await session_literal_values(session, subject, property_uri("nct_id")))
    phase = _lit(await session_literal_values(session, subject, property_uri("phase")))
    status = _lit(
        await session_literal_values(session, subject, property_uri("overall_status"))
    )
    title = _lit(
        await session_literal_values(session, subject, property_uri("brief_title"))
    ) or _lit(
        await session_literal_values(session, subject, property_uri("official_title"))
    )
    cond_uris = await session_object_values(
        session, subject, property_uri("studies_condition")
    )
    conditions: list[str] = []
    for uri in cond_uris:
        name = _lit(
            await session_literal_values(session, uri, property_uri("condition_name"))
        )
        if name:
            conditions.append(name)
    return {
        "subject": subject,
        "nct_id": nct,
        "phase": phase,
        "overall_status": status,
        "title": title,
        "conditions": conditions,
    }


def _is_sample_nct(nct: str, sample_subjects: set[str], subject: str) -> bool:
    if subject in sample_subjects:
        return True
    return nct.upper().startswith("SAMPLE-") or not REAL_NCT_RE.match(nct)


def _matches_phase3_obesity_recruiting(card: Mapping[str, Any]) -> bool:
    phase = str(card.get("phase") or "").upper()
    status = str(card.get("overall_status") or "").upper()
    hay = " ".join(
        [
            str(card.get("title") or ""),
            " ".join(card.get("conditions") or []),
        ]
    ).lower()
    return (
        "PHASE3" in phase.replace(" ", "")
        and status in _RECRUITING
        and "obesity" in hay
    )


async def answer_supported_question(
    manifest: BlueprintManifest,
    *,
    tenant_id: str,
    kg: str,
    question: str | None,
    sample_subjects: list[str],
    sample_captured_at: str | None,
) -> tuple[str, str, list[str], bool]:
    """Answer on the tenant graph. Sample rows are labelled and never current."""
    asked = (question or "").strip() or default_supported_question(manifest)
    session = get_graph_store().session(GraphScope.for_instance(tenant_id, kg))
    subjects = await session_entities_of_type(
        session, type_uri("ClinicalTrial"), include_subclasses=False
    )
    cards = [await _trial_card(session, uri) for uri in subjects]
    sample_set = set(sample_subjects)
    matches = [c for c in cards if _matches_phase3_obesity_recruiting(c) and c["nct_id"]]
    live = [
        c
        for c in matches
        if not _is_sample_nct(c["nct_id"], sample_set, c["subject"])
    ]
    sample_hits = [
        c for c in matches if _is_sample_nct(c["nct_id"], sample_set, c["subject"])
    ]
    if live:
        ncts = [c["nct_id"] for c in live]
        answer = (
            f"{asked} — {', '.join(ncts)} "
            f"({len(ncts)} recruiting Phase 3 obesity trial(s) on this workspace graph)."
        )
        return asked, answer, ncts, False
    if sample_hits:
        ncts = [c["nct_id"] for c in sample_hits]
        captured = sample_captured_at or "unknown"
        answer = (
            f"{asked} — sample, captured {captured}, not current. "
            f"Expected sample ids: {', '.join(ncts)}."
        )
        return asked, answer, ncts, True
    live_any = [
        c
        for c in cards
        if c["nct_id"] and not _is_sample_nct(c["nct_id"], sample_set, c["subject"])
    ]
    if live_any:
        ncts = [c["nct_id"] for c in live_any[:8]]
        answer = (
            f"{asked} — no recruiting Phase 3 obesity trials matched; "
            f"acquired NCT ids on this graph include {', '.join(ncts)}."
        )
        return asked, answer, ncts, False
    answer = (
        f"{asked} — no acquired trials on this workspace graph yet. "
        "Sample rows, if present, are not current."
    )
    return asked, answer, [], False


__all__ = [
    "DEFAULT_QUESTION",
    "answer_supported_question",
    "default_supported_question",
]
