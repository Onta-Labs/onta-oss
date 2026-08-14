"""Wide-table REASON pass + v2 mapping conversion."""

from __future__ import annotations

import asyncio
import json

from infona_client.resolver.csv_prompts_wide import (
    COLUMN_ASSIGN_SYSTEM,
    COLUMN_ASSIGN_USER,
    ENTITY_SYSTEM,
    ENTITY_USER,
)
import structlog

from infona_client.resolver.csv_extensions import _sanitize_ontology_extensions
from infona_client.resolver.csv_helpers import (
    _TEXT_KIND_UNSET,
    _as_confidence,
    _as_violation,
    _check_reason_shape,
    _datatype_from_profile,
    _decide_text_kind,
    _pascal_case,
    _snake_case,
    _strip_code_fences,
)
from infona_client.resolver.csv_mapping import _chunked, _v2_max_tokens
from pydantic import ValidationError

from infona_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    EntityRelationSpec,
    EntitySpec,
    InferenceAudit,
    RejectedSlot,
)

logger = structlog.stdlib.get_logger("infona.resolver.csv")


def _host():
    from infona_client.resolver import csv_resolver as _mod

    return _mod


class CSVReasonMixin:
    """Wide REASON + convert-v2."""

    async def _reason_wide(
        self,
        headers: list[str],
        profile: TableProfile,
        profile_json: str,
        types_str: str,
        ranked_samples: list[dict],
        sample_str: str,
    ) -> dict:
        """COG-58 wide-table REASON: split the single per-column pass into a
        global entity-decomposition call plus chunked column-assignment calls,
        then merge into the standard ``{entities, columns, relationships}``
        shape that :func:`_check_reason_shape` validates and :meth:`_convert_v2`
        consumes.

        No single call's output scales with the total column count: the entity
        pass emits only the (small) entity list, and each column pass emits tags
        for at most :data:`_host().MAX_INFERENCE_COLUMNS` columns. Coverage is
        guaranteed deterministically — any column the model drops is backfilled
        as an attribute of the first entity, so every header is tagged exactly
        once (row conservation, ADR 0003 §2). The chunk calls run concurrently
        under a small semaphore so a very wide table doesn't fan out unbounded.
        """
        import asyncio

        # --- Pass B1: global entity decomposition (output bounded by entity count).
        entity_user = ENTITY_USER.format(
            rows_profiled=profile.rows_profiled,
            total_rows=profile.total_rows,
            profile=profile_json,
            n=len(ranked_samples),
            sample_rows=sample_str,
            existing_types=types_str,
            n_columns=len(headers),
        )

        def _check_entities(data: dict) -> None:
            if not isinstance(data, dict):
                raise KeyError("entity output must be a JSON object")
            ents = data.get("entities")
            if not isinstance(ents, list) or not ents:
                raise KeyError("entities")
            for e in ents:
                if not isinstance(e, dict) or not e.get("type_name"):
                    raise KeyError("entities[].type_name")

        try:
            decomposition = await self._call_llm_v2(
                ENTITY_SYSTEM, entity_user, temperature=0.0, max_tokens=_host()._V2_BASE_MAX_TOKENS,
            )
            _check_entities(decomposition)
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("csv_entity_validation_retry", error=str(e))
            decomposition = await self._call_llm_v2(
                ENTITY_SYSTEM, entity_user, temperature=0.3, max_tokens=_host()._V2_BASE_MAX_TOKENS,
            )
            _check_entities(decomposition)

        entities = decomposition["entities"]
        relationships = decomposition.get("relationships") or []
        # Every entity needs a stable name handle for column assignment + convert.
        for e in entities:
            if not e.get("name"):
                e["name"] = _snake_case(e["type_name"])
        entity_names = [e["name"] for e in entities]
        valid_names = set(entity_names)
        default_owner = entity_names[0]

        entities_brief = "\n".join(
            f'- "{e["name"]}" (type {e["type_name"]}, key '
            f'{e.get("key_strategy") or "?"}: '
            f'{", ".join(e.get("key_columns") or []) or "none"})'
            for e in entities
        )

        profile_dict = profile.to_prompt_dict()
        all_col_profiles = profile_dict.get("columns", {})

        # --- Pass B2: chunked column assignment (output bounded by chunk size).
        chunks = _chunked(headers, _host().MAX_INFERENCE_COLUMNS)
        sem = asyncio.Semaphore(_host()._WIDE_CHUNK_CONCURRENCY)

        def _check_cols(data: dict) -> None:
            if not isinstance(data, dict):
                raise KeyError("column output must be a JSON object")
            cols = data.get("columns")
            if not isinstance(cols, list) or not cols:
                raise KeyError("columns")

        async def _assign_chunk(chunk: list[str]) -> list[dict]:
            chunk_profile = {
                "rows_profiled": profile_dict.get("rows_profiled"),
                "total_rows": profile_dict.get("total_rows"),
                "columns": {h: all_col_profiles.get(h, {}) for h in chunk},
            }
            chunk_samples = "\n".join(
                json.dumps({h: row.get(h) for h in chunk}, default=str)
                for row in ranked_samples
            )
            user = COLUMN_ASSIGN_USER.format(
                entities=entities_brief,
                rows_profiled=profile.rows_profiled,
                total_rows=profile.total_rows,
                profile=json.dumps(chunk_profile),
                n=len(ranked_samples),
                sample_rows=chunk_samples,
            )
            tokens = _v2_max_tokens(len(chunk))
            async with sem:
                try:
                    data = await self._call_llm_v2(
                        COLUMN_ASSIGN_SYSTEM, user, temperature=0.0, max_tokens=tokens,
                    )
                    _check_cols(data)
                except (ValidationError, KeyError, ValueError) as e:
                    logger.warning("csv_column_assign_retry", error=str(e), chunk=len(chunk))
                    data = await self._call_llm_v2(
                        COLUMN_ASSIGN_SYSTEM, user, temperature=0.3, max_tokens=tokens,
                    )
                    _check_cols(data)
            return [
                c for c in data.get("columns", [])
                if isinstance(c, dict) and c.get("column")
            ]

        chunk_results = await asyncio.gather(*[_assign_chunk(c) for c in chunks])

        # Merge + coverage repair. First tag per column wins; unknown owners are
        # reassigned to the default entity; any untagged header is backfilled as
        # an attribute so EVERY column is tagged exactly once (row conservation).
        header_set = set(headers)
        columns: list[dict] = []
        seen: set[str] = set()
        for chunk_cols in chunk_results:
            for col in chunk_cols:
                name = col["column"]
                if name in seen or name not in header_set:
                    continue
                if col.get("entity") not in valid_names:
                    col["entity"] = default_owner
                columns.append(col)
                seen.add(name)

        tagged = len(seen)
        for h in headers:
            if h not in seen:
                columns.append({
                    "column": h,
                    "role": "attribute",
                    "entity": default_owner,
                    "predicate_or_attr": _snake_case(h),
                    "why": "backfilled — model did not tag this column",
                    "confidence": 0.3,
                })
                seen.add(h)

        proposed = {
            "entities": entities,
            "columns": columns,
            "relationships": relationships,
        }
        _check_reason_shape(proposed)
        logger.info(
            "csv_reason_wide",
            columns=len(headers),
            chunks=len(chunks),
            entities=[e["type_name"] for e in entities],
            backfilled=len(headers) - tagged,
        )
        return proposed

    def _convert_v2(
        self,
        corrected: dict,
        violations: list[dict],
        profile: TableProfile,
        extensions: OntologyExtensions | None = None,
        text_candidacy: dict[str, TextCandidacy] | None = None,
    ) -> CSVSchemaMapping:
        """Convert a (corrected) Pass B/C schema into the ``CSVSchemaMapping``
        contract consumed by ``apply_mapping`` and the web Explorer.

        - ``key_strategy: "column"`` → ``EntitySpec.id_column`` (first key
          column); ``"composite"`` → ``EntitySpec.id_from``; ``"synthetic"``
          → neither, so ``apply_mapping`` mints deterministic content-hash
          keys per row (COG-51).
        - ``role: "key"`` columns become regular ATTRIBUTE columns owned by
          their entity — identity is carried by the owning ``EntitySpec``,
          and COG-51 guarantees the key value is also emitted as a queryable
          attribute (refute template 6, LOST KEY).
        - ``role: "relationship"`` columns become out-of-row RELATIONSHIP
          columns (dimension target + edge), exactly like the legacy contract.
        - Datatypes are derived deterministically from the Pass A value-shape
          evidence (the v2 schema carries none — the profile already measured
          the values).
        - ``text_kind`` (ONTA-177/ONTA-173): attribute columns carry the
          free-text candidacy verdict via :func:`_decide_text_kind` —
          deterministic ``"free_text"`` for unambiguously long prose, the
          REASON pass's name-informed adjudication for the ambiguous band
          (``"free_text"``, or the durable decided-no ``"not_text"`` when the
          pass explicitly declined), and NEVER set when the name-blind
          classifier found the column non-text-shaped (the profiler is
          authoritative over candidacy; an LLM-volunteered ``text_kind`` on a
          code/number column is discarded). Relationship columns become
          edges, not literals — no marker. Old recordings without the field
          simply leave the ambiguous band UNDECIDED (no marker of either
          polarity).
        """
        text_candidacy = text_candidacy or {}
        specs: list[EntitySpec] = []
        for ent in corrected.get("entities", []):
            type_name = ent["type_name"]
            name = ent.get("name") or _snake_case(type_name)
            strategy = ent.get("key_strategy")
            key_columns = [c for c in (ent.get("key_columns") or []) if c]
            id_column: str | None = None
            id_from: list[str] | None = None
            if strategy == "column" and key_columns:
                id_column = key_columns[0]
            elif strategy == "composite" and key_columns:
                id_from = key_columns
            elif strategy in ("column", "composite"):
                # Keyed strategy declared with no key columns — degrade to a
                # synthetic key (row conservation) rather than dropping rows.
                logger.warning(
                    "csv_v2_key_strategy_degraded", entity=name, declared=strategy,
                )
                strategy = "synthetic"
            elif strategy != "synthetic":
                # Unknown/missing strategy: infer it from the key columns.
                if len(key_columns) == 1:
                    strategy, id_column = "column", key_columns[0]
                elif key_columns:
                    strategy, id_from = "composite", key_columns
                else:
                    strategy = "synthetic"
            specs.append(EntitySpec(
                name=name,
                type_name=type_name,
                id_column=id_column,
                id_from=id_from,
                key_strategy=strategy,
                confidence=_as_confidence(ent.get("confidence")),
                why=ent.get("why"),
            ))

        spec_names = {s.name for s in specs}
        default_owner = specs[0].name if len(specs) == 1 else None

        columns: list[ColumnMapping] = []
        for col in corrected.get("columns", []):
            column_name = col["column"]
            role = str(col.get("role") or "attribute").strip().lower()
            owner = col.get("entity") or default_owner
            if owner not in spec_names:
                # Repair: a flat (single-entity) schema may omit owners.
                logger.warning(
                    "csv_v2_unowned_column", column=column_name, entity=owner,
                )
                owner = default_owner
            raw_attr = col.get("predicate_or_attr")
            attr_name = _snake_case(raw_attr) if raw_attr else _snake_case(column_name)
            shared = {
                "column_name": column_name,
                "attribute_name": attr_name,
                "entity": owner,
                "confidence": _as_confidence(col.get("confidence")),
                "why": col.get("why"),
            }
            if role == "relationship":
                target = col.get("target_type") or _pascal_case(attr_name)
                columns.append(ColumnMapping(
                    role=ColumnRole.RELATIONSHIP,
                    target_type=target,
                    datatype="string",
                    **shared,
                ))
            else:
                # "key" and "attribute" both land as attribute columns: the
                # identity half of a key column lives on its EntitySpec.
                columns.append(ColumnMapping(
                    role=ColumnRole.ATTRIBUTE,
                    datatype=_datatype_from_profile(profile.column(column_name)),
                    text_kind=_decide_text_kind(
                        text_candidacy.get(column_name),
                        # Sentinel default: an ABSENT field (old recording /
                        # backfilled column) is undecided; an explicit null is
                        # a genuine adjudicated NO (→ durable "not_text").
                        col.get("text_kind", _TEXT_KIND_UNSET),
                    ),
                    **shared,
                ))

        relationships: list[EntityRelationSpec] = []
        for rel in corrected.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            subject, predicate, obj = rel.get("subject"), rel.get("predicate"), rel.get("object")
            if not (subject and predicate and obj):
                continue
            if subject not in spec_names or obj not in spec_names:
                logger.warning(
                    "csv_v2_dangling_relationship", subject=subject, object=obj,
                )
                continue
            relationships.append(EntityRelationSpec(
                subject=subject, predicate=predicate, object=obj, why=rel.get("why"),
            ))

        mapping = CSVSchemaMapping(
            # entity_type is ignored in multi-entity mode; keep it meaningful
            # for older readers that only look at the headline type.
            entity_type=specs[0].type_name,
            columns=columns,
            entities=specs,
            relationships=relationships or None,
            violations=[_as_violation(v) for v in violations],
            inference_audit=InferenceAudit(
                pipeline="reason_refute_v2",
                rows_profiled=profile.rows_profiled,
                total_rows=profile.total_rows,
                profile=profile.to_prompt_dict(),
            ),
            ontology_extensions=extensions,
        )
        # Drop dual-world Pass D promotions (own key / FK relationship columns)
        # before the mapping leaves inference — held-for-review UX, ontology
        # pre-registration, and apply_mapping all consume this field.
        if mapping.ontology_extensions is not None:
            mapping = mapping.model_copy(update={
                "ontology_extensions": _sanitize_ontology_extensions(
                    mapping, mapping.ontology_extensions,
                ),
            })
        return mapping

