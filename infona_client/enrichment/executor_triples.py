"""Instance-triple construction + literal-to-node promotion."""

from __future__ import annotations

from typing import Optional

from infona_client.enrichment.executor_const import RDF_TYPE, RDFS_LABEL
from infona_client.enrichment.executor_helpers import _attr_uri, _host, _type_uri
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.ontology_queries import PRIMITIVE_TYPES, entity_uri as _entity_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.normalization.clean import clean_value
from infona_client.resolver.models import CleanReport, ValidatedTriple
from infona_client.resolver.validator import validate_triple


class EnrichmentTriplesMixin:
    """Build instance triples; promote leftover literal attrs to nodes."""

    async def _promote_literal_attr_to_nodes(
        self,
        tenant_id: str,
        kg_name: str,
        type_name: str,
        attr_name: str,
        target_type: str,
        *,
        extra_values: list[str] | None = None,
    ) -> None:
        """Turn already-written string values of ``attr_name`` into target nodes.

        Job c7c2c7d2 wrote ``lead_sponsor`` as attrs/lead_sponsor literals.
        After we flip the declaration to a Company relationship, those
        literals must become ``onto/lead_sponsor`` edges + Company nodes or
        Explorer keeps showing a string column. GraphStore-only — no SPARQL.
        """
        del extra_values  # values ride the subsequent insert_facts path
        try:
            from infona_client.graph.explore_store import (
                get_entity_detail,
                list_entities_by_type,
            )
            from infona_client.graph.ontology_queries import attr_uri as _attr_iri
            from infona_client.graph.store import GraphConfigError
        except Exception:  # noqa: BLE001
            return
        try:
            page = await list_entities_by_type(
                tenant_id=tenant_id,
                kg_name=kg_name,
                type_name=type_name,
                limit=200,
            )
        except GraphConfigError:
            return
        except Exception:
            _host().logger.warning(
                "enrich_promote_list_failed",
                type_name=type_name,
                attr=attr_name,
                exc_info=True,
            )
            return
        if page is None or not page.entities:
            return
        store = resolve_optional_graph_store()
        graph_uri = kg_graph_uri(tenant_id, kg_name)
        lit_pred = _attr_iri(type_name, attr_name)
        triples: list[tuple[str, str, str]] = []
        clear: list[tuple[str, str, None]] = []
        for ent in page.entities:
            try:
                detail = await get_entity_detail(
                    tenant_id=tenant_id, kg_name=kg_name, entity_id=ent.id
                )
            except Exception:
                continue
            if detail is None:
                continue
            raw = (detail.properties or {}).get(attr_name)
            if raw is None or raw == "":
                continue
            if isinstance(raw, (list, tuple)):
                labels = [str(x) for x in raw if x]
            else:
                labels = [str(raw)]
            for label in labels:
                if label.startswith("http://") or label.startswith("https://"):
                    continue
                triples.extend(
                    self._instance_triples_for_value(
                        ent.id, type_name, attr_name, label, target_type
                    )
                )
                clear.append((ent.id, lit_pred, None))
        if triples:
            await _host().insert_facts(self._neptune, graph_uri, triples, store=store)
        if clear:
            await _host().delete_facts(
                self._neptune,
                graph_uri,
                triples=clear,
                reason="enrich:promote_literal_to_node",
                store=store,
            )

    @staticmethod
    def _instance_triples_for_value(
        entity_uri: str,
        type_name: str,
        attribute: str,
        value: str,
        datatype: str,
        clean_report: Optional[CleanReport] = None,
    ) -> list[tuple[str, str, str]]:
        """Build the instance triple(s) for ONE applied attribute value, typed with
        the SAME resolved ``datatype`` the attribute is DECLARED with (P1 fix).

        Two branches mirror ingestion's value-typing path:

        - **relationship** — ``datatype`` is NOT a primitive (it is an entity-type
          name, e.g. ``City``). If the value is ALREADY an entity IRI (a premium
          adapter that resolved it) write that edge directly. Otherwise the enriched
          value is a plain LABEL (``"San Francisco"``): resolve it to the SAME
          canonical ``entities/<Type>/<safe_id>`` URI ingestion mints and ALSO emit
          the node's ``rdf:type`` + ``rdfs:label`` — so the fact becomes ONE shared
          node across the discovery and enrichment rails, never a dangling string in
          a node-valued slot (the cross-rail correctness fix). No ``validate_triple``
          (an entity edge is not an XSD-typed literal).
        - **primitive** (string/integer/float/datetime/boolean/uri) — route the
          value through the SAME ``validate_triple`` ingestion uses so the stored
          literal is properly TYPED (``"92^^…#integer"`` → a typed literal via
          ``_escape_value``). A ``ValidatedTriple`` is written; a ``RejectedValue``
          (value can't conform/coerce to the declared range) yields NO triple — we
          skip it rather than pin a mismatched literal that the typed NL filters
          would then miss (validate_triple already logs the rejection).

        Returns ``[]`` when a primitive value is rejected; otherwise the single
        instance triple.

        ONTA-344: when a ``clean_report`` is supplied, every PRIMITIVE value is
        recorded into the A3 clean ledger (passed / transformed / dropped) — so a
        non-conforming value that yields no triple is a RECORDED ``dropped`` entry
        with a reason, not a silent skip. Additive: this changes nothing about which
        triples are written (relationship edges are not datatype-cleaned literals, so
        they are outside the clean ledger's scope and are not recorded)."""
        attr_uri_str = _attr_uri(type_name, attribute)
        # Relationship: a non-primitive datatype names an entity TYPE (a node range).
        if datatype not in PRIMITIVE_TYPES:
            # A relationship INSTANCE edge lives on the onto/<leaf> predicate — the
            # form the NL query planner emits for a type-ranged attribute
            # (nlp/prompts, ontology_embeddings) and the form discovery's PRIMARY
            # relationship writes use. Writing the edge on the attrs/<leaf>
            # ATTRIBUTE predicate instead leaves it INVISIBLE to NL queries (they
            # traverse onto/<leaf>, with no attrs/<leaf> fallback). The ontology
            # DECLARATION stays the attrs/<leaf> property with a types/<T> range
            # (the established dual convention: attrs declares, onto carries the
            # instance); only the instance edge is onto/<leaf>.
            onto_pred = f"{IRI_BASE}/onto/{attribute}"
            # Already an entity IRI (e.g. a premium adapter that resolved it) → the
            # edge is ready as-is.
            if value.startswith("http://") or value.startswith("https://"):
                return [(entity_uri, onto_pred, value)]
            # Otherwise the enriched value is a plain LABEL. Resolve it to the SAME
            # canonical entity URI ingestion mints (entities/<Type>/<safe_id>) so the
            # same real-world thing is ONE shared node across the discovery +
            # enrichment rails, and create/type that node (idempotent INSERT) so the
            # edge is never a dangling string — closing the cross-rail divergence
            # where enrichment wrote a literal into a node-valued attribute the
            # ontology declares as a relationship. Uses the SAME shared entity_uri
            # minter discovery keys its entity URIs with (graph/ontology_queries), so
            # the URIs coincide exactly — one shared node across both rails.
            target_uri = _entity_uri(datatype, value)
            return [
                (entity_uri, onto_pred, target_uri),
                (target_uri, RDF_TYPE, _type_uri(datatype)),
                (target_uri, RDFS_LABEL, value),
            ]
        # Primitive: type the literal exactly as ingestion does. validate_triple
        # returns a ValidatedTriple (typed object) on conform/coerce, else a
        # RejectedValue (skip — never write a literal that mismatches the range).
        # Record the A3 clean outcome (passed/transformed/DROPPED) into the ledger so
        # a non-conforming value is a recorded drop, not a silent skip (ONTA-344).
        if clean_report is not None:
            clean_report.record(
                clean_value(value, datatype, entity_id=entity_uri, attribute=attribute)
            )
        validated = validate_triple(
            entity_uri,
            attr_uri_str,
            value,
            datatype,
            entity_id=entity_uri,
            attribute_name=attribute,
        )
        if isinstance(validated, ValidatedTriple):
            return [(validated.subject, validated.predicate, validated.object)]
        return []

    @staticmethod
    def _log_clean_report(report: CleanReport, *, type_name: str, phase: str) -> None:
        """Surface the A3 clean ledger for one enrichment write (ONTA-344).

        Emits the partition COUNTS, and — the point of the ledger — a structured
        record of every DROPPED value (non-conforming primitives enrichment used to
        skip silently), so a caller can see WHAT was not written and WHY. No-op when
        nothing was cleaned."""
        if report.total == 0:
            return
        counts = report.counts()
        _host().logger.info("enrichment_clean_report", type=type_name, phase=phase, **counts)
        for fact in report.dropped:
            _host().logger.warning(
                "enrichment_value_dropped",
                type=type_name,
                phase=phase,
                entity=fact.entity_id,
                attr=fact.attribute,
                value=fact.raw_value,
                datatype=fact.datatype,
                reason=fact.reason,
            )
