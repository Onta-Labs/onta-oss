"""Full-ontology fetch + summary assembly for NL planning.

Invariant: never drop THIS-KG populated types; annotate ``[no instances]``.
"""
from __future__ import annotations

import time

import structlog

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import skip_invalid_type_name
from infona_client.nlp.pipeline_helpers import (
    MAX_ENUM_DISCOVERY_CONCURRENCY,
    ONTOLOGY_CACHE_TTL,
    ONTOLOGY_EMPTY,
    ONTOLOGY_FETCH_ERROR,
    _active_types_cache,
    _active_types_cache_key,
    _ontology_cache,
    _store_active_types,
)

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")

class PipelineOntologyMixin:
    async def _fetch_ontology(
        self,
        graph_uri: str,
        instance_graph: str | None = None,
        layer_graph_uris: list[str] | None = None,
    ) -> str:
        # Cache key includes instance graph + layer stack so different KGs and
        # entitlement shapes get the correct filtered ontology (ONTA-397).
        layers_key = ",".join(layer_graph_uris or ())
        cache_key = f"{graph_uri}|{instance_graph or ''}|{layers_key}"
        cached = _ontology_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < ONTOLOGY_CACHE_TTL:
            return cached[0]

        from infona_client.graph.layers import (
            Layer,
            enhanced_graph_uri,
            layer_type_uri,
            public_graph_uri,
            type_name_from_uri,
        )
        from infona_client.graph.ontology_queries import get_full_ontology_query, type_uri, attr_uri

        def _layer_for_graph(g: str) -> Layer:
            if g == public_graph_uri():
                return Layer.PUBLIC
            if g == enhanced_graph_uri():
                return Layer.ENHANCED
            return Layer.TENANT

        def _attr_uri_for(layer: Layer, type_name: str, attr_name: str) -> str:
            if layer is Layer.TENANT:
                return attr_uri(type_name, attr_name)
            return f"{layer_type_uri(layer, type_name)}/attrs/{attr_name}"

        def _type_uri_for(layer: Layer, type_name: str) -> str:
            if layer is Layer.TENANT:
                return type_uri(type_name)
            return layer_type_uri(layer, type_name)

        try:
            # Which types actually have instances is resolved AFTER the schema
            # read below (ONTA-427). The declared type list is what makes the
            # cheap, bounded form of that question possible.
            active_types: set[str] | None = None
            # Populated only if we end up running the UNBOUNDED scan, so the
            # schema-missing fallback further down can reuse it instead of
            # scanning the instance graph a second time.
            scanned_instance_types: set[str] | None = None

            # Graphs in precedence order (first wins under shadowing). When no
            # layer stack is threaded, behaviour is exactly the pre-ONTA-397
            # single tenant-graph read.
            ontology_graphs = list(layer_graph_uris) if layer_graph_uris else [graph_uri]

            types: dict[str, dict] = {}
            type_layers: dict[str, Layer] = {}
            for onto_g in ontology_graphs:
                layer = _layer_for_graph(onto_g)
                try:
                    raw = await self.neptune.query(get_full_ontology_query(onto_g))
                    _, bindings = parse_sparql_results(raw)
                except Exception:
                    # Per-layer degradation (ADR 0002 §1): a missing/erroring
                    # global layer contributes nothing; others still load.
                    logger.warning(
                        "layer_ontology_fetch_failed",
                        graph_uri=onto_g,
                        layer=layer.value,
                        exc_info=True,
                    )
                    continue
                for row in bindings:
                    tl = row.get("typeLabel", "")
                    if not tl:
                        continue
                    # Fail SOFT on a corrupt stored label (ONTA-425). This whole
                    # block sits under one `except Exception: return
                    # ONTOLOGY_FETCH_ERROR`, so letting `_type_uri_for` /
                    # `_attr_uri_for` raise on ONE bad name would replace the
                    # ENTIRE schema summary with "ontology unavailable" for every
                    # NL query in the workspace — the infona-oss#274 all-or-nothing
                    # failure, on the hottest read path there is. One unqueryable
                    # type is the honest cost; a blinded planner is not.
                    if skip_invalid_type_name(tl, "ask_ontology_summary"):
                        continue
                    # NOTE: we no longer drop a declared type that is absent from
                    # `active_types` here (ONTA-258). Every declared type is parsed
                    # in; types with no instances in the queried KG are annotated
                    # "[no instances]" during summary assembly below instead of being
                    # hidden. See the empty-type handling after this loop.
                    #
                    # Shadowing (ONTA-397): the first visible layer that defines
                    # this name wins; later layers' definitions are skipped.
                    if tl not in types:
                        types[tl] = {
                            "attributes": [],
                            "relationships": [],
                            "functions": set(),
                        }
                        type_layers[tl] = layer
                    elif type_layers.get(tl) is not layer:
                        # Already claimed by a higher-precedence layer.
                        continue
                    # Same fail-soft rule for the ATTRIBUTE half: its label is
                    # equally a stored literal, and `_attr_uri_for` mints an IRI
                    # from it below. Skipping only the attribute (not the whole
                    # row) keeps the row's function binding.
                    if row.get("attrLabel") and not skip_invalid_type_name(
                        row["attrLabel"], "ask_ontology_attr"
                    ):
                        attr_name = row["attrLabel"]
                        range_str = row.get("range", "")
                        target_type = type_name_from_uri(range_str) if range_str else None
                        if target_type:
                            # Relationship predicates use onto/ namespace in instance data
                            onto_uri = f"{IRI_BASE}/onto/{attr_name}"
                            entry = f"{attr_name} → {target_type} — predicate URI: <{onto_uri}>"
                            if entry not in types[tl]["relationships"]:
                                types[tl]["relationships"].append(entry)
                        else:
                            dtype = range_str.split("#")[-1] if "#" in range_str else "string"
                            a_uri = _attr_uri_for(type_layers[tl], tl, attr_name)
                            entry = f"{attr_name} ({dtype}) — URI: <{a_uri}>"
                            if entry not in types[tl]["attributes"]:
                                types[tl]["attributes"].append(entry)
                    if row.get("funcName"):
                        types[tl]["functions"].add(row["funcName"])

            # ── Which declared types carry instances? (ONTA-258 signal) ──────
            # This used to run BEFORE the schema read, as one unbounded
            # `SELECT DISTINCT ?type` over the entire instance graph: a full scan
            # of the KG's rdf:type index on every ontology fetch. Because
            # `refresh_after_write` invalidates the ontology cache after EVERY
            # converged write, an active ingest meant essentially every /ask paid
            # for that scan, and paid for it while the same graph was being
            # written (ONTA-427).
            #
            # Now that `types` is known we ask the question the "[no instances]"
            # annotation actually needs, "does THIS declared type have at least
            # one instance?", as one LIMIT-1 index probe per candidate URI. The
            # signal is IDENTICAL (same name-based matching, same layer
            # namespaces, no caching, no staleness added); only the cost changes,
            # from O(entities in the KG) to O(declared types).
            #
            # NOT derived from the Explorer type-stats that `refresh_after_write`
            # already recomputes, even though those carry per-type entity counts:
            # they are fire-and-forget and best-effort (a failed recompute is
            # swallowed), they only cover the tenant type namespace, and their
            # scan applies a PRIMARY-type guard that attributes each entity to a
            # single type, so an entity asserting both a subtype and its
            # supertype contributes to only one of them, and the other would read
            # as 0 instances. Any of those would produce a FALSE "[no instances]"
            # on a populated type, which is precisely the ONTA-258 failure.
            #
            # Shared with the semantic-retrieval path through one TTL cache
            # (ONTA-411) so both build the SAME notion of "in scope for THIS
            # graph" from one probe rather than each running their own.
            if instance_graph and instance_graph != graph_uri:
                active_key = _active_types_cache_key(instance_graph, types)
                cached_active = _active_types_cache.get(active_key)
                # `cached_active[0]` for the same reason `_active_types` checks it:
                # an EMPTY probe result is exactly the "might be mid-ingest" case,
                # and serving it for the rest of the TTL would mark every declared
                # type "[no instances]" on a KG that has just been populated.
                if (
                    cached_active
                    and cached_active[0]
                    and (time.time() - cached_active[1]) < ONTOLOGY_CACHE_TTL
                ):
                    active_types = cached_active[0]
                else:
                    active_types, scanned_instance_types = await self._resolve_active_types(
                        instance_graph, types
                    )
                    _store_active_types(active_key, active_types)

            # A DECLARED type with no correctly-typed instances in the queried KG
            # is KEPT and annotated "[no instances]" — NOT dropped (ONTA-258).
            # This mirrors the ONTA-248 treatment of declared-but-empty
            # attributes/relationships further down. Hiding a declared type made
            # it indistinguishable from a nonexistent one, so the SPARQL-
            # generating LLM asserted "that type doesn't exist" (or silently
            # queried the closest wrong type) instead of returning an honest
            # zero-row answer. `active_types` still scopes which types carry
            # instance data — it no longer decides a declared type's VISIBILITY.
            empty_types: set[str] = (
                {tl for tl in types if tl not in active_types}
                if active_types is not None else set()
            )
            # Declared types that actually carry instances in this KG. When this
            # is zero we fall through to the SAME instance-graph fallback /
            # ONTOLOGY_EMPTY handling as before (ONTA-248): a schema that shares
            # NO type with the instance data is the "schema missing" case, and a
            # summary of only [no instances] types would be worse than the
            # instance-derived fallback.
            active_matched = len(types) - len(empty_types)

            if active_matched == 0:
                # No DECLARED type carries instances in this KG (the schema query
                # returned nothing, or nothing that overlaps the instance data).
                # When querying a SPECIFIC KG (distinct instance graph), that can
                # mean two very different things which look identical here:
                #  (a) instances exist but the base-graph schema hasn't been
                #      written yet (fresh ingest, schema-write lagging) — a basic
                #      "list all X" ask SHOULD still work, so fall back to the
                #      types present in the instance data and emit a distinct
                #      diagnostic instead of the misleading "No ontology" text.
                #  (b) the KG is genuinely empty — keep the original message.
                # This fallback needs EVERY type present in the data, including
                # ones the schema never declared, so it is the one caller that
                # genuinely requires the unbounded scan (the bounded probe only
                # answers about DECLARED types). Run it here rather than on every
                # fetch: reaching this branch at all means no declared type is
                # populated, i.e. the rare cold-start / disjoint-schema case, not
                # the hot path. Reuses the scan if the probe already fell back to
                # it, so this never costs two scans. Only attempt this for a
                # distinct instance graph; a bare tenant/ontology graph with no
                # schema genuinely has no ontology.
                if instance_graph and instance_graph != graph_uri:
                    if scanned_instance_types is None:
                        scanned_instance_types = await self._scan_instance_types(
                            instance_graph
                        )
                if scanned_instance_types:
                    fallback = await self._instance_graph_ontology_fallback(
                        graph_uri, instance_graph, scanned_instance_types
                    )
                    if fallback is not None:
                        summary, has_instances = fallback
                        if has_instances:
                            logger.info(
                                "ontology_schema_missing_instances_present",
                                graph_uri=graph_uri,
                                instance_graph=instance_graph,
                                instance_types=len(scanned_instance_types),
                            )
                            _ontology_cache[cache_key] = (summary, time.time())
                            return summary
                return ONTOLOGY_EMPTY

            # Discover enumerated values for low-cardinality string attributes.
            # Runs cardinality checks concurrently (asyncio.gather) instead of
            # serially, cutting ontology fetch from ~7s to ~500ms. Concurrency
            # is bounded by a semaphore (COG-58) so a wide table with hundreds
            # of attributes can't launch hundreds of simultaneous queries
            # against serverless Neptune — the count stays capped regardless of
            # column count.
            import asyncio
            MAX_ENUM_CARDINALITY = 25
            from infona_client.nlp import pipeline as _pl

            _enum_sem = asyncio.Semaphore(_pl.MAX_ENUM_DISCOVERY_CONCURRENCY)

            async def _gather_bounded(coros: list) -> list:
                """asyncio.gather, but each coroutine acquires the shared enum
                semaphore first so at most MAX_ENUM_DISCOVERY_CONCURRENCY run at
                once. Preserves return_exceptions semantics for callers."""
                async def _run(coro):
                    async with _enum_sem:
                        return await coro
                return await asyncio.gather(
                    *[_run(c) for c in coros], return_exceptions=True
                )
            enum_values: dict[str, dict[str, list[str]]] = {}
            enum_counts: dict[str, dict[str, int]] = {}
            empty_rels: set[tuple[str, str]] = set()
            if instance_graph:
                # Collect all attribute and relationship URIs for cardinality checks
                all_attrs: list[tuple[str, str, str]] = []  # (type_name, attr_name, uri)
                string_attrs: list[tuple[str, str, str]] = []  # string attrs only (for enum values)
                rel_uris: list[tuple[str, str, str]] = []  # (type_name, rel_name, onto_uri)
                for type_name, info in types.items():
                    # Empty declared types have zero instances by definition, so
                    # every cardinality COUNT would return 0 — skip the probes
                    # (no extra Neptune round-trips) and render their declared
                    # schema plainly under the type-level [no instances] mark.
                    if type_name in empty_types:
                        continue
                    t_layer = type_layers.get(type_name, Layer.TENANT)
                    for attr_entry in info["attributes"]:
                        a_name = attr_entry.split(" (")[0]
                        a_uri = _attr_uri_for(t_layer, type_name, a_name)
                        all_attrs.append((type_name, a_name, a_uri))
                        if "(string)" in attr_entry:
                            string_attrs.append((type_name, a_name, a_uri))
                    for rel_entry in info["relationships"]:
                        r_name = rel_entry.split(" →")[0].strip()
                        onto_uri = f"{IRI_BASE}/onto/{r_name}"
                        rel_uris.append((type_name, r_name, onto_uri))

                # Define cardinality check function ONCE (used for both attrs and rels)
                async def _count_predicate(tn: str, an: str, uri: str) -> tuple[str, str, int]:
                    count_query = (
                        f"SELECT (COUNT(DISTINCT ?val) AS ?cnt) FROM <{instance_graph}> "
                        f"WHERE {{ ?s <{uri}> ?val }}"
                    )
                    raw = await self.neptune.query(count_query)
                    _, bindings = parse_sparql_results(raw)
                    cnt = int(bindings[0].get("cnt", 0)) if bindings else 0
                    return tn, an, cnt

                # Phase 1: Concurrent cardinality checks for ALL attributes
                if all_attrs:
                    try:
                        count_results = await _gather_bounded(
                            [_count_predicate(tn, an, uri) for tn, an, uri in all_attrs]
                        )

                        low_card_attrs: list[tuple[str, str, str]] = []
                        exceptions = sum(1 for r in count_results if isinstance(r, Exception))
                        if exceptions:
                            logger.warning("cardinality_check_exceptions", count=exceptions, total=len(count_results))
                        for result in count_results:
                            if isinstance(result, Exception):
                                continue
                            tn, an, cnt = result
                            enum_counts.setdefault(tn, {})[an] = cnt
                            if 0 < cnt <= MAX_ENUM_CARDINALITY:
                                t_layer = type_layers.get(tn, Layer.TENANT)
                                low_card_attrs.append(
                                    (tn, an, _attr_uri_for(t_layer, tn, an))
                                )

                        # Phase 2: Concurrent value fetches for low-cardinality attrs
                        async def _fetch_vals(tn: str, an: str, uri: str) -> tuple[str, str, list[str]]:
                            enum_values_query = (
                                f"SELECT DISTINCT ?val FROM <{instance_graph}> "
                                f"WHERE {{ ?s <{uri}> ?val }} LIMIT {MAX_ENUM_CARDINALITY}"
                            )
                            raw = await self.neptune.query(enum_values_query)
                            _, bindings = parse_sparql_results(raw)
                            return tn, an, [r["val"] for r in bindings if r.get("val")]

                        if low_card_attrs:
                            val_results = await _gather_bounded(
                                [_fetch_vals(tn, an, uri) for tn, an, uri in low_card_attrs]
                            )
                            for result in val_results:
                                if isinstance(result, Exception):
                                    continue
                                tn, an, vals = result
                                if vals:
                                    enum_values.setdefault(tn, {})[an] = sorted(vals)
                    except Exception:
                        logger.warning("cardinality_attr_check_failed", exc_info=True)

                # Phase 3: Check relationship cardinality to annotate empty ones.
                # A CONFIRMED-empty relationship is annotated "[no instances]" but
                # NEVER removed (ONTA-248 determinism): a DECLARED relationship is
                # part of the schema, and dropping it on a cnt==0 — which a
                # transient throttle produces exactly like a genuinely-empty edge —
                # made a relationship appear then vanish across identical calls.
                empty_rels: set[tuple[str, str]] = set()  # (type_name, rel_name)
                if rel_uris:
                    try:
                        rel_counts = await _gather_bounded(
                            [_count_predicate(tn, rn, uri) for tn, rn, uri in rel_uris]
                        )
                        for result in rel_counts:
                            if isinstance(result, Exception):
                                continue
                            tn, rn, cnt = result
                            if cnt == 0:
                                empty_rels.add((tn, rn))
                    except Exception:
                        logger.warning("cardinality_rel_check_failed", exc_info=True)

            lines = []
            for type_name, info in types.items():
                # DECLARED-but-empty type: annotate at the type level (ONTA-258)
                # so the LLM writes a valid zero-row query with an honest
                # "declared but no instances" explanation instead of claiming the
                # type is absent or substituting a different type.
                empty_suffix = " [no instances]" if type_name in empty_types else ""
                t_layer = type_layers.get(type_name, Layer.TENANT)
                t_uri = _type_uri_for(t_layer, type_name)
                lines.append(f"Type: {type_name} — URI: <{t_uri}>{empty_suffix}")
                if info["attributes"]:
                    # Prefer populated attrs first; declared-empty trail them
                    # (same planning preference as relationships / GraphStore path).
                    populated_attrs: list[str] = []
                    empty_attrs: list[str] = []
                    for attr_entry in sorted(info["attributes"]):
                        a_name = attr_entry.split(" (")[0]
                        if type_name in enum_values and a_name in enum_values[type_name]:
                            # Low-cardinality: show actual values
                            vals = enum_values[type_name][a_name]
                            val_str = ", ".join(f'"{v}"' for v in vals[:10])
                            if len(vals) > 10:
                                val_str += f", ... ({len(vals)} total)"
                            populated_attrs.append(
                                f"{attr_entry} [values: {val_str}]"
                            )
                        elif type_name in enum_counts and a_name in enum_counts[type_name]:
                            cnt = enum_counts[type_name][a_name]
                            if cnt == 0:
                                # DECLARED attribute with zero instances. Keep it
                                # (do NOT drop) — dropping made the schema the LLM
                                # sees NON-DETERMINISTIC (ONTA-248): a transient
                                # Neptune throttle returns an empty COUNT result
                                # (cnt=0) exactly like a genuinely-empty attribute,
                                # so the attribute flickered in and out of the
                                # summary between otherwise-identical calls. The
                                # attribute is DECLARED in the ontology, so it
                                # exists; annotate it as empty rather than deleting
                                # it, so an existence claim stays stable.
                                empty_attrs.append(f"{attr_entry} [no instances]")
                            elif cnt > MAX_ENUM_CARDINALITY:
                                # High-cardinality: just show the count
                                populated_attrs.append(
                                    f"{attr_entry} [{cnt} unique values]"
                                )
                            else:
                                populated_attrs.append(attr_entry)
                        else:
                            populated_attrs.append(attr_entry)
                    lines.append(
                        f"  Attributes: {', '.join(populated_attrs + empty_attrs)}"
                    )
                if info["relationships"]:
                    # Keep EVERY declared relationship; annotate confirmed-empty
                    # ones instead of hiding them (ONTA-248 determinism).
                    # Prefer populated edges first so the LLM plans on live
                    # leaves before declared-empty dead ends (persona 56a8c2).
                    annotated_rels = []
                    empty_rel_lines = []
                    for r in sorted(info["relationships"]):
                        if (type_name, r.split(" →")[0].strip()) in empty_rels:
                            empty_rel_lines.append(f"{r} [no instances]")
                        else:
                            annotated_rels.append(r)
                    annotated_rels.extend(empty_rel_lines)
                    lines.append(f"  Relationships: {', '.join(annotated_rels)}")
                if info["functions"]:
                    lines.append(f"  Functions: {', '.join(sorted(info['functions']))}")
            summary = "\n".join(lines)
            # Log types that made it into the summary
            types_in_summary = [l.split("—")[0].replace("Type:", "").strip() for l in lines if l.startswith("Type:")]
            logger.info("ontology_summary_built", types_shown=len(types_in_summary),
                        types_active=len(active_types) if active_types else "all",
                        types_with_attrs=len(types),
                        types_empty=len(empty_types),
                        names=types_in_summary[:10])

            # Cache it
            _ontology_cache[cache_key] = (summary, time.time())
            return summary
        except Exception:
            logger.error("ontology_fetch_failed", exc_info=True)
            # Distinct from the empty-graph message: a transient fetch failure must
            # NOT be reported to the LLM as "graph is empty" (ONTA-248 A2).
            return ONTOLOGY_FETCH_ERROR
