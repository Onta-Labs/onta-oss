from __future__ import annotations

"""Verify seam, truth-verdict companions, and free-text candidacy.

Job: A4 verify (default-off), persist TruthVerdict companions on
``attr_meta/`` (never declared in the ontology), and mark free-text
attributes. Do not fork a second verify or companion writer.
"""

import json

from infona_client.graph.ontology_queries import TEXT_KIND_FREE_TEXT, TEXT_KIND_NOT_TEXT
from infona_client.graph.provenance import build_truth_verdict_companion
from infona_client.graph.text_markers import (
    TextCandidacy,
    classify_text_candidacy,
    invalidate_for_graph as invalidate_text_marker_cache,
)
from infona_client.resolver.attribute_resolver import _normalize_attr_name
from infona_client.resolver.models import (
    CSVSchemaMapping,
    ColumnRole,
    ExtractedEntity,
    IngestResult,
)
from infona_client.verification.verifier import _policy_enabled, verify_clean_facts
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr

# --- ONTA-177: free-text candidacy adjudication (the REASON layer) ----------
# The name-blind classifier (graph/text_markers.classify_text_candidacy —
# profiler ValueShape.TEXT proposes, ADR 0003 litmus) hands the AMBIGUOUS band
# to this prompt: text-shaped attributes whose values could equally be prose
# or structured strings (addresses, org names, composite titles). This is the
# ONE layer where the attribute NAME may be consulted. Verdicts become
# `<attr> <onto/textKind> "free_text"` ontology markers for the semantic
# instance index (ONTA-173) and its query-side filter (ONTA-176).

TEXT_CANDIDACY_SYSTEM = """\
You adjudicate FREE-TEXT candidacy for knowledge-graph attributes feeding a semantic \
(meaning-based) search index. Every candidate below is text-SHAPED (multi-word string \
values) but not obviously prose. Using each attribute's NAME plus its sample values, \
decide whether its values are free-running PROSE — descriptions, reviews, speeches, \
notes, transcripts, summaries, commentary — worth semantic indexing. Structured strings \
are NOT free text: postal addresses, person or organization names, titles used as \
identifiers or labels, delimited value lists, codes or paths containing spaces.

Respond with strict JSON only:
{"attributes":[{"type":"<TypeName>","attribute":"<attr_name>","free_text":true|false,"why":"<brief>"}]}
Include EVERY candidate exactly once. JSON only."""

TEXT_CANDIDACY_USER = """\
Candidate attributes (each with up to {n_samples} sample values):
{candidates}

Return the adjudication JSON now."""

#: Per-attribute cap on collected sample values for candidacy evidence — keeps
#: memory bounded on large batches; the shape statistics stabilize long before
#: this many samples.
_TEXT_EVIDENCE_MAX_VALUES = 50
#: How many sample values (truncated) each ambiguous attribute contributes to
#: the adjudication prompt.
_TEXT_ADJUDICATION_SAMPLES = 5
_TEXT_ADJUDICATION_SAMPLE_MAX_LEN = 140


class SchemaTextMixin:
    """Verify + free-text candidacy half of SchemaResolver."""

    def _verify_clean_facts(
        self,
        result: IngestResult,
        *,
        workspace_id: str | None,
        run_id: str | None,
    ) -> None:
        """A4 Verify seam (ONTA-370) — the OPT-IN wedge between the A3 clean
        ledger and the write.

        **DEFAULT-OFF is load-bearing.** With no ``VerifyPolicy`` configured (the
        default, ``self._verify_policy is None``) the very FIRST check
        short-circuits and returns: no verifier is constructed, ``result.clean_report``
        is not iterated, no LLM / network / cost / latency is incurred, and
        ``result.verified_facts`` stays its empty default. The written graph and
        the rest of the returned :class:`IngestResult` are therefore byte-identical
        to a build without this seam. Even the offline
        :class:`~infona_client.verification.verifier.DefaultOfflineVerifier` is NOT
        run on the default path — "off" means the seam does nothing at all.
        Verification is strictly OPT-IN, exactly like the ``_provenance_enabled`` /
        ``_attr_provenance_enabled`` seams above.

        When a policy turns it ON, the A3 :class:`CleanFact`\\ s the ONTA-373 ledger
        already collected (passed + transformed + dropped) are handed to the shared
        Wave-6 orchestrator :func:`verify_clean_facts` under the run envelope's
        ``workspace_id`` / ``run_id`` (ONTA-372); the resulting
        :class:`~infona_client.verification.types.VerifiedFact`\\ s (verdict +
        independent evidence + confidence + A4 lineage) are stamped on the result.
        Verification is READ-ONLY and sits BEFORE the write — it never forks the
        converged writer (:func:`insert_facts`); the facts still flow through the
        shared write path below unchanged.
        """
        policy = self._verify_policy
        # FIRST and ONLY thing evaluated on the default path. `_policy_enabled` is
        # the SAME gate the orchestrator uses, so the seam can't drift from it.
        if not _policy_enabled(policy):
            return
        # --- opt-in path only past this point ---
        report = result.clean_report
        a3_facts = [*report.passed, *report.transformed, *report.dropped]
        if not a3_facts:
            return
        # Thread the real run scope when we have it; fall back to the
        # orchestrator's own "local" defaults (its ArtifactEnvelope rejects an
        # empty workspace_id/run_id) when a direct caller threaded none.
        scope: dict[str, str] = {}
        if workspace_id:
            scope["workspace_id"] = workspace_id
        if run_id:
            scope["run_id"] = run_id
        try:
            result.verified_facts = verify_clean_facts(a3_facts, policy, **scope)
        except Exception:
            # A misbehaving verifier must never fail an otherwise-successful write:
            # the seam sits before the write but degrades to "no verdicts", never a
            # rollback (mirrors the best-effort embedding / free-text seams). The
            # FactVerifier contract already requires fail-closed; this is defense.
            _sr.logger.warning("verify_seam_failed", exc_info=True)

    def _verdict_companion_triples(
        self,
        result: IngestResult,
        entity_uri_map: dict[str, str],
        entity_type_map: dict[str, str],
    ) -> list[tuple[str, str, str]]:
        """A4 verdict PERSIST (ONTA-375) — stamp each verified fact's epistemic
        ``TruthVerdict`` as a per-attribute ``attr_meta/`` companion.

        DEFAULT-OFF passthrough: with no ``VerifyPolicy`` enabled the A4 seam left
        ``result.verified_facts`` empty (the common path), so this returns ``[]`` and
        the write is byte-identical — no companion is minted. When the seam produced
        verdicts, each written fact's ``TruthVerdict`` is minted via the SHARED
        companion minter (:func:`build_truth_verdict_companion`) onto an INTERNAL
        ``attr_meta/`` predicate (``is_internal_predicate`` True), so it is invisible
        to Explorer/type-stats/NL dumps yet stays queryable by the P7 answer layer.
        The triples ride the SAME shared write path (they are appended to the
        instance-triple collector) — never a bespoke insert.

        Skips DROPPED facts (``value is None`` — no domain triple was written, so
        there is nothing to attach a verdict to) and any fact whose entity did not
        resolve to a URI/type in this batch. The verdict is a per-attribute signal
        keyed by ``(subject, Type, attribute)`` — matching how the surface-form /
        display companions are keyed."""
        verified = getattr(result, "verified_facts", None)
        if not verified:
            return []
        out: list[tuple[str, str, str]] = []
        for vf in verified:
            if vf.value is None:  # DROPPED — no domain fact to annotate.
                continue
            entity_uri = entity_uri_map.get(vf.entity_id)
            type_name = entity_type_map.get(vf.entity_id)
            if not entity_uri or not type_name:
                continue
            verdict = vf.verdict.value if hasattr(vf.verdict, "value") else str(vf.verdict)
            out.extend(
                build_truth_verdict_companion(entity_uri, type_name, vf.attribute, verdict)
            )
        return out
    async def _mark_free_text_attributes(
        self,
        graph_uri: str,
        text_values: dict[tuple[str, str], list[str]],
        result: IngestResult,
    ) -> None:
        """Decide + persist free-text candidacy for schema-pass attributes.

        The seam lives HERE (not only in the CSV resolver) so every ingest
        modality that runs a schema pass — text, JSON ``/ingest``, and
        web-discovery — produces ``textKind`` markers, not just CSV
        (ONTA-177: candidacy must not be CSV-only).

        Two-tier decision, mirroring the CSV pipeline's:

        1. Name-blind classification of the sampled values
           (:func:`classify_text_candidacy` — the profiler's ``ValueShape.TEXT``
           proposes; ADR 0003 litmus: no attribute-name inspection here).
           Unambiguously long prose is marked directly.
        2. The AMBIGUOUS band (text-shaped but borderline: could be addresses,
           org names, composite titles) goes to ONE LLM adjudication call
           (:meth:`_adjudicate_free_text`) — the REASON layer, the only place
           the attribute NAME may be consulted.

        Confirmed attributes get the single-valued, idempotent
        ``<attr> <onto/textKind> "free_text"`` upsert; attributes the LLM
        EXPLICITLY declined get the durable decided-no ``"not_text"`` upsert
        (ONTA-173: an unpersisted NO is indistinguishable from never-decided —
        the reconciler would re-sample it every run and its name-blind
        ≥120-char auto tier could later overrule the LLM). Non-candidates
        (non-TEXT shapes) are never marked at all — absence = not-a-candidate,
        and the reconciler's cheap heuristic re-classifies them itself. Both
        upserts are written alongside the other schema-apply attribute upserts,
        and the tenant's marker cache is invalidated HERE (the write site owns
        it — refresh_after_write deliberately doesn't). Best-effort throughout:
        any failure logs a warning and never blocks or fails the ingest (the
        ONTA-181 reconciler heuristic can revisit undecided attributes).
        """
        try:
            auto: list[tuple[str, str]] = []
            ambiguous: dict[tuple[str, str], list[str]] = {}
            for (type_name, attr_name), values in text_values.items():
                verdict = classify_text_candidacy(values)
                if verdict is TextCandidacy.FREE_TEXT:
                    auto.append((type_name, attr_name))
                elif verdict is TextCandidacy.AMBIGUOUS:
                    ambiguous[(type_name, attr_name)] = values
            confirmed: set[tuple[str, str]] = set(auto)
            declined: set[tuple[str, str]] = set()
            if ambiguous:
                adjudicated_yes, adjudicated_no = await self._adjudicate_free_text(
                    ambiguous
                )
                confirmed |= adjudicated_yes
                declined |= adjudicated_no - confirmed
            for type_name, attr_name in sorted(confirmed):
                await self._commit_ontology(graph_uri, [
                    self._mut_text_kind(type_name, attr_name, TEXT_KIND_FREE_TEXT),
                ])
                result.free_text_attributes.append(f"{type_name}.{attr_name}")
            for type_name, attr_name in sorted(declined):
                await self._commit_ontology(graph_uri, [
                    self._mut_text_kind(type_name, attr_name, TEXT_KIND_NOT_TEXT),
                ])
            if confirmed or declined:
                # Marker write site self-invalidates (mirrors the reconciler's
                # heuristic) so query-side consumers see the fresh verdicts
                # before the TTL; the TTL stays the cross-process backstop.
                invalidate_text_marker_cache(graph_uri)
                _sr.logger.info(
                    "free_text_attributes_marked",
                    auto=len(auto),
                    adjudicated=len(confirmed) - len(auto),
                    declined=len(declined),
                    attributes=sorted(f"{t}.{a}" for t, a in confirmed),
                    not_text_attributes=sorted(f"{t}.{a}" for t, a in declined),
                )
        except Exception:
            _sr.logger.warning("free_text_marking_failed", exc_info=True)

    async def _adjudicate_free_text(
        self, candidates: dict[tuple[str, str], list[str]],
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        """One REASON-layer LLM call adjudicating AMBIGUOUS free-text candidates.

        This is the only layer where the attribute NAME is consulted
        (ADR 0003 keeps names out of the deterministic layers; ONTA-177).
        Returns ``(confirmed, declined)`` — the ``(type_name, attr_name)``
        pairs the model judged free-running prose, and the pairs it EXPLICITLY
        judged not (``free_text`` falsy in its response). Both sets are
        filtered to the offered candidate set — the model cannot mint (or
        decline) candidacy for attributes the name-blind classifier never
        proposed. A candidate absent from the response stays UNDECIDED (in
        neither set): a genuine adjudication is required before ONTA-173's
        durable ``not_text`` marker may be persisted. Fail-closed and
        best-effort: any LLM/parse failure returns two empty sets (attributes
        stay unmarked AND undecided; a later re-ingest or the ONTA-181
        reconciler heuristic gets another look), never raises.
        """
        try:
            lines = []
            for (type_name, attr_name), values in sorted(candidates.items()):
                samples = [
                    v[:_TEXT_ADJUDICATION_SAMPLE_MAX_LEN]
                    for v in values[:_TEXT_ADJUDICATION_SAMPLES]
                ]
                lines.append(json.dumps({
                    "type": type_name,
                    "attribute": attr_name,
                    "sample_values": samples,
                }))
            user_content = TEXT_CANDIDACY_USER.format(
                n_samples=_TEXT_ADJUDICATION_SAMPLES,
                candidates="\n".join(lines),
            )
            if self.EXTRACT_PROVIDER == "openrouter" and self._openrouter_key:
                text = await _sr.openrouter_chat(
                    self._openrouter_key,
                    TEXT_CANDIDACY_SYSTEM,
                    user_content,
                    model=self.EXTRACT_MODEL,
                    temperature=0,
                    max_tokens=2048,
                    timeout=60,
                )
            else:
                from infona_client.offline import assert_online_host
                assert_online_host("api.anthropic.com", purpose="Anthropic text candidacy")
                msg = await self._anthropic.messages.create(
                    model=self.INFER_MODEL,
                    max_tokens=2048,
                    system=TEXT_CANDIDACY_SYSTEM,
                    messages=[{"role": "user", "content": user_content}],
                )
                text = msg.content[0].text
            stripped = text.strip()
            if stripped.startswith("```"):
                stripped = "\n".join(
                    l for l in stripped.split("\n") if not l.strip().startswith("```")
                )
            data = json.loads(stripped)
            confirmed: set[tuple[str, str]] = set()
            declined: set[tuple[str, str]] = set()
            for item in data.get("attributes", []):
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("type")), str(item.get("attribute")))
                if key not in candidates:
                    continue  # offered candidates only — never mint new ones
                if item.get("free_text"):
                    confirmed.add(key)
                else:
                    # An entry the model returned with free_text falsy is a
                    # genuine adjudicated NO — persisted durably by the caller.
                    declined.add(key)
            _sr.logger.info(
                "free_text_adjudicated",
                candidates=len(candidates),
                confirmed=len(confirmed),
                declined=len(declined),
            )
            return confirmed, declined
        except Exception:
            _sr.logger.warning(
                "free_text_adjudication_failed",
                candidates=len(candidates),
                exc_info=True,
            )
            return set(), set()

    async def _apply_mapping_text_markers(
        self,
        mapping: CSVSchemaMapping,
        resolved_by_decl_type: dict[str, str],
        graph_uri: str,
        result: IngestResult,
    ) -> None:
        """Persist a mapping's schema-time ``text_kind`` verdicts as markers.

        The CSV pipeline decides candidacy ONCE, at schema-inference time
        (profiler proposes → REASON pass adjudicates → the verdict rides on
        ``ColumnMapping.text_kind``, ONTA-177); this applies that verdict at
        schema-apply time as the idempotent ``textKind`` upsert on the
        RESOLVED attribute URI (the mapping's declared type may have been
        matched onto an existing ontology type). BOTH verdict polarities are
        persisted (ONTA-173): ``"free_text"`` marks the attribute for the
        semantic index; ``"not_text"`` (the REASON pass explicitly declined a
        TEXT-shaped column) durably records the decided NO so the reconciler
        stops re-sampling it and its name-blind auto tier can never overrule
        the LLM. Attribute names are normalized exactly like the ingest pass
        normalizes them (:func:`_normalize_attr_name`) so the marker lands on
        the same attr URI the instance triples use. Legacy / hand-written
        mappings carry no ``text_kind`` → no markers, no LLM (candidacy
        undecided; the reconciler-side default heuristic covers those later —
        ONTA-181). After any marker write the tenant's marker cache is
        invalidated HERE (write sites own it — refresh_after_write
        deliberately doesn't). Best-effort: failures log a warning and never
        block ingest.
        """
        try:
            specs_by_name = {s.name: s for s in (mapping.entities or [])}
            seen: set[tuple[str, str]] = set()
            marked_free_text: list[str] = []
            marked_not_text: list[str] = []
            for col in mapping.columns:
                if col.role != ColumnRole.ATTRIBUTE or col.text_kind not in (
                    TEXT_KIND_FREE_TEXT,
                    TEXT_KIND_NOT_TEXT,
                ):
                    continue
                if col.entity and col.entity in specs_by_name:
                    decl_type = specs_by_name[col.entity].type_name
                else:
                    decl_type = mapping.entity_type
                if not decl_type:
                    continue
                resolved_type = resolved_by_decl_type.get(decl_type, decl_type)
                attr_name = _normalize_attr_name(col.attribute_name or col.column_name)
                key = (resolved_type, attr_name)
                if not attr_name or key in seen:
                    continue
                seen.add(key)
                await self._commit_ontology(graph_uri, [
                    self._mut_text_kind(resolved_type, attr_name, col.text_kind),
                ])
                if col.text_kind == TEXT_KIND_FREE_TEXT:
                    result.free_text_attributes.append(f"{resolved_type}.{attr_name}")
                    marked_free_text.append(f"{resolved_type}.{attr_name}")
                else:
                    marked_not_text.append(f"{resolved_type}.{attr_name}")
            if seen:
                # Marker write site self-invalidates (mirrors the reconciler's
                # heuristic); the TTL stays the cross-process backstop.
                invalidate_text_marker_cache(graph_uri)
                _sr.logger.info(
                    "free_text_mapping_markers_applied",
                    attributes=sorted(marked_free_text),
                    not_text_attributes=sorted(marked_not_text),
                )
        except Exception:
            _sr.logger.warning("free_text_mapping_markers_failed", exc_info=True)
