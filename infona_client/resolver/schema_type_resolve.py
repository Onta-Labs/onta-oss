from __future__ import annotations

"""Type resolution and ancestor synthesis.

Job: pick / mint the entity type (subtype collapse, focus seed, also_types).
Do not mint entity instance URIs here — that is ``entity_uri`` on write.
"""

from infona_client.resolver.attribute_resolver import AttributeSchema, is_junk_type_name
from infona_client.resolver.models import ExtractedEntity, IngestResult, MatchVerdict, TypeMatch
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


def coerce_subtype_match(
    match: TypeMatch, *, allow_subtype_link: bool, proposed: str,
) -> TypeMatch:
    """Mapped CSV treats TypeMatcher SUBTYPE as a peer type (no subClassOf)."""
    if allow_subtype_link or match.verdict != MatchVerdict.SUBTYPE:
        return match
    _sr.logger.info(
        "type_subtype_treated_as_different",
        proposed=proposed,
        parent=match.parent_type,
    )
    return match.model_copy(update={
        "verdict": MatchVerdict.DIFFERENT,
        "parent_type": None,
        "is_new": True,
        "resolved": proposed,
    })


class SchemaTypeResolveMixin:
    """Type-matching half of SchemaResolver."""

    async def _synthesize_ancestors(
        self,
        child_type: str,
        parent_type: str | None,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        parent_chain: list[str] | None = None,
        emit_child_edge: bool = False,
        *,
        parent_of: dict[str, str] | None = None,
    ) -> None:
        """Close the rdfs:subClassOf lineage from `child_type` up to the nearest
        existing root (ADR 0001 rule 3).

        `parent_type` is the immediate parent (may be None when only an extractor
        chain is available). `parent_chain` is the extractor's full ancestor list
        for `child_type`, most-specific first — seeding it lets a brand-new
        MULTI-LEVEL lineage (e.g. Condo < Property < Asset, all new) close in a
        single pass. `emit_child_edge=True` makes this method emit the
        child->immediate-parent subClassOf edge itself; callers that already
        emitted it (the SUBTYPE branches) pass False to avoid a redundant write.

        For each ancestor NOT yet in existing_types, emits insert_type +
        insert_subtype and registers it in existing_types / existing_attrs /
        result.types_created. Idempotent: ancestors already present are skipped.

        ``parent_of`` (ONTA-268): the CALL-LOCAL child->parent map to read+mutate;
        falls back to ``self._parent_of`` for legacy direct callers. Runs under the
        caller's ontology-write lock (``_resolve_type``) — must NOT acquire it here
        (``asyncio.Lock`` is not reentrant).
        """
        from infona_client.resolver.er import ancestor_chain

        parent_of = self._parent_of if parent_of is None else parent_of
        parent_chain = parent_chain or []
        # Immediate parent: explicit hint wins; otherwise top of the extractor chain.
        if not parent_type:
            parent_type = parent_chain[0] if parent_chain else None
        if not parent_type:
            return

        # Record the child->parent edge so later entities in this batch can climb it.
        if child_type and child_type != parent_type:
            parent_of[child_type] = parent_type
        # Seed the deeper extractor lineage (ancestors of child, most-specific
        # first) without clobbering edges already recorded (setdefault).
        prev = child_type
        for anc in parent_chain:
            if prev and anc and prev != anc:
                parent_of.setdefault(prev, anc)
            prev = anc

        # Brand-new lineage: the caller couldn't link child->parent because the
        # parent didn't exist yet. Emit that edge here.
        if emit_child_edge and child_type and child_type != parent_type:
            await self._commit_ontology(
                graph_uri, [self._mut_subclass(child_type, parent_type)], holding_lock=True,
            )

        # Walk root-ward from the immediate parent. ancestor_chain is cycle-guarded.
        chain = ancestor_chain(parent_type, parent_of)
        for i, ancestor in enumerate(chain):
            grandparent = chain[i + 1] if i + 1 < len(chain) else None
            if ancestor not in existing_types:
                muts = [self._mut_type(ancestor)]
                if grandparent:
                    muts.append(self._mut_subclass(ancestor, grandparent))
                    parent_of[ancestor] = grandparent
                await self._commit_ontology(graph_uri, muts, holding_lock=True)
                result.types_created.append(ancestor)
                existing_types[ancestor] = ""
                existing_attrs[ancestor] = {}

    async def _link_parent(
        self,
        entity: ExtractedEntity,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        *,
        parent_of: dict[str, str] | None = None,
        allow_subtype_link: bool = True,
    ) -> None:
        """Attach a freshly-created type to its parent lineage.

        Two cases:
        - immediate parent already exists → link directly, then synthesize any
          deeper ancestors the extractor named (parent_chain);
        - brand-new lineage (parent not in the ontology, or only a parent_chain) →
          let _synthesize_ancestors create every missing ancestor AND the
          child->parent edge (emit_child_edge=True). This closes a fully-new
          multi-level chain like Condo < Property < Asset in one row (ADR rule 3).

        ``parent_of`` (ONTA-268): CALL-LOCAL child->parent map threaded to
        `_synthesize_ancestors`; falls back to ``self._parent_of``. Runs under the
        caller's ontology-write lock — does not acquire it.
        """
        if not allow_subtype_link:
            return
        parent_of = self._parent_of if parent_of is None else parent_of
        pt = entity.parent_type
        linked_as_subtype = False
        if pt and pt in existing_types:
            # Immediate parent exists — link directly, then synthesize any deeper
            # ancestors the extractor named.
            await self._commit_ontology(
                graph_uri, [self._mut_subclass(entity.type_name, pt)], holding_lock=True,
            )
            await self._synthesize_ancestors(
                entity.type_name, pt, graph_uri, existing_types, existing_attrs, result,
                parent_chain=entity.parent_chain, parent_of=parent_of,
            )
            _sr.logger.info("type_new_with_parent", child=entity.type_name, parent=pt)
            linked_as_subtype = True
        elif entity.parent_chain:
            # Brand-new lineage. We DON'T trust a parent_type that names a
            # non-existing type (preserves the "parent_type must be existing"
            # contract); the full chain comes from parent_chain instead.
            await self._synthesize_ancestors(
                entity.type_name, None, graph_uri, existing_types, existing_attrs, result,
                parent_chain=entity.parent_chain, emit_child_edge=True, parent_of=parent_of,
            )
            _sr.logger.info(
                "type_new_lineage", child=entity.type_name, parent=entity.parent_chain[0],
            )
            linked_as_subtype = True

        # The caller's top-level mint wrote NO comment (FIX 3): subtype_description
        # may only describe a real subtype. Now that a parent linkage has made
        # this type a genuine subtype, write the description here. Use the
        # COMMENT-ONLY upsert: the subClassOf edge was just created above (by
        # insert_subtype / _synthesize_ancestors), and plain upsert_type would
        # DELETE it (it clears subClassOf when no parent_type is passed) — the
        # new-parent-edge bug. upsert_type_comment touches only rdfs:comment, so
        # the edge survives while the description stays idempotent on re-ingest.
        if linked_as_subtype and entity.subtype_description:
            await self._commit_ontology(
                graph_uri,
                [self._mut_comment(entity.type_name, entity.subtype_description)],
                holding_lock=True,
            )
    async def _resolve_also_types(
        self,
        entity: ExtractedEntity,
        primary_resolved: str,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        *,
        parent_of: dict[str, str] | None = None,
        allow_subtype_link: bool = True,
    ) -> list[str]:
        """Resolve genuine co-classifications (entity.also_types) so each exists
        in the ontology (ADR rule 1). Returns the resolved co-type names, deduped.

        Skips any co-type that is actually in the primary's subClassOf lineage
        (an ancestor or descendant) — those are recovered by query-time closure,
        not asserted. Only genuinely INDEPENDENT types are returned.

        ``parent_of`` (ONTA-268): CALL-LOCAL lineage map; falls back to
        ``self._parent_of``.
        """
        if not entity.also_types:
            return []
        from infona_client.resolver.er import ancestor_chain

        parent_of = self._parent_of if parent_of is None else parent_of
        resolved: list[str] = []
        seen = {primary_resolved}
        for co in entity.also_types:
            if not co:
                continue
            proxy = ExtractedEntity(type_name=co, id=entity.id)
            rt = await self._resolve_type(
                proxy, graph_uri, existing_types, existing_attrs, result,
                parent_of=parent_of,
                allow_subtype_link=allow_subtype_link,
            )
            if not rt or rt in seen:
                continue
            # Same-lineage guard: skip if one is an ancestor of the other.
            if rt in ancestor_chain(primary_resolved, parent_of) or \
               primary_resolved in ancestor_chain(rt, parent_of):
                _sr.logger.info("also_type_in_lineage_skipped", primary=primary_resolved, co_type=rt)
                continue
            resolved.append(rt)
            seen.add(rt)
        return resolved

    async def _mint_subtype(
        self, graph_uri: str, type_name: str, subtype_description: str | None,
    ) -> None:
        """Create a NEW subtype's type declaration, carrying its description
        idempotently (FIX 3 + FIX 4).

        When a ``subtype_description`` is present it is written via
        :func:`upsert_type_comment`, which REPLACES the single-valued
        ``rdfs:comment`` instead of appending — so re-minting the same subtype
        across ingests can't accumulate duplicate comments — while leaving
        ``rdfs:subClassOf`` untouched (plain :func:`upsert_type` would CLEAR the
        edge a caller's ``insert_subtype`` creates). With no description we emit a
        plain ``insert_type`` (no comment), keeping the common no-description write
        byte-identical to before.
        """
        if subtype_description:
            await self._commit_ontology(
                graph_uri, [self._mut_comment(type_name, subtype_description)], holding_lock=True,
            )
        else:
            await self._commit_ontology(
                graph_uri, [self._mut_type(type_name)], holding_lock=True,
            )

    async def _ensure_focus_types(
        self,
        focus_types: list[str],
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
    ) -> None:
        """Pre-seed soft-mode focus types into the ontology (ONTA-383).

        Discovery's ``proposed_type`` is the consolidation anchor. Minting it
        BEFORE pass-1 type matching means the matcher can return SUBTYPE for
        University/College against Institution instead of free-standing peers,
        and junk/primary retypes have a real parent to fall back to.
        """
        for ft in focus_types:
            if not ft or ft in existing_types:
                continue
            if is_junk_type_name(ft):
                # A junk focus is a caller bug; refuse to seed it so we don't
                # anchor the whole batch under a property-class type.
                _sr.logger.warning("focus_type_rejected_junk", focus_type=ft)
                continue
            async with self._ontology_lock:
                if ft in existing_types:
                    continue
                await self._commit_ontology(
                    graph_uri,
                    [self._mut_type(ft, description="Confirmed focus type (discovery)")],
                    holding_lock=True,
                )
                result.types_created.append(ft)
                existing_types[ft] = ""
                existing_attrs.setdefault(ft, {})
                _sr.logger.info("focus_type_seeded", focus_type=ft)

    async def _resolve_type(
        self,
        entity: ExtractedEntity,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        *,
        parent_of: dict[str, str] | None = None,
        focus_types: list[str] | None = None,
        is_primary: bool | None = None,
        allow_subtype_link: bool = True,
    ) -> str | None:
        """Pass 1: Resolve the type for an entity. Returns resolved type name or None.

        ``parent_of`` (ONTA-268): CALL-LOCAL lineage map, threaded to the
        subtype/ancestor synthesis; falls back to ``self._parent_of``.

        ``focus_types`` / ``is_primary`` (ONTA-383): soft-mode consolidation.
        When focus types are set (discovery's proposed_type):
          * junk property-class names on a primary record retype to the focus;
          * a primary with no parent/same_as is anchored as a SUBTYPE of the
            first focus type (collapses University/College peer sprawl under
            Institution);
          * dimension-only nodes (is_primary=False) keep free minting unless junk
            (junk dimensions are dropped — return None so the entity is skipped).

        The whole read-decide-WRITE of ontology existence runs under the
        ontology-write lock (ONTA-268) so concurrent per-sub-query resolvers
        sharing that lock serialize type creation — no two overlap between the
        "does this type exist / what does the matcher say" decision and the
        insert_type/insert_subtype that acts on it, which is what fragments the
        ontology under a raced ingest. The exact-name in-memory hit short-circuits
        BEFORE the lock (a pure read on the hot path — every repeated row of a
        known type — so it never contends). The lock does NOT cover the LLM
        EXTRACTION (`_extract`, upstream); it does cover the type-MATCH decision
        because that decision + its write must be atomic to avoid a race.
        """
        if entity.type_name in existing_types:
            return entity.type_name

        # ONTA-383 junk-type guard (pre-lock, pure): property-class names never
        # become ontology types. Primary records fall back to the focus type when
        # it is already seeded; dimension-only junk (and any junk with no usable
        # focus) is skipped (return None).
        if is_junk_type_name(entity.type_name):
            focus = (focus_types or [None])[0]
            if is_primary and focus and focus in existing_types:
                _sr.logger.info(
                    "type_junk_retyped_to_focus",
                    proposed=entity.type_name,
                    focus=focus,
                    entity_id=entity.id,
                )
                return focus
            _sr.logger.info(
                "type_junk_rejected",
                proposed=entity.type_name,
                entity_id=entity.id,
                reason=(
                    "junk_primary_focus_missing"
                    if is_primary and focus
                    else "junk_dimension_or_no_focus"
                ),
            )
            return None

        # ONTA-394 consolidation: a PRIMARY record under a confirmed soft focus
        # whose proposed type is a BRAND-NEW type (not already in the ontology) is
        # an ACCIDENTAL subtype the user never confirmed. Discovery confirms
        # exactly ONE focus type; soft extract nonetheless labels rows with near-
        # synonym kinds (College / University / PublicInstitution under an
        # Institution focus).
        #   * COLLAPSE (default, ONTA-394): retype the record to the focus itself
        #     so NO unconfirmed subtype collection is minted (kills the dogfood's
        #     `College (23)` Explorer collection). An ALREADY-EXISTING type short-
        #     circuits at the top of this method and is REUSED — a prior confirmed
        #     subtype is never collapsed. Dimension-only nodes (is_primary=False)
        #     keep free minting.
        #   * ANCHOR (INFONA_sr._DISCOVERY_COLLAPSE_SUBTYPES=0, the ONTA-383 fallback):
        #     an evidence-free primary is anchored as a SUBTYPE of the focus rather
        #     than an orphan peer.
        # ``same_as`` is preserved in BOTH modes: an explicit "this is the SAME AS
        # <existing type>" is a de-dup to a confirmed type, not an accidental new
        # subtype — it falls through to the same_as→existing-type mapping below. An
        # extractor-guessed parent_type/parent_chain, by contrast, IS overridden by
        # collapse (it is the LLM's guess, not user confirmation — the exact
        # `College`-under-`Institution` case AC#4 targets).
        if (
            focus_types
            and is_primary
            and not entity.same_as
            and entity.type_name not in focus_types
        ):
            focus = focus_types[0]
            if focus and focus in existing_types and entity.type_name != focus:
                if _sr._DISCOVERY_COLLAPSE_SUBTYPES:
                    _sr.logger.info(
                        "type_subtype_collapsed_to_focus",
                        proposed=entity.type_name,
                        focus=focus,
                        entity_id=entity.id,
                    )
                    return focus
                if (
                    not entity.same_as
                    and not entity.parent_type
                    and not entity.parent_chain
                ):
                    entity = entity.model_copy(update={"parent_type": focus})
                    _sr.logger.info(
                        "type_anchored_under_focus",
                        proposed=entity.type_name,
                        focus=focus,
                        entity_id=entity.id,
                    )

        if not allow_subtype_link and (entity.parent_type or entity.parent_chain):
            entity = entity.model_copy(update={"parent_type": None, "parent_chain": []})
        parent_of = self._parent_of if parent_of is None else parent_of
        async with self._ontology_lock:
            # ONTA-268: point the embedding pre-filter at THIS ingest's tenant
            # store under the lock, right before the match, so a single shared
            # TypeMatcher serving interleaved ingests can't read a clobbered
            # `_graph_uri` (the lock serializes the set→match→write, and in
            # production each per-sub-query resolver holds its own TypeMatcher).
            self._type_matcher._graph_uri = graph_uri
            if entity.same_as and entity.same_as in existing_types:
                match = coerce_subtype_match(
                    await self._type_matcher.match(entity.type_name, "", existing_types),
                    allow_subtype_link=allow_subtype_link,
                    proposed=entity.type_name,
                )
                if match.verdict == MatchVerdict.SAME:
                    _sr.logger.info("type_same_as_verified", proposed=entity.type_name, resolved=match.resolved)
                    return match.resolved
                elif match.verdict == MatchVerdict.SUBTYPE:
                    # SUBTYPE branch — subtype_description legitimately describes this
                    # NEW subtype (FIX 3). Written idempotently (FIX 4): upsert
                    # REPLACES the single-valued rdfs:comment so re-minting the same
                    # type across ingests can't accumulate duplicate comments.
                    await self._mint_subtype(graph_uri, entity.type_name, entity.subtype_description)
                    await self._commit_ontology(
                        graph_uri,
                        [self._mut_subclass(entity.type_name, match.parent_type)],
                        holding_lock=True,
                    )
                    _sr.logger.info("type_same_as_was_subtype", child=entity.type_name, parent=match.parent_type)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._synthesize_ancestors(
                        entity.type_name, match.parent_type, graph_uri,
                        existing_types, existing_attrs, result,
                        parent_chain=entity.parent_chain, parent_of=parent_of,
                    )
                    return entity.type_name
                elif match.inconclusive:
                    # Verifier couldn't reach a real decision (e.g. LLM unavailable).
                    # Trust the extractor's explicit same_as rather than fabricating a
                    # duplicate type — creating "Home" alongside "Property" is exactly
                    # the ontology pollution this verification step exists to prevent.
                    _sr.logger.info("type_same_as_trusted", proposed=entity.type_name, resolved=entity.same_as)
                    return entity.same_as
                else:
                    # same_as REJECTED → this is a genuine TOP-LEVEL type, not a
                    # subtype. subtype_description must NOT be written here (FIX 3):
                    # the field's contract is "describes a NEW SUBTYPE" only.
                    await self._commit_ontology(
                        graph_uri, [self._mut_type(entity.type_name)], holding_lock=True,
                    )
                    _sr.logger.info("type_same_as_rejected", proposed=entity.type_name, claimed=entity.same_as)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    return entity.type_name
            else:
                match = coerce_subtype_match(
                    await self._type_matcher.match(entity.type_name, "", existing_types),
                    allow_subtype_link=allow_subtype_link,
                    proposed=entity.type_name,
                )
                if match.verdict == MatchVerdict.SAME:
                    _sr.logger.info("type_matched_existing", proposed=entity.type_name, resolved=match.resolved)
                    return match.resolved
                elif match.verdict == MatchVerdict.SUBTYPE:
                    # SUBTYPE branch — subtype_description describes this NEW subtype
                    # (FIX 3), written idempotently via upsert (FIX 4).
                    await self._mint_subtype(graph_uri, entity.type_name, entity.subtype_description)
                    await self._commit_ontology(
                        graph_uri,
                        [self._mut_subclass(entity.type_name, match.parent_type)],
                        holding_lock=True,
                    )
                    _sr.logger.info("type_subtype", child=entity.type_name, parent=match.parent_type)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._synthesize_ancestors(
                        entity.type_name, match.parent_type, graph_uri,
                        existing_types, existing_attrs, result,
                        parent_chain=entity.parent_chain, parent_of=parent_of,
                    )
                    return entity.type_name
                elif match.verdict == MatchVerdict.FLAGGED:
                    # Top-level mint: do NOT write subtype_description here (FIX 3).
                    # If _link_parent then establishes a parent (the entity carried a
                    # parent_type/parent_chain), it upserts the description there —
                    # the only place the type is actually a subtype.
                    await self._commit_ontology(
                        graph_uri, [self._mut_type(entity.type_name)], holding_lock=True,
                    )
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._link_parent(
                        entity, graph_uri, existing_types, existing_attrs, result,
                        parent_of=parent_of, allow_subtype_link=allow_subtype_link,
                    )
                    _sr.logger.warning("type_flagged_for_review", proposed=entity.type_name)
                    result.flagged_types.append(entity.type_name)
                    return entity.type_name
                else:
                    # Top-level mint: no subtype_description here (FIX 3). _link_parent
                    # upserts it iff this turns out to be a subtype (parent_chain).
                    await self._commit_ontology(
                        graph_uri, [self._mut_type(entity.type_name)], holding_lock=True,
                    )
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._link_parent(
                        entity, graph_uri, existing_types, existing_attrs, result,
                        parent_of=parent_of, allow_subtype_link=allow_subtype_link,
                    )
                    # Governance seam: the genuinely-new type MAY also be proposed
                    # for the Global-Public layer. No-op unless the flag is on.
                    await self._maybe_govern_new_type(entity, graph_uri)
                    return entity.type_name
