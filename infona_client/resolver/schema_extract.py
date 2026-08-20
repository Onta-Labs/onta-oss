from __future__ import annotations

"""LLM extraction + chunk recovery. Call SchemaResolver._extract; do not fork.
"""

import asyncio
import json
import time

import httpx

from infona_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionConstraint,
    ExtractionResult,
)
from infona_client.resolver.schema_extract_constraints import (
    _apply_extraction_constraint,
    _build_constraint_user_block,
)
from infona_client.resolver.schema_extract_prompts import (
    EXTRACTION_CONSTRAINT_SYSTEM,
    EXTRACTION_SYSTEM,
    EXTRACTION_TARGET_ATTR_CEILING,
    EXTRACTION_TARGET_SYSTEM,
    EXTRACTION_USER_TEMPLATE,
)
from infona_client.resolver.schema_grounding import _drop_ungrounded_attributes
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


class SchemaExtractMixin:
    """Extraction half of SchemaResolver — LLM call, recovery, concurrency."""

    async def _extract(
        self,
        content: str,
        content_type: str,
        existing_types: dict[str, str] | None = None,
        constraint: ExtractionConstraint | None = None,
        existing_attrs: dict[str, dict] | None = None,
    ) -> ExtractionResult:
        """Extract entities and relationships from raw content.

        ``constraint`` (ONTA-199) is OPT-IN and defaults to ``None``: with no
        constraint the system/user prompt is byte-for-byte the open-ended
        default and the result is returned untouched (the document/CSV/text
        path). An active constraint appends a type/attribute restriction to both
        prompts and drops any off-type entities / unrequested attributes the
        model still emits (the web-discovery path).

        ``existing_attrs`` (optional, dogfood S1): when provided, the prompt
        lists each type's known attributes so free-text follow-ups reuse
        ``description`` instead of inventing ``summary``.
        """
        if existing_types:
            lines: list[str] = []
            for name in existing_types:
                attrs = (existing_attrs or {}).get(name) or {}
                if attrs:
                    attr_names = ", ".join(sorted(attrs.keys())[:24])
                    lines.append(f"- {name} (use attrs: {attr_names})")
                else:
                    lines.append(f"- {name}")
            types_str = "\n".join(lines)
        else:
            types_str = "(none — this is a fresh ontology)"

        user_content = EXTRACTION_USER_TEMPLATE.format(
            content=content,
            existing_types=types_str,
        )
        # Discovery-only prompt narrowing. Inactive constraint → no change: the
        # system/user prompt AND the ``_extract_via_openrouter`` call are byte-for-
        # byte the pre-ONTA-199 default, so existing tests that patch
        # ``_extract_via_openrouter`` with a mock lacking a ``system_prompt``
        # parameter still pass (the no-op path never sends the kwarg).
        system_prompt = EXTRACTION_SYSTEM
        constraint_block = _build_constraint_user_block(constraint)
        _sys_kw: dict = {}
        if constraint_block:
            # SOFT (seed) → the target-schema PRIOR (decompose faithfully);
            # HARD (ONTA-199) → the flat single-type cage. Both narrow the prompt
            # but only HARD flattens. ONTA-382: soft + attributes_exhaustive
            # appends the attribute-ceiling note so the model sees the closed set.
            if getattr(constraint, "soft", False):
                constraint_system = EXTRACTION_TARGET_SYSTEM
                if getattr(constraint, "attributes_exhaustive", False):
                    constraint_system = constraint_system + EXTRACTION_TARGET_ATTR_CEILING
            else:
                constraint_system = EXTRACTION_CONSTRAINT_SYSTEM
            system_prompt = EXTRACTION_SYSTEM + constraint_system
            user_content = user_content + constraint_block
            _sys_kw = {"system_prompt": system_prompt}

        # ONTA-200: count the records in the chunk being extracted so the
        # per-call log below can be read against output-token size — a slow run
        # with bloated completions is diagnosable directly (records → tokens).
        # Only JSON chunks are a records array; free text has no record count.
        from infona_client.resolver.chunker import (
            estimate_tokens_per_record_from_input,
            json_array_len,
        )

        records_in_chunk = json_array_len(content) if content_type == "json" else None
        # ONTA-381: adaptive completion budget sized to this chunk's predicted
        # reified output (input-aware tokens/record × headroom), floored at the
        # base cap and clamped to the hard cap. Dense multi-attribute pages no
        # longer hit finish_reason=length at a flat 8192 while still sizing
        # batches proactively against the same density signal.
        tokens_per_record = (
            estimate_tokens_per_record_from_input(content)
            if content_type == "json"
            else None
        )
        completion_budget = self._completion_budget_for(
            records_in_chunk, tokens_per_record=tokens_per_record,
        )

        truncated = False
        finish_reason: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        provider = self.EXTRACT_PROVIDER if (
            self.EXTRACT_PROVIDER == "openrouter" and self._openrouter_key
        ) else "anthropic"

        # Time ONLY the LLM round-trip (not the JSON parse below) so duration_ms
        # attributes the latency to the model call itself.
        _t0 = time.perf_counter()
        if provider == "openrouter":
            # ``**_sys_kw`` carries the constraint-narrowed system prompt on a
            # discovery run (ONTA-199); empty on the open-ended document path.
            text, finish_reason, usage = await self._extract_via_openrouter(
                user_content, max_tokens=completion_budget, **_sys_kw,
            )
            # Honest truncation signal on the OpenRouter path, mirroring the
            # Anthropic ``stop_reason == "max_tokens"`` check below: OpenRouter
            # reports ``finish_reason == "length"`` when the model hit the token
            # ceiling mid-output, so the JSON is almost certainly incomplete.
            # Surfacing it lets a JSON chunk be split + retried instead of the
            # whole batch being silently dropped on the parse failure below.
            if finish_reason == "length":
                truncated = True
            if usage:
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
        else:
            from infona_client.offline import assert_online_host
            assert_online_host("api.anthropic.com", purpose="Anthropic extract")
            msg = await self._anthropic.messages.create(
                model=self.INFER_MODEL,
                max_tokens=completion_budget,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            text = msg.content[0].text
            finish_reason = getattr(msg, "stop_reason", None)
            # Explicit truncation signal from the Anthropic SDK: the model hit
            # the token ceiling mid-output, so the JSON is almost certainly
            # incomplete. Surface it so a JSON chunk can be split + retried
            # instead of silently dropping the whole batch.
            if finish_reason == "max_tokens":
                truncated = True
            msg_usage = getattr(msg, "usage", None)
            if msg_usage is not None:
                prompt_tokens = getattr(msg_usage, "input_tokens", None)
                completion_tokens = getattr(msg_usage, "output_tokens", None)
        duration_ms = (time.perf_counter() - _t0) * 1000.0

        # ONTA-200: ONE structured log per extraction LLM call. Pure
        # observability — no control-flow effect. Lets a slow discovery run
        # reveal output-token bloat directly (completion_tokens vs
        # records_in_chunk) instead of reconstructing it from request gaps.
        # ONTA-381 adds max_tokens so a truncated run is diagnosable against the
        # adaptive budget that was actually requested.
        _sr.logger.info(
            "extract_call",
            provider=provider,
            completion_tokens=completion_tokens,
            prompt_tokens=prompt_tokens,
            finish_reason=finish_reason,
            records_in_chunk=records_in_chunk,
            max_tokens=completion_budget,
            duration_ms=duration_ms,
        )

        try:
            # Strip code fences if present
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
                stripped = "\n".join(lines)
            data = json.loads(stripped)
            entities = [ExtractedEntity(**e) for e in data.get("entities", [])]
            relationships = [ExtractedRelationship(**r) for r in data.get("relationships", [])]
            result = ExtractionResult(
                entities=entities,
                relationships=relationships,
                source_text=content,
            )
            # ONTA-380: drop attribute families the source never stated (name AND
            # value both ungrounded) before the discovery constraint guard runs.
            # Model-agnostic defense-in-depth behind the prompt contract.
            result = _drop_ungrounded_attributes(result)
            # Discovery-only post-guard: inactive constraint returns unchanged.
            return _apply_extraction_constraint(result, constraint)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            # A parse failure on a TRUNCATED response is the expected symptom of
            # the output exceeding max_tokens (the recovery loop will split +
            # retry); log it distinctly so it isn't mistaken for a malformed
            # model reply.
            #
            # ``ValueError`` (added belt-and-suspenders) also covers
            # ``pydantic.ValidationError`` — a ``ValueError`` subclass — so a
            # NOVEL bad record shape the extractor returns (e.g. a value type the
            # models don't yet coerce) degrades to empty-extraction + split-retry
            # instead of hard-failing the whole discovery job. The systemic
            # fatal LLM errors (``LLMBillingError`` / ``LLMAuthError``, 402/401)
            # are NOT ``ValueError`` subclasses and are raised in the LLM call
            # ABOVE this try block, so they still propagate and abort the run
            # fast (ONTA-201) rather than being swallowed here.
            _sr.logger.warning(
                "extraction_parse_error",
                error=str(e),
                truncated=truncated,
                raw=text[:500],
            )
            return ExtractionResult(source_text=content)

    def _completion_budget_for(
        self,
        n_records: int | None,
        *,
        tokens_per_record: int | None = None,
    ) -> int:
        """Adaptive completion-token budget for one extraction call (ONTA-381).

        Scales with predicted reified output so a dense multi-record chunk gets
        enough room to finish clean JSON (no mid-stream ``finish_reason=length``)
        while staying under :attr:`EXTRACT_MAX_TOKENS_HARD`. Small / unknown
        record counts still receive the base :attr:`EXTRACT_MAX_TOKENS` ceiling.
        """
        from infona_client.resolver.chunker import adaptive_completion_tokens

        return adaptive_completion_tokens(
            n_records or 0,
            base_cap=self.EXTRACT_MAX_TOKENS,
            hard_cap=self.EXTRACT_MAX_TOKENS_HARD,
            tokens_per_record=tokens_per_record,
        )

    async def _extract_via_openrouter(
        self,
        user_content: str,
        system_prompt: str = EXTRACTION_SYSTEM,
        *,
        max_tokens: int | None = None,
    ) -> tuple[str, str | None, dict | None]:
        """Extract entities via OpenRouter, with primary→fallback routing.

        Returns ``(content, finish_reason, usage)``: ``finish_reason`` lets the
        caller detect a length-truncated reply (``"length"``) and route the chunk
        into split-and-retry instead of dropping it; ``usage`` (the OpenRouter
        ``prompt_tokens`` / ``completion_tokens`` object, or ``None``) is threaded
        back for per-call token accounting (ONTA-200) — previously discarded.

        ``system_prompt`` defaults to the open-ended :data:`EXTRACTION_SYSTEM`;
        a constrained (discovery) extraction passes the type/attribute-narrowed
        system prompt (ONTA-199).

        ``max_tokens`` is the adaptive completion budget (ONTA-381); when omitted
        the base :attr:`EXTRACT_MAX_TOKENS` ceiling is used so existing callers /
        mocks that don't pass the kwarg keep working.
        """
        return await _sr.openrouter_chat(
            self._openrouter_key,
            system_prompt,
            user_content,
            model=self.EXTRACT_MODEL,
            temperature=0,
            max_tokens=(
                max_tokens if max_tokens is not None else self.EXTRACT_MAX_TOKENS
            ),
            timeout=180 if _sr.is_reasoning_extract_model(self.EXTRACT_MODEL) else 60,
            return_finish_reason=True,
            return_usage=True,
        )

    # Floor below which a JSON chunk is no longer worth splitting: a handful of
    # records can't overflow max_tokens, so a still-empty extraction is a genuine
    # extraction failure to account for, not a truncation to recover.
    _RECOVERY_MIN_RECORDS = 3

    async def _extract_json_chunk_with_recovery(
        self,
        chunk: str,
        existing_types: dict[str, str],
        constraint: ExtractionConstraint | None = None,
    ) -> tuple[ExtractionResult, int]:
        """Extract one JSON-array chunk, RECOVERING from a silent batch loss.

        The reification/lift prompt makes each record emit many entities +
        relationships, so a dense chunk's JSON output can exceed the model's
        ``max_tokens``, get truncated, fail to parse, and return an EMPTY
        :class:`ExtractionResult` — silently dropping every record in the chunk.

        When that happens (zero entities extracted from a chunk that actually
        held records) we SPLIT the chunk's JSON array in half and retry each
        half, recursing down to :attr:`_RECOVERY_MIN_RECORDS`. Smaller chunks
        produce smaller outputs that fit under the cap. If a minimal chunk still
        yields nothing it is a real extraction failure: we log at ERROR and
        return its record count as ``dropped`` so the caller can surface it in
        row-conservation accounting instead of presenting the run as complete.

        Returns ``(merged_extraction, dropped_record_count)``.
        """
        from infona_client.resolver.chunker import split_json_array_chunk, json_array_len

        # A fatal billing/auth error (402/401) raised by the extraction LLM call
        # is SYSTEMIC — the next call fails identically — so it must NOT be
        # treated as a truncation to recover from. It is neither caught by
        # `_extract` (which only swallows JSON/parse errors) nor here, so it
        # propagates straight out of the recovery recursion and aborts the whole
        # ingest, instead of splitting the chunk and burning more doomed calls
        # (ONTA-201). Every other empty extraction still splits + retries below.
        #
        # Only forward ``constraint`` when it's active, so the default document
        # path calls ``_extract`` with the EXACT same argument shape as before
        # ONTA-199 (existing tests patch ``_extract`` with a mock that has no
        # ``constraint`` parameter — the no-op path must not pass the kwarg).
        _c = {"constraint": constraint} if constraint is not None else {}
        extraction = await self._extract(chunk, "json", existing_types, **_c)
        n_records = json_array_len(chunk)
        # Success, or a genuinely empty chunk (no records to lose) → nothing to recover.
        if extraction.entities or n_records == 0:
            return extraction, 0

        # Too small to split further: a few records can't overflow the token
        # cap, so this is a real extraction failure — account for the loss.
        if n_records <= self._RECOVERY_MIN_RECORDS:
            _sr.logger.error(
                "extraction_chunk_dropped",
                records=n_records,
                reason="empty_extraction_at_min_chunk",
            )
            return extraction, n_records

        halves = split_json_array_chunk(chunk)
        if not halves:
            # Couldn't split (not a parseable array) — count the loss.
            _sr.logger.error("extraction_chunk_dropped", records=n_records, reason="unsplittable")
            return extraction, n_records

        _sr.logger.warning(
            "extraction_chunk_split_retry", records=n_records, halves=len(halves),
        )
        merged_entities: list[ExtractedEntity] = []
        merged_relationships: list[ExtractedRelationship] = []
        seen_ids: set[str] = set()
        total_dropped = 0
        for half in halves:
            sub_extraction, sub_dropped = await self._extract_json_chunk_with_recovery(
                half, existing_types, **_c,
            )
            total_dropped += sub_dropped
            for e in sub_extraction.entities:
                if e.id not in seen_ids:
                    merged_entities.append(e)
                    seen_ids.add(e.id)
            merged_relationships.extend(sub_extraction.relationships)
        return (
            ExtractionResult(
                entities=merged_entities,
                relationships=merged_relationships,
                source_text=chunk[:500],
            ),
            total_dropped,
        )

    async def _extract_chunks_concurrently(
        self, extract_calls: list,
    ) -> list[ExtractionResult]:
        """Run per-chunk extraction coroutine-factories under a bounded semaphore.

        ONTA-197 item 3: independent chunks each take ~70s sequentially; running
        them concurrently under an :class:`asyncio.Semaphore` (size
        :attr:`EXTRACT_CONCURRENCY`) overlaps the LLM calls while capping how many
        are in flight. ``extract_calls`` is a list of zero-arg callables each
        returning the extraction coroutine for one chunk; results are returned in
        the SAME order as ``extract_calls`` (``asyncio.gather`` preserves input
        order regardless of completion order), so downstream merge/dedup stays
        deterministic. A tuple-returning factory (recovery: ``(result, dropped)``)
        is passed straight through unchanged.
        """
        sem = asyncio.Semaphore(max(1, self.EXTRACT_CONCURRENCY))

        async def _guarded(make_call):
            async with sem:
                return await make_call()

        return await asyncio.gather(*(_guarded(mk) for mk in extract_calls))

    async def _extract_json_chunks_calibrated(
        self,
        chunks: list[str],
        content: str,
        existing_types: dict[str, str],
        constraint: ExtractionConstraint | None = None,
    ) -> tuple[ExtractionResult, int, int]:
        """Extract multiple JSON chunks with first-batch calibration + concurrency.

        Composes ONTA-197 items 2 and 3 (see :meth:`ingest`):

          1. Extract chunk 1 SEQUENTIALLY (with recovery) — we need its result
             before we can learn the real per-record output size.
          2. CALIBRATE: estimate chunk 1's real output tokens from its serialized
             extraction, derive observed tokens-per-record (clamped to a floor so
             a fluke-light first batch can't oversize the rest), and RE-CHUNK the
             not-yet-processed remainder ONCE against that ratio. Sparse records
             (which the conservative ONTA-196 default over-shrinks) get larger,
             still cap-safe batches; dense records keep small batches, never
             reintroducing truncation.
          3. Extract the re-chunked remainder CONCURRENTLY under the semaphore,
             preserving order, per-chunk recovery, and dropped-record accounting.

        Returns ``(merged_extraction, rows_in, rows_dropped)``.
        """
        from infona_client.resolver.chunker import (
            json_array_len,
            chunk_json_array,
            estimate_output_tokens,
            calibrated_tokens_per_record,
        )

        merged_entities: list[ExtractedEntity] = []
        merged_relationships: list[ExtractedRelationship] = []
        seen_ids: set[str] = set()
        rows_in = 0
        rows_dropped = 0

        def _merge(ex: ExtractionResult) -> None:
            for e in ex.entities:
                if e.id not in seen_ids:
                    merged_entities.append(e)
                    seen_ids.add(e.id)
            merged_relationships.extend(ex.relationships)

        # --- Step 1: chunk 1, sequential, with recovery ------------------------
        first_chunk = chunks[0]
        first_records = json_array_len(first_chunk)
        rows_in += first_records
        first_ex, first_dropped = await self._extract_json_chunk_with_recovery(
            first_chunk, existing_types, constraint=constraint,
        )
        rows_dropped += first_dropped
        _merge(first_ex)

        # --- Step 2: calibrate + re-chunk the remainder ------------------------
        # The records NOT covered by chunk 1 (chunk_json_array splits in order, so
        # the remainder is exactly the tail of the original array past chunk 1).
        remainder_chunks = chunks[1:]
        observed_tokens = estimate_output_tokens(
            self._serialize_extraction_for_sizing(first_ex)
        )
        # Only re-chunk when chunk 1 actually produced something to learn from.
        # A fluke-empty/dropped first batch → keep the conservative sizing.
        if first_records > 0 and observed_tokens > 0:
            tpr = calibrated_tokens_per_record(observed_tokens, first_records)
            try:
                remainder_records = json.loads(content)[first_records:]
            except (json.JSONDecodeError, TypeError):
                remainder_records = None
            if remainder_records:
                rechunked = chunk_json_array(
                    json.dumps(remainder_records, default=str),
                    max_tokens=self.EXTRACT_MAX_TOKENS,
                    tokens_per_record=tpr,
                )
                remainder_chunks = rechunked
                _sr.logger.info(
                    "extract_calibrated_rechunk",
                    first_records=first_records,
                    observed_tokens=observed_tokens,
                    tokens_per_record=tpr,
                    remainder_records=len(remainder_records),
                    remainder_chunks=len(remainder_chunks),
                )

        if not remainder_chunks:
            return (
                ExtractionResult(
                    entities=merged_entities,
                    relationships=merged_relationships,
                    source_text=content[:500],
                ),
                rows_in,
                rows_dropped,
            )

        # --- Step 3: extract the remainder concurrently, preserving order ------
        for chunk in remainder_chunks:
            rows_in += json_array_len(chunk)
        results = await self._extract_chunks_concurrently(
            [
                lambda c=chunk: self._extract_json_chunk_with_recovery(
                    c, existing_types, constraint=constraint,
                )
                for chunk in remainder_chunks
            ]
        )
        for sub_ex, sub_dropped in results:
            rows_dropped += sub_dropped
            _merge(sub_ex)

        return (
            ExtractionResult(
                entities=merged_entities,
                relationships=merged_relationships,
                source_text=content[:500],
            ),
            rows_in,
            rows_dropped,
        )

    @staticmethod
    def _serialize_extraction_for_sizing(ex: ExtractionResult) -> str:
        """Serialize an extraction back to the model's JSON shape for size sizing.

        Calibration needs chunk 1's real OUTPUT size, but the extraction call
        site does not surface provider ``usage`` counts. Re-serializing the parsed
        entities + relationships to the same ``{"entities":[...],
        "relationships":[...]}`` document the model emitted is a faithful proxy
        for that output's length (the driver of :func:`estimate_output_tokens`).
        """
        try:
            return json.dumps(
                {
                    "entities": [e.model_dump() for e in ex.entities],
                    "relationships": [r.model_dump() for r in ex.relationships],
                },
                default=str,
            )
        except Exception:
            return ""
