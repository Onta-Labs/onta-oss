"""Grounded numeric / money plans for NL→Cypher (planning layer only).

Sibling to :mod:`infona_client.nlp.ontology_subgraph_match` (relationship /
location hops). This module grounds **literal numeric** compare and aggregate
intents — type + prop_key + op/threshold or agg_op — as structured prompt
context. It never short-circuits the LLM (always-LLM product rule).

Plan fields feed ``format_numeric_grounding_for_prompt`` which is merged into
the same grounding spine the Cypher generator already injects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from infona_client.graph.rdfs_helpers import (
    TEMPLATE_LITERAL_AGGREGATE,
    TEMPLATE_LITERAL_COMPARE,
    type_names_with_subclasses,
)
from infona_client.nlp.cypher_generate import (
    DEFAULT_LIST_LIMIT,
    extract_type_names_from_ontology,
    resolve_type_name,
)
from infona_client.nlp.numeric_attr_resolve import (
    is_money_nl_cue,
    resolve_numeric_attr,
)

_SAFE_PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CMP_OP_MAP = {
    "<": "lt",
    "less than": "lt",
    "under": "lt",
    "below": "lt",
    ">": "gt",
    "more than": "gt",
    "over": "gt",
    "above": "gt",
    "<=": "le",
    "at most": "le",
    ">=": "ge",
    "at least": "ge",
    "=": "eq",
    "==": "eq",
    "equals": "eq",
    "equal": "eq",
    "exactly": "eq",
    "than": "lt",  # bare "than" after cheaper
}

_AGG_OP_MAP = {
    "total": "sum",
    "sum": "sum",
    "average": "avg",
    "avg": "avg",
    "mean": "avg",
    "minimum": "min",
    "min": "min",
    "maximum": "max",
    "max": "max",
}

_TRAILING_PUNCT_RE = re.compile(r"[?!.\s]+$")

# Compare: "widgets under 15" / "products that cost less than 15" /
# "items with price under 15" / "widgets cheaper than 12.5"
_NUMERIC_COMPARE_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|which|what)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:"
    # money verb + op + number
    r"(?:cost|priced?|costs?|cheaper|more\s+expensive)\s+"
    r"(?P<cost_op>less\s+than|under|below|more\s+than|over|above|at\s+least|"
    r"at\s+most|exactly|than)\s+"
    r"(?:\$|USD\s*)?(?P<cost_num>\d+(?:\.\d+)?)\s*(?:dollars?|usd|\$)?"
    r"|"
    # with|having|where prop op number
    r"(?:with|having|where)\s+(?P<prop>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?:is\s+)?"
    r"(?P<cmp><=|>=|<|>|=|==|less\s+than|under|below|more\s+than|over|above|"
    r"at\s+least|at\s+most|equals?)\s+"
    r"(?:\$|USD\s*)?(?P<num>\d+(?:\.\d+)?)\s*(?:dollars?|usd|\$)?"
    r"|"
    # "price under 15" with type earlier: label ends with type, prop is price
    r"(?P<bare_prop>price|cost|unit_cost|list_price|amount|fee|rate)\s+"
    r"(?P<bare_op>less\s+than|under|below|more\s+than|over|above|at\s+least|"
    r"at\s+most|exactly)\s+"
    r"(?:\$|USD\s*)?(?P<bare_num>\d+(?:\.\d+)?)\s*(?:dollars?|usd|\$)?"
    r")"
    r"(?:\s+.*)?$"
)

# Aggregate: "average price of widgets" / "total unit_cost of widgets" /
# "what is the average price of widgets" / "average unit cost of widgets"
_NUMERIC_AGG_RE = re.compile(
    r"(?ix)^"
    r"(?:what(?:'s|\s+is)\s+)?"
    r"(?:the\s+)?"
    r"(?P<agg>total|sum|average|avg|mean|minimum|min|maximum|max)"
    r"(?:"
    # Explicit prop then of/for type: "average price of widgets"
    # / "average unit cost of widgets" / "sum of unit_cost for widgets"
    r"(?:\s+(?:of|for))?"
    r"(?:\s+(?:the\s+)?)?"
    r"(?P<prop>[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*){0,3})"
    r"\s+(?:of|for|across|over|on)"
    r"(?:\s+(?:all\s+)?)?"
    r"(?P<label>.+?)"
    r"|"
    # No prop: "total of widgets" / "average widgets" — prop resolved later
    r"(?:\s+(?:of|for|across|over|on))?"
    r"(?:\s+(?:all\s+)?)?"
    r"(?P<label2>.+?)"
    r")"
    r"$"
)

_MONEY_MENTION_DEFAULT = "price"


@dataclass
class GroundedNumericPlan:
    """Structured numeric grounding for the Cypher LLM prompt."""

    question: str
    intent: str  # "compare" | "agg" | "unknown"
    type_names: list[str] = field(default_factory=list)
    subject_type: str | None = None
    prop_key: str | None = None
    op: str | None = None  # lt/le/gt/ge/eq
    threshold: float | None = None
    agg_op: str | None = None  # sum/avg/min/max
    confidence: str = "none"  # "unique" | "ambiguous" | "none"
    template: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""
    candidates: list[str] = field(default_factory=list)
    mention: str = ""

    def is_unique(self) -> bool:
        return self.confidence == "unique" and bool(self.prop_key)


def ground_numeric_plan(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
    mention_index: Any | None = None,
    query_embedding: Sequence[float] | None = None,
) -> GroundedNumericPlan | None:
    """Ground a numeric compare or aggregate plan from NL + ontology.

    Returns ``None`` when the question is not a numeric shape. Returns a plan
    with ``confidence="ambiguous"`` / ``"none"`` when prop resolve fails closed
    (still useful prompt context). Never emits executable free-form Cypher.
    """
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q or not (ontology_summary or "").strip():
        return None

    names = type_names or extract_type_names_from_ontology(ontology_summary) or None

    # Prefer aggregate first when both could match ("average price of widgets")
    agg = _try_ground_agg(
        q,
        ontology_summary,
        type_names=names,
        mention_index=mention_index,
        query_embedding=query_embedding,
    )
    if agg is not None:
        return agg

    cmp_plan = _try_ground_compare(
        q,
        ontology_summary,
        type_names=names,
        mention_index=mention_index,
        query_embedding=query_embedding,
    )
    return cmp_plan


def _try_ground_compare(
    q: str,
    ontology_summary: str,
    *,
    type_names: list[str] | None,
    mention_index: Any | None,
    query_embedding: Sequence[float] | None,
) -> GroundedNumericPlan | None:
    m = _NUMERIC_COMPARE_RE.match(q)
    if not m:
        # Soft path: money cue + number without full regex (still type-scoped)
        if not is_money_nl_cue(q):
            return None
        num_m = re.search(
            r"(?i)(?:under|below|less\s+than|over|above|more\s+than|"
            r"at\s+least|at\s+most|cheaper\s+than|>=|<=|>|<)\s*"
            r"(?:\$|USD\s*)?(\d+(?:\.\d+)?)",
            q,
        )
        if not num_m:
            return None
        # Type: first resolvable token-ish phrase
        label_guess = re.sub(
            r"(?i)\b(?:under|below|less\s+than|over|above|more\s+than|"
            r"cost|price|priced|cheaper|expensive).*$",
            "",
            q,
        ).strip()
        label_guess = re.sub(
            r"(?i)^(list|show|find|get|which|what)\s+",
            "",
            label_guess,
        ).strip()
        matched = resolve_type_name(label_guess, type_names, ontology_summary)
        if matched is None:
            return None
        op_raw = "less than"
        g0 = q.lower()
        if any(x in g0 for x in ("over", "above", "more than", "expensive")):
            op_raw = "more than"
        elif "at least" in g0:
            op_raw = "at least"
        elif "at most" in g0:
            op_raw = "at most"
        threshold = float(num_m.group(1))
        prop_mention = _MONEY_MENTION_DEFAULT
        return _finish_compare(
            question=q,
            matched=matched,
            prop_mention=prop_mention,
            op_raw=op_raw,
            threshold=threshold,
            ontology_summary=ontology_summary,
            mention_index=mention_index,
            query_embedding=query_embedding,
            money=True,
        )

    label = (m.group("label") or "").strip()
    label = re.sub(
        r"(?i)\s+(?:list|show|return|with)\s+(?:their\s+)?titles?.*$",
        "",
        label,
    ).strip()
    # For bare_prop branch, label may include trailing words; strip prop phrase
    if m.groupdict().get("bare_prop"):
        # label is the type phrase before bare prop — already non-greedy
        pass
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None

    if m.group("cost_num") is not None:
        prop_mention = _MONEY_MENTION_DEFAULT
        op_raw = (m.group("cost_op") or "less than").strip().lower()
        g0 = (m.group(0) or "").lower()
        if "cheaper" in g0 and op_raw in ("than", "less than", "under", "below"):
            op_raw = "less than"
        elif "more expensive" in g0 and op_raw in (
            "than",
            "more than",
            "over",
            "above",
        ):
            op_raw = "more than"
        threshold = float(m.group("cost_num"))
        money = True
    elif m.groupdict().get("bare_num") is not None:
        prop_mention = (m.group("bare_prop") or "price").strip()
        op_raw = (m.group("bare_op") or "under").strip().lower()
        threshold = float(m.group("bare_num"))
        money = True
    else:
        prop_mention = (m.group("prop") or "").strip()
        if not prop_mention or not _SAFE_PROP_RE.match(prop_mention):
            return None
        op_raw = (m.group("cmp") or "<").strip().lower()
        threshold = float(m.group("num"))
        money = is_money_nl_cue(prop_mention)

    return _finish_compare(
        question=q,
        matched=matched,
        prop_mention=prop_mention,
        op_raw=op_raw,
        threshold=threshold,
        ontology_summary=ontology_summary,
        mention_index=mention_index,
        query_embedding=query_embedding,
        money=money,
    )


def _finish_compare(
    *,
    question: str,
    matched: str,
    prop_mention: str,
    op_raw: str,
    threshold: float,
    ontology_summary: str,
    mention_index: Any | None,
    query_embedding: Sequence[float] | None,
    money: bool,
) -> GroundedNumericPlan:
    op = _CMP_OP_MAP.get(op_raw)
    if op is None:
        return GroundedNumericPlan(
            question=question,
            intent="compare",
            subject_type=matched,
            confidence="none",
            explanation=f"unknown compare op {op_raw!r}",
            mention=prop_mention,
        )

    if money:
        resolved = resolve_numeric_attr(
            prop_mention,
            type_name=matched,
            ontology_summary=ontology_summary,
            mention_index=mention_index,
            query_embedding=query_embedding,
            money_family=True,
        )
    else:
        # Explicit prop from NL — still type-scoped
        resolved = resolve_numeric_attr(
            prop_mention,
            type_name=matched,
            ontology_summary=ontology_summary,
            mention_index=mention_index,
            query_embedding=query_embedding,
            money_family=False,
        )
        # Exact prop word may still be the leaf even if not money
        if resolved.confidence != "unique":
            # Accept the prop as-is if declared on type
            from infona_client.nlp.numeric_attr_resolve import literal_leaves_for_type

            leaves = {x.lower(): x for x in literal_leaves_for_type(matched, ontology_summary)}
            if prop_mention.lower() in leaves:
                resolved.prop_key = leaves[prop_mention.lower()]
                resolved.confidence = "unique"
                resolved.explanation = f"exact leaf {resolved.prop_key}"

    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    cand_names = [c.leaf for c in resolved.candidates]

    if resolved.confidence == "unique" and resolved.prop_key:
        params = {
            "type_names": expanded,
            "prop_key": resolved.prop_key,
            "op": op,
            "threshold": threshold,
            "limit": DEFAULT_LIST_LIMIT,
        }
        return GroundedNumericPlan(
            question=question,
            intent="compare",
            type_names=list(expanded),
            subject_type=matched,
            prop_key=resolved.prop_key,
            op=op,
            threshold=threshold,
            confidence="unique",
            template=TEMPLATE_LITERAL_COMPARE,
            params=params,
            explanation=resolved.explanation
            or f"{matched}.{resolved.prop_key} {op} {threshold}",
            candidates=cand_names,
            mention=prop_mention,
        )

    return GroundedNumericPlan(
        question=question,
        intent="compare",
        type_names=list(expanded),
        subject_type=matched,
        prop_key=None,
        op=op,
        threshold=threshold,
        confidence=resolved.confidence if resolved.confidence != "none" else "none",
        template=TEMPLATE_LITERAL_COMPARE if resolved.confidence == "ambiguous" else None,
        explanation=resolved.explanation or "could not resolve numeric prop",
        candidates=cand_names,
        mention=prop_mention,
    )


def _try_ground_agg(
    q: str,
    ontology_summary: str,
    *,
    type_names: list[str] | None,
    mention_index: Any | None,
    query_embedding: Sequence[float] | None,
) -> GroundedNumericPlan | None:
    m = _NUMERIC_AGG_RE.match(q)
    if not m:
        return None
    agg_word = (m.group("agg") or "").strip().lower()
    agg_op = _AGG_OP_MAP.get(agg_word)
    if not agg_op:
        return None
    prop_raw = (m.group("prop") or "").strip() or None
    label = (m.group("label") or m.group("label2") or "").strip()
    label = _TRAILING_PUNCT_RE.sub("", label)
    label = re.sub(r"(?i)^(all|the|of|for)\s+", "", label).strip()
    if not label:
        return None

    # "total number of widgets" is COUNT — not numeric agg on a prop named number
    if prop_raw and prop_raw.lower() in {
        "number",
        "count",
        "counts",
        "entities",
        "records",
        "items",
        "rows",
        "things",
    }:
        return None

    # Normalize multi-word prop ("unit cost" → unit_cost mention)
    prop_mention = None
    if prop_raw:
        prop_mention = re.sub(r"\s+", "_", prop_raw.strip())
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", prop_mention):
            # keep spaced for family resolve
            prop_mention = prop_raw.strip()

    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None

    money = bool(prop_mention and is_money_nl_cue(prop_mention)) or (
        prop_mention is None and is_money_nl_cue(q)
    )
    mention = prop_mention or (_MONEY_MENTION_DEFAULT if money else "amount")

    resolved = resolve_numeric_attr(
        mention,
        type_name=matched,
        ontology_summary=ontology_summary,
        mention_index=mention_index,
        query_embedding=query_embedding,
        money_family=money or is_money_nl_cue(mention),
    )
    # If explicit prop declared on type, prefer exact even when family weak
    if prop_mention and resolved.confidence != "unique":
        from infona_client.nlp.numeric_attr_resolve import (
            literal_leaves_for_type,
            normalize_leaf_key,
        )

        leaves = literal_leaves_for_type(matched, ontology_summary)
        target = normalize_leaf_key(prop_mention)
        for leaf in leaves:
            if normalize_leaf_key(leaf) == target:
                resolved.prop_key = leaf
                resolved.confidence = "unique"
                resolved.explanation = f"exact leaf {leaf}"
                break

    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    cand_names = [c.leaf for c in resolved.candidates]

    if resolved.confidence == "unique" and resolved.prop_key:
        params = {
            "type_names": expanded,
            "prop_key": resolved.prop_key,
            "agg_op": agg_op,
        }
        return GroundedNumericPlan(
            question=q,
            intent="agg",
            type_names=list(expanded),
            subject_type=matched,
            prop_key=resolved.prop_key,
            agg_op=agg_op,
            confidence="unique",
            template=TEMPLATE_LITERAL_AGGREGATE,
            params=params,
            explanation=resolved.explanation
            or f"{agg_op} of {resolved.prop_key} for {matched}",
            candidates=cand_names,
            mention=mention,
        )

    return GroundedNumericPlan(
        question=q,
        intent="agg",
        type_names=list(expanded),
        subject_type=matched,
        prop_key=None,
        agg_op=agg_op,
        confidence=resolved.confidence if resolved.confidence != "none" else "none",
        template=TEMPLATE_LITERAL_AGGREGATE if resolved.confidence == "ambiguous" else None,
        explanation=resolved.explanation or "could not resolve aggregate prop",
        candidates=cand_names,
        mention=mention,
    )


def format_numeric_grounding_for_prompt(plan: GroundedNumericPlan | None) -> str:
    """Render a numeric plan as LLM prompt context (never executable Cypher)."""
    if plan is None:
        return ""
    lines: list[str] = [
        "Numeric grounding (structured hint — prefer these when confident;",
        "still emit valid Cypher / allowlisted template JSON; do not invent props):",
        f"  intent: {plan.intent}",
    ]
    if plan.subject_type:
        lines.append(f"  subject_type: {plan.subject_type}")
    if plan.mention:
        lines.append(f"  prop_mention: {plan.mention!r}")
    lines.append(f"  confidence: {plan.confidence}")

    if plan.confidence == "unique" and plan.prop_key:
        lines.append(f"  prop_key: {plan.prop_key}")
        if plan.intent == "compare":
            if plan.op is not None:
                lines.append(f"  op: {plan.op}")
            if plan.threshold is not None:
                lines.append(f"  threshold: {plan.threshold}")
            lines.append(f"  preferred_template: {TEMPLATE_LITERAL_COMPARE}")
        elif plan.intent == "agg":
            if plan.agg_op:
                lines.append(f"  agg_op: {plan.agg_op}")
            lines.append(f"  preferred_template: {TEMPLATE_LITERAL_AGGREGATE}")
        if plan.params:
            bits = [f"{k}={plan.params[k]!r}" for k in sorted(plan.params.keys())]
            lines.append(f"  template_params: {{{', '.join(bits)}}}")
        if plan.explanation:
            lines.append(f"  explanation: {plan.explanation}")
    elif plan.confidence == "ambiguous":
        lines.append(
            "  note: multiple money/numeric leaves score equally — do NOT "
            "silently pick one prop; prefer a clarifying shape or the strongest "
            "schema-supported reading. Shortlist:"
        )
        for i, c in enumerate(plan.candidates[:5], 1):
            lines.append(f"    {i}. {c}")
        if plan.op is not None:
            lines.append(f"  op: {plan.op}")
        if plan.threshold is not None:
            lines.append(f"  threshold: {plan.threshold}")
        if plan.agg_op:
            lines.append(f"  agg_op: {plan.agg_op}")
    else:
        if plan.candidates:
            lines.append("  candidate_props: " + ", ".join(plan.candidates[:5]))
        if plan.explanation:
            lines.append(f"  note: {plan.explanation}")

    return "\n".join(lines) + "\n"


def merge_grounding_texts(*blocks: str) -> str:
    """Concatenate non-empty grounding blocks for the Cypher prompt."""
    parts = [b.strip() for b in blocks if b and b.strip()]
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


__all__ = [
    "GroundedNumericPlan",
    "format_numeric_grounding_for_prompt",
    "ground_numeric_plan",
    "merge_grounding_texts",
]
