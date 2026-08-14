"""CSV schema inference, then deterministic mapping for all rows (no LLM per
row).

Implementation lives in sibling ``csv_*.py`` modules. Every previously
importable name is re-exported here.

Ids are slugged via the shared ``_safe_id``; the write path (not this
module) mints entity IRIs with ``entity_uri`` and inserts via
``insert_facts``.
"""

from __future__ import annotations

import os

import anthropic
import httpx  # noqa: F401 — tests / mixins
import structlog

from infona_client.graph.ontology_queries import _safe_id
from infona_client.resolver.csv_apply import CSVApplyMixin
from infona_client.resolver.csv_extensions import (  # noqa: F401
    _ConstantEdgePlan,
    _ExtensionApplier,
    _PromotionPlan,
    _TypeConstantPlan,
    _build_extension_plans,
    _column_is_entity_key,
    _find_source_column,
    _promotion_skip_reason,
    _sanitize_ontology_extensions,
)
from infona_client.resolver.csv_helpers import (  # noqa: F401
    _as_confidence,
    _as_violation,
    _cell,
    _check_complete_shape,
    _check_reason_shape,
    _check_refute_shape,
    _datatype_from_profile,
    _decide_text_kind,
    _is_int,
    _is_opaque_identifier,
    _pascal_case,
    _rank_sample_rows,
    _rel_values,
    _snake_case,
    _strip_code_fences,
    _synthetic_key,
)
from infona_client.resolver.csv_infer import CSVInferMixin
from infona_client.resolver.csv_llm import CSVLlmMixin
from infona_client.resolver.csv_mapping import (  # noqa: F401
    AppliedMapping,
    _chunked,
    _v2_enabled,
    _v2_max_tokens,
)
from infona_client.resolver.csv_prompts import (  # noqa: F401
    COMPLETE_SYSTEM,
    COMPLETE_USER,
    CSV_SCHEMA_SYSTEM,
    CSV_SCHEMA_USER,
    REASON_SYSTEM,
    REASON_USER,
    REFUTE_SYSTEM,
    REFUTE_USER,
)
from infona_client.resolver.csv_prompts_wide import (  # noqa: F401
    COLUMN_ASSIGN_SYSTEM,
    COLUMN_ASSIGN_USER,
    ENTITY_SYSTEM,
    ENTITY_USER,
)
from infona_client.resolver.csv_reason import CSVReasonMixin
from infona_client.resolver.csv_reconcile import (  # noqa: F401
    _drop_redundant_promotions,
    _owner_type_name,
    _resnake_mapping_names,
    format_existing_ontology_for_prompt,
    reconcile_mapping_to_existing,
)
from infona_client.resolver.llm_router import openrouter_chat  # noqa: F401
from infona_client.resolver.profiler import profile_table  # noqa: F401
from infona_client.resolver.sensitivity import (  # noqa: F401
    redact_privileged_profile_examples,
    redact_privileged_sample_rows,
)

logger = structlog.stdlib.get_logger("infona.resolver.csv")

MAX_INFERENCE_COLUMNS = int(os.environ.get("INFONA_CSV_MAX_INFERENCE_COLUMNS", "40"))
_V2_BASE_MAX_TOKENS = 8192
_V2_MAX_TOKENS_CAP = 32768
_V2_TOKENS_PER_COLUMN = 100
_WIDE_CHUNK_CONCURRENCY = 5
COMPLETION_REVIEW_THRESHOLD = 0.7


def _csv_safe_id(raw: str) -> str:
    """Shared sanitizer — entity IRIs via ontology_queries._safe_id."""
    return _safe_id(raw)


class CSVResolver(CSVInferMixin, CSVReasonMixin, CSVLlmMixin, CSVApplyMixin):
    """CSV schema inference + deterministic row mapping."""

    SCHEMA_MODEL_DEFAULT = "openai/gpt-5.6-luna"
    EXTRACT_MODEL = os.environ.get("INFONA_CSV_SCHEMA_MODEL", SCHEMA_MODEL_DEFAULT)
    EXTRACT_PROVIDER = os.environ.get("INFONA_EXTRACT_PROVIDER", "openrouter")
    INFER_MODEL = os.environ.get("INFONA_INFER_MODEL", "claude-opus-4-8")

    def __init__(self, client: anthropic.AsyncAnthropic, openrouter_key: str = ""):
        self._client = client
        self._openrouter_key = openrouter_key or os.environ.get("OPENROUTER_API_KEY", "")
