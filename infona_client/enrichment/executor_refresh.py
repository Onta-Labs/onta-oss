"""Refresh-path writes (P6 supersession) for EnrichmentExecutor."""

from __future__ import annotations

from typing import Optional

from infona_client.enrichment.executor_helpers import _host
from infona_client.enrichment.models import ConflictPolicy, RowResult
from infona_client.graph.ontology_queries import PRIMITIVE_TYPES
from infona_client.graph.suppression import is_suppressed
from infona_client.pipeline.mutations import (
    DEFAULT_RECENCY_POLICY,
    write_with_conflict_resolution,
)


class EnrichmentRefreshMixin:
    """ONTA-279 refresh: supersede via P6, never blind-append."""

    @classmethod
    def _primary_value_write(
        cls,
        entity_uri: str,
        type_name: str,
        attribute: str,
        value: str,
        datatype: str,
    ) -> Optional[tuple[str, str, list[tuple[str, str, str]]]]:
        """Split one applied value into its PRIMARY edge + any node-minting triples,
        for the ONTA-279 refresh path (which routes the primary through the P6
        conflict op rather than a blind insert).

        Reuses :meth:`_instance_triples_for_value` (the ONE value-typing +
        node-linking implementation) and splits its output: the FIRST triple is
        always the primary ``(entity, predicate, term)`` — the literal value on
        ``attrs/<leaf>`` or the relationship edge on ``onto/<leaf>`` — and the rest
        (a relationship target's ``rdf:type`` / ``rdfs:label``) are the node-minting
        companions that still ride the shared ``insert_facts``. Returns
        ``(predicate, term, node_triples)`` or ``None`` when the value produced no
        primary triple (a rejected non-conforming primitive — never clear/replace an
        incumbent for a value that won't be written)."""
        triples = cls._instance_triples_for_value(
            entity_uri, type_name, attribute, value, datatype
        )
        if not triples:
            return None
        _s, predicate, term = triples[0]
        node_triples = list(triples[1:])
        return predicate, term, node_triples

    async def _apply_refresh_writes(
        self,
        graph_uri: str,
        rows: list[RowResult],
        type_name: str,
        write_policy: ConflictPolicy,
        resolved_datatypes: dict[str, str],
        run_id: str,
    ) -> list[tuple[str, str, str]]:
        """Apply a REFRESH job's primary values through the P6 supersession op
        (ONTA-279) — the write half of "refresh supersedes, never blind-appends".

        For each row that :meth:`_row_is_applied` under ``write_policy``
        (verify → fills; overwrite → fills/conflicts/verifies), route the PRIMARY
        ``(subject, predicate, value)`` through
        :func:`pipeline.mutations.write_with_conflict_resolution` instead of a raw
        ``insert_facts`` / ``delete_facts``+``insert_facts``. That op:

          * reads the existing current value's authority back from provenance and
            arbitrates on the ONE shared policy (authority > confidence > recency >
            value) — so a machine refresh CLOSES a stale value's validity interval
            (supersession, never a hard delete) but LOSES to a ``user_assertion``
            correction (completing ONTA-281's e2e);
          * inherits the ONTA-277 resurrection semantics for free (``reopen_facts``),
            so an A→B→A oscillation lands A current again.

        Before writing, the value is checked against the STICKY suppression list
        (:func:`graph.suppression.is_suppressed`): a retracted/suppressed value is a
        no-op (a refresh must never re-acquire it), which — unlike a validity
        closure — the op's reopen cannot resurrect.

        Node-minting triples (a relationship target's type/label) and the
        per-attribute DISPLAY provenance companions are RETURNED for the caller to
        write in ONE shared ``insert_facts`` + one ``refresh_after_write``, keeping
        the companions on the converged write path.
        """
        companion_triples: list[tuple[str, str, str]] = []
        for r in rows:
            if not self._row_is_applied(r, write_policy) or r.verdict is None:
                continue
            datatype = resolved_datatypes.get(r.attribute, "string")
            primary = self._primary_value_write(
                r.entity_uri, type_name, r.attribute, r.verdict.value, datatype
            )
            if primary is None:
                # A non-conforming primitive produced no primary triple → write
                # nothing (never supersede an incumbent for a value we can't store).
                continue
            predicate, term, node_triples = primary
            # Suppression consult: a retracted/suppressed value must NOT be
            # re-acquired by a refresh (ONTA-279). Skip it entirely — no
            # supersession, no reopen, no companions — so it stays off.
            if await is_suppressed(self._neptune, graph_uri, r.entity_uri, predicate, term):
                _host().logger.info(
                    "enrichment_refresh_value_suppressed",
                    subject=r.entity_uri,
                    predicate=predicate,
                    value=term,
                )
                continue
            # Node-minting companions (relationship target type/label) ride the
            # shared insert_facts the caller issues; write them before the edge is
            # arbitrated so the target node exists.
            companion_triples.extend(node_triples)
            # ONTA-536: prefer source_url so Assertion identity / fold matches the
            # companion citation insert (same source_discriminator → one Assertion).
            _src = (
                getattr(r.verdict, "source_url", None)
                or getattr(r.verdict, "source", "")
                or ""
            )
            await write_with_conflict_resolution(
                self._neptune,
                graph_uri,
                subject=r.entity_uri,
                predicate=predicate,
                type_name=type_name,
                value=term,
                authority=self._verdict_authority(r.verdict),
                confidence=float(r.verdict.confidence),
                source=_src,
                observed_at=self._verdict_as_of(r.verdict),
                run_id=run_id,
                reason="enrichment refresh (supersede stale value)",
                recency_policy=DEFAULT_RECENCY_POLICY,
                # This op runs PER ROW; the caller (run()'s is_refresh branch) issues
                # ONE final refresh_after_write for the touched types after the loop.
                # Deferring the per-row refresh turns a bulk refresh from ~N+1
                # housekeeping passes (re-embed + stats) into 1.
                refresh=False,
            )
            # ONTA-536: re-include the primary value triple so the caller's
            # companion insert_facts batch has a domain Fact for
            # fold_attr_citations_onto_facts (Assertion.source_url / verified_at).
            # Idempotent re-write — same s/p/o already landed above.
            companion_triples.append((r.entity_uri, predicate, term))
            # Per-attribute DISPLAY provenance companions (source_url / provenance /
            # verified_at) for the value we just wrote — same citations as the
            # non-refresh path, collected for one shared insert.
            companion_triples.extend(
                self._provenance_triples(r.entity_uri, type_name, r.attribute, r.verdict)
            )
        return companion_triples

    @staticmethod
    def _row_is_applied(r: RowResult, policy: ConflictPolicy) -> bool:
        """Whether a row's verdict actually contributes instance triples under
        ``policy``. Single source of truth shared by :meth:`_select_triples_for_policy`
        (which data to write) and :meth:`_applied_attribute_names` (which schema to
        declare) so the two can never drift."""
        if r.verdict is None:
            return False
        if policy == ConflictPolicy.overwrite:
            return r.action in ("filled", "conflict", "verified")
        if policy in (ConflictPolicy.verify, ConflictPolicy.skip):
            return r.action == "filled"
        return False

    @staticmethod
    def _affected_types(type_name: str, resolved_datatypes: dict[str, str]) -> set[str]:
        """Types whose embeddings + Explorer stats a post-write refresh must touch:
        the SUBJECT type PLUS the type of every node-valued attribute.

        A node-valued fill mints a target NODE
        (:meth:`_instance_triples_for_value` — e.g. ``Physician.located_in`` →
        a ``City`` node), so ``refresh_after_write`` must re-embed / re-stat that
        target TYPE too. Passing only the subject type (the old behavior) left a
        freshly-minted ``City`` node stale until ``City``'s own next write —
        the enrichment mirror of discovery's Part-3 gap. Non-primitive
        ``resolved_datatypes`` values are exactly the node-valued ranges."""
        return {type_name} | {
            dt for dt in resolved_datatypes.values() if dt not in PRIMITIVE_TYPES
        }
