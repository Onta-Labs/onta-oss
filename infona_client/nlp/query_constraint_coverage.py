"""Post-gen constraint coverage + query confidence (filter-miss class).

Companion to :mod:`cypher_filter_integrity` (OPTIONAL MATCH smell). This module
checks whether generated Cypher **covers** NL constraints:

* filter intent / extracted filter tokens present in the question
* plan is not a silent unfiltered aggregate / pure type count when filters
  were asked for
* multi-constraint tokens not dropped wholesale
* **dim-registry unique binds** (leaf+value) actually applied — not merely that
  a token string appears next to a *different* leaf (wrong-leaf / multi-filter
  drop class)
* **zero-instance primary types** when live inventory is provided: plan must
  not target only empty pollution types while the question matches other
  *populated* types (high-conf empty totals class)

**Confidence** (attached to timing / NLResult):

* ``high`` — integrity OK + coverage OK
* ``medium`` — ran with soft gaps (plan has a dimension filter but multi-token
  coverage is partial: ≥1 token bound, not all)
* ``low`` — coverage fail, integrity fail, or ambiguous multi-bind with no
  usable filter

**Fail-closed:** never recommend executing an unfiltered aggregate/count when
the question has filter intent (or unbound filter tokens). Prefer clarification
over a silent wrong total. Unique registry binds on aggregate/count plans are
required predicates: missing any unique bind → fail-closed (even if some other
dimension filter is present). When ``populated_types`` / ``type_counts`` is
supplied, a plan whose primary types all have 0 entities while the question
matched other populated types is also fail-closed (retry with inventory
feedback) so we never ship ``0 @ high conf`` for a pollution type.

Anti-overfit: synthetic types/attrs/values only in tests; no persona gold.

Implementation lives in sibling ``query_constraint_coverage_*.py`` modules.
Every previously importable name is re-exported here.
"""

from __future__ import annotations

from infona_client.nlp.query_constraint_coverage_check import (  # noqa: F401
    check_constraint_coverage,
)
from infona_client.nlp.query_constraint_coverage_argmax import (  # noqa: F401
    argmax_vs_list_fail_closed,
)
from infona_client.nlp.query_constraint_coverage_count import (  # noqa: F401
    count_vs_list_fail_closed,
    early_template_shape_fail_closed,
)
from infona_client.nlp.query_constraint_coverage_unique import (  # noqa: F401
    unique_count_wrong_grain,
)
from infona_client.nlp.query_constraint_coverage_dim import (  # noqa: F401
    _dim_bind_label,
    _leaf_present_in_plan,
    _param_nonempty,
    _plan_blob,
    _plan_is_aggregate_or_count,
    _token_variants,
    _value_present_in_plan,
    effective_has_dim_filter,
    plan_covers_dim_bind,
    plan_has_dimension_filter,
    plan_has_distinct_count,
    split_dim_binds_coverage,
    tokens_bound_in_plan,
)
from infona_client.nlp.query_constraint_coverage_feedback import (  # noqa: F401
    assign_query_confidence,
    build_clarification_prompt,
    coverage_feedback,
    fail_closed_answer,
)
from infona_client.nlp.query_constraint_coverage_populated import (  # noqa: F401
    _normalize_type_set,
    plan_primary_types,
    resolve_populated_type_set,
    zero_instance_type_coverage,
)
from infona_client.nlp.query_constraint_coverage_types import (  # noqa: F401
    CoverageResult,
    DimBindLike,
    QueryConfidence,
    _AGG_RETURN_RE,
    _DIM_FILTER_TEMPLATES,
    _DIM_PARAM_KEYS,
    _DIM_VALUE_IN_CYPHER_RE,
    _DimEntryLike,
    _DimValueLike,
    _MEASURE_ONLY_TEMPLATES,
    _PURE_TYPE_TEMPLATES,
    _host,
)

__all__ = [
    "CoverageResult",
    "DimBindLike",
    "QueryConfidence",
    "assign_query_confidence",
    "build_clarification_prompt",
    "check_constraint_coverage",
    "argmax_vs_list_fail_closed",
    "count_vs_list_fail_closed",
    "early_template_shape_fail_closed",
    "unique_count_wrong_grain",
    "coverage_feedback",
    "fail_closed_answer",
    "effective_has_dim_filter",
    "plan_covers_dim_bind",
    "plan_has_dimension_filter",
    "plan_has_distinct_count",
    "plan_primary_types",
    "resolve_populated_type_set",
    "split_dim_binds_coverage",
    "tokens_bound_in_plan",
    "zero_instance_type_coverage",
]
