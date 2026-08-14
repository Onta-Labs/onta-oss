"""Declare enriched attributes + build initial-fill instance triples."""

from __future__ import annotations

from infona_client.enrichment.executor_const import ENRICH_ATTR_DESCRIPTION
from infona_client.enrichment.executor_helpers import (
    _host,
    _infer_datatype_from_values,
    _infer_relationship_target,
)
from infona_client.enrichment.models import ConflictPolicy, RowResult
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.graph.ontology_queries import PRIMITIVE_TYPES
from infona_client.graph.queries import tenant_graph_uri
from infona_client.models.ontology import OntologyMutation, OntologyOpKind
from infona_client.resolver.models import CleanReport


class EnrichmentDeclareMixin:
    """Ontology upsert + initial-fill triple selection."""

    def _select_triples_for_policy(
        self,
        rows: list[RowResult],
        type_name: str,
        policy: ConflictPolicy,
        resolved_datatypes: dict[str, str],
    ) -> list[tuple[str, str, str]]:
        """Build the instance triples to write for the INITIAL-FILL / skip path.

        Used only for the non-refresh write policy (``skip``, from ``skip``/``stage``
        — a conflict-free fill); a refresh (verify/overwrite) instead routes each
        primary value through the P6 supersession op (:meth:`_apply_refresh_writes`,
        ONTA-279). ``resolved_datatypes`` is the ``{attribute -> datatype}`` map
        :meth:`_declare_attributes` just declared, so each primary value is TYPED
        with the SAME datatype its attribute is DECLARED with (P1 fix). The primary
        value goes through :meth:`_instance_triples_for_value` (relationship → IRI;
        primitive → ``validate_triple`` typed literal, skipped if non-conforming);
        the citation companions (``*_source_url`` / ``*_provenance``) stay plain
        string literals exactly as before. The one typed exception is
        ``*_verified_at`` — a timestamp, not a citation string — which
        :meth:`_provenance_triples` emits as a typed ``xsd:dateTime`` literal so the
        NL planner's typed date FILTERs match it (an untyped string would be
        type-incompatible and silently drop the row)."""
        triples: list[tuple[str, str, str]] = []
        clean_report = CleanReport()  # A3 ledger: partitions every primitive fill value
        for r in rows:
            if not self._row_is_applied(r, policy):
                continue
            # Default to ``string`` if a datatype somehow wasn't resolved for this
            # attribute (defensive — _declare_attributes covers every applied attr).
            datatype = resolved_datatypes.get(r.attribute, "string")
            value_triples = self._instance_triples_for_value(
                r.entity_uri, type_name, r.attribute, r.verdict.value, datatype,
                clean_report=clean_report,
            )
            # Only stamp provenance for a value that was ACTUALLY written. A rejected
            # primitive (validate_triple → no triple) writes no primary value, so
            # emitting fresh `_source_url` / `_verified_at` here would falsely cite a
            # source on a value that was never stored. Gate the citation on the same
            # condition as the primary value (reviewer finding).
            if not value_triples:
                continue
            triples.extend(value_triples)
            # Provenance companions are user-facing citations (URLs / free text) —
            # plain string literals — EXCEPT `<attr>_verified_at`, which
            # _provenance_triples types as xsd:dateTime so typed date FILTERs match.
            triples.extend(self._provenance_triples(r.entity_uri, type_name, r.attribute, r.verdict))
        self._log_clean_report(clean_report, type_name=type_name, phase="fill")
        return triples

    def _applied_attribute_values(
        self, rows: list[RowResult], policy: ConflictPolicy
    ) -> dict[str, list[str]]:
        """The PRIMARY attribute names that ACTUALLY received a written value
        under ``policy``, mapped to the list of string VALUES applied for each —
        the set whose ontology declarations the apply step upserts so an enriched
        attribute becomes first-class schema (visible in the /schema view, the
        Explorer column schema, and the Enrich dialog's predicate dropdown).
        Attributes that found nothing are excluded so enrichment never pollutes
        the ontology with empty slots. Insertion-ordered + value-accumulating so
        the caller issues one declaration per attribute (not one per row) AND can
        infer that attribute's datatype from the actual values written.

        The provenance companions are deliberately NOT here (ONTA-262): they are
        metadata OF an attribute, minted on the attr_meta namespace and never
        declared as ontology attributes — declaring them was exactly what made
        `<attr>_provenance` / `<attr>_verified_at` render as sibling columns in
        every schema surface. Their instance triples still ride the same write
        (:meth:`_select_triples_for_policy`)."""
        out: dict[str, list[str]] = {}

        for r in rows:
            if not self._row_is_applied(r, policy):
                continue
            out.setdefault(r.attribute, []).append(r.verdict.value)
        return out

    async def _resolve_declared_datatype(
        self,
        onto_graph: str,
        type_name: str,
        attr_name: str,
        values: list[str],
        *,
        tenant_id: str | None = None,
    ) -> str:
        """Resolve the ``datatype`` to declare for one enriched attribute, never
        DOWNGRADING an existing richer range.

        Two inputs combine:
          1. The datatype INFERRED from the actual applied ``values`` (integer /
             float / string) — so a numeric enriched attribute is typed, not
             stamped ``xsd:string`` blindly.
          2. The attribute's range as ALREADY declared in the ontology. If that
             existing range is anything other than ``xsd:string`` — a richer XSD
             primitive (integer/float/dateTime) OR a relationship ``types/<X>``
             URI declared by ingestion — it is PRESERVED verbatim; enrichment must
             not clobber an ingest-inferred integer or a relationship edge down to
             a string.

        Net rule: ``existing_range if (existing_range and existing_range !=
        xsd:string) else inferred``. With no existing range, or an existing
        ``xsd:string``, the inferred datatype wins (so a brand-new attribute is
        typed correctly, and a previously-untyped string slot can be upgraded)."""
        inferred = _infer_datatype_from_values(values)
        del onto_graph  # catalog-only; SPARQL range query is retired (ONTA-527)
        declared_type_names: list[str] = []
        if inferred not in PRIMITIVE_TYPES:
            return inferred
        if not tenant_id:
            return _infer_relationship_target(attr_name) or inferred
        try:
            from infona_client.graph.ontology_catalog import list_attributes, list_types
            from infona_client.graph.store import GraphConfigError

            try:
                declared_type_names = [
                    t.name
                    for t in await list_types(tenant_id=tenant_id, layer="tenant")
                    if getattr(t, "name", None)
                ]
            except Exception:  # noqa: BLE001 — type list is advisory
                declared_type_names = []
            attrs = await list_attributes(
                tenant_id=tenant_id, type_name=type_name, layer="tenant"
            )
            match = next((a for a in attrs if a.name == attr_name), None)
            if match is not None:
                if match.kind == "relationship" and match.range_type:
                    return match.range_type
                if match.datatype and match.datatype != "string":
                    return match.datatype
            # Existing string (or no declaration) can UPGRADE to a relationship
            # when the leaf is org-valued (lead_sponsor → Company). Values are
            # labels, so inferred is "string".
            return (
                _infer_relationship_target(attr_name, declared_type_names)
                or inferred
            )
        except GraphConfigError:
            _host().logger.error(
                "enrich_declare_range_no_store",
                type_name=type_name,
                attr=attr_name,
            )
            return _infer_relationship_target(attr_name, declared_type_names) or inferred
        except Exception:  # noqa: BLE001 — never fail a write over a range read
            _host().logger.exception(
                "enrich_declare_range_catalog_failed",
                type_name=type_name,
                attr=attr_name,
            )
            return _infer_relationship_target(attr_name, declared_type_names) or inferred

    async def _declare_attributes(
        self,
        tenant_id: str,
        type_name: str,
        attr_values: dict[str, list[str]],
        *,
        kg_name: str | None = None,
    ) -> dict[str, str]:
        """Upsert each enrichment-applied attribute's ontology declaration into the
        TENANT (ontology) graph so it becomes first-class schema. Reuses the same
        idempotent :func:`upsert_attribute` the ontology endpoint uses
        (``rdf:Property ; rdfs:label ; rdfs:domain <Type> ; rdfs:range <…>``), one
        update per attribute. The declared ``rdfs:range`` is resolved per attribute
        by :meth:`_resolve_declared_datatype`: inferred from the actual applied
        values, but never downgrading an existing richer range. Called BEFORE the
        instance write (declare schema, then write data) and inside the job's
        try/except so a declaration failure fails the job, consistent with
        existing behavior.

        ``attr_values`` maps each applied attribute name (primary + provenance
        companions) to the string values written for it.

        Returns the ``{attribute_name -> resolved_datatype}`` map so the caller can
        type each INSTANCE value with the SAME datatype the attribute is DECLARED
        with (P1 data-correctness fix): a numeric value must be stored as a typed
        literal (``"92"^^xsd:integer``) matching the declared integer range, not as
        a bare ``xsd:string`` literal the typed NL filters then miss. Computing the
        datatype ONCE here and reusing it for both the declaration and the value
        typing is what keeps the declared range and the stored literal in lock-step.
        The provenance companions resolve to ``string`` (URLs / free text) and are
        intentionally never typed as anything richer."""
        onto_graph = tenant_graph_uri(tenant_id)
        resolved: dict[str, str] = {}
        for name, values in attr_values.items():
            datatype = await self._resolve_declared_datatype(
                onto_graph, type_name, name, values, tenant_id=tenant_id
            )
            resolved[name] = datatype
            if datatype not in PRIMITIVE_TYPES:
                await commit_ontology(
                    self._neptune,
                    onto_graph,
                    [
                        OntologyMutation(
                            op=OntologyOpKind.UPSERT_TYPE,
                            type_name=datatype,
                        ),
                        OntologyMutation(
                            op=OntologyOpKind.UPSERT_RELATIONSHIP,
                            type_name=type_name,
                            slot_name=name,
                            target_type=datatype,
                            description=ENRICH_ATTR_DESCRIPTION,
                        ),
                    ],
                )
                if kg_name:
                    await self._promote_literal_attr_to_nodes(
                        tenant_id,
                        kg_name,
                        type_name,
                        name,
                        datatype,
                        extra_values=values,
                    )
            else:
                await commit_ontology(
                    self._neptune,
                    onto_graph,
                    [OntologyMutation(
                        op=OntologyOpKind.UPSERT_ATTRIBUTE,
                        type_name=type_name,
                        slot_name=name,
                        datatype=datatype,
                        description=ENRICH_ATTR_DESCRIPTION,
                    )],
                )
        return resolved
