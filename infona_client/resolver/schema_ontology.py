from __future__ import annotations

"""Ontology fetch / refresh / commit helpers for SchemaResolver.

Job: read the tenant ontology + parent map, refresh mid-ingest, reconcile
stale version stamps, and commit mutations through the ONE
``commit_ontology`` API. Do not hand-roll SPARQL INSERTs for schema.
"""

from infona_client.graph.iri import TYPE_URI_PREFIX
from infona_client.graph.layers import LayerStack, type_name_from_uri
from infona_client.graph.ontology_commit import commit_ontology, commit_ontology_unlocked
from infona_client.graph.ontology_queries import (
    PRIMITIVE_TYPES,
    TEXT_KIND_FREE_TEXT,
    get_full_ontology_query,
    ontology_version,
    parent_map_query,
)
from infona_client.graph.parser import parse_sparql_results
from infona_client.models.ontology import OntologyMutation, OntologyOpKind
from infona_client.resolver.attribute_resolver import AttributeSchema
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


class SchemaOntologyMixin:
    """Ontology read/write half of SchemaResolver."""

    async def _commit_ontology(
        self,
        graph_uri: str,
        mutations: list[OntologyMutation],
        *,
        holding_lock: bool = False,
    ):
        """Apply schema mutations through the ONE commit API (ONTA-403).

        ``holding_lock=True`` when the caller already holds ``self._ontology_lock``
        (e.g. inside ``_resolve_type``) — asyncio.Lock is not reentrant.
        ``holding_lock=False`` (default) takes the shared lock via
        :func:`commit_ontology` so pass-2 writes serialize with REST commits.
        """
        if not mutations:
            return None
        if holding_lock:
            return await commit_ontology_unlocked(self._neptune, graph_uri, mutations)
        return await commit_ontology(self._neptune, graph_uri, mutations)

    def _mut_type(self, name: str, description: str | None = None, parent_type: str | None = None) -> OntologyMutation:
        return OntologyMutation(
            op=OntologyOpKind.UPSERT_TYPE,
            type_name=name,
            description=description,
            parent_type=parent_type,
        )

    def _mut_subclass(self, child: str, parent: str) -> OntologyMutation:
        return OntologyMutation(
            op=OntologyOpKind.SET_SUBCLASS,
            type_name=child,
            parent_type=parent,
        )

    def _mut_comment(self, name: str, description: str) -> OntologyMutation:
        return OntologyMutation(
            op=OntologyOpKind.SET_COMMENT,
            type_name=name,
            description=description,
        )

    def _mut_attr(self, type_name: str, attr_name: str, datatype: str = "string", description: str = "") -> OntologyMutation:
        # Non-primitive datatype ⇒ relationship-ranged attribute.
        if datatype not in PRIMITIVE_TYPES:
            return OntologyMutation(
                op=OntologyOpKind.UPSERT_RELATIONSHIP,
                type_name=type_name,
                slot_name=attr_name,
                target_type=datatype,
                description=description if description else "",
            )
        return OntologyMutation(
            op=OntologyOpKind.UPSERT_ATTRIBUTE,
            type_name=type_name,
            slot_name=attr_name,
            datatype=datatype,
            description=description if description else "",
        )

    def _mut_range(self, type_name: str, attr_name: str, target_type: str) -> OntologyMutation:
        # description=None → range-only upgrade (preserves comment).
        return OntologyMutation(
            op=OntologyOpKind.UPSERT_RELATIONSHIP,
            type_name=type_name,
            slot_name=attr_name,
            target_type=target_type,
            description=None,
        )

    def _mut_text_kind(self, type_name: str, attr_name: str, kind: str = TEXT_KIND_FREE_TEXT) -> OntologyMutation:
        return OntologyMutation(
            op=OntologyOpKind.SET_TEXT_KIND,
            type_name=type_name,
            slot_name=attr_name,
            text_kind=kind,
        )
    @staticmethod
    def _layer_stack_for(tenant_id: str, graph_uri: str) -> LayerStack:
        """Build the LayerStack for an ingest run (ONTA-397).

        Uses the OSS entitlement seam with a minimal TenantContext keyed by
        ``tenant_id``. Env-allowlisted workspaces (and Clerk-stamped ones when
        the bit is available via a richer context elsewhere) see Enhanced;
        everyone else degrades to Tenant > Public. Never consults client input.
        """
        from infona_client.auth.api_keys import TenantContext
        from infona_client.graph.entitlement import is_entitled

        return LayerStack(
            tenant_graph_uri=graph_uri,
            entitled=is_entitled(TenantContext(tenant_id=tenant_id, api_key="")),
        )

    async def _ontology_bindings(self, graph_uri: str) -> list[dict] | None:
        """Rows for :meth:`_fetch_ontology`'s assembly loop — catalog first (ONTA-534).

        The read was ``get_full_ontology_query`` over the SPARQL HTTP client,
        which is RETIRED under the shipped Neo4j GraphStore: every ingest raised
        ``SparqlClientRetired``, the ``except`` below logged
        ``ontology_fetch_failed``, and the ingest planned against an EMPTY
        ontology. That is not a degraded summary — it is the input
        :meth:`TypeMatcher.match` short-circuits on (``type_match_auto_new``,
        ``reason="empty_ontology"``), so every type gets re-minted from scratch
        instead of matched against what the workspace already declares.

        **Tenant layer only, deliberately.** The SPARQL arm read exactly ONE
        graph — the tenant ontology graph the caller passes — and this arm reads
        exactly the same scope. The ``LayerStack`` built next to it
        (:meth:`_layer_stack_for`) is for the layered *parent map* (ONTA-397):
        subclass edges may span layers, so climbing them must. Folding
        Public/Enhanced type NAMES into ``existing_types`` is a different
        decision — the matcher would then resolve a proposal onto a global type
        that the tenant graph does not declare and ingest would write instance
        data typed against it — so that stays out of this fix.

        Returns the binding rows, or ``None`` when neither arm can answer.
        """
        from infona_client.graph.layers import Layer
        from infona_client.nlp.pipeline_ontology_catalog import layer_ontology_bindings

        # Same reader the NL planner (PR #447) and the Explorer ontology browser
        # use, in `get_full_ontology_query`'s own binding shape — so the loop
        # below stays ONE code path over store rows and SPARQL rows. A store
        # threaded onto the resolver wins; otherwise the catalog session
        # resolves the process store (and a missing one is a decline, not a
        # raise, because it is raised INSIDE the reader's own guard).
        bindings = await layer_ontology_bindings(
            Layer.TENANT,
            onto_graph=graph_uri,
            store=getattr(self, "_graph_store", None),
        )
        if bindings is not None:
            return bindings
        # Residual SPARQL arm, unchanged: still what answers when the store
        # declines (no workspace in the graph URI, no store, store error, or
        # nothing declared yet), and still exercised by the dual-arm tests.
        try:
            raw = await self._neptune.query(get_full_ontology_query(graph_uri))
            _, sparql_bindings = parse_sparql_results(raw)
        except Exception:
            _sr.logger.warning("ontology_fetch_failed", exc_info=True)
            return None
        return sparql_bindings

    async def _fetch_ontology(
        self, graph_uri: str
    ) -> tuple[dict[str, str], dict[str, dict[str, AttributeSchema]]]:
        """Fetch existing types and attributes for this workspace.

        Returns:
            (types: {name: description}, attrs: {type_name: {attr_name: schema}})
        """
        bindings = await self._ontology_bindings(graph_uri)
        if bindings is None:
            return {}, {}

        types: dict[str, str] = {}
        attrs: dict[str, dict[str, AttributeSchema]] = {}

        for row in bindings:
            type_label = row.get("typeLabel", "")
            if not type_label:
                continue
            if type_label not in types:
                types[type_label] = ""
                attrs[type_label] = {}
            if row.get("attrLabel"):
                range_str = row.get("range", "")
                type_uri_prefix = TYPE_URI_PREFIX
                if range_str.startswith(type_uri_prefix):
                    # Range is a reference to another ontology type
                    datatype = range_str[len(type_uri_prefix):]
                elif "#" in range_str:
                    fragment = range_str.split("#")[-1]
                    # Map XSD names to our datatype names. The `dateTime` /
                    # `Resource` spellings are the SPARQL arm's (an rdfs:range
                    # minted by `_datatype_to_xsd`); `datetime` / `uri` / `geo`
                    # are the catalog arm's, which re-mints the IRI from the
                    # stored primitive NAME. Both spell the same datatype and
                    # no writer emits the catalog spellings as an rdfs:range, so
                    # adding them leaves the SPARQL arm byte-identical while
                    # keeping a declared `datetime` column from silently reading
                    # back as `string` once the fetch runs off the catalog.
                    # One asymmetry stays, deliberately: `geo` reads back as
                    # `geo` from the catalog but the SPARQL spelling
                    # (`geosparql#wktLiteral`) still falls through to `string`,
                    # as it always has. Both are in PRIMITIVE_TYPES, so
                    # literal-vs-relationship is unaffected either way, and
                    # changing the retired arm's answer is not this fix's job.
                    dt_map = {
                        "string": "string", "integer": "integer", "float": "float",
                        "boolean": "boolean", "dateTime": "datetime", "Resource": "uri",
                        "datetime": "datetime", "uri": "uri", "geo": "geo",
                    }
                    datatype = dt_map.get(fragment, "string")
                else:
                    datatype = "string"
                attrs[type_label][row["attrLabel"]] = AttributeSchema(
                    name=row["attrLabel"], datatype=datatype,
                )

        return types, attrs

    async def _fetch_parent_map(
        self, graph_uri: str, layer_stack: LayerStack | None = None
    ) -> dict[str, str]:
        """Fetch the child->parent subclass map (keyed by type *name*).

        Reads every rdfs:subClassOf edge via parent_map_query and reduces each
        URI to its type name so it can feed the pure hierarchy helpers
        (ancestor_chain / config_for_with_hierarchy). Returns {} on any error —
        callers degrade to flat (zero-hierarchy) behavior.

        Layer-aware variant (ADR 0002 §1, COG-37): pass a LayerStack and the
        edges are read from the UNION of the tenant's visible layer graphs in
        one query — subClassOf edges may span layers (a tenant leaf under a
        Public parent). Duplicate child names are resolved by shadowing: edges
        from higher-precedence layers (Tenant > Enhanced > Public) win. With
        no layer_stack the single-graph behavior is exactly as before.
        """
        if layer_stack is None:
            try:
                raw = await self._neptune.query(parent_map_query(graph_uri))
                _, bindings = parse_sparql_results(raw)
            except Exception:
                _sr.logger.warning("parent_map_fetch_failed", exc_info=True)
                return {}
            return self._parent_map_from_bindings(bindings)

        try:
            raw = await self._neptune.query(
                parent_map_query(layer_stack.visible_graph_uris())
            )
            _, bindings = parse_sparql_results(raw)
        except Exception:
            _sr.logger.warning("parent_map_fetch_failed", exc_info=True)
            return {}

        rows_by_graph: dict[str, list[dict]] = {}
        for row in bindings:
            rows_by_graph.setdefault(row.get("graph", ""), []).append(row)
        # Merge lowest-precedence layer first so higher layers overwrite
        # duplicate child keys — Tenant > Enhanced > Public shadowing.
        parent_of: dict[str, str] = {}
        for g in reversed(layer_stack.visible_graph_uris()):
            parent_of.update(self._parent_map_from_bindings(rows_by_graph.get(g, [])))
        return parent_of

    @staticmethod
    def _parent_map_from_bindings(bindings: list[dict]) -> dict[str, str]:
        """Reduce ?child/?parent URI bindings to a {child_name: parent_name} map.

        Names are extracted via type_name_from_uri, which understands every
        layer namespace — so a tenant-graph edge whose PARENT is a Public-layer
        URI (`types/public/Person`) keys correctly instead of being dropped.
        Edges with either end outside all layer namespaces are skipped, as are
        self-edges.
        """
        parent_of: dict[str, str] = {}
        for row in bindings:
            child_name = type_name_from_uri(row.get("child", ""))
            parent_name = type_name_from_uri(row.get("parent", ""))
            if child_name and parent_name and child_name != parent_name:
                parent_of[child_name] = parent_name
        return parent_of
    async def _refresh_ontology(
        self,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
    ) -> None:
        """Re-fetch ontology from Neptune and merge into in-memory state.

        Additive merge only: new types/attrs from concurrent ingestions are added,
        but nothing is removed (this ingestion may have added types not yet visible).
        """
        fresh_types, fresh_attrs = await self._fetch_ontology(graph_uri)
        added = 0
        for t, desc in fresh_types.items():
            if t not in existing_types:
                existing_types[t] = desc
                added += 1
        for t, attrs in fresh_attrs.items():
            if t not in existing_attrs:
                existing_attrs[t] = attrs
            else:
                for a, schema in attrs.items():
                    if a not in existing_attrs[t]:
                        existing_attrs[t][a] = schema
        if added:
            _sr.logger.info("ontology_refreshed", new_types=added)

    async def _reconcile_ontology_version(
        self,
        graph_uri: str,
        stamped_version: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        parent_of: dict[str, str],
    ) -> str:
        """ONTA-270 optimistic-concurrency guard: reject-and-recompute a STALE A5
        placement plan at P6 apply time. Returns the CURRENT ontology version.

        ``stamped_version`` is the fingerprint :meth:`ingest` computed at the TOP
        of this run — the ontology state P5 planned against, read BEFORE the long,
        async LLM extraction. Here, at the START of the apply, we re-read the
        CURRENT ontology under the ontology-write lock (so the compare + any
        per-type mint pass 1 then does can't interleave with a concurrent writer)
        and fingerprint it:

        * **Match** (the common case — no ontology write landed during our
          extraction): the plan is fresh, so we return and pass 1 applies it
          unchanged. Cost is one ontology read; nothing is mutated.
        * **Mismatch**: a concurrent run advanced the ontology T→T+1 while we were
          extracting, so the placement about to be applied was computed against a
          STALE snapshot and would mint duplicate terms (a synonym of a type the
          other run just created, a re-declared attribute). We REJECT that stale
          basis and RECOMPUTE by refreshing the in-place snapshot to the current
          ontology (additive merge, mirroring :meth:`_refresh_ontology`), so pass
          1's type/attribute resolution runs against T+1 and lands on the existing
          terms instead of duplicating them.

        Complements ONTA-268: 268's ontology-write lock serializes INDIVIDUAL
        mutations; this version stamp catches a whole PLAN computed before another
        run advanced the ontology — the read-modify-write side of the same race.
        """
        async with self._ontology_lock:
            fresh_types, fresh_attrs = await self._fetch_ontology(graph_uri)
            # Reuse the run's LayerStack when available so reconcile sees the
            # same layered parent map ingest planned against (ONTA-397).
            stack = getattr(self, "_active_layer_stack", None)
            fresh_parent = await self._fetch_parent_map(graph_uri, layer_stack=stack)
            current = ontology_version(fresh_types, fresh_attrs, fresh_parent)
            if current == stamped_version:
                return current
            _sr.logger.info(
                "stale_placement_plan_recomputed",
                stamped_version=stamped_version,
                current_version=current,
                graph_uri=graph_uri,
            )
            # Additive merge (never remove — this run hasn't written yet, so the
            # snapshot == ingest-top state; we only need the concurrent run's new
            # terms). setdefault keeps any snapshot entry, adds the fresh ones.
            for t, desc in fresh_types.items():
                existing_types.setdefault(t, desc)
            for t, attrs in fresh_attrs.items():
                dst = existing_attrs.setdefault(t, {})
                for a, schema in attrs.items():
                    dst.setdefault(a, schema)
            for child, parent in fresh_parent.items():
                parent_of.setdefault(child, parent)
            return current
