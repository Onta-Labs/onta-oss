"""Read-only *probe* helpers for NL→Cypher planning (cheap, best-effort).

Companion to :mod:`query_build` (live type inventory). Probes inject:

* low-cardinality dim **values** (from registry binds / inventory)
* **money leaf candidates** when the question cues cost/price/tuition

Never short-circuits the LLM (always-LLM product rule). Failures → empty
string so generation continues. Anti-overfit: no persona CSV hardcodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from infona_client.nlp.numeric_attr_resolve import (
    is_money_family_leaf,
    is_money_nl_cue,
    leaf_host_types,
    leaf_tokens,
    literal_leaves_for_type,
    normalize_leaf_key,
    normalize_populated_types,
    resolve_numeric_attr,
)
from infona_client.nlp.query_build import (
    QueryBuildContext,
    format_query_build_for_prompt,
)

if TYPE_CHECKING:
    from infona_client.graph.store import GraphStore
    from infona_client.nlp.dim_registry import DimBind, DimEntry, DimRegistry

# Cap distinct values shown per dim in probe notes (high-card never dumped).
_MAX_DIM_VALUES = 20
_MAX_DIMS_IN_PROBE = 16
_MAX_MONEY_CANDIDATES = 8

# Leaf name patterns that look money-ish for ontology grep (general stems).
_MONEY_LEAF_RE = re.compile(
    r"(?i)(?:cost|price|tuition|amount|fee|charge|msrp|fare|rate|wage|salary|rent)"
)

_MONEY_CUE_RE = re.compile(
    r"(?ix)\b(?:"
    r"cost|costs|costing|price|prices|priced|tuition|fee|fees|amount|"
    r"charge|charges|msrp|fare|rate|rates|payment|payments|"
    r"cheaper|expensive"
    r")\b"
)


@dataclass(frozen=True, slots=True)
class MoneyLeafCandidate:
    """One money/numeric leaf ranked for a cost/price cue."""

    leaf: str
    score: float
    host_types: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    preferred: bool = False


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Combined cheap probes for the Cypher prompt grounding spine."""

    build: QueryBuildContext | None = None
    dim_values_text: str = ""
    money_candidates: tuple[MoneyLeafCandidate, ...] = ()
    money_cue: str = ""
    money_text: str = ""
    combined_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def populated_type_names(self) -> tuple[str, ...]:
        if self.build is None:
            return ()
        return self.build.populated_type_names


def question_has_money_cue(question: str) -> bool:
    """True when NL carries a cost/price/tuition-style cue."""
    q = question or ""
    if is_money_nl_cue(q):
        return True
    return bool(_MONEY_CUE_RE.search(q))


def extract_money_cue(question: str) -> str:
    """Best single cue token for resolve (cost | price | tuition | fee | …)."""
    q = (question or "").lower()
    # Prefer more specific compounds first.
    for phrase, cue in (
        ("list price", "price"),
        ("unit cost", "cost"),
        ("sale price", "price"),
        ("tuition", "tuition"),
        ("assay cost", "cost"),
        ("price", "price"),
        ("cost", "cost"),
        ("fee", "fee"),
        ("amount", "amount"),
        ("charge", "charge"),
        ("msrp", "msrp"),
        ("fare", "fare"),
        ("rate", "rate"),
    ):
        if re.search(rf"(?i)\b{re.escape(phrase)}\b", q):
            return cue
    if is_money_nl_cue(q):
        # First money-ish word.
        for t in re.findall(r"[A-Za-z_]+", q):
            if is_money_nl_cue(t):
                return t.lower().rstrip("s")
    return ""


def format_dim_values_for_prompt(
    registry: "DimRegistry | None" = None,
    *,
    binds: Sequence["DimBind"] = (),
    max_dims: int = _MAX_DIMS_IN_PROBE,
    max_values: int = _MAX_DIM_VALUES,
) -> str:
    """List leaf → values for low-card dims (cap values; never dump high-card).

    Prefer dims that have values. Unique binds from the question are listed
    first so the planner can equality-filter with exact stored strings.
    """
    lines: list[str] = []
    shown = 0
    seen_ids: set[str] = set()

    if binds:
        lines.append("Bound dim values from the question (use EXACT strings):")
        for b in binds:
            dim = b.dim
            val = getattr(b.matched_value, "display", "") or ""
            leaf = getattr(dim, "leaf", "") or ""
            st = getattr(dim, "subject_type", "") or ""
            kind = getattr(dim, "kind", "") or ""
            label = f"{st}.{leaf}" if st else leaf
            if kind == "entity_dim":
                rt = getattr(dim, "range_type", None) or ""
                if rt:
                    label = f"{label}->{rt}"
            lines.append(f'  - {label} = "{val}" (from token {b.token!r})')
            did = getattr(dim, "dim_id", None) or label
            seen_ids.add(str(did))

    if registry is not None and getattr(registry, "dims", None):
        header = "Low-cardinality dim values (equality-filter with these exact strings):"
        if not lines:
            lines.append(header)
        else:
            lines.append(header)
        for dim in registry.dims:
            if shown >= max_dims:
                lines.append(f"  … ({len(registry.dims) - shown} more dims omitted)")
                break
            did = getattr(dim, "dim_id", "") or f"{dim.subject_type}.{dim.leaf}"
            if did in seen_ids:
                # Still useful to list remaining values for that dim once.
                pass
            vals = list(getattr(dim, "values", ()) or ())
            if not vals and int(getattr(dim, "distinct_count", 0) or 0) == 0:
                continue
            # High-card guard: registry only stores low-card; still hard-cap.
            display_vals = vals[:max_values]
            val_bits = ", ".join(
                f'"{getattr(v, "display", v)}"' for v in display_vals
            )
            more = ""
            dc = int(getattr(dim, "distinct_count", 0) or 0)
            if dc > len(display_vals):
                more = f" … (n={dc}, capped at {max_values})"
            kind = getattr(dim, "kind", "literal_enum") or "literal_enum"
            if kind == "entity_dim":
                rt = getattr(dim, "range_type", None) or ""
                range_bit = f" → {rt}" if rt else ""
                lines.append(
                    f"  - {dim.subject_type}.{dim.leaf}{range_bit} "
                    f"(entity_dim): {val_bits}{more}"
                )
            else:
                lines.append(
                    f"  - {dim.subject_type}.{dim.leaf} "
                    f"(literal_enum): {val_bits}{more}"
                )
            shown += 1
            seen_ids.add(str(did))

    if not lines:
        return ""
    return "\n".join(lines)


def _all_literal_leaves(ontology_summary: str) -> list[str]:
    """De-duped literal leaves across the ontology (declaration order)."""
    return literal_leaves_for_type(None, ontology_summary or "")


def probe_money_leaves(
    ontology_summary: str,
    *,
    question: str = "",
    populated_types: Sequence[str] | None = None,
    type_hint: str | None = None,
) -> tuple[MoneyLeafCandidate, ...]:
    """Rank money-ish leaves for cost/price cues on *this* ontology.

    Prefers leaves hosted on ``populated_types``. When ``type_hint`` is set,
    type-scoped resolve wins if unique. Bare ``cost`` prefers *cost* stems;
    bare ``price`` prefers *price* stems. Never invents leaves absent from
    the ontology text.
    """
    text = ontology_summary or ""
    if not text.strip():
        return ()
    cue = extract_money_cue(question) if question else "price"
    if not cue and not question_has_money_cue(question or ""):
        # Still allow explicit call with empty question → general money list.
        cue = "price"
    if question and not question_has_money_cue(question) and not cue:
        return ()

    pop = normalize_populated_types(populated_types, text)
    leaves = _all_literal_leaves(text)
    money_leaves = [L for L in leaves if is_money_family_leaf(L) or _MONEY_LEAF_RE.search(L)]
    if not money_leaves:
        return ()

    # Type-scoped unique resolve when we have a hint.
    preferred_leaf: str | None = None
    if type_hint:
        r = resolve_numeric_attr(
            cue or "price",
            type_name=type_hint,
            ontology_summary=text,
            money_family=True,
            populated_types=list(pop) if pop else populated_types,
        )
        if r.confidence == "unique" and r.prop_key:
            preferred_leaf = r.prop_key

    # Cross-type resolve for ranking signal.
    cross = resolve_numeric_attr(
        cue or "price",
        type_name=None,
        ontology_summary=text,
        money_family=True,
        populated_types=list(pop) if pop else populated_types,
    )
    score_by_leaf: dict[str, float] = {}
    reasons_by: dict[str, list[str]] = {}
    for c in cross.candidates:
        score_by_leaf[c.leaf] = c.score
        reasons_by[c.leaf] = list(c.reasons)
    if cross.confidence == "unique" and cross.prop_key and preferred_leaf is None:
        preferred_leaf = cross.prop_key

    # Stem preference: cost cue → cost-bearing leaves; price cue → price-bearing.
    cue_toks = set(leaf_tokens(cue or "price"))
    out: list[MoneyLeafCandidate] = []
    for leaf in money_leaves:
        hosts = tuple(leaf_host_types(leaf, text))
        base = score_by_leaf.get(leaf, 0.55 if is_money_family_leaf(leaf) else 0.45)
        reasons = list(reasons_by.get(leaf, ()))
        l_toks = set(leaf_tokens(leaf))
        if cue_toks & l_toks:
            base = max(base, 0.88)
            if "cue_stem" not in reasons:
                reasons.append("cue_stem")
        # Populated host boost (mirror resolve ranking).
        if pop and any(h in pop for h in hosts):
            base = min(1.0, base + 0.08)
            if "populated_type" not in reasons:
                reasons.append("populated_type")
        elif pop and hosts and not any(h in pop for h in hosts):
            base = max(0.0, base - 0.06)
            if "empty_type_only" not in reasons:
                reasons.append("empty_type_only")
        # Prefer cue-family compounds on populated types.
        n = normalize_leaf_key(leaf)
        if cue in ("cost", "costs") and "cost" in l_toks:
            base = min(1.0, base + 0.04)
        if cue in ("price", "prices", "priced") and "price" in l_toks:
            base = min(1.0, base + 0.04)
        if preferred_leaf and normalize_leaf_key(leaf) == normalize_leaf_key(
            preferred_leaf
        ):
            base = min(1.0, base + 0.05)
            reasons.append("preferred")
        out.append(
            MoneyLeafCandidate(
                leaf=leaf,
                score=base,
                host_types=hosts,
                reasons=tuple(reasons) or ("money_leaf",),
                preferred=bool(
                    preferred_leaf
                    and normalize_leaf_key(leaf) == normalize_leaf_key(preferred_leaf)
                ),
            )
        )

    out.sort(key=lambda c: (-c.score, c.leaf.lower()))
    # Drop very weak empty-only noise when better candidates exist.
    if pop:
        strong = [c for c in out if any(h in pop for h in c.host_types) or not c.host_types]
        if strong:
            out = strong + [c for c in out if c not in strong]
    return tuple(out[:_MAX_MONEY_CANDIDATES])


def format_money_candidates_for_prompt(
    candidates: Sequence[MoneyLeafCandidate] | None,
    *,
    cue: str = "",
    question: str = "",
) -> str:
    """Inject money leaf candidates into the grounding spine."""
    if not candidates:
        return ""
    cue = cue or extract_money_cue(question) or "cost/price"
    lines = [
        "## Money / measure leaf candidates (from ontology + populated types)",
        f'User said {cue!r} → candidates (prefer populated hosts; use exact prop_key):',
    ]
    for i, c in enumerate(candidates, 1):
        hosts = ", ".join(c.host_types[:4]) if c.host_types else "?"
        pref = " ★ preferred" if c.preferred else ""
        reason_s = ",".join(c.reasons) if c.reasons else ""
        lines.append(
            f"  {i}. {c.leaf} (hosts: {hosts}; score={c.score:.2f}"
            f"; reasons={reason_s}){pref}"
        )
    top = candidates[0]
    if top.preferred or top.score >= 0.7:
        lines.append(
            f"Resolved measure leaf hint: {top.leaf} "
            f"(from {cue!r} — use params.prop_key={top.leaf!r} when aggregating/"
            f"comparing; do not invent a bare 'price'/'cost' leaf if undeclared)."
        )
    lines.append(
        "Rules: equality-filter dims with exact strings from dim values; "
        "multi-constraint questions MUST constrain ALL listed dims before SUM/COUNT."
    )
    return "\n".join(lines)


def format_probe_for_prompt(ctx: ProbeContext | None) -> str:
    """Full probe block for the Cypher generation prompt."""
    if ctx is None:
        return ""
    if ctx.combined_text:
        return ctx.combined_text
    parts = [
        format_query_build_for_prompt(ctx.build),
        ctx.dim_values_text or "",
        ctx.money_text or "",
    ]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


async def build_probe_context(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str = "",
    ontology_summary: str = "",
    registry: "DimRegistry | None" = None,
    binds: Sequence["DimBind"] = (),
    populated_types: Sequence[str] | None = None,
    build_ctx: QueryBuildContext | None = None,
    type_hint: str | None = None,
) -> ProbeContext:
    """Combine inventory + dim values + money candidates (best-effort).

    Store/registry failures degrade to empty sections; never raises.
    """
    from infona_client.nlp.query_build import collect_query_build_context

    build = build_ctx
    if build is None and store is not None and tenant_id and kg:
        try:
            build = await collect_query_build_context(
                store,
                tenant_id=tenant_id,
                kg=kg,
                question=question,
            )
        except Exception:
            build = None

    pops: list[str] = []
    if populated_types:
        pops = [str(t).strip() for t in populated_types if str(t).strip()]
    elif build is not None:
        pops = list(build.populated_type_names)

    # Prefer question type hits as type_hint when not supplied.
    hint = type_hint
    if not hint and build is not None and build.question_type_hits:
        hint = build.question_type_hits[0]

    dim_text = ""
    try:
        dim_text = format_dim_values_for_prompt(registry, binds=binds)
    except Exception:
        dim_text = ""

    money_cands: tuple[MoneyLeafCandidate, ...] = ()
    money_text = ""
    cue = ""
    try:
        if ontology_summary and (
            question_has_money_cue(question) or extract_money_cue(question)
        ):
            cue = extract_money_cue(question)
            money_cands = probe_money_leaves(
                ontology_summary,
                question=question,
                populated_types=pops or None,
                type_hint=hint,
            )
            money_text = format_money_candidates_for_prompt(
                money_cands, cue=cue, question=question
            )
    except Exception:
        money_cands = ()
        money_text = ""

    build_text = format_query_build_for_prompt(build)
    parts = [build_text, dim_text, money_text]
    combined = "\n\n".join(p.strip() for p in parts if p and p.strip())
    if combined:
        combined = combined + "\n"

    return ProbeContext(
        build=build,
        dim_values_text=dim_text,
        money_candidates=money_cands,
        money_cue=cue,
        money_text=money_text,
        combined_text=combined,
        extra={
            "populated_types": pops,
            "money_candidate_count": len(money_cands),
            "dim_values_present": bool(dim_text),
        },
    )


__all__ = [
    "MoneyLeafCandidate",
    "ProbeContext",
    "build_probe_context",
    "extract_money_cue",
    "format_dim_values_for_prompt",
    "format_money_candidates_for_prompt",
    "format_probe_for_prompt",
    "probe_money_leaves",
    "question_has_money_cue",
]
