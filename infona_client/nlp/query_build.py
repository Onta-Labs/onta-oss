"""Live-graph *build* context for NL→Cypher (probe inventory, not guess).

``/ask`` still uses an LLM to write Cypher (always-LLM product rule), but the
model must **build** from live instance facts for *this* KG rather than
hallucinate against a polluted tenant ontology.

This module collects cheap GraphStore probes **before** generation:

* populated type → entity counts
* optional type-name cues from the question that match those types

Results are formatted into the grounding spine. No fixture short-circuit; no
product hardcodes for persona CSVs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from infona_client.graph.store import GraphStore

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,40}")
_MAX_TYPES_IN_PROMPT = 24
_MAX_QUESTION_MATCHES = 8


@dataclass(frozen=True, slots=True)
class TypePopulation:
    """One type with a live entity count in the active KG."""

    name: str
    entity_count: int


@dataclass(frozen=True, slots=True)
class QueryBuildContext:
    """Structured build notes from GraphStore probes."""

    types: tuple[TypePopulation, ...] = ()
    question_type_hits: tuple[str, ...] = ()
    total_entities: int = 0
    source: str = "graph_store"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def populated_type_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.types if t.entity_count > 0)


def _normalize_type_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_question_types(
    question: str,
    populated: Sequence[TypePopulation | str],
) -> tuple[str, ...]:
    """Return populated type names whose tokens appear in the question.

    Case-insensitive substring / token match. Synthetic-safe; no domain list.
    """
    q = (question or "").strip()
    if not q:
        return ()
    q_norm = _normalize_type_token(q)
    q_tokens = {_normalize_type_token(t) for t in _TOKEN_RE.findall(q)}
    hits: list[str] = []
    for item in populated or ():
        name = item.name if isinstance(item, TypePopulation) else str(item)
        if not name or not str(name).strip():
            continue
        n = str(name).strip()
        n_norm = _normalize_type_token(n)
        if not n_norm:
            continue
        # Whole-type cue in question (e.g. "parts" ~ "Part", "assays" ~ "Assay")
        if n_norm in q_norm or any(
            n_norm in tok or tok in n_norm for tok in q_tokens if len(tok) >= 3
        ):
            if n not in hits:
                hits.append(n)
        # Plural-ish: question has "part" and type is "Part"
        singular = n_norm[:-1] if n_norm.endswith("s") and len(n_norm) > 3 else n_norm
        if singular and singular in q_tokens and n not in hits:
            hits.append(n)
        if len(hits) >= _MAX_QUESTION_MATCHES:
            break
    return tuple(hits)


def format_query_build_for_prompt(ctx: QueryBuildContext | None) -> str:
    """Format live inventory for the Cypher generation prompt."""
    if ctx is None or not ctx.types:
        return ""
    lines = [
        "## Graph build notes (LIVE instance inventory for THIS knowledge graph)",
        "Build the Cypher from these facts. Do NOT invent types or prefer empty "
        "pollution types from the shared tenant ontology when a populated type "
        "fits the question.",
        "",
        "Populated types (entity counts > 0):",
    ]
    for t in ctx.types[:_MAX_TYPES_IN_PROMPT]:
        if t.entity_count <= 0:
            continue
        lines.append(f"- {t.name}: {t.entity_count} entities")
    if ctx.total_entities:
        lines.append(f"Total entities in KG (approx): {ctx.total_entities}")
    if ctx.question_type_hits:
        lines.append("")
        lines.append(
            "Question likely refers to type(s): "
            + ", ".join(ctx.question_type_hits)
            + ". Prefer these for $type_names / INSTANCE_OF filters."
        )
    lines.append("")
    lines.append(
        "Rules: (1) Only use types listed above for primary MATCH unless the "
        "question explicitly requires another. (2) Prefer relationship/attr "
        "leaves marked populated in the ontology schema. (3) Filtered aggregates "
        "must constrain first, then SUM/COUNT. (4) Do not emit a pure Product/"
        "Book/pollution type scan when a more specific populated type matches. "
        "(5) If Graph build notes / probe list dim values, equality-filter with "
        "those exact strings. (6) If money candidates are listed for cost/price, "
        "use that prop_key. (7) Multi-constraint questions MUST constrain all "
        "listed dims before aggregate. (8) Project Assertion.literal_value (or a "
        "cache key unmarked as populated). Never Entity.title / Entity.date_end "
        "when a sibling unmarked leaf exists. (9) Kind/class filters: prefer a "
        "populated typed enum/select on the asked type over unstructured "
        "category on a related type."
    )
    return "\n".join(lines)


async def collect_query_build_context(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str = "",
) -> QueryBuildContext | None:
    """Probe GraphStore for populated type counts (best-effort).

    Returns ``None`` when the store is missing or the probe fails — generation
    still proceeds (always-LLM) without build notes.
    """
    if store is None or not tenant_id or not kg:
        return None
    try:
        from infona_client.graph.explore_store import type_counts

        rows = await type_counts(store=store, tenant_id=tenant_id, kg=kg)
    except Exception:
        return None
    if not rows:
        return QueryBuildContext(types=(), total_entities=0, source="graph_store_empty")

    pops: list[TypePopulation] = []
    total = 0
    for r in rows:
        name = str(getattr(r, "type_name", None) or getattr(r, "name", None) or "").strip()
        if not name:
            # TypeCountRow field names
            name = str(getattr(r, "type", None) or "").strip()
        if not name and isinstance(r, dict):
            name = str(r.get("type_name") or r.get("name") or r.get("type") or "").strip()
        try:
            cnt = int(
                getattr(r, "entity_count", None)
                if not isinstance(r, dict)
                else r.get("entity_count")
            )
        except (TypeError, ValueError):
            cnt = 0
        if not name or cnt <= 0:
            continue
        pops.append(TypePopulation(name=name, entity_count=cnt))
        total += cnt

    pops.sort(key=lambda t: (-t.entity_count, t.name.lower()))
    hits = match_question_types(question, pops)
    return QueryBuildContext(
        types=tuple(pops),
        question_type_hits=hits,
        total_entities=total,
        source="graph_store",
    )


__all__ = [
    "QueryBuildContext",
    "TypePopulation",
    "collect_query_build_context",
    "format_query_build_for_prompt",
    "match_question_types",
]
