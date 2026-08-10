"""Read the ENTIRE Global ontology (Public + Enhanced layers) in one pass.

Backs the operator-only browser ``GET /operator/ontology/global``. One batched
:func:`~infona_client.graph.ontology_queries.full_ontology_detail_query` per
layer graph — no N+1 per-type round trips — assembled into the flat, sorted,
search-friendly payload the Explorer renders.

Reads exactly what the premium ``GlobalShapeWriter`` writes (``infona/
governance/writer.py``, read-only reference — never imported here, OSS
boundary): a type as ``rdf:type rdfs:Class`` + ``rdfs:label`` + optional
``rdfs:comment``, and per slot a property under ``<type>/attrs/<slot>`` with
``rdfs:label`` / ``rdfs:domain`` / ``rdfs:range`` / ``onto/coreSlot
"true"^^xsd:boolean`` / optional ``rdfs:comment`` (the slot rationale).

Two things a type carries beyond its own triples:

* ``sources`` — registered API sources whose declared coverage PLAUSIBLY covers
  the type. A **fuzzy token match**, not a stored link: computed by
  :func:`~infona_client.api_registry.matching.type_matches`, the SAME predicate
  the enrichment rail self-gates on, so this page can never disagree with the
  rail about which source covers ``City``. See
  :class:`~infona_client.models.ontology.GlobalOntologySource` for the honest
  phrasing this permits. Only the GLOBAL catalog is read (no ``tenant_id``): the
  route is cross-tenant, so a workspace's private ``tenant_custom`` entries must
  never surface on it. A source reports its catalog layer as ``registry_layer``,
  NOT ``layer`` — the registry's layer axis (``global_public`` /
  ``global_enhanced``) is unrelated to the ontology layer (``public`` /
  ``enhanced``) on the type it is attached to, and the two travel in the same
  payload.
* ``functions`` — the executable code attached to the type, read from THAT
  LAYER'S GRAPH. Read path only, and empty in practice today: no writer mints a
  function against a layer-qualified type URI yet (see
  :func:`full_ontology_detail_query`'s note). This field covers functions and
  nothing else.
* ``skills`` — the curated GLOBAL-layer markdown PROSE attached to the type, for
  an LM agent to read (boundary doc §27). Functions COMPUTE, skills TEACH: two
  subsystems, two storage layers, two consumers — never merge the fields. Read
  from :func:`~infona_client.skills.registry.global_skills_for_type`, which is
  a plain synchronous registry lookup over the two GLOBAL layers and takes no
  tenant context, so a workspace's private tenant-layer skills (which live in
  the durable store, never in the registry) cannot surface on this cross-tenant
  page. Bodies are NOT inlined — see
  :class:`~infona_client.models.ontology.GlobalOntologySkill` for why, and for
  the canonical route that serves a full body on demand.

Degradation (mirrors :func:`~infona_client.graph.layers.fetch_types_by_layer`,
ADR 0002 §1): a layer whose graph is missing or whose query raises is reported
``available=False`` with ``type_count=0`` and contributes no types; the other
layer is unaffected and the request still returns 200. An EMPTY Global ontology
— today's expected state — is likewise a normal 200 with ``types: []``, never
an error. The registry degrades the same way: an unavailable/erroring/
unimportable catalog yields ``sources: []`` on every type, never a failed
ontology read — the ontology is the payload, the sources are an overlay on it.
The skills overlay degrades identically (an unimportable or erroring skills
subsystem yields ``skills: []`` on every type).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import structlog

from infona_client.graph.layers import (
    Layer,
    enhanced_graph_uri,
    layer_from_uri,
    public_graph_uri,
    type_name_from_uri,
)
from infona_client.graph.ontology_queries import (
    full_ontology_detail_query,
    xsd_to_datatype,
)
from infona_client.graph.parser import parse_sparql_results
from infona_client.models.function import FunctionRef
from infona_client.models.ontology import (
    GlobalOntologyAttribute,
    GlobalOntologyLayer,
    GlobalOntologyRelationship,
    GlobalOntologyResponse,
    GlobalOntologySkill,
    GlobalOntologySource,
    GlobalOntologyType,
    WorkspaceOntologyLayer,
    WorkspaceOntologyResponse,
    WorkspaceOntologyType,
)

logger = structlog.stdlib.get_logger("infona.graph.global_ontology")

#: The two GLOBAL layers, in the order they are reported. Deliberately excludes
#: ``Layer.TENANT`` — this browser is the cross-tenant canon, not one tenant's
#: ontology (which the tenant-scoped ``/graphs/{tenant}/ontology`` routes serve).
GLOBAL_LAYERS: tuple[tuple[Layer, str], ...] = (
    (Layer.PUBLIC, public_graph_uri()),
    (Layer.ENHANCED, enhanced_graph_uri()),
)

#: Lexical forms a SPARQL boolean/marker literal can arrive as. The writer emits
#: ``"true"^^xsd:boolean`` (parsed to the bare string ``"true"``), but a marker
#: hand-written as a plain literal must not silently read as False.
_TRUTHY = {"true", "1", "yes"}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _name_key(name: str) -> str:
    """Case-insensitive alphabetical sort key (the contract's ordering)."""
    return name.lower()


def _pick(values: set[str]) -> str | None:
    """Deterministically choose ONE value for a predicate that arrived with
    several — ``min()`` over the candidate set.

    Everything folded here is SINGLE-VALUED by the ontology's own upsert
    contract (``upsert_type`` / ``upsert_attribute`` DELETE-then-INSERT exactly
    for that reason), but a graph written by a blind ``INSERT DATA``, a partial
    migration, or a hand edit can still carry two. Taking "the first row that
    bound it" would then make the RESPONSE depend on Neptune's row order, which
    SPARQL leaves unspecified: two identical requests could flip a slot between
    ``attributes`` and ``relationships`` (one XSD range, one ``types/…`` range).
    That is the same intermittent-by-row-order failure class the QC fuzzer
    caught in ER lineage, so it is closed by construction here rather than left
    merely unlikely. ``min()`` is arbitrary but total and stable; the query also
    carries an ``ORDER BY`` so the engine's own output is reproducible.
    """
    return min(values) if values else None


class _TypeAccumulator:
    """Mutable per-(layer, type) scratch built up across the query's rows.

    A batched query returns one row per (type × parent × slot) combination, so
    the same type name recurs; this folds those rows back into one record.
    Every folded field is collected as a SET and resolved by :func:`_pick` at
    build time — never "first row wins", which would inherit the engine's
    unspecified row order.
    """

    __slots__ = ("name", "layer", "descriptions", "parent_uris", "slots", "functions")

    def __init__(self, name: str, layer: str) -> None:
        self.name = name
        self.layer = layer
        self.descriptions: set[str] = set()
        #: Raw rdfs:subClassOf object URIs — kept as URIs, not names, because a
        #: bare name is not an identity across layers (see :meth:`parent`).
        self.parent_uris: set[str] = set()
        #: slot name -> {"descriptions": set, "ranges": set, "core": bool}
        self.slots: dict[str, dict[str, Any]] = {}
        #: function name -> {"descriptions": set, "endpoints": set}. Keyed by
        #: NAME (the identity the ontology exposes), folded with the same
        #: set + _pick discipline as everything else, so the attribute ×
        #: function row cross-product folds idempotently.
        self.functions: dict[str, dict[str, set[str]]] = {}

    def absorb(self, row: dict[str, str]) -> None:
        if row.get("typeComment"):
            self.descriptions.add(row["typeComment"])
        if row.get("parent"):
            self.parent_uris.add(row["parent"])

        func_name = row.get("funcName")
        if func_name:
            func = self.functions.setdefault(
                func_name, {"descriptions": set(), "endpoints": set()}
            )
            if row.get("funcDesc"):
                func["descriptions"].add(row["funcDesc"])
            if row.get("funcEndpoint"):
                func["endpoints"].add(row["funcEndpoint"])

        attr_name = row.get("attrLabel")
        if not attr_name:
            return
        slot = self.slots.setdefault(
            attr_name, {"descriptions": set(), "ranges": set(), "core": False}
        )
        if row.get("attrComment"):
            slot["descriptions"].add(row["attrComment"])
        if row.get("range"):
            slot["ranges"].add(row["range"])
        # coreSlot is a MARKER, not a value: any row asserting it wins, so the
        # fold is order-independent without needing _pick.
        if _truthy(row.get("core")):
            slot["core"] = True

    def parent(self) -> tuple[Layer, str] | None:
        """The parent's LAYER-QUALIFIED identity, or None.

        ``rdfs:subClassOf`` may point outside every layer namespace (e.g.
        ``rdfs:Resource``), in which case the type is left un-parented rather
        than given an invented name.
        """
        uri = _pick(self.parent_uris)
        if not uri:
            return None
        layer = layer_from_uri(uri)
        name = type_name_from_uri(uri)
        if layer is None or not name:
            return None
        return layer, name

    def build(
        self,
        subtypes: list[str],
        sources: list[GlobalOntologySource] | None = None,
        skills: list[GlobalOntologySkill] | None = None,
    ) -> GlobalOntologyType:
        attributes: list[GlobalOntologyAttribute] = []
        relationships: list[GlobalOntologyRelationship] = []
        parent = self.parent()
        for slot_name, slot in self.slots.items():
            range_uri = _pick(slot["ranges"]) or ""
            # A slot is a RELATIONSHIP iff its range resolves to a type in ANY
            # layer namespace (tenant / enhanced / public). Everything else —
            # an XSD primitive, rdfs:Resource, a geo WKT literal, or no range
            # at all — is a literal attribute. Check the type namespaces FIRST:
            # xsd_to_datatype would otherwise happily reduce `types/X` to "X"
            # and mis-file a relationship as a primitive datatype.
            target = type_name_from_uri(range_uri) if range_uri else None
            if target:
                relationships.append(
                    GlobalOntologyRelationship(
                        name=slot_name,
                        target_type=target,
                        description=_pick(slot["descriptions"]),
                        core_slot=slot["core"],
                    )
                )
            else:
                attributes.append(
                    GlobalOntologyAttribute(
                        name=slot_name,
                        datatype=xsd_to_datatype(range_uri) if range_uri else "string",
                        description=_pick(slot["descriptions"]),
                        core_slot=slot["core"],
                    )
                )
        attributes.sort(key=lambda a: _name_key(a.name))
        relationships.sort(key=lambda r: _name_key(r.name))
        functions = [
            FunctionRef(
                name=func_name,
                # The function's own graph carries `attachedTo <this type URI>`,
                # so the enclosing type IS its entity_type — reported as the
                # bare NAME, matching every other cross-reference in this
                # contract (parent_type, subtypes, target_type).
                entity_type=self.name,
                description=_pick(func["descriptions"]) or "",
                endpoint_url=_pick(func["endpoints"]),
                # `tier` is deliberately left at the model default: NOTHING in
                # the graph records a tier (register_function_triple writes
                # name/endpointUrl/description only), and the tenant
                # GET /graphs/{tenant}/functions route reports the same default
                # for the same reason. Inventing PLATFORM for global-layer
                # functions would be a fabricated field.
                # Layer is the enclosing type's layer (ONTA-399): Enhanced
                # functions now attach to types/x/<T> in the Enhanced graph.
                layer=self.layer,
            )
            for func_name, func in sorted(
                self.functions.items(), key=lambda kv: (_name_key(kv[0]), kv[0])
            )
        ]
        return GlobalOntologyType(
            name=self.name,
            layer=self.layer,
            description=_pick(self.descriptions),
            # The CONTRACT carries a bare name; the layer qualification is an
            # internal identity concern (see the children map below).
            parent_type=parent[1] if parent else None,
            subtypes=subtypes,
            attributes=attributes,
            relationships=relationships,
            sources=list(sources or []),
            functions=functions,
            skills=list(skills or []),
        )


#: Audit flags that are FRESHNESS grades, in reporting priority. Live-smoke
#: flags (EMPTY / UNREACHABLE) are excluded by construction — this read never
#: goes to the network, so they can never be present.
_FRESHNESS_FLAGS = ("UNVERIFIED", "FUTURE", "STALE")


def _freshness(finding: dict[str, Any]) -> str:
    """Grade one ``catalog_audit`` finding. Reuses THAT module's judgement — this
    never re-decides what "stale" means, it only names the flag it already
    raised (and reports ``"OK"`` when it raised none)."""
    flags = finding.get("flags") or []
    for flag in _FRESHNESS_FLAGS:
        if flag in flags:
            return flag
    return "OK"


class _SourceIndex:
    """Answers "which registered API sources plausibly cover type X?".

    Built ONCE per request (the catalog is process-wide and the freshness grade
    is pure date arithmetic), then memoized per type NAME — a name declared in
    both Global layers asks the same question twice and must get the same
    answer, since the registry has no notion of ontology layers at all.

    An EMPTY index is the degradation state: every type gets ``sources: []``.
    """

    __slots__ = ("_specs", "_grades", "_cache")

    def __init__(self, specs: list[Any], grades: dict[str, str]) -> None:
        self._specs = specs
        self._grades = grades
        self._cache: dict[str, list[GlobalOntologySource]] = {}

    def for_type(self, type_name: str) -> list[GlobalOntologySource]:
        cached = self._cache.get(type_name)
        if cached is not None:
            return cached
        # Lazy import, and tolerant: an import failure or a matcher raising on
        # one odd spec must degrade this OVERLAY to empty, never fail the
        # ontology read (module docstring).
        try:
            from infona_client.api_registry.matching import type_matches

            out = [
                GlobalOntologySource(
                    slug=spec.slug,
                    title=spec.title,
                    publisher=spec.publisher,
                    # NOTE the field name: the registry's layer vocabulary
                    # (global_public / global_enhanced) is a DIFFERENT AXIS from
                    # the ontology layer (public / enhanced) on the enclosing
                    # type. Both would read as "layer" in one payload, so the
                    # contract keeps them apart by name.
                    registry_layer=spec.layer,
                    authority_level=getattr(
                        spec.authority_level, "value", str(spec.authority_level)
                    ),
                    enabled=bool(spec.enabled),
                    verified_at=spec.verified_at or "",
                    freshness=self._grades.get(spec.slug, "UNVERIFIED"),
                    entity_kinds=list(spec.coverage.entity_kinds),
                )
                for spec in self._specs
                if type_matches(spec, type_name)
            ]
        except Exception:
            logger.warning(
                "global_ontology_source_match_failed", type_name=type_name, exc_info=True
            )
            out = []
        self._cache[type_name] = out
        return out


async def _build_source_index(
    catalog: Any | None = None, today: date | None = None
) -> _SourceIndex:
    """Load the GLOBAL API-source catalog + grade each entry's freshness.

    ``catalog`` / ``today`` are injection seams (mirroring ``audit_catalog``'s
    own parameters) so the overlay is testable without a process catalog or a
    moving clock; production passes neither.

    NO ``tenant_id`` is passed to :func:`get_api_source_catalog` — deliberately.
    This endpoint is cross-tenant, so it must show only the operator-curated
    global layers; a workspace's private ``tenant_custom`` entries are not the
    Global canon and must never leak onto it.

    Never raises: any failure (import error, unreadable data dir, audit blowing
    up) logs and returns an EMPTY index, so ``sources`` degrades to ``[]``
    exactly as an unreachable ontology layer degrades to ``available=False``.
    """
    try:
        # Imported lazily so `graph/` takes no import-time dependency on the
        # registry (and its httpx/executor chain), and so an unimportable
        # registry degrades instead of breaking the ontology module outright.
        from infona_client.api_registry.catalog import get_api_source_catalog
        from infona_client.api_registry.catalog_audit import audit_catalog

        cat = catalog if catalog is not None else get_api_source_catalog()
        # live_smoke stays off (the default): the grading must be OFFLINE and
        # deterministic — an ontology read must not issue network calls.
        findings = await audit_catalog(catalog=cat, today=today)
        grades = {f.get("slug", ""): _freshness(f) for f in findings}
        specs = sorted(cat.all(), key=lambda spec: spec.slug)
    except Exception:
        logger.warning("global_ontology_source_registry_unavailable", exc_info=True)
        return _SourceIndex([], {})
    return _SourceIndex(specs, grades)


# --------------------------------------------------------------------------- #
# Skills overlay — curated PROSE attached to a type (boundary doc §27)
# --------------------------------------------------------------------------- #

#: Characters of body carried inline as ``excerpt``. Sized to show a paragraph —
#: enough that the browser can actually convey what a skill SAYS, which is the
#: whole point of a prose tab — while keeping the worst case bounded: a body may
#: be 20 000 chars (``skills.models.MAX_BODY_CHARS``) and this endpoint returns
#: every type in both global layers in ONE payload, so inlining bodies would
#: turn an ontology read into a document download. The full text stays one
#: canonical request away (``GET /graphs/{tenant}/skills/{type_name}/{slug}``).
SKILL_EXCERPT_CHARS = 400


def _excerpt(body: str, limit: int = SKILL_EXCERPT_CHARS) -> str:
    """A bounded, single-line preview of a markdown body.

    Whitespace runs collapse to single spaces, so markdown structure does NOT
    survive — deliberate: this is a plain-prose preview, and a half-open code
    fence or a dangling list marker rendered as markdown would look like
    corruption. Truncation cuts on a word boundary (when one is reasonably
    close) and is ANNOUNCED with an ellipsis, so a reader is never handed half a
    sentence as if it were the whole instruction — the same discipline
    ``render_skills_block`` applies to its own truncation.
    """
    text = " ".join((body or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _skill_to_payload(s: Any) -> GlobalOntologySkill:
    """Project a :class:`~infona_client.skills.models.TypeSkill` onto the wire."""
    return GlobalOntologySkill(
        slug=s.slug,
        type_name=s.type_name,
        title=s.title,
        summary=s.summary,
        excerpt=_excerpt(s.body),
        # The FULL body's length, not the excerpt's — that gap is exactly what
        # tells a reader there is more to fetch.
        body_chars=len(s.body or ""),
        layer=s.layer.value if hasattr(s.layer, "value") else str(s.layer),
        enabled=bool(s.enabled),
        version=int(s.version),
    )


class _SkillIndex:
    """Answers "which curated GLOBAL skills are attached to type X?".

    Memoized per type NAME for the same reason as :class:`_SourceIndex`: a name
    declared in BOTH ontology layers asks the same question twice and must get
    the same answer — the skills registry is keyed by type name, not by the
    layer of the type asking.

    **Tenant isolation is structural, not a filter here.** The only call is
    :func:`~infona_client.skills.registry.global_skills_for_type`, which reads
    the process-wide curated registry plus the OSS seed directory and takes no
    tenant argument at all. Tenant-authored skills live in the durable
    ``TypeSkillStore`` and never enter that registry — ``register_skill_layer``
    raises on ``Layer.TENANT`` and blanks ``tenant_id`` on everything it
    accepts. So there is no tenant row for this page to leak, which is why this
    class does not (and must not) grow a "drop tenant skills" filter: a filter
    would imply tenant rows can arrive here, and inviting them is the failure
    mode.

    Workspace reads use :class:`_WorkspaceSkillIndex` instead, which unions the
    caller's tenant layer on top (ONTA-408).

    An EMPTY index is the degradation state: every type gets ``skills: []``.
    """

    __slots__ = ("_cache",)

    def __init__(self) -> None:
        self._cache: dict[str, list[GlobalOntologySkill]] = {}

    def for_type(self, type_name: str) -> list[GlobalOntologySkill]:
        cached = self._cache.get(type_name)
        if cached is not None:
            return cached
        # Lazy import, and tolerant — same contract as the sources overlay: an
        # unimportable skills subsystem, an unreadable seed directory, or a
        # malformed registered skill must degrade this OVERLAY to empty, never
        # fail the ontology read.
        try:
            from infona_client.skills import global_skills_for_type

            skills = list(global_skills_for_type(type_name))
            # Deterministic and stable: slug first, then the skill's own layer,
            # so a slug curated in BOTH global layers (the override case) lists
            # as two ADJACENT rows in a fixed order. `global_skills_for_type`
            # returns precedence order (Enhanced, then Public), which is the
            # right order for a PROMPT but leaves a same-slug pair split apart
            # in a browse list sorted any other way.
            skills.sort(key=lambda s: (_name_key(s.slug), s.slug, s.layer.value))
            out = [_skill_to_payload(s) for s in skills]
        except Exception:
            logger.warning(
                "global_ontology_skill_lookup_failed", type_name=type_name, exc_info=True
            )
            out = []
        self._cache[type_name] = out
        return out


class _WorkspaceSkillIndex:
    """Workspace browse overlay: Tenant ∪ Enhanced ∪ Public with slug shadowing.

    Loads the durable tenant skill store ONCE per request, then merges with the
    global registry per type name. Precedence matches
    :func:`~infona_client.skills.resolve.resolve_skills` (Tenant > Enhanced >
    Public, Enhanced only when entitled) — but DISABLED higher-layer skills are
    still LISTED (this is a raw browse view, same as the operator global page)
    while still suppressing a same-slug lower-layer skill.

    Tenant isolation is by store key: ``list_for_tenant(tenant_id)`` can only
    return that tenant's rows. Never call this with an empty tenant_id.
    """

    __slots__ = ("_tenant_id", "_visible_layers", "_tenant_by_type", "_cache")

    def __init__(
        self,
        tenant_id: str,
        *,
        visible_layers: set[str],
        tenant_skills: list[Any] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._visible_layers = visible_layers
        self._tenant_by_type: dict[str, list[Any]] = {}
        if tenant_skills:
            for s in tenant_skills:
                key = (s.type_name or "").casefold()
                self._tenant_by_type.setdefault(key, []).append(s)
        self._cache: dict[str, list[GlobalOntologySkill]] = {}

    def for_type(self, type_name: str) -> list[GlobalOntologySkill]:
        cached = self._cache.get(type_name)
        if cached is not None:
            return cached
        try:
            from infona_client.skills import global_skills_for_type

            global_rows = [
                s
                for s in global_skills_for_type(type_name)
                if s.layer.value in self._visible_layers
            ]
        except Exception:
            logger.warning(
                "workspace_ontology_skill_lookup_failed",
                type_name=type_name,
                tenant_id=self._tenant_id,
                exc_info=True,
            )
            global_rows = []

        # Precedence order for browse: tenant first (when visible), then
        # enhanced, then public. Within a layer, slug order is stable.
        by_layer: dict[str, list[Any]] = {
            "tenant": list(self._tenant_by_type.get(type_name.casefold(), [])),
            "enhanced": [s for s in global_rows if s.layer.value == "enhanced"],
            "public": [s for s in global_rows if s.layer.value == "public"],
        }
        for layer_rows in by_layer.values():
            layer_rows.sort(key=lambda s: (_name_key(s.slug), s.slug))

        precedence = [
            layer
            for layer in ("tenant", "enhanced", "public")
            if layer in self._visible_layers
        ]
        seen: set[str] = set()
        out: list[GlobalOntologySkill] = []
        for layer in precedence:
            for s in by_layer.get(layer, []):
                slug = s.slug
                if slug in seen:
                    continue
                seen.add(slug)
                out.append(_skill_to_payload(s))
        self._cache[type_name] = out
        return out


async def _load_tenant_skills(tenant_id: str) -> list[Any]:
    """Load all durable skills for one tenant. Degrades to ``[]`` on any failure."""
    if not tenant_id:
        return []
    try:
        from infona_client.skills.store import make_type_skill_store

        store = make_type_skill_store()
        return list(await store.list_for_tenant(tenant_id))
    except Exception:
        logger.warning(
            "workspace_ontology_tenant_skills_unavailable",
            tenant_id=tenant_id,
            exc_info=True,
        )
        return []


async def fetch_ontology(
    neptune,
    *,
    layers: Sequence[tuple[Layer, str]],
    catalog: Any | None = None,
    today: date | None = None,
    entitled: bool = False,
    tenant_id: str = "",
    apply_shadowing: bool = True,
) -> WorkspaceOntologyResponse:
    """Generalized layered ontology reader (ONTA-397).

    Parameters
    ----------
    neptune:
        Graph client.
    layers:
        Ordered ``(Layer, graph_uri)`` pairs to read — typically from
        :class:`~infona_client.graph.layers.LayerStack` for a workspace.
        Precedence is the sequence order (first wins under shadowing).
    catalog / today:
        Registry-overlay injection seams (same as
        :func:`fetch_global_ontology`).
    entitled:
        Whether Enhanced is visible; recorded on the response and used by the
        caller to build ``layers``. The reader does not re-check entitlement.
    tenant_id:
        Workspace id stamped onto the response (isolation / audit).
    apply_shadowing:
        When True (workspace default), first-visible-layer-wins. When False,
        every layer's declarations are returned as separate rows (operator-style
        raw browse over an arbitrary layer list).

    Returns
    -------
    WorkspaceOntologyResponse
        Effective (or raw, when ``apply_shadowing=False``) ontology. Empty is
        a normal empty response, never an error.

    Notes
    -----
    :func:`fetch_global_ontology` remains the dedicated two-layer operator call
    and is **not** rewritten to call this function — the operator payload must
    stay byte-stable. Reads are layered; ordinary writes always go to the
    tenant graph only (never into a global layer).
    """
    layer_infos: list[WorkspaceOntologyLayer] = []
    # Keyed by bare type name when shadowing; by (layer, name) when raw.
    accumulators: dict[Any, _TypeAccumulator] = {}
    # Parallel map: bare name -> winning layer value (shadowing path only).
    winning_layer: dict[str, str] = {}
    sources = await _build_source_index(catalog=catalog, today=today)
    # Skills are process-registry prose keyed by type name across GLOBAL
    # layers. Only surface skills whose OWN layer is visible in this stack —
    # otherwise a non-entitled workspace would see Enhanced skill rows via the
    # overlay even though Enhanced types are excluded from ``layers``.
    # When ``tenant_id`` is set (workspace read), also union the durable
    # tenant skill store so the browser matches agent resolution (ONTA-408).
    visible_skill_layers = {layer.value for layer, _ in layers}
    if tenant_id:
        tenant_skills = await _load_tenant_skills(tenant_id)
        skills: _SkillIndex | _WorkspaceSkillIndex = _WorkspaceSkillIndex(
            tenant_id,
            visible_layers=visible_skill_layers,
            tenant_skills=tenant_skills,
        )
    else:
        skills = _SkillIndex()

    for layer, graph_uri in layers:
        available = True
        bindings: list[dict[str, str]] = []
        try:
            raw = await neptune.query(full_ontology_detail_query(graph_uri))
            _, bindings = parse_sparql_results(raw)
        except Exception:
            available = False
            logger.warning(
                "workspace_ontology_layer_unavailable",
                layer=layer.value,
                graph_uri=graph_uri,
                tenant_id=tenant_id,
                exc_info=True,
            )

        layer_types = 0
        seen_names: set[str] = set()
        for row in bindings:
            label = row.get("typeLabel", "")
            if not label:
                continue
            if label not in seen_names:
                seen_names.add(label)
                layer_types += 1

            if apply_shadowing:
                # First-visible-layer-wins across layers — but the SPARQL result
                # is one row per attribute/relationship slot. Once a type is
                # claimed by THIS layer we must keep absorbing later rows for
                # that type; only skip when a *higher-precedence* layer already
                # owns it. (Bug found by OSS dogfood S6: the old `label in
                # winning_layer → continue` dropped every attr after the first.)
                prev_layer = winning_layer.get(label)
                if prev_layer is not None and prev_layer != layer.value:
                    continue
                winning_layer[label] = layer.value
                key: Any = label
            else:
                key = (layer.value, label)

            acc = accumulators.get(key)
            if acc is None:
                acc = _TypeAccumulator(label, layer.value)
                accumulators[key] = acc
            acc.absorb(row)

        layer_infos.append(
            WorkspaceOntologyLayer(
                layer=layer.value,
                graph_uri=graph_uri,
                type_count=layer_types,
                available=available,
            )
        )

    # Subtype inversion — same layer-qualified parent identity discipline as
    # fetch_global_ontology so a Public parent is not polluted by a tenant
    # homonym (or vice versa). Under shadowing we only list children that
    # themselves survived shadowing (their accumulator is present).
    children: dict[tuple[str, str], set[str]] = {}
    for acc in accumulators.values():
        parent = acc.parent()
        if parent:
            parent_layer, parent_name = parent
            children.setdefault((parent_layer.value, parent_name), set()).add(acc.name)

    types: list[WorkspaceOntologyType] = []
    for acc in accumulators.values():
        skill_rows = [
            s for s in skills.for_type(acc.name) if s.layer in visible_skill_layers
        ]
        built = acc.build(
            sorted(
                children.get((acc.layer, acc.name), set()),
                key=lambda n: (_name_key(n), n),
            ),
            sources.for_type(acc.name),
            skill_rows,
        )
        types.append(
            WorkspaceOntologyType(
                name=built.name,
                layer=built.layer,
                description=built.description,
                parent_type=built.parent_type,
                subtypes=built.subtypes,
                attributes=built.attributes,
                relationships=built.relationships,
                sources=built.sources,
                functions=built.functions,
                skills=built.skills,
            )
        )
    types.sort(key=lambda t: (_name_key(t.name), t.layer))

    return WorkspaceOntologyResponse(
        tenant_id=tenant_id,
        entitled=entitled,
        layers=layer_infos,
        types=types,
    )


async def fetch_global_ontology(
    neptune, *, catalog: Any | None = None, today: date | None = None
) -> GlobalOntologyResponse:
    """Assemble the full Global ontology payload — one query per layer graph.

    Never raises for an unreachable/erroring layer, nor for an unavailable API
    source registry or skills registry; see the module docstring. ``catalog`` /
    ``today`` are the registry-overlay injection seams described on
    :func:`_build_source_index`.

    Wave 0: this remains the two-layer operator call. The generalized
    workspace reader is :func:`fetch_ontology` (signature frozen; body ONTA-397).
    """
    layer_infos: list[GlobalOntologyLayer] = []
    accumulators: dict[tuple[str, str], _TypeAccumulator] = {}
    sources = await _build_source_index(catalog=catalog, today=today)
    # No injection seam: the skills registry is a plain process-wide lookup with
    # no clock and no I/O to fake — tests register curated content through the
    # subsystem's own public seam (`register_skill_layer`), which is the same
    # path the premium overlay uses.
    skills = _SkillIndex()

    for layer, graph_uri in GLOBAL_LAYERS:
        available = True
        bindings: list[dict[str, str]] = []
        try:
            raw = await neptune.query(full_ontology_detail_query(graph_uri))
            _, bindings = parse_sparql_results(raw)
        except Exception:
            available = False
            logger.warning(
                "global_ontology_layer_unavailable",
                layer=layer.value,
                graph_uri=graph_uri,
                exc_info=True,
            )

        layer_types = 0
        for row in bindings:
            label = row.get("typeLabel", "")
            if not label:
                continue
            key = (layer.value, label)
            acc = accumulators.get(key)
            if acc is None:
                acc = _TypeAccumulator(label, layer.value)
                accumulators[key] = acc
                layer_types += 1
            acc.absorb(row)

        layer_infos.append(
            GlobalOntologyLayer(
                layer=layer.value,
                graph_uri=graph_uri,
                type_count=layer_types,
                available=available,
            )
        )

    # Invert rdfs:subClassOf across BOTH layers at once — an Enhanced type may
    # subclass a Public one, and the Public parent should still list it — but
    # key on the parent's LAYER-QUALIFIED identity, never its bare name. The
    # parent's layer comes from its URI namespace, so `types/x/Doctor
    # subClassOf types/x/Person` attaches to the ENHANCED Person only, and an
    # unrelated Public `Person` homonym is left alone. Name-keying would list
    # Doctor under both — the exact shadowing confusion this payload exists to
    # make visible.
    children: dict[tuple[str, str], set[str]] = {}
    for acc in accumulators.values():
        parent = acc.parent()
        if parent:
            parent_layer, parent_name = parent
            children.setdefault((parent_layer.value, parent_name), set()).add(acc.name)

    types = [
        # `children` values are SETS, so a key that folds two distinct names to
        # the same value (case) would leave their relative order to set
        # iteration — i.e. PYTHONHASHSEED-dependent, differing between API
        # workers for the same graph. Tie-break on the raw name for a total
        # order, same reason `_pick` uses min() rather than next(iter(...)).
        acc.build(
            sorted(
                children.get((acc.layer, acc.name), set()),
                key=lambda n: (_name_key(n), n),
            ),
            sources.for_type(acc.name),
            skills.for_type(acc.name),
        )
        for acc in accumulators.values()
    ]
    # Alphabetical by name (case-insensitive); layer breaks ties so a name
    # declared in both layers has a stable order.
    types.sort(key=lambda t: (_name_key(t.name), t.layer))

    return GlobalOntologyResponse(layers=layer_infos, types=types)
