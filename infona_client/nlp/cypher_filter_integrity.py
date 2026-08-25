"""Post-gen Cypher filter integrity (silent-wrong OPTIONAL MATCH filters).

Persona round 56a8c2 / product P0: free-form LLM Cypher often writes::

    MATCH (e:Entity …)-[:INSTANCE_OF]->(c:Class …)
    OPTIONAL MATCH (a:Assertion {subject_id:e.id})-[:PREDICATE]->(p:Property)
    WHERE p.name = $prop AND a.literal_value = $value
    RETURN count(e)

In Cypher, a ``WHERE`` attached to ``OPTIONAL MATCH`` only decides whether the
optional pattern binds; it does **not** drop the primary ``MATCH`` rows. The
filter is effectively discarded → **unfiltered counts** (e.g. "how many Phase 2
trials?" → total trials) or **silent zeros** on filtered aggregates.

Allowlisted ADR 0013 templates (``literal_values``, ``literal_compare``, …) use
OPTIONAL MATCH for Assertion reads but always re-assert the filter after a
``WITH`` (``WHERE a IS NOT NULL OR e[prop] = …`` / ``WHERE raw IS NOT NULL``).
Free-form must do the same, prefer a required ``MATCH``, or filter on the
entity denorm property (``e.phase_label = $v``).

**Product rules:**

* Always-LLM for user-facing ``/ask`` — this module never short-circuits
  generation; it only **rejects** bad plans so the retry loop can regenerate.
* Fail closed: honest empty / "could not answer" beats silent wrong total.
* Anti-overfit: detectors use structural Cypher shape + general NL filter cues,
  not persona CSV gold labels.
"""

from __future__ import annotations

import re
from typing import Any

# Templates that correctly apply property / related-entity filters (or are pure
# type scans — those are gated separately when the question has filter intent).
_FILTERING_TEMPLATES = frozenset(
    {
        "literal_values",
        "literal_values_count",
        "literal_compare",
        "literal_aggregate",
        "related_entity_name_filter",
        "related_entity_name_filter_inverse",
        "related_entities",
    }
)

_PURE_TYPE_TEMPLATES = frozenset(
    {
        "entities_of_type",
        "entities_of_type_count",
        "entity_count_total",
        "entity_count_by_type",
    }
)

# NL cues that a required property/status/value filter is intended.
_FILTER_INTENT_RE = re.compile(
    r"(?ix)"
    r"("
    r"\b(?:where|with|having|whose|which\s+are|that\s+are|that\s+have|that\s+is|"
    r"labeled|labelled|named|matching|filtered|status|equals?|equal\s+to|"
    r"less\s+than|more\s+than|under|over|above|below|at\s+least|at\s+most|"
    r"is\s+not|are\s+not)\b"
    r"|"
    r"['\"][^'\"]+['\"]"  # quoted needle
    r"|"
    # "sum/total/avg X for <status-or-value>" / "count X for active"
    r"\b(?:sum|total|average|avg|mean|count|how\s+many)\b[\s\S]{0,80}\bfor\b\s+\w+"
    r")"
)

# "Tier 2 widgets" / "Phase 3 trials" — label token + digit, excluding top/first N.
_LABEL_DIGIT_RE = re.compile(
    r"(?ix)\b(?P<label>[A-Za-z][A-Za-z0-9_]*)\s+(?P<num>[0-9]+(?:\.[0-9]+)?)\b"
)
_LABEL_DIGIT_STOP = frozenset(
    {
        "top",
        "first",
        "last",
        "limit",
        "page",
        "offset",
        "skip",
        "take",
        "next",
        "prev",
        "previous",
        "row",
        "rows",
        "item",
        "items",
        "of",
        "and",
        "or",
        "the",
        "a",
        "an",
        "in",
        "on",
        "at",
        "by",
        "from",
        "to",
        "vs",
        "version",
        "v",
    }
)

# Major clause boundaries used to slice OPTIONAL MATCH regions.
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?i)\b(?:WITH|RETURN|MATCH|OPTIONAL\s+MATCH|UNWIND|ORDER\s+BY|LIMIT|UNION)\b"
)

# Value predicates that, when only applied inside OPTIONAL MATCH WHERE, do not
# constrain the primary entity rows.
_VALUE_FILTER_RE = re.compile(
    r"(?ix)"
    r"("
    r"\bliteral_value\s*(?:=|<>|!=|<=|>=|<|>|=~)"
    r"|"
    r"=\s*\$prop_value\b"
    r"|"
    r"=\s*\$needle\b"
    r"|"
    r"=\s*\$threshold\b"
    r"|"
    r"(?:CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s*\("
    r"|"
    r"(?:CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s+"
    r"|"
    r"=\s*'[^']*'"
    r"|"
    r'=\s*"[^"]*"'
    r")"
)

# Property-key selection alone (not a value filter).
_PROP_KEY_ONLY_RE = re.compile(
    r"(?ix)\bp\.name\s*=\s*\$prop_key\b|\bp\.name\s*=\s*'[A-Za-z_][A-Za-z0-9_]*'"
)

# Post-OPTIONAL required constraints that re-apply the filter to primary rows.
_POST_CONSTRAINT_RE = re.compile(
    r"(?ix)"
    r"("
    r"\bIS\s+NOT\s+NULL\b"
    r"|"
    r"\be\s*\[\s*\$prop_key\s*\]"
    r"|"
    r"\be\s*\[\s*'[A-Za-z_][A-Za-z0-9_]*'\s*\]"
    r"|"
    r"\be\.[A-Za-z_][A-Za-z0-9_]*\s*(?:=|<>|!=|<=|>=|<|>|=~|CONTAINS)"
    r"|"
    r"\braw\s+IS\s+NOT\s+NULL\b"
    r"|"
    r"\ba\s+IS\s+NOT\s+NULL\b"
    r")"
)

# Free-form entity property equality (safe filter path).
_ENTITY_PROP_FILTER_RE = re.compile(
    r"(?ix)"
    r"("
    r"\be\.[A-Za-z_][A-Za-z0-9_]*\s*(?:=|<>|!=|<=|>=|<|>|=~|CONTAINS)"
    r"|"
    r"\be\s*\[\s*(?:\$prop_key|'[A-Za-z_][A-Za-z0-9_]*')\s*\]"
    r"|"
    r"\bliteral_value\s*(?:=|<>|!=|<=|>=|<|>)"
    r"|"
    r"=\s*\$prop_value\b"
    r"|"
    r"=\s*\$needle\b"
    r"|"
    r"=\s*\$threshold\b"
    r")"
)

def _has_required_assertion_match(cypher: str) -> bool:
    """True when there is a non-OPTIONAL MATCH of an Assertion node."""
    c = cypher or ""
    for m in re.finditer(r"(?i)\bMATCH\s*\(", c):
        # Walk back over whitespace for OPTIONAL keyword.
        j = m.start()
        k = j
        while k > 0 and c[k - 1].isspace():
            k -= 1
        prefix = c[max(0, k - 12) : k]
        if re.search(r"(?i)OPTIONAL\s*$", prefix):
            continue
        window = c[m.end() : m.end() + 160]
        if re.search(r"(?i)\bAssertion\b", window):
            return True
    return False

# Pure type-count / list shape without property filter signals.
_TYPE_COUNT_SHAPE_RE = re.compile(
    r"(?is)"
    r"MATCH\s*\(\s*\w+\s*:\s*Entity\b[\s\S]*?INSTANCE_OF[\s\S]*?"
    r"RETURN\s+count\s*\("
)


def question_has_filter_intent(question: str) -> bool:
    """True when the NL question likely requires a property/value constraint."""
    q = (question or "").strip()
    if not q:
        return False
    if _FILTER_INTENT_RE.search(q):
        return True
    for m in _LABEL_DIGIT_RE.finditer(q):
        if m.group("label").lower() not in _LABEL_DIGIT_STOP:
            return True
    return False


def _optional_assertion_regions(cypher: str) -> list[tuple[str, str]]:
    """Return (optional_region, trailing_query) pairs for each OPTIONAL MATCH.

    ``optional_region`` is the text from the pattern start through the WHERE that
    belongs to that optional match (up to the next major clause). ``trailing``
    is everything after that region in the full query (used for post-constraints).
    """
    c = cypher or ""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"(?i)OPTIONAL\s+MATCH", c):
        start = m.end()
        # Only care about Assertion / Property optional reads.
        window = c[start : start + 500]
        if not re.search(r"(?i)\b(?:Assertion|Property)\b", window):
            continue
        rest = c[start:]
        bound = _CLAUSE_BOUNDARY_RE.search(rest)
        if bound:
            region = rest[: bound.start()]
            trailing = rest[bound.start() :]
        else:
            region = rest
            trailing = ""
        out.append((region, trailing))
    return out


def _region_has_value_filter(region: str) -> bool:
    """True when the optional region's WHERE constrains a value (not just p.name)."""
    if not region:
        return False
    # Must have a WHERE-ish predicate in the region.
    if not re.search(r"(?i)\bWHERE\b|=|CONTAINS", region):
        return False
    if not _VALUE_FILTER_RE.search(region):
        return False
    # Pure p.name = $prop_key without any other value predicate is selection only.
    # If VALUE_FILTER matched only via a false friend, still require a real value cue.
    # Strip prop-key selection and re-check.
    stripped = _PROP_KEY_ONLY_RE.sub(" ", region)
    return bool(_VALUE_FILTER_RE.search(stripped))


def _trailing_has_post_constraint(trailing: str) -> bool:
    return bool(_POST_CONSTRAINT_RE.search(trailing or ""))


def optional_match_filter_smell(cypher: str) -> str | None:
    """Return a short reason when OPTIONAL MATCH value filters do not constrain rows.

    Allowlisted template bodies that re-assert ``a IS NOT NULL`` / ``raw IS NOT
    NULL`` / entity denorm after ``WITH`` return ``None``.
    """
    regions = _optional_assertion_regions(cypher)
    if not regions:
        return None
    for region, trailing in regions:
        if not _region_has_value_filter(region):
            continue
        if _trailing_has_post_constraint(trailing):
            continue
        return (
            "OPTIONAL MATCH value filter does not constrain primary rows "
            "(WHERE on OPTIONAL MATCH only nulls the optional bind; it does not "
            "drop entities). Use required MATCH, e.prop = $value, or "
            "WITH … WHERE a IS NOT NULL / raw IS NOT NULL after the optional read."
        )
    return None


def cypher_has_constraining_filter(cypher: str) -> bool:
    """True when free-form Cypher appears to apply a real property/value filter."""
    c = cypher or ""
    if not c.strip():
        return False
    if _ENTITY_PROP_FILTER_RE.search(c):
        # Entity prop filters always constrain; Assertion value filters only
        # when not solely inside unconstrained OPTIONAL MATCH.
        smell = optional_match_filter_smell(c)
        if smell is None:
            return True
        # Has entity-side filter even if optional smells? Check e.prop / e[ separately.
        if re.search(
            r"(?ix)\be\.[A-Za-z_][A-Za-z0-9_]*\s*(?:=|<>|!=|<=|>=|<|>|=~|CONTAINS)"
            r"|\be\s*\[\s*(?:\$prop_key|'[A-Za-z_][A-Za-z0-9_]*')\s*\]",
            c,
        ):
            return True
        return False
    if _has_required_assertion_match(c) and _VALUE_FILTER_RE.search(c):
        return True
    # OPTIONAL MATCH + post-constraint path (literal_values shape).
    if _optional_assertion_regions(c) and optional_match_filter_smell(c) is None:
        # Has optional assertion; either no value filter (aggregate read) or
        # value filter with post-constraint.
        for region, trailing in _optional_assertion_regions(c):
            if _region_has_value_filter(region) and _trailing_has_post_constraint(
                trailing
            ):
                return True
            # literal_compare shape: optional selects prop, post filters value
            if _PROP_KEY_ONLY_RE.search(region) and _trailing_has_post_constraint(
                trailing
            ):
                if _VALUE_FILTER_RE.search(trailing) or re.search(
                    r"(?i)\$op\b|\$threshold\b|\$prop_value\b|\$needle\b", trailing
                ):
                    return True
    return False


def pure_type_scan_without_filter(cypher: str) -> bool:
    """True when Cypher looks like a type membership count/list with no prop filter."""
    c = cypher or ""
    if not c.strip():
        return False
    if cypher_has_constraining_filter(c):
        return False
    if _TYPE_COUNT_SHAPE_RE.search(c):
        return True
    # List shape: MATCH Entity INSTANCE_OF … RETURN e.id (no value WHERE)
    if re.search(r"(?is)MATCH\s*\(\s*\w+\s*:\s*Entity\b[\s\S]*INSTANCE_OF", c):
        if not _VALUE_FILTER_RE.search(c) and not re.search(
            r"(?ix)\be\.[A-Za-z_][A-Za-z0-9_]*\s*=", c
        ):
            # Has type WHERE only
            if re.search(r"(?i)\bc\.name\b|\btype_names\b|INSTANCE_OF", c):
                return True
    return False


def check_cypher_filter_integrity(
    cypher: str,
    *,
    question: str = "",
    template: str | None = None,
    params: dict[str, Any] | None = None,
) -> str | None:
    """Return a human-readable rejection reason, or ``None`` if the plan is OK.

    Callers should retry generation with this reason as ``error_feedback`` when
    non-``None``. Known-good filter templates skip free-form shape checks.
    """
    tmpl = (template or "").strip()
    params = params or {}

    # Allowlisted filter templates always constrain (ADR 0013 bodies).
    if tmpl in _FILTERING_TEMPLATES:
        return None

    # Pure type templates are only OK when the question has no filter intent.
    if tmpl in _PURE_TYPE_TEMPLATES:
        if question_has_filter_intent(question):
            # Exception: template still carries filter params (shouldn't for pure
            # type templates, but be defensive).
            if any(
                k in params and params[k] not in (None, "", [], {})
                for k in ("prop_key", "prop_value", "needle", "threshold", "op", "rel_attr")
            ):
                return None
            return (
                "question has filter intent but plan is a pure type scan "
                f"({tmpl}) with no property/value constraint — that yields a "
                "silent unfiltered total. Prefer template literal_values_count "
                "for how-many/count + equality, literal_values for list/show, "
                "literal_compare / related_entity_name_filter, or free-form "
                "with required MATCH / e.prop = $value (never OPTIONAL MATCH "
                "value filters without a post-WITH WHERE a IS NOT NULL)."
            )
        return None

    # Free-form / unknown template path.
    smell = optional_match_filter_smell(cypher)
    if smell:
        return smell

    if question_has_filter_intent(question) and pure_type_scan_without_filter(cypher):
        return (
            "question has filter intent but Cypher is a type-only scan with no "
            "constraining property filter — results would be a silent unfiltered "
            "total. Add a required filter: e.<prop> = $value, MATCH (not OPTIONAL) "
            "on Assertion with value predicate, or template literal_values_count "
            "(how-many) / literal_values (list); if using OPTIONAL MATCH for Assertion, follow with "
            "WITH e, a WHERE a IS NOT NULL (or WHERE raw IS NOT NULL after coalesce)."
        )

    return None


def filter_integrity_feedback(reason: str, *, previous_cypher: str = "") -> str:
    """Build LLM error_feedback for a filter-integrity rejection."""
    parts = [
        "FILTER INTEGRITY FAILURE (silent-wrong filter risk):",
        reason,
        "",
        "Rewrite rules (REQUIRED):",
        "1. Never put a required value filter only in WHERE after OPTIONAL MATCH "
        "on Assertion/Property — that does not drop primary entity rows.",
        "2. Prefer one of:",
        "   - template: literal_values_count (how-many/count + equality) or "
        "literal_values (list/show) or literal_compare (inequality) "
        "with $type_names, $prop_key, $prop_value / $op+$threshold",
        "   - entity denorm: MATCH (e:Entity {tenant_id:$tenant_id, kg:$kg})"
        "-[:INSTANCE_OF]->(c:Class …) WHERE c.name IN $type_names AND "
        "e.<prop_key> = $prop_value",
        "   - required MATCH: MATCH (a:Assertion {tenant_id:$tenant_id, kg:$kg, "
        "subject_id:e.id})-[:PREDICATE]->(p:Property) WHERE p.name = $prop_key "
        "AND a.literal_value = $prop_value",
        "   - OPTIONAL MATCH only if followed by WITH e, a WHERE a IS NOT NULL "
        "(or coalesce → WHERE raw IS NOT NULL AND raw = $prop_value)",
        "3. For filtered aggregates (sum/avg of X for status=Y): first constrain "
        "entities with a required filter, then aggregate — do not OPTIONAL-filter "
        "the status predicate.",
        "4. Fail closed: honest empty is better than an unfiltered total.",
    ]
    if previous_cypher and previous_cypher.strip():
        parts.extend(["", f"Rejected query was:\n{previous_cypher.strip()}"])
    return "\n".join(parts)


__all__ = [
    "check_cypher_filter_integrity",
    "cypher_has_constraining_filter",
    "filter_integrity_feedback",
    "optional_match_filter_smell",
    "pure_type_scan_without_filter",
    "question_has_filter_intent",
]
