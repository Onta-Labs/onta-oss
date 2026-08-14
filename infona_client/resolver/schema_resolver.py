"""Schema Resolver — orchestrator between LLM extraction and the graph write.

Pipeline:
  Raw data → LLM extraction (non-deterministic) → Schema Resolver → graph

This module keeps the public ``SchemaResolver`` type and every previously
importable name. Implementation lives in sibling ``schema_*.py`` modules
(extract, ingest, ontology, entity write). Do not fork a second write path —
instance facts go through ``insert_facts``; relationship edges on
``onto/<leaf>``; entity IRIs via ``entity_uri``.
"""

from __future__ import annotations

import asyncio
import os

import anthropic
import structlog

from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE, TYPE_URI_PREFIX  # noqa: F401 — public re-export
from infona_client.graph.kg_writer import build_graph_delta, insert_facts  # noqa: F401
from infona_client.graph.ontology_commit import (
    commit_ontology,
    commit_ontology_unlocked,
    ontology_write_lock,
)
from infona_client.graph.ontology_queries import (
    PRIMITIVE_TYPES,
    TEXT_KIND_FREE_TEXT,
    TEXT_KIND_NOT_TEXT,
    batch_entity_exists_query,
    entities_by_key_value_query,
    entity_exists_query,
    entity_uri as _entity_uri,
    get_full_ontology_query,
    ontology_version,
    parent_map_query,
    type_uri,
    attr_uri,
)
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.models.ontology import OntologyMutation, OntologyOpKind  # noqa: F401
from infona_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat  # noqa: F401
from infona_client.resolver.models import (  # noqa: F401 — public re-exports
    AttrAction,
    CleanFact,
    CleanOutcome,
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    ExtractionConstraint,
    ExtractionResult,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    IngestResult,
    KeyJoin,
    MatchVerdict,
    RejectedValue,
    ValidatedTriple,
    ValidationOutcome,
    assert_soft_a2,
    soft_a2_from_structured_rows,
    validate_soft_a2,
)
from infona_client.resolver.type_matcher import TypeMatcher
from infona_client.resolver.verdict_cache import JsonVerdictCache
from infona_client.verification.verifier import _policy_enabled, verify_clean_facts  # noqa: F401

logger = structlog.stdlib.get_logger("infona.resolver")

# ONTA-394: collapse accidental subtypes of the confirmed discovery focus into
# the focus type itself (default ON). Set ``INFONA_DISCOVERY_COLLAPSE_SUBTYPES=0``
# to restore the ONTA-383 anchor-as-subtype behavior without a redeploy.
_DISCOVERY_COLLAPSE_SUBTYPES = (
    os.environ.get("INFONA_DISCOVERY_COLLAPSE_SUBTYPES", "1") != "0"
)

# ONTA-394: kill-switch for the node-label plausibility gate (default ON).
# Set INFONA_NODE_LABEL_GUARD=0 to restore pre-ONTA-394 "always mint the node".
_NODE_LABEL_GUARD = os.environ.get("INFONA_NODE_LABEL_GUARD", "1") != "0"

# --- sibling helpers (stable import path: from schema_resolver import NAME) ---
from infona_client.resolver.schema_extract_prompts import (  # noqa: E402
    EXTRACTION_CONSTRAINT_SYSTEM,
    EXTRACTION_CONSTRAINT_USER_TEMPLATE,
    EXTRACTION_SYSTEM,
    EXTRACTION_TARGET_ATTR_CEILING,
    EXTRACTION_TARGET_SYSTEM,
    EXTRACTION_TARGET_USER_CEILING_TEMPLATE,
    EXTRACTION_TARGET_USER_TEMPLATE,
    EXTRACTION_USER_TEMPLATE,
)
from infona_client.resolver.schema_focus import (  # noqa: E402
    _apply_soft_focus_floor,
    _is_metric_attribute,
    _is_standards_concept_type,
    _primary_entity_ids,
    _split_type_tokens,
    _BARE_STANDARD_TOKENS,
    _LEADING_NUMBER_RE,
    _METRIC_NAME_SUBSTRINGS,
    _NUMERIC_DATATYPES,
    _STANDARDS_CONCEPT_TOKENS,
)
from infona_client.resolver.schema_extract_constraints import (  # noqa: E402
    _apply_attribute_ceiling,
    _apply_extraction_constraint,
    _build_constraint_user_block,
    _drop_offplan_compound_attributes,
    _is_implausible_node_label,
    _CEILING_IDENTITY_ATTRS,
    _NAV_JUNK_RE,
    _YEAR_RANGE_RE,
)
from infona_client.resolver.schema_grounding import (  # noqa: E402
    _attribute_grounded_in_source,
    _drop_ungrounded_attributes,
    _is_fabricated_placeholder,
    _looks_like_url,
    _name_grounded_in_source,
    _value_grounded_in_source,
    _ATTR_NAME_STOPWORDS,
    _MIN_GROUNDED_VALUE_LEN,
    _MIN_PLACEHOLDER_DIGITS,
    _PLACEHOLDER_FILLER_TOKENS,
    _PLACEHOLDER_RUN_CHARS,
    _SEQ_DIGITS_ASC,
    _SEQ_DIGITS_DESC,
)
from infona_client.resolver.schema_ingest_struct import (  # noqa: E402
    _STRUCTURED_PROVENANCE_COLS,
    _project_structured_rows_to_attributes,
    _structured_rows_mapping,
)
from infona_client.resolver.schema_text import (  # noqa: E402
    TEXT_CANDIDACY_SYSTEM,
    TEXT_CANDIDACY_USER,
    _TEXT_ADJUDICATION_SAMPLE_MAX_LEN,
    _TEXT_ADJUDICATION_SAMPLES,
    _TEXT_EVIDENCE_MAX_VALUES,
)

# Mixins after helpers so ``import schema_resolver as _sr`` inside them sees
# logger / insert_facts / flags already bound.
from infona_client.resolver.schema_extract import SchemaExtractMixin  # noqa: E402
from infona_client.resolver.schema_ontology import SchemaOntologyMixin  # noqa: E402
from infona_client.resolver.schema_type_resolve import SchemaTypeResolveMixin  # noqa: E402
from infona_client.resolver.schema_governance import SchemaGovernanceMixin  # noqa: E402
from infona_client.resolver.schema_ingest import SchemaIngestMixin  # noqa: E402
from infona_client.resolver.schema_ingest_mapped import SchemaIngestMappedMixin  # noqa: E402
from infona_client.resolver.schema_ingest_flush import SchemaIngestFlushMixin  # noqa: E402
from infona_client.resolver.schema_entity_write import SchemaEntityWriteMixin  # noqa: E402
from infona_client.resolver.schema_entity_insert import SchemaEntityInsertMixin  # noqa: E402
from infona_client.resolver.schema_text import SchemaTextMixin  # noqa: E402


def _mint_entity_uri(type_name: str, raw_id: str) -> str:
    """The only entity mint — ``entity_uri(type, raw_id)``. Do not hand-build IRIs."""
    return _entity_uri(type_name, raw_id)


async def _write_facts(neptune, instance_graph, triples, **kwargs):
    """Shared write path — always ``insert_facts``. Do not fork a second flush."""
    return await insert_facts(
        neptune,
        instance_graph,
        triples,
        store=kwargs.pop("store", resolve_optional_graph_store()),
        **kwargs,
    )


class SchemaResolver(
    SchemaExtractMixin,
    SchemaOntologyMixin,
    SchemaTypeResolveMixin,
    SchemaGovernanceMixin,
    SchemaIngestMixin,
    SchemaIngestMappedMixin,
    SchemaIngestFlushMixin,
    SchemaEntityWriteMixin,
    SchemaEntityInsertMixin,
    SchemaTextMixin,
):
    """Deterministic layer: extract → resolve types/attrs → insert_facts."""

    EXTRACT_MODEL = os.environ.get("INFONA_EXTRACT_MODEL", PRIMARY_MODEL)
    EXTRACT_PROVIDER = os.environ.get("INFONA_EXTRACT_PROVIDER", "openrouter")
    INFER_MODEL = os.environ.get("INFONA_INFER_MODEL", "claude-opus-4-8")
    ONTOLOGY_REFRESH_INTERVAL = int(os.environ.get("INFONA_ONTOLOGY_REFRESH_INTERVAL", "50"))
    EXTRACT_MAX_TOKENS = int(os.environ.get("INFONA_EXTRACT_MAX_TOKENS", "16384"))
    EXTRACT_MAX_TOKENS_HARD = int(
        os.environ.get("INFONA_EXTRACT_MAX_TOKENS_HARD", "32768")
    )
    EXTRACT_CONCURRENCY = int(os.environ.get("INFONA_EXTRACT_CONCURRENCY", "5"))

    def __init__(
        self,
        neptune: NeptuneClient,
        anthropic_key: str,
        verdict_cache: JsonVerdictCache,
        embedding_service: object | None = None,
        ontology_lock: asyncio.Lock | None = None,
        verify_policy: object | None = None,
    ):
        self._neptune = neptune
        self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
        self._embedding_service = embedding_service
        # ONTA-268: ontology-write lock. Serializes the read-decide-write of
        # ontology EXISTENCE (type/subtype/attribute/range creation) so several
        # per-sub-query resolvers ingesting concurrently can't race on
        # type-creation (which fragments the ontology). SHAREABLE: pass ONE lock
        # to every per-sub-query resolver in a discovery job (web_ingest_cap) and
        # their ontology mutations serialize against each other; default is a
        # private lock so a standalone resolver still guards its own critical
        # sections. Only the ontology existence read-decide-write is guarded —
        # NOT the LLM extraction (`_extract`), and NOT the instance-data write
        # (which is per-sub-query by construction). asyncio.Lock is NOT reentrant,
        # so the guarded methods never nest a second acquisition (see `_resolve_type`
        # / `_locked_ontology_update`).
        self._ontology_lock = ontology_lock or ontology_write_lock()
        from infona_client.config import settings
        self._openrouter_key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._type_matcher = TypeMatcher(self._openrouter_key, verdict_cache, embedding_service)
        # Cross-file entity resolution. Best-effort: failures never block ingest.
        from infona_client.resolver.er import ERPipeline
        self._er = ERPipeline(neptune)
        self._er_enabled = os.environ.get("INFONA_ER_ENABLED", "1") != "0"
        # Per-fact provenance (ADR 0002 §4): statement-metadata nodes in the
        # companion provenance graph. Default OFF so default triple output and
        # Neptune call pattern stay byte-identical.
        self._provenance_enabled = os.environ.get("INFONA_PROVENANCE_ENABLED", "0") == "1"
        # Per-attribute DISPLAY provenance companions (ONTA-245 F1): the same
        # attr_meta `source_url` / `verified_at` instance companions enrichment
        # always writes (metadata namespace, never ontology attributes — ONTA-262),
        # emitted by discovery too so a DISCOVERED fact and an
        # ENRICHED fact are provenance-symmetric (attribute-level, not just the
        # per-record `onto/source`). Default OFF so bulk CSV ingest stays byte-stable
        # (it would otherwise add up to 3 companions PER attribute fact); web
        # discovery flips it on to give the personas the per-fact citation + freshness
        # signal. Flows through the SAME shared write path (insert_facts) as every
        # other fact — the companions ride in the instance-triple collector.
        self._attr_provenance_enabled = (
            os.environ.get("INFONA_DISCOVERY_ATTR_PROVENANCE", "0") == "1"
        )
        # Governance seam (ADR 0002 §2): when ON, a brand-new type is ALSO
        # proposed to an LLM judge panel; on majority approval it is written
        # to the Global-Public layer with governance provenance. The tenant
        # write stays today's behavior either way — governance never blocks
        # or gates ingest. Default OFF (matching INFONA_PROVENANCE_ENABLED).
        self._governance_enabled = os.environ.get("INFONA_GOVERNANCE_ENABLED", "0") == "1"
        if self._governance_enabled:
            from infona_client.resolver.governance import GovernanceEngine, LLMJudgePanel
            self._governance = GovernanceEngine(neptune)
            self._judge_panel = LLMJudgePanel(self._openrouter_key)
        # Background governance tasks (COG-46): the judge panel + Public-layer
        # write are scheduled off the ingest path; references are retained
        # here so drain_governance() can await them deterministically.
        self._governance_tasks: list[asyncio.Task] = []
        # child->parent (type-name) map for subclass-chain walks. Built once per
        # ingest from parent_map_query and mutated in-place as new subtypes are
        # created so later entities in the same batch can climb the chain.
        # ONTA-268: on the reentrant ingest path this is threaded call-locally
        # (see `ingest`/`_resolve_and_insert` `parent_of=` params); the instance
        # attribute remains the fallback for legacy direct-call sites (the
        # `/ingest/csv/rows` route and unit tests that seed it directly).
        self._parent_of: dict[str, str] = {}
        # ONTA-370: A4 Verify policy — the OPT-IN gate for the verify seam wedged
        # between the A3 clean ledger and the write. DEFAULT None => verification
        # is OFF: the seam short-circuits with ZERO cost (no verifier, no
        # iteration, no LLM/network) and the write stays byte-identical. A caller
        # that resolves a `VerifyPolicy` for the tenant/type hands it in here to
        # turn the seam on; duck-typed (`object | None`) so this module never
        # imports the policy type — the shared `_policy_enabled` reads its
        # `mode`/`enabled`. Mirrors the other DEFAULT-OFF opt-ins above
        # (`_provenance_enabled` / `_attr_provenance_enabled`).
        self._verify_policy = verify_policy
