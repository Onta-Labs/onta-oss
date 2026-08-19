"""Argmax-intent vs list/scalar-helper fail-closed (group-by SUM top-1).

``which X has the highest total Y`` must not execute a row list or a bare
SUM/AVG. Inspect the template *name* (executor body), not RETURN text.
"""

from __future__ import annotations

from infona_client.nlp.query_constraint_coverage_types import CoverageResult
from infona_client.nlp.query_intent import QueryIntentSketch

_ARGMAX_WRONG_TEMPLATES = frozenset(
    {
        "literal_values",
        "literal_values_count",
        "entities_of_type",
        "entities_of_type_count",
        "literal_aggregate",
    }
)


def argmax_vs_list_fail_closed(
    sketch: QueryIntentSketch,
    template: str | None,
) -> CoverageResult | None:
    """Reject which+highest-total when the named helper cannot return a dim."""
    if not getattr(sketch, "has_argmax_intent", False):
        return None
    tmpl = (template or "").strip()
    if tmpl not in _ARGMAX_WRONG_TEMPLATES:
        return None
    return CoverageResult(
        ok=False,
        confidence="low",
        reason=(
            f"argmax intent (which + highest/greatest/largest + total/sum) but "
            f"template {tmpl} cannot return a grouped top-1 dim — use "
            f"literal_argmax_by_dim ($group_key, $prop_key) or free-form "
            f"WITH grp, sum(num) ORDER BY total DESC LIMIT 1"
        ),
        fail_closed=True,
        sketch=sketch,
        extra={"argmax_vs_list": tmpl, "argmax_twin": "literal_argmax_by_dim"},
    )
