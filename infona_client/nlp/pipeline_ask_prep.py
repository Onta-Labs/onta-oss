"""Load planning ontology, schema inventory, alias map, Cypher examples.

Invariant: semantic top-K must not hide THIS-KG populated types.
"""
from __future__ import annotations

import time
from typing import Any

import structlog

from infona_client.models.query import NLResult
from infona_client.nlp.cypher_generate import ontology_from_graph_store
from infona_client.nlp.pipeline_helpers import ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR
from infona_client.nlp.token_usage import STAGE_REPHRASE, TokenUsageLedger, pop_attached_usage, stage_for_attempt

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class PipelineAskPrepMixin:
    async def _ask_cypher_load_ontology(self, st) -> None:
        """Populate ``st`` with ontology / inventory / examples for one /ask."""
        question = st.question
        graph_uri = st.graph_uri
        data_graph = st.data_graph
        layer_graph_uris = st.layer_graph_uris
        exclude_questions = st.exclude_questions
        t0 = st.t0
        timing = st.timing
        tenant_id = st.tenant_id
        kg_name = st.kg_name
        store = st.store

        # ---- Ontology context (populated GraphStore → semantic → sparql) ----
        # Planning truth is instance-populated schema for THIS KG (declared-empty
        # edges demoted). Semantic retrieval ranks extra declared types when the
        # catalog is large, and is the fallback text when GraphStore has no rows.
        # It must not hide a type that has instances in THIS kg.
        ontology = ""
        type_names: list[str] = []
        ontology_source = "full"
        kg_active_types: set[str] | None = None
        kg_declared_names: list[str] | None = None
        full_ontology_loaded = False
        semantic_text: str | None = None
        semantic_type_names: list[str] | None = None

        from infona_client.nlp import pipeline as _pl
        embedding_svc = _pl.get_embedding_service()
        if embedding_svc:
            try:
                from infona_client.config import settings

                try:
                    declared = await embedding_svc.type_names(graph_uri)
                    active_types = (
                        await self._active_types(
                            data_graph, graph_uri, declared_names=declared
                        )
                        if declared
                        else None
                    )
                    kg_declared_names = list(declared) if declared else None
                    kg_active_types = active_types
                except Exception:
                    logger.warning(
                        "active_types_probe_failed",
                        instance_graph=data_graph,
                        exc_info=True,
                    )
                    active_types = None
                    kg_active_types = None
                    kg_declared_names = None
                semantic = await embedding_svc.retrieve(
                    graph_uri,
                    question,
                    top_k=settings.embeddings_top_k,
                    active_types=active_types,
                )
                if semantic:
                    from infona_client.nlp.cypher_generate import (
                        extract_type_names_from_ontology,
                    )

                    semantic_text = semantic
                    semantic_type_names = extract_type_names_from_ontology(semantic)
                    timing["ontology_scope"] = (
                        "kg" if active_types is not None else "tenant"
                    )
                    timing["semantic_type_count"] = float(
                        len(semantic_type_names or [])
                    )
            except Exception:
                pass

        # Semantic top-K is ranking / extra context, not a license to hide
        # THIS-KG populated types. Sibling-ingest leftovers (empty
        # BenchIdentifier / KitIdentifier) can outrank Product; if we pass
        # those names as a hard GraphStore filter, Product is dropped from
        # the planning prompt and the model invents prop_key=price.
        populated_type_names: list[str] = (
            sorted(kg_active_types) if kg_active_types else []
        )
        from infona_client.nlp.planning_schema import resolve_planning_type_scope

        plan_scope = resolve_planning_type_scope(
            semantic_names=semantic_type_names,
            populated_names=populated_type_names,
        )
        scope_type_names = (
            list(plan_scope.type_names)
            if plan_scope.type_names is not None
            else None
        )
        force_populated = list(plan_scope.force_include) or None

        if store is not None:
            try:
                ontology, type_names = await ontology_from_graph_store(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    prefer_populated=True,
                    type_names=scope_type_names,
                    force_include=force_populated,
                )
                if ontology:
                    ontology_source = (
                        "graph_store_populated"
                        if semantic_type_names
                        else "graph_store_catalog"
                    )
                    timing["ontology_source"] = ontology_source
                    full_ontology_loaded = not bool(semantic_type_names)
            except Exception:
                logger.debug("cypher_ask_catalog_ontology_failed", exc_info=True)
                ontology = ""

        # Semantic text is the fallback when GraphStore has no catalog rows
        # (embeddings exist but catalog empty / cold). Prefer populated store
        # when both are present so dead declared edges do not win.
        if not ontology and semantic_text:
            ontology = semantic_text
            ontology_source = "semantic"
            timing["ontology_source"] = "semantic"
            type_names = list(semantic_type_names or [])

        if not ontology:
            try:
                fetched = await self._fetch_ontology(
                    graph_uri, data_graph, layer_graph_uris=layer_graph_uris
                )
                if fetched in (ONTOLOGY_FETCH_ERROR, ONTOLOGY_EMPTY):
                    ontology = ""
                elif fetched:
                    ontology = fetched
                    ontology_source = "full"
                    timing["ontology_source"] = "full"
                    full_ontology_loaded = True
            except Exception:
                logger.debug("cypher_ask_ontology_fetch_failed", exc_info=True)
                ontology = ""

        if ontology_source in (
            "full",
            "graph_store_catalog",
            "graph_store_populated",
        ):
            if ontology_source != "graph_store_populated" or not semantic_type_names:
                full_ontology_loaded = True
        timing["ontology_fetch_ms"] = round((time.time() - t0) * 1000, 1)
        # Visible RCA: which types the prompt saw vs retrieve vs THIS-KG live.
        timing["ontology_type_names"] = ", ".join(type_names or [])[:400]
        timing["semantic_type_names"] = ", ".join(semantic_type_names or [])[:400]
        timing["populated_type_names"] = ", ".join(populated_type_names)[:400]
        if plan_scope.ignored_semantic:
            timing["ontology_semantic_ignored"] = 1.0

        # Schema-valid allowlist: prefer live GraphStore catalog + instance-
        # populated leaves for THIS tenant+kg. Ontology text is fallback only
        # when the store probe fails (sparse text must not reject real
        # unit_cost / located_at / has_* inventory that vis+export show).
        schema_inventory = None
        if store is not None and tenant_id and kg_name:
            try:
                from infona_client.nlp.schema_valid_cypher import (
                    inventory_from_graph_store,
                )

                schema_inventory = await inventory_from_graph_store(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    # Full KG inventory — not semantic top-K alone — so
                    # schema-valid does not thrash on out-of-window leaves.
                    type_names=None,
                )
                if schema_inventory is not None and not schema_inventory.empty:
                    timing["schema_valid_inventory_source"] = "graph_store"
                    timing["schema_valid_inventory_rels"] = float(
                        len(schema_inventory.relationship_leaves)
                    )
                    timing["schema_valid_inventory_attrs"] = float(
                        len(schema_inventory.attribute_leaves)
                    )
            except Exception:
                logger.debug(
                    "schema_valid_inventory_probe_failed",
                    exc_info=True,
                )
                schema_inventory = None
        if schema_inventory is None or schema_inventory.empty:
            if ontology:
                from infona_client.nlp.schema_valid_cypher import (
                    OntologyLeafInventory,
                )

                schema_inventory = OntologyLeafInventory.from_ontology(ontology)
                timing["schema_valid_inventory_source"] = "ontology_text"
            else:
                schema_inventory = None
                timing["schema_valid_inventory_source"] = "empty"

        # Attribute-alias map (ADR 0002 §7) — leaf renames for Cypher property keys.
        alias_map: dict[str, str] = {}
        if self._aliases_enabled:
            alias_map = await self._fetch_alias_map(graph_uri)

        # Cypher-mode examples only.
        examples_text = ""
        try:
            from infona_client.nlp.example_bank import (
                format_examples_for_prompt,
                get_example_bank,
            )

            bank = get_example_bank()
            if bank and bank._examples:
                examples = await bank.retrieve(
                    question=question,
                    ontology_context=ontology,
                    exclude_questions=exclude_questions or [],
                    kg_name=kg_name,
                    top_k=3,
                    language="cypher",
                    tenant_id=tenant_id,
                )
                if examples:
                    examples_text = format_examples_for_prompt(
                        examples, language="cypher"
                    )
                    cypher_n = sum(
                        1
                        for ex in examples
                        if (getattr(ex, "cypher", None) or "").strip()
                    )
                    timing["examples_retrieved"] = float(cypher_n)
                    if not examples_text:
                        examples_text = ""
        except Exception:
            pass

        st.ontology = ontology
        st.type_names = type_names
        st.ontology_source = ontology_source
        st.kg_active_types = kg_active_types
        st.kg_declared_names = kg_declared_names
        st.full_ontology_loaded = full_ontology_loaded
        st.semantic_type_names = semantic_type_names
        st.populated_type_names = populated_type_names
        st.schema_inventory = schema_inventory
        st.alias_map = alias_map
        st.examples_text = examples_text
