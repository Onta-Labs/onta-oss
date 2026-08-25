"""Count-intent vs list-template fail-closed (list-vs-count class).

How-many / count questions must not execute a row-returning helper. The
executor prefers ``TEMPLATES[template].cypher``, so the *name* is the plan.
Inspect template name only — do not key off ``has_aggregate_intent`` (that
matches sum/total/avg and would reject argmax / top-k entity answers).
"""

from __future__ import annotations

from infona_client.nlp.query_constraint_coverage_types import CoverageResult
from infona_client.nlp.query_intent import QueryIntentSketch

# List helpers that already have a named count twin. Do not include
# literal_compare / related_entity_name_filter until those twins exist.
_COUNT_LIST_TWINS: dict[str, str] = {
    "literal_values": "literal_values_count",
    "entities_of_type": "entities_of_type_count",
}


def count_vs_list_fail_closed(
    sketch: QueryIntentSketch,
    template: str | None,
) -> CoverageResult | None:
    """Reject how-many + row helper so retry can name the count twin."""
    if "count" not in (sketch.aggregate_ops or ()):
        return None
    tmpl = (template or "").strip()
    twin = _COUNT_LIST_TWINS.get(tmpl)
    if not twin:
        return None
    return CoverageResult(
        ok=False,
        confidence="low",
        reason=(
            f"count intent but template {tmpl} returns entity rows — "
            f"use {twin} or free-form RETURN count(DISTINCT e) AS n "
            f"(do not set a list template name)"
        ),
        fail_closed=True,
        sketch=sketch,
        extra={"count_vs_list": tmpl, "count_twin": twin},
    )


def early_template_shape_fail_closed(
    sketch: QueryIntentSketch,
    template: str | None,
) -> CoverageResult | None:
    """Count-vs-list then argmax-vs-list. Single hook for the 549-line checker."""
    from infona_client.nlp.query_constraint_coverage_argmax import (
        argmax_vs_list_fail_closed,
    )

    return count_vs_list_fail_closed(sketch, template) or argmax_vs_list_fail_closed(
        sketch, template
    )
