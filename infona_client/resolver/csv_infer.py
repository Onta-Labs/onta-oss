"""Schema inference entry + legacy / v2 orchestrators.

Looks up ``_host().MAX_INFERENCE_COLUMNS`` / ``openrouter_chat`` / logger on
``csv_resolver`` at call time so existing monkeypatches keep working.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from infona_client.resolver.csv_prompts import (
    COMPLETE_SYSTEM,
    COMPLETE_USER,
    CSV_SCHEMA_SYSTEM,
    CSV_SCHEMA_USER,
    REASON_SYSTEM,
    REASON_USER,
    REFUTE_SYSTEM,
    REFUTE_USER,
)
import structlog

from infona_client.resolver.csv_mapping import _v2_enabled
from infona_client.resolver.csv_reconcile import (
    format_existing_ontology_for_prompt,
    reconcile_mapping_to_existing,
)
from infona_client.graph.ontology_queries import TEXT_KIND_FREE_TEXT
from infona_client.resolver.csv_extensions import _sanitize_ontology_extensions
from infona_client.graph.text_markers import classify_text_candidacy
from infona_client.resolver.csv_helpers import (
    _check_complete_shape,
    _check_reason_shape,
    _check_refute_shape,
    _rank_sample_rows,
    _snake_case,
    _strip_code_fences,
)
from infona_client.resolver.csv_mapping import _v2_max_tokens
from infona_client.resolver.models import ColumnRole, CSVSchemaMapping, InferenceAudit
from infona_client.resolver.sensitivity import (
    redact_privileged_profile_examples,
    redact_privileged_sample_rows,
)
from infona_client.resolver.profiler import profile_table

logger = structlog.stdlib.get_logger("infona.resolver.csv")


def _host():
    from infona_client.resolver import csv_resolver as _mod

    return _mod


class CSVInferMixin:
    """infer_schema + legacy / v2 pipelines."""

    async def infer_schema(
        self,
        headers: list[str],
        sample_rows: list[dict[str, str]],
        existing_types: dict[str, str],
        total_rows: int = 0,
        existing_attrs: dict[str, dict[str, AttributeSchema]] | None = None,
    ) -> CSVSchemaMapping:
        """Infer column-to-ontology mapping from sample rows.

        Default (``INFONA_CSV_INFERENCE_V2`` unset or truthy): the ADR 0003
        evidence-grounded pipeline — deterministic profile (Pass A), REASON
        LLM call (Pass B), adversarial REFUTE LLM call (Pass C), conceptual
        COMPLETE LLM call (Pass D), then conversion to the same
        :class:`CSVSchemaMapping` contract the legacy path returns (extended
        with optional ``key_strategy``/``why``/``confidence``/``violations``/
        ``inference_audit``/``ontology_extensions`` fields).

        ``INFONA_CSV_INFERENCE_V2=0``: the legacy single-LLM-call path,
        verbatim — including its NAME_HINTS / FORCE_RELATIONSHIP post-hoc
        patches, which the v2 pipeline deliberately retires (ADR 0003 §4).

        ``existing_attrs`` (per-type property snapshot from
        :meth:`SchemaResolver._fetch_ontology`) is fed into the LLM prompts
        AND a deterministic post-reconcile so CSV expansion reuses existing
        properties (e.g. Drug.manufacturer) instead of minting parallel
        names (manufactured_by). Callers that omit it keep greenfield
        behavior.

        Each LLM call keeps the existing retry contract: one retry at
        temperature 0.3 when the response fails validation, then propagate
        (the /ingest/csv/schema route converts that into its 422 guidance).

        If the v2 pipeline still fails after its internal retry (common on
        dense one-row demos where the model omits ``entities``), fall back to
        the legacy single-call path rather than 422 — then reconcile.
        """
        if _v2_enabled():
            try:
                mapping = await self._infer_schema_v2(
                    headers, sample_rows, existing_types, total_rows, existing_attrs,
                )
            except KeyError as e:
                # Only the REASON/REFUTE shape failure KeyError("entities") (or
                # "columns") falls back. Complete-pass ValidationError /
                # other KeyErrors still propagate so their retry contracts
                # stay testable and strict.
                # Observed live: dense one-row multi-hop CSVs (Opdualag demo)
                # where REASON omits a non-empty entities[] twice → 422.
                key = e.args[0] if e.args else None
                if key not in ("entities", "columns"):
                    raise
                logger.warning(
                    "csv_schema_v2_failed_fallback_legacy",
                    error=str(e),
                    columns=len(headers),
                    rows=total_rows or len(sample_rows),
                )
                mapping = await self._infer_schema_legacy(
                    headers, sample_rows, existing_types, total_rows, existing_attrs,
                )
        else:
            mapping = await self._infer_schema_legacy(
                headers, sample_rows, existing_types, total_rows, existing_attrs,
            )
        return reconcile_mapping_to_existing(mapping, existing_types, existing_attrs)

    async def _infer_schema_legacy(
        self,
        headers: list[str],
        sample_rows: list[dict[str, str]],
        existing_types: dict[str, str],
        total_rows: int = 0,
        existing_attrs: dict[str, dict[str, AttributeSchema]] | None = None,
    ) -> CSVSchemaMapping:
        """Legacy single-call inference (pre-ADR 0003), kept verbatim behind
        ``INFONA_CSV_INFERENCE_V2=0``: one LLM call with one retry at higher
        temperature if the response fails validation."""
        types_str = format_existing_ontology_for_prompt(existing_types, existing_attrs)

        # Prefer rows with the most non-empty fields. CSVs whose leading rows
        # are mostly-empty (e.g. `status=deleted` records with only slug+url)
        # otherwise feed the LLM a near-blank sample, which reliably produces
        # malformed JSON keys (observed: `column118 name`).
        ranked_samples = _rank_sample_rows(sample_rows)[:10]
        # Never send privileged column VALUES (ssn, secret*, privileged*, …)
        # to the LLM — keep keys so the model can still invent a mapping.
        ranked_samples = redact_privileged_sample_rows(ranked_samples)
        sample_str = "\n".join(
            json.dumps(row, default=str) for row in ranked_samples
        )

        user_content = CSV_SCHEMA_USER.format(
            columns=", ".join(headers),
            n=len(ranked_samples),
            total=total_rows or len(sample_rows),
            sample_rows=sample_str,
            existing_types=types_str,
        )

        try:
            data = await self._call_llm(user_content, temperature=0.0)
            mapping = self._build_mapping(data)
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("csv_schema_validation_retry", error=str(e))
            data = await self._call_llm(user_content, temperature=0.3)
            mapping = self._build_mapping(data)

        # In multi-entity mode, ids come from the EntitySpec specs (not a
        # type_id column), so the single-entity type_id enforcement below is
        # skipped. The geographic/entity promotion pass still runs (columns keep
        # their `entity` owner).
        multi = mapping.entities is not None

        # Validate: must have exactly one type_id (single-entity mode only)
        if not multi:
            id_cols = [c for c in mapping.columns if c.role == ColumnRole.TYPE_ID]
            if len(id_cols) != 1:
                logger.warning("csv_schema_no_id", id_cols=len(id_cols))
                # Fallback: use first column as ID
                if mapping.columns:
                    mapping.columns[0].role = ColumnRole.TYPE_ID

        # Post-processing: if the chosen type_id is numeric, prefer a string
        # column with a name-like label (institution, title, name, etc.)
        # Numeric IDs cause deduplication when values repeat.
        id_col = None if multi else next((c for c in mapping.columns if c.role == ColumnRole.TYPE_ID), None)
        if id_col and id_col.datatype in ("integer", "float"):
            NAME_HINTS = {"name", "title", "institution", "series_title", "label", "id"}
            for col in mapping.columns:
                col_key = (col.attribute_name or col.column_name).lower().replace(" ", "_")
                if col_key in NAME_HINTS and col.role != ColumnRole.TYPE_ID:
                    logger.info(
                        "csv_type_id_override",
                        old=id_col.column_name,
                        new=col.column_name,
                        reason="numeric ID replaced with name-like column",
                    )
                    id_col.role = col.role
                    col.role = ColumnRole.TYPE_ID
                    break

        # Post-processing: enforce entity-first for known geographic/entity columns
        # The LLM sometimes ignores the prompt and treats these as string attributes
        FORCE_RELATIONSHIP = {
            # Geographic
            "city": "City",
            "state": "State",
            "country": "Country",
            "region": "Region",
            "zipcode": "ZipCode",
            "zip_code": "ZipCode",
            "zip": "ZipCode",
            "postal_code": "PostalCode",
            "county": "County",
            "district": "District",
            "neighborhood": "Neighborhood",
            "area": "Area",
            # People
            "owner": "Person",
            "agent": "Person",
            "broker": "Person",
            "manager": "Person",
            "seller": "Person",
            "buyer": "Person",
            "author": "Person",
            "creator": "Person",
            # Organizations
            "company": "Company",
            "brokerage": "Company",
            "firm": "Company",
            "agency": "Company",
            "school": "School",
            "university": "University",
        }
        for col in mapping.columns:
            col_key = (col.attribute_name or col.column_name).lower().replace(" ", "_")
            if col.role == ColumnRole.ATTRIBUTE and col_key in FORCE_RELATIONSHIP:
                col.role = ColumnRole.RELATIONSHIP
                col.target_type = FORCE_RELATIONSHIP[col_key]
                col.datatype = "string"
                logger.info("csv_column_promoted", column=col.column_name, target_type=col.target_type)

        logger.info(
            "csv_schema_inferred",
            entity_type=mapping.entity_type,
            columns=len(mapping.columns),
            relationships=sum(1 for c in mapping.columns if c.role == ColumnRole.RELATIONSHIP),
        )
        return mapping

    # --- ADR 0003 v2 pipeline: profile → reason → refute → complete --------

    async def _infer_schema_v2(
        self,
        headers: list[str],
        sample_rows: list[dict[str, str]],
        existing_types: dict[str, str],
        total_rows: int = 0,
        existing_attrs: dict[str, dict[str, AttributeSchema]] | None = None,
    ) -> CSVSchemaMapping:
        """Evidence-grounded inference (ADR 0003 Passes A–D).

        Pass A profiles the provided rows deterministically; Pass B (REASON)
        proposes a schema grounded in that profile with per-decision
        ``why``/``confidence``; Pass C (REFUTE) adversarially checks it
        against the six structural failure templates and corrects it; Pass D
        (COMPLETE) makes each type conceptually coherent — dependent-entity
        promotions plus constitutive core slots under the three hard tests,
        capped at 3 per type. The corrected schema is converted to the
        existing ``CSVSchemaMapping`` contract, with the completion output on
        ``ontology_extensions``. No post-hoc keyword patches run on this path;
        property-level reuse of an existing ontology is enforced afterwards by
        :func:`reconcile_mapping_to_existing` (called from :meth:`infer_schema`).
        """
        profile = profile_table(headers, sample_rows, total_rows)
        # Scrub privileged examples (top-N values) before they enter any prompt.
        redact_privileged_profile_examples(profile)
        profile_json = json.dumps(profile.to_prompt_dict())
        types_str = format_existing_ontology_for_prompt(existing_types, existing_attrs)

        # Same density ranking as the legacy path: the sample exists for value
        # context only — statistics come from the profile. Redact privileged
        # column VALUES before the LLM sees them (keys kept for mapping).
        ranked_samples = redact_privileged_sample_rows(
            _rank_sample_rows(sample_rows)[:6]
        )
        sample_str = "\n".join(json.dumps(row, default=str) for row in ranked_samples)

        # COG-58: scale each pass's output budget to the column count so the
        # REFUTE/COMPLETE echoes (which still carry the whole schema) aren't
        # truncated on wide tables.
        max_tokens = _v2_max_tokens(len(headers))

        # Pass B — REASON. Narrow tables: one call. Wide tables (COG-58): a
        # global entity-decomposition pass plus chunked column-assignment passes
        # so no single call must emit a per-column tag for every column.
        if len(headers) > _host().MAX_INFERENCE_COLUMNS:
            proposed = await self._reason_wide(
                headers, profile, profile_json, types_str, ranked_samples, sample_str,
            )
        else:
            reason_user = REASON_USER.format(
                rows_profiled=profile.rows_profiled,
                total_rows=profile.total_rows,
                profile=profile_json,
                n=len(ranked_samples),
                sample_rows=sample_str,
                existing_types=types_str,
            )
            # Retry once at temperature 0.3, then propagate.
            try:
                proposed = await self._call_llm_v2(
                    REASON_SYSTEM, reason_user, temperature=0.0, max_tokens=max_tokens,
                )
                _check_reason_shape(proposed)
            except (ValidationError, KeyError, ValueError) as e:
                logger.warning("csv_reason_validation_retry", error=str(e))
                proposed = await self._call_llm_v2(
                    REASON_SYSTEM, reason_user, temperature=0.3, max_tokens=max_tokens,
                )
                _check_reason_shape(proposed)

        refute_user = REFUTE_USER.format(
            rows_profiled=profile.rows_profiled,
            total_rows=profile.total_rows,
            profile=profile_json,
            schema=json.dumps(proposed),
        )

        # Pass C — REFUTE (same retry contract).
        try:
            refuted = await self._call_llm_v2(
                REFUTE_SYSTEM, refute_user, temperature=0.0, max_tokens=max_tokens,
            )
            violations, corrected = _check_refute_shape(refuted, proposed)
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("csv_refute_validation_retry", error=str(e))
            refuted = await self._call_llm_v2(
                REFUTE_SYSTEM, refute_user, temperature=0.3, max_tokens=max_tokens,
            )
            violations, corrected = _check_refute_shape(refuted, proposed)

        complete_user = COMPLETE_USER.format(
            rows_profiled=profile.rows_profiled,
            total_rows=profile.total_rows,
            profile=profile_json,
            schema=json.dumps(corrected),
        )

        # Pass D — COMPLETE (same retry contract; a >3-core-slot response
        # fails pydantic validation here, triggering the retry).
        try:
            completed = await self._call_llm_v2(
                COMPLETE_SYSTEM, complete_user, temperature=0.0, max_tokens=max_tokens,
            )
            extensions = _check_complete_shape(completed)
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("csv_complete_validation_retry", error=str(e))
            completed = await self._call_llm_v2(
                COMPLETE_SYSTEM, complete_user, temperature=0.3, max_tokens=max_tokens,
            )
            extensions = _check_complete_shape(completed)

        # ONTA-177: name-blind free-text candidacy per header, computed from the
        # same sample the profile was (profiler proposes; the REASON output's
        # text_kind field only adjudicates WITHIN these proposals — see
        # _decide_text_kind for the gate).
        text_candidacy = {
            h: classify_text_candidacy([row.get(h) for row in sample_rows])
            for h in headers
        }

        mapping = self._convert_v2(
            corrected, violations, profile, extensions, text_candidacy=text_candidacy,
        )
        logger.info(
            "csv_schema_inferred_v2",
            entities=[e.type_name for e in (mapping.entities or [])],
            columns=len(mapping.columns),
            free_text=[
                c.column_name for c in mapping.columns
                if c.text_kind == TEXT_KIND_FREE_TEXT
            ],
            violations=[v.template for v in mapping.violations],
            promotions=[
                t.type_name for t in extensions.types if t.promoted_from_attribute
            ],
            held_for_review=[
                t.type_name for t in extensions.types
                if t.held_for_review or any(s.held_for_review for s in t.core_slots)
            ],
        )
        return mapping

