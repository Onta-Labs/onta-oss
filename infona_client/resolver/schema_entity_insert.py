from __future__ import annotations

"""Per-entity resolve + insert (attribute promotion, node mint, companions).

Job: write one entity's facts. Promotion / relationship INSTANCE edges
go on ``onto/<leaf>`` (never ``attrs/<leaf>``). New nodes via
``entity_uri(type, raw_id)`` only. Flush through ``insert_facts``.
Companions via the shared provenance builders — never declare companions
in the ontology.
"""

from datetime import datetime, timezone

from infona_client.graph.facts import entity_display_label
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.ontology_catalog_models import canonicalize_literal_datatype
from infona_client.graph.ontology_queries import PRIMITIVE_TYPES, attr_uri, type_uri
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.graph.provenance import (
    build_attribute_provenance_companions,
    build_provenance_triples,
)
from infona_client.graph.queries import BATCH_PREDICATE
from infona_client.normalization.clean import clean_value
from infona_client.pipeline.envelope import derive_fact_id
from infona_client.resolver.attribute_resolver import (
    AttributeSchema,
    check_promotion,
    is_junk_type_name,
    resolve_attribute,
)
from infona_client.resolver.models import (
    AttrAction,
    CleanFact,
    ExtractedEntity,
    IngestResult,
    RejectedValue,
    ValidatedTriple,
    ValidationOutcome,
)
from infona_client.resolver.schema_extract_constraints import _is_implausible_node_label
from infona_client.resolver.schema_grounding import (
    _is_fabricated_placeholder,
    _looks_like_url,
)
from infona_client.resolver.schema_text import _TEXT_EVIDENCE_MAX_VALUES
from infona_client.resolver.validator import validate_triple
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


class SchemaEntityInsertMixin:
    """Per-entity write half of SchemaResolver."""

    async def _resolve_and_insert_entity(
        self,
        entity: ExtractedEntity,
        resolved_type: str,
        entity_uri: str,
        is_duplicate: bool,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        result: IngestResult,
        batch_id: str = "",
        _collect_triples: list[tuple[str, str, str]] | None = None,
        _collect_provenance: list[tuple[str, str, str]] | None = None,
        also_types: list[str] | None = None,
        _collect_text_values: dict[tuple[str, str], list[str]] | None = None,
        drop_placeholder_values: bool = False,
        *,
        instance_graph: str | None = None,
        observed_at: datetime | None = None,
        allow_prefix_promotion: bool = True,
    ) -> None:
        """Pass 2: Resolve attributes, validate, and collect triples for one entity.

        ``instance_graph`` (ONTA-268): CALL-LOCAL target graph for the legacy
        per-entity insert / provenance write; falls back to ``self._instance_graph``.

        ``observed_at`` (ONTA-271): the run's ``onto/ingested_at`` timestamp. When
        given (the reentrant ``ingest`` path) it is used verbatim so a preserved-
        run_id replay writes the SAME ingested_at and the delta stays byte-
        identical; ``None`` (the CSV / legacy path, unchanged) falls back to
        wall-clock now.

        If _collect_triples is provided, triples are appended to that list instead of
        being inserted immediately. The caller is responsible for batch-inserting them.
        This is ~10-50x faster because it avoids per-entity Neptune INSERT calls.

        If _collect_provenance is provided (COG-46), per-fact provenance triples
        (when INFONA_PROVENANCE_ENABLED is on) are likewise appended for the
        caller to flush in one batched INSERT into the companion provenance
        graph, instead of being inserted here per entity.

        `also_types` are genuine independent co-classifications (ADR rule 1): each
        gets its own asserted rdf:type triple alongside the primary resolved_type.

        If _collect_text_values is provided (ONTA-177), validated STRING
        attribute values are sampled into it keyed by (resolved_type,
        resolved attr name) — free-text candidacy evidence the caller decides
        on after the write. Values only, never names: the name-blind
        classification happens downstream (ADR 0003 litmus).

        ``drop_placeholder_values`` (ONTA-259): on the model-proposed extraction
        path (text / JSON / web-discovery) drop any attribute whose VALUE is an
        obvious fabricated placeholder ("1234567890", "0000000000", "N/A", …)
        BEFORE it is resolved or written — a dropped value is treated as
        UNSTATED (the attribute is omitted, as if the source gave no value),
        counted via a structured log. OFF (default) for the authoritative CSV
        path, whose cells are written verbatim.
        """
        # ONTA-259: deterministic anti-fabrication backstop. Filter placeholder
        # VALUES up front so a hallucinated identifier is uniformly invisible to
        # EVERY downstream step (promotion, resolution, the write) — exactly as
        # if the source had never stated it. Prompt-forbidden too; this is the
        # model-agnostic defense-in-depth layer behind the prompt.
        if drop_placeholder_values and entity.attributes:
            kept_attrs = []
            for a in entity.attributes:
                if _is_fabricated_placeholder(a.value):
                    _sr.logger.info(
                        "discovery_placeholder_value_dropped",
                        entity_id=entity.id,
                        type_name=resolved_type,
                        attribute=a.name,
                        value=a.value,
                    )
                    continue
                kept_attrs.append(a)
            if len(kept_attrs) != len(entity.attributes):
                entity = entity.model_copy(update={"attributes": kept_attrs})

        type_attrs = existing_attrs.get(resolved_type, {})

        # Option D promotions. CSV mapped ingest passes False — skip entirely
        # (auto_promote_new=False still leaks into existing types).
        promotions = (
            check_promotion(entity, type_attrs, existing_types=existing_types)
            if allow_prefix_promotion else []
        )
        promoted_type_names: set[str] = set()
        for promo in promotions:
            if promo.promoted_type and promo.promoted_type not in promoted_type_names:
                # Defense-in-depth: never mint a junk type even if a caller
                # bypassed the attribute_resolver gate.
                if is_junk_type_name(promo.promoted_type):
                    _sr.logger.info(
                        "attr_promotion_skipped_junk",
                        promoted_type=promo.promoted_type,
                        entity_id=entity.id,
                    )
                    continue
                promoted_type_names.add(promo.promoted_type)

        for ptype in promoted_type_names:
            if ptype not in existing_types:
                await self._commit_ontology(graph_uri, [
                    self._mut_type(ptype, description=f"Promoted from {resolved_type} attributes"),
                ])
                result.types_created.append(ptype)
                existing_types[ptype] = ""
                existing_attrs[ptype] = {}

        rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
        rdfs_label = "http://www.w3.org/2000/01/rdf-schema#label"

        # Duplicate entities skip rdf:type triple but still merge attributes
        if is_duplicate:
            triples_to_insert: list[tuple[str, str, str]] = []
        else:
            triples_to_insert: list[tuple[str, str, str]] = [
                (entity_uri, rdf_type, type_uri(resolved_type)),
                (entity_uri, rdfs_label, entity_display_label(entity.id, entity.attributes)),
            ]
            # Multi-typing: emit an additional asserted rdf:type per genuine
            # co-classification (ADR rule 1). Ancestors are NOT asserted here —
            # they are recovered via query-time subclass closure.
            for co_type in (also_types or ()):
                if co_type and co_type != resolved_type:
                    triples_to_insert.append((entity_uri, rdf_type, type_uri(co_type)))

        promoted_entities: dict[str, str] = {}
        # Attribute assertions made for this entity — mirrors the attribute
        # appends to triples_to_insert so per-fact provenance (ADR 0002 §4)
        # can be emitted for them when enabled.
        attr_facts: list[tuple[str, str, str]] = []

        for attr in entity.attributes:
            canon_dt = canonicalize_literal_datatype(attr.datatype)
            if canon_dt != attr.datatype:
                attr = attr.model_copy(update={"datatype": canon_dt})
            _norm = attr.name.lower().replace(" ", "_")
            _short = _norm.split("_", 1)[-1]
            promo_match = next(
                (
                    p for p in promotions
                    if p.promoted_type is not None and p.name in (_norm, _short)
                ),
                None,
            )
            if promo_match and promo_match.promoted_type:
                ptype = promo_match.promoted_type
                if ptype.lower() == resolved_type.lower():
                    promo_match = None
            if promo_match and promo_match.promoted_type:
                ptype = promo_match.promoted_type
                if ptype not in promoted_entities:
                    p_uri = f"{_sr._entity_uri(ptype, entity.id)}-{ptype.lower()}"
                    promoted_entities[ptype] = p_uri
                    triples_to_insert.append((p_uri, rdf_type, type_uri(ptype)))
                    rel_pred = f"{IRI_BASE}/onto/has_{ptype.lower()}"
                    triples_to_insert.append((entity_uri, rel_pred, p_uri))
                    # Post-write housekeeping must re-embed / re-stat the promoted
                    # node's TYPE too (Part 3), not just the subject type — else a
                    # pre-existing ptype that gains its first node this pass stays
                    # stale until its next write. See IngestResult.affected_types().
                    result.node_target_types.append(ptype)

                p_uri = promoted_entities[ptype]
                attr_name = promo_match.name
                p_attrs = existing_attrs.get(ptype, {})
                if attr_name not in p_attrs:
                    await self._commit_ontology(graph_uri, [
                        self._mut_attr(ptype, attr_name, attr.datatype),
                    ])
                    result.attributes_added.append(f"{ptype}.{attr_name}")
                    existing_attrs.setdefault(ptype, {})[attr_name] = AttributeSchema(
                        name=attr_name, datatype=attr.datatype,
                    )

                pred_uri = attr_uri(ptype, attr_name)
                # ONTA-373: record the A3 clean outcome (passed/transformed/dropped
                # + reason) into the discovery ingest ledger BEFORE typing — mirrors
                # enrichment's _instance_triples_for_value. Purely additive: the
                # written triple is unchanged (validate_triple re-derives the same
                # CleanFact internally), this only makes the decision non-silent.
                result.clean_report.record(
                    clean_value(
                        attr.value, attr.datatype,
                        entity_id=entity.id, attribute=attr_name,
                    )
                )
                validated = validate_triple(
                    p_uri, pred_uri, attr.value, attr.datatype,
                    entity_id=entity.id, attribute_name=attr_name, type_name=ptype,
                )
                if isinstance(validated, ValidatedTriple):
                    triples_to_insert.append((validated.subject, validated.predicate, validated.object))
                    attr_facts.append((validated.subject, validated.predicate, validated.object))
                    # ONTA-347: preserve the ORIGINAL surface form (attr_meta
                    # companion) when A3 coerced/canonicalized it — rides the SAME
                    # write path, but NOT attr_facts (metadata OF the attribute, not
                    # a domain fact, so it gets no provenance record of its own).
                    if validated.surface_form_companion:
                        triples_to_insert.append(validated.surface_form_companion)
                    # ONTA-528: do not count here — triples_inserted increments
                    # only after insert_facts lands the batch.
                else:
                    result.rejections.append(validated)

                resolved = resolve_attribute(attr, type_attrs)
                if resolved.action == AttrAction.EXTEND:
                    await self._commit_ontology(graph_uri, [
                        self._mut_attr(resolved_type, resolved.name, resolved.datatype),
                    ])
                    result.attributes_added.append(f"{resolved_type}.{resolved.name}")
                    type_attrs[resolved.name] = AttributeSchema(name=resolved.name, datatype=resolved.datatype)

                pred_uri = attr_uri(resolved_type, resolved.name)
                # ONTA-373: record the A3 clean outcome into the ingest ledger.
                result.clean_report.record(
                    clean_value(
                        resolved.value, resolved.datatype,
                        entity_id=entity.id, attribute=resolved.name,
                    )
                )
                validated = validate_triple(
                    entity_uri, pred_uri, resolved.value, resolved.datatype,
                    entity_id=entity.id, attribute_name=resolved.name,
                    type_name=resolved_type,
                )
                if isinstance(validated, ValidatedTriple):
                    triples_to_insert.append((validated.subject, validated.predicate, validated.object))
                    attr_facts.append((validated.subject, validated.predicate, validated.object))
                    # ONTA-347: preserve the ORIGINAL surface form on transform.
                    if validated.surface_form_companion:
                        triples_to_insert.append(validated.surface_form_companion)
                else:
                    result.rejections.append(validated)
                continue

            resolved = resolve_attribute(attr, type_attrs)

            if resolved.action == AttrAction.EXTEND:
                await self._commit_ontology(graph_uri, [
                    self._mut_attr(resolved_type, resolved.name, resolved.datatype),
                ])
                result.attributes_added.append(f"{resolved_type}.{resolved.name}")
                type_attrs[resolved.name] = AttributeSchema(name=resolved.name, datatype=resolved.datatype)

            if resolved.datatype not in PRIMITIVE_TYPES:
                # This attribute is TYPED as a relationship to `resolved.datatype`
                # (a non-primitive type name), not a literal — so its value is
                # another entity and MUST be minted as a node reached by an edge,
                # never stored as a bare string. Two cases converge here:
                #   * DECLARED (warm): the ontology already declares this an object
                #     property whose range is `resolved.datatype` (an existing type)
                #     — the original promotion path.
                #   * COLD START: THIS extraction typed the attribute as a
                #     relationship (LLM emitted `datatype=<Type>`), so the EXTEND
                #     branch above already declared the object property with
                #     `rdfs:range = types/<datatype>` (insert_attribute maps a
                #     non-primitive datatype to a type URI). Without minting the type
                #     the schema carried a DANGLING range (an object property whose
                #     range type was never created) and the value fell to the literal
                #     path — a literal on attrs/<leaf>, INVISIBLE to NL relationship
                #     traversal, with no target node (the #123-class bug, in the
                #     cold-start branch). Create the target type so the schema is
                #     internally consistent and the edge is NL-queryable.
                # This never OVER-PROMOTES: a plain literal attribute has a PRIMITIVE
                # datatype and takes the else-branch below unchanged — only an
                # attribute EXPLICITLY typed as a relationship is minted as a node.
                #
                # ONTA-383 + ONTA-394: do NOT mint a relationship-target node when
                # either (a) the TARGET TYPE is a junk property-class name (Colour,
                # Online, InstructionMode — ONTA-383), or (b) the VALUE is not a
                # plausible entity label (a bare year/number, a URL/navigation
                # fragment, a slug, or truncated text — ONTA-394). Case (b) is the
                # dogfood defect: a skewed `city` cell (a year, `UBC_Academic_
                # Calendar`, `WCC_-_Western_Community_Colle…`) was promoted into a
                # City NODE + `city ->` edge. Fall through to the literal path so
                # the value is kept verbatim on attrs/<leaf> without polluting the
                # ontology or minting a junk node — a single stray/skewed cell
                # never becomes a type or a node.
                _rel_reject = None
                if is_junk_type_name(resolved.datatype):
                    _rel_reject = "junk_target_type"
                elif _sr._NODE_LABEL_GUARD and _is_implausible_node_label(resolved.value):
                    _rel_reject = "implausible_node_label"
                if _rel_reject:
                    _sr.logger.info(
                        "relationship_target_kept_literal",
                        type_name=resolved_type,
                        attribute=resolved.name,
                        rejected_type=resolved.datatype,
                        value=resolved.value,
                        reason=_rel_reject,
                        entity_id=entity.id,
                    )
                    resolved = resolved.model_copy(update={"datatype": "string"})
                    pred_uri = attr_uri(resolved_type, resolved.name)
                    result.clean_report.record(
                        clean_value(
                            resolved.value, resolved.datatype,
                            entity_id=entity.id, attribute=resolved.name,
                        )
                    )
                    validated = validate_triple(
                        entity_uri, pred_uri, resolved.value, resolved.datatype,
                        entity_id=entity.id, attribute_name=resolved.name,
                        type_name=resolved_type,
                    )
                    if isinstance(validated, ValidatedTriple):
                        triples_to_insert.append(
                            (validated.subject, validated.predicate, validated.object)
                        )
                        attr_facts.append(
                            (validated.subject, validated.predicate, validated.object)
                        )
                        if validated.surface_form_companion:
                            triples_to_insert.append(validated.surface_form_companion)
                    else:
                        result.rejections.append(validated)
                    continue
                if resolved.datatype not in existing_types:
                    await self._commit_ontology(graph_uri, [
                        self._mut_type(
                            resolved.datatype,
                            description=f"Relationship target of {resolved_type}.{resolved.name}",
                        ),
                    ])
                    result.types_created.append(resolved.datatype)
                    existing_types[resolved.datatype] = ""
                    existing_attrs.setdefault(resolved.datatype, {})
                target_uri = _sr._entity_uri(resolved.datatype, resolved.value)
                # Relationship INSTANCE edge → onto/<leaf>. That is the ONLY
                # predicate the NL→SPARQL planner queries a type-ranged attribute on
                # (nlp/ontology_embeddings publishes onto/<leaf> for relationships,
                # with NO attrs/<leaf> fallback), so an edge on attrs/<leaf> is
                # invisible to NL — the exact bug enrichment hit in #123 and fixed in
                # #126. The attrs/<leaf> predicate is the ontology DECLARATION of the
                # property (its range names the target type, via insert_attribute),
                # NOT the instance edge. Matches enrichment
                # (executor._instance_triples_for_value) and the sibling has_<ptype>
                # promotion edge above — both on onto/<leaf>.
                onto_pred = f"{IRI_BASE}/onto/{resolved.name}"
                triples_to_insert.append((entity_uri, onto_pred, target_uri))
                attr_facts.append((entity_uri, onto_pred, target_uri))
                # Materialize the target as a FIRST-CLASS node: emit its rdf:type +
                # rdfs:label too. Without them the promoted node is bare — untyped,
                # unlabelled, invisible to "list all <Type>" queries — even though
                # the edge points at it. Mirrors enrichment's node-linking
                # (executor._instance_triples_for_value) so discovery + enrichment
                # mint the identical shared NODE for the same real-world thing.
                # NOT added to attr_facts: this is node materialization, not a fact
                # ABOUT the subject — same as how the subject's own rdf:type/label
                # are emitted untracked above.
                triples_to_insert.append((target_uri, rdf_type, type_uri(resolved.datatype)))
                triples_to_insert.append((target_uri, rdfs_label, resolved.value))
                # refresh coverage (Part 3): the newly-minted node's TYPE must be
                # re-embedded / re-stat'd now, not only on its next write.
                result.node_target_types.append(resolved.datatype)
            else:
                pred_uri = attr_uri(resolved_type, resolved.name)
                # ONTA-373: record the A3 clean outcome (passed/transformed/dropped
                # + reason) into the discovery ingest ledger. This is the primary
                # literal path — a non-conforming value that yields NO triple below
                # becomes a RECORDED `dropped` entry, not a silent skip. Additive:
                # the write is unchanged.
                result.clean_report.record(
                    clean_value(
                        resolved.value, resolved.datatype,
                        entity_id=entity.id, attribute=resolved.name,
                    )
                )
                validated = validate_triple(
                    entity_uri, pred_uri, resolved.value, resolved.datatype,
                    entity_id=entity.id, attribute_name=resolved.name,
                    type_name=resolved_type,
                )
                if isinstance(validated, ValidatedTriple):
                    triples_to_insert.append((validated.subject, validated.predicate, validated.object))
                    attr_facts.append((validated.subject, validated.predicate, validated.object))
                    # ONTA-347: preserve the ORIGINAL surface form on transform.
                    if validated.surface_form_companion:
                        triples_to_insert.append(validated.surface_form_companion)
                    # ONTA-177: sample validated string values as free-text
                    # candidacy evidence (bounded per attribute).
                    if _collect_text_values is not None and resolved.datatype == "string":
                        samples = _collect_text_values.setdefault(
                            (resolved_type, resolved.name), [],
                        )
                        if len(samples) < _TEXT_EVIDENCE_MAX_VALUES:
                            samples.append(validated.object)
                else:
                    result.rejections.append(validated)

        # Per-fact provenance (ADR 0002 §4), gated by INFONA_PROVENANCE_ENABLED
        # (default off). Statement-metadata triples target the COMPANION
        # provenance graph — a different graph than the instance-triple
        # collector. With a _collect_provenance collector (the batched fast
        # path, COG-46) they accumulate for ONE batched INSERT by the caller;
        # without one they are inserted here per entity (legacy path).
        # Confidence is 1.0 for directly-ingested facts.
        if self._provenance_enabled and attr_facts:
            instance_graph = (
                instance_graph if instance_graph is not None
                else getattr(self, "_instance_graph", graph_uri)
            )
            prov_ts = datetime.now(timezone.utc)
            prov_triples: list[tuple[str, str, str]] = []
            for s, p, o in attr_facts:
                prov_triples.extend(build_provenance_triples(
                    s, p, o, source=source, confidence=1.0,
                    timestamp=prov_ts, graph_uri=instance_graph,
                ))
            if _collect_provenance is not None:
                _collect_provenance.extend(prov_triples)
            else:
                # Legacy path (no collector): shared write dual-routes SPARQL /
                # GraphStore. On Neo4j, RDF companion provenance is a Wave-1 no-op
                # inside insert_facts store path; Assertion provenance rides the
                # instance write when enabled.
                await _sr.insert_facts(
                    self._neptune,
                    instance_graph,
                    [],
                    provenance_triples=prov_triples,
                    store=resolve_optional_graph_store(),
                )

        # Per-attribute DISPLAY provenance companions (ONTA-245 F1), gated by
        # INFONA_DISCOVERY_ATTR_PROVENANCE (default off). The SAME
        # `<attr>_source_url` / `<attr>_verified_at` instance companions enrichment
        # always writes, so a DISCOVERED fact and an ENRICHED fact are
        # provenance-symmetric at the ATTRIBUTE level (not just the per-record
        # `onto/source`). Built via the shared builder and appended to
        # `triples_to_insert` so they flow through the SAME shared write path
        # (insert_facts) as every other fact — no separate writer. The record's
        # `source` (a URL for web discovery) becomes each attribute's
        # `_source_url`/`_provenance`; the freshness stamp is now-UTC (first-seen).
        if self._attr_provenance_enabled and attr_facts:
            attr_prov_ts = datetime.now(timezone.utc)
            for s, p, _o in attr_facts:
                leaf = p.rstrip("/").rsplit("/", 1)[-1]
                if not leaf:
                    continue
                triples_to_insert.extend(
                    build_attribute_provenance_companions(
                        s,
                        resolved_type,
                        leaf,
                        source_url=source if _looks_like_url(source) else "",
                        provenance=source or "",
                        verified_at=attr_prov_ts,
                    )
                )

        # Provenance triples. ingested_at is sourced from the run's observed_at
        # (ONTA-271) when threaded, so a preserved-run_id replay writes the
        # identical stamp (idempotent) instead of a fresh wall-clock nonce;
        # legacy/CSV callers pass None → wall-clock now, unchanged.
        now = (observed_at or datetime.now(timezone.utc)).isoformat()
        triples_to_insert.append((entity_uri, f"{IRI_BASE}/onto/ingested_at", now))
        if source:
            triples_to_insert.append((entity_uri, f"{IRI_BASE}/onto/source", source))
        if batch_id:
            triples_to_insert.append((entity_uri, BATCH_PREDICATE, batch_id))

        # Collect triples for batch insert (or insert immediately if no collector).
        # ONTA-528: never increment triples_inserted on collect — only after a
        # successful insert_facts (here for the legacy no-collector path; the
        # batched callers count after their own insert_facts).
        if triples_to_insert:
            if _collect_triples is not None:
                _collect_triples.extend(triples_to_insert)
            else:
                # Legacy path: insert per-entity via shared write. Callers without
                # a collector (direct unit tests / older entry points) stay on
                # insert_facts — never SPARQL HTTP update / INSERT.
                instance_graph = (
                    instance_graph if instance_graph is not None
                    else getattr(self, "_instance_graph", graph_uri)
                )
                await _sr.insert_facts(
                    self._neptune,
                    instance_graph,
                    triples_to_insert,
                    store=resolve_optional_graph_store(),
                )
                result.triples_inserted += len(triples_to_insert)
