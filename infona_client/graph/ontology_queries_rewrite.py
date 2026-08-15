"""Pure SPARQL string rewrites: subclass closure, sameAs, layered FROM.

Deterministic regex transforms — no ontology lookup, no store, no LLM.
"""

from infona_client.graph.iri import ENTITY_URI_PREFIX, TYPE_URI_PREFIX

from infona_client.graph.ontology_queries_uris import INFONA_ONTO, RDF, RDFS

# Property path that turns a type-assertion predicate into its subclass closure.
_CLOSURE_PATH = f"<{RDF}#type>/<{RDFS}#subClassOf>*"
_TYPES_URI = TYPE_URI_PREFIX


def rewrite_type_predicate_to_closure(sparql: str) -> str:
    """Rewrite type-assertion triples to use subclass-closure property paths.

    Turns `?var a <types/X>`, `?var <rdf:type> <types/X>`, and the prefixed
    `?var rdf:type <types/X>` into `?var <rdf:type>/<rdfs:subClassOf>* <types/X>`,
    so a query over a parent type returns all subtype instances (ADR rule 2).

    Deterministic and regex-based — no ontology lookup, no Neptune, no LLM:
      - Only matches type-assertion predicate position whose OBJECT is a
        `https://graph.infona.ai/types/...` URI (the only place rewriting is valid).
      - Closure over a leaf type is set-equal to the leaf itself, so the rewrite
        is safe to apply unconditionally.
      - Idempotent: a triple already using the closure path (`.../subClassOf>*`)
        is left untouched.

    Beyond the three DIRECT forms above, the rewriter also closes the INDIRECT
    type-selection shapes the LLM sometimes emits (COG-34), where the type is
    bound to a VARIABLE in the rdf:type object position and that variable is
    constrained to a `types/...` URI elsewhere in the query:

      D) VALUES form: `VALUES ?t { <types/X> } ... ?x <rdf:type> ?t`
      E) FILTER equality: `?x <rdf:type> ?t . FILTER(?t = <types/X>)`
      F) FILTER IN: `?x <rdf:type> ?t . FILTER(?t IN (<types/X>, <types/Y>))`

    For these, the OBJECT variable is left in place and only the rdf:type
    PREDICATE of the matching triple is upgraded to the closure path. Closure
    over the constrained value still yields subtypes, because the constraint
    pins the variable to the named type(s) and `subClassOf*` walks down from
    there. A UNION of explicit `?x a <types/Ti>` branches needs no new code —
    each branch is already a Form A/B/C direct triple.

    Deterministic and regex-based — no ontology lookup, no Neptune, no LLM:
      - Only matches type-assertion predicate position whose OBJECT is a
        `https://graph.infona.ai/types/...` URI (the only place rewriting is valid).
      - Closure over a leaf type is set-equal to the leaf itself, so the rewrite
        is safe to apply unconditionally.
      - Idempotent: a triple already using the closure path (`.../subClassOf>*`)
        is left untouched.

    NOTE: best-effort safety net, NOT a SPARQL parser. The indirect-shape pass
    is intentionally narrow: it only fires when the SAME variable appears both as
    the bare object of an rdf:type triple AND in a VALUES / FILTER constraint that
    references a `types/...` URI. Shapes it deliberately does NOT cover (to avoid
    brittle rewrites): a type variable constrained only indirectly (e.g. via a
    join to another triple), VALUES blocks that mix type and non-type URIs for the
    same variable, and constraints that span subquery boundaries. In those cases
    the query returns only the named type — acceptable, since the NL prompt steers
    the model toward the direct `?x a <type>` form that closes reliably.

    Pure string transform — unit-testable with a plain SPARQL string.
    """
    import re

    rdf_type_full = f"<{RDF}#type>"
    types_obj = re.escape(_TYPES_URI)

    # Form A: `?var a <https://graph.infona.ai/types/X>`
    sparql = re.sub(
        rf'(\?\w+)\s+a\s+(<{types_obj}\w+>)',
        rf'\1 {_CLOSURE_PATH} \2',
        sparql,
    )

    # Form B: `?var <http://...#type> <https://graph.infona.ai/types/X>`
    # The negative-lookahead on the predicate guards idempotence: skip when the
    # predicate is already the closure path (which itself contains <...#type>).
    sparql = re.sub(
        rf'(\?\w+)\s+{re.escape(rdf_type_full)}(?!/)\s+(<{types_obj}\w+>)',
        rf'\1 {_CLOSURE_PATH} \2',
        sparql,
    )

    # Form C: prefixed `?var rdf:type <https://graph.infona.ai/types/X>`. Common
    # when the model declares `PREFIX rdf:`. Negative-lookahead on `/` keeps it
    # idempotent against an already-rewritten `rdf:type/rdfs:subClassOf*`.
    sparql = re.sub(
        rf'(\?\w+)\s+rdf:type(?!/)\s+(<{types_obj}\w+>)',
        rf'\1 {_CLOSURE_PATH} \2',
        sparql,
    )

    # ---- Indirect forms (COG-34): type bound to a variable + a constraint ----
    sparql = _rewrite_indirect_type_constraints(sparql)

    return sparql


def _rewrite_indirect_type_constraints(sparql: str) -> str:
    """Close rdf:type triples whose OBJECT is a VARIABLE constrained to a type URI.

    Handles COG-34 forms D/E/F (VALUES, FILTER `=`, FILTER `IN`). For each
    candidate variable `?t` we (1) confirm it is the bare object of an rdf:type
    triple, (2) confirm it is constrained to at least one `https://graph.infona.ai/
    types/...` URI via VALUES or FILTER, then (3) upgrade ONLY that triple's
    rdf:type predicate to the closure path. The object variable is untouched, so
    the existing VALUES/FILTER constraint keeps pinning it to the named type(s)
    while `subClassOf*` walks down to subtypes.

    Narrow and idempotent by construction: it rewrites a predicate only when the
    predicate is a bare rdf:type (`a`, `<...#type>`, or `rdf:type`) immediately
    followed by the SAME variable, and the closure-path predicate already contains
    `<...#type>/` so it can never re-match.
    """
    import re

    # Predicate alternation for a *bare* rdf:type, in any of the three notations.
    # Each branch forbids a trailing `/` so an already-rewritten closure path
    # (`...#type>/<...subClassOf>*`) is never matched again -> idempotent.
    rdf_type_full = f"<{RDF}#type>"
    bare_type_pred = (
        rf'(?:a|rdf:type(?!/)|{re.escape(rdf_type_full)}(?!/))'
    )

    # 1) Find every variable used as the bare object of an rdf:type triple:
    #    `?subj <bare-type-pred> ?typevar` (object MUST be a variable here).
    type_obj_vars: set[str] = set()
    for m in re.finditer(
        rf'\?\w+\s+{bare_type_pred}\s+(\?\w+)',
        sparql,
    ):
        type_obj_vars.add(m.group(1))

    if not type_obj_vars:
        return sparql

    types_uri = re.escape(_TYPES_URI)

    def _is_constrained_to_type(var: str) -> bool:
        """True if `var` (e.g. '?t') is bound/constrained to a types URI via a
        VALUES block or a FILTER (= / IN) elsewhere in the query."""
        v = re.escape(var)

        # VALUES ?t { ... <types/X> ... }  (single-var form)
        for vm in re.finditer(
            rf'VALUES\s+{v}\s*\{{([^}}]*)\}}',
            sparql,
            flags=re.IGNORECASE,
        ):
            if re.search(rf'<{types_uri}\w+>', vm.group(1)):
                return True

        # FILTER(?t = <types/X>)  and  FILTER(?t IN (<types/X>, ...))
        # Scan each FILTER body that mentions the variable and references a type.
        for fm in re.finditer(r'FILTER\s*\((.*?)\)', sparql, flags=re.IGNORECASE | re.DOTALL):
            body = fm.group(1)
            if not re.search(rf'{v}\b', body):
                continue
            # `?t = <types/X>` or `?t IN (...types/X...)`
            if re.search(rf'{v}\s*=\s*<{types_uri}\w+>', body):
                return True
            if re.search(rf'{v}\s+IN\s*\(', body, flags=re.IGNORECASE) and re.search(
                rf'<{types_uri}\w+>', body
            ):
                return True
        return False

    constrained = {v for v in type_obj_vars if _is_constrained_to_type(v)}
    if not constrained:
        return sparql

    # 2) Upgrade ONLY the rdf:type predicate of triples whose object is a
    #    constrained variable. Leave the variable in place.
    for var in constrained:
        v = re.escape(var)
        sparql = re.sub(
            rf'(\?\w+)\s+{bare_type_pred}\s+({v})\b',
            rf'\1 {_CLOSURE_PATH} \2',
            sparql,
        )

    return sparql


# --- Merged-alias (sameAs) query expansion (ONTA-278) -------------------------
# The alias/redirect INSTANCE edge a merge writes: ``(canonical, sameAs, merged)``
# on ``onto/`` (node-valued → visible to the NL planner, never ``attrs/``). This is
# the SAME predicate ``pipeline/mutations.py`` mints — kept here too because the
# read-side builder needs the constant with no import from the mutation layer. NOT
# standard ``owl:sameAs``; the repo has no ``2002/07/owl`` namespace.
SAME_AS = f"{INFONA_ONTO}/sameAs"

# Instance-node IRIs are the ONLY URIs a sameAs alias applies to (types/attr/onto
# URIs are schema, never re-keyed by a merge). The rewrite is scoped narrowly to
# these so it can never touch a type-closure or attribute reference.
_ENTITIES_URI = ENTITY_URI_PREFIX

# Bidirectional sameAs walk: from EITHER alias reach the canonical (and back), so a
# query pinning a merged-away IRI resolves the canonical's facts and vice-versa.
# ``*`` includes the zero-length step, so an un-merged entity still matches itself.
_SAMEAS_PATH = f"(<{SAME_AS}>|^<{SAME_AS}>)*"


def _entity_ref_in_unsafe_slot(sparql: str, ent: str) -> bool:
    """True when an ``…/entities/…`` IRI appears somewhere the sameAs rewrite cannot
    safely transform — inside a ``VALUES`` data block, inside a ``BIND(...)``
    expression, or adjacent to a ``,`` object-list separator. Used by
    :func:`rewrite_entity_ref_to_sameas_closure` to DECLINE (return unchanged)
    rather than corrupt such a query; see that function's SAFETY BAIL-OUT note.

    ``ent`` is the entity-IRI regex fragment the caller already built.
    """
    import re

    ent_re = re.compile(ent)

    # (a) A `VALUES ?v { … }` / `VALUES (?a ?b) { … }` data block referencing an
    #     entity IRI — rewriting there would splice a triple pattern into `{ … }`.
    for m in re.finditer(r"VALUES\b.*?\{.*?\}", sparql, re.IGNORECASE | re.DOTALL):
        if ent_re.search(m.group(0)):
            return True

    # (b) A `BIND( … )` expression referencing an entity IRI (e.g. `BIND(<E> AS ?x)`).
    #     Non-greedy to the first `)` is sufficient for the shapes an entity IRI
    #     appears in; over-matching would only make this guard more conservative.
    for m in re.finditer(r"BIND\s*\(.*?\)", sparql, re.IGNORECASE | re.DOTALL):
        if ent_re.search(m.group(0)):
            return True

    # (c) A `,`-continued object list where an entity IRI abuts the comma on either
    #     side — the classifier can only reach the LAST object, so it half-rewrites
    #     the group. (A plain `FILTER(?x = <E>)` has no such comma and is safe.)
    if re.search(ent + r"\s*,", sparql) or re.search(r",\s*" + ent, sparql):
        return True

    return False


def rewrite_entity_ref_to_sameas_closure(sparql: str) -> str:
    """Expand a concrete ``…/entities/<Type>/<id>`` reference to a sameAs walk so a
    MERGED entity (ONTA-274) resolves under EITHER alias.

    A merge re-keys all of the merged nodef's triples onto the canonical
    (``kg_writer.rewrite_subject``) and records ``(canonical, <onto/sameAs>, merged)``.
    Facts therefore live on the canonical URI; a query that PINS the merged-away
    IRI directly (e.g. a later re-mint of ``entity_uri(Type, raw_id)`` produces the
    same merged-away slug) would otherwise find nothing. This rewrite routes such a
    pinned entity reference through the bidirectional walk
    ``(<onto/sameAs>|^<onto/sameAs>)*`` so either alias reaches the same fact set:

      - SUBJECT: ``<E> P O``    → ``<E> (<sameAs>|^<sameAs>)* ?_saN . ?_saN P O``
      - OBJECT:  ``S P <E>``    → ``S P ?_saN . ?_saN (<sameAs>|^<sameAs>)* <E>``

    The read-path mirror of :func:`rewrite_type_predicate_to_closure` (subclass
    closure): a pure, deterministic SPARQL property-path rewrite — no ontology
    lookup, no Neptune round-trip, no LLM. The zero-length ``*`` step means an
    un-merged entity still matches itself, so applying the rewrite to a plain-triple
    query is always semantics-preserving.

    Scoped and idempotent by construction:
      - Only ``https://graph.infona.ai/entities/…`` IRIs are touched — a ``types/`` /
        ``attrs/`` / ``onto/`` URI (schema, never sameAs-aliased) is left verbatim,
        so this never disturbs the subclass-closure or attribute rewrites.
      - SUBJECT/OBJECT position is classified by the following token: an entity IRI
        followed by another TERM is a subject; one followed by a triple terminator
        (``.`` / ``}`` / end) is an object. A reference already followed (subject)
        or preceded (object) by the sameAs path is skipped, so running the rewrite
        twice equals running it once.

    SAFETY BAIL-OUT (this is a best-effort string transform, NOT a SPARQL parser):
    the position classifier above only holds for an entity IRI in a plain triple
    slot. An entity IRI sitting inside a ``VALUES`` data block, a ``BIND(...)``
    expression, or a ``,``-continued object list is NOT in such a slot, and blindly
    rewriting it there produces invalid or semantically-wrong SPARQL — a triple
    pattern spliced into a ``VALUES { … }`` block, a stray ``. ?_sa .`` inside a
    ``BIND(…)`` expression, or a half-rewritten object list where only the final
    object gains the walk. Detecting those constructs precisely is beyond a regex,
    so when ANY of them references an entity IRI this function DECLINES the rewrite
    and returns the query UNCHANGED (:func:`_entity_ref_in_unsafe_slot`). That
    degrades gracefully: the un-merged entity still matches itself, so a query with
    an unsafe construct simply does not resolve a merged ALIAS — it is never
    corrupted. A plain FILTER operand (``FILTER(?x = <E>)``) is left untouched by
    the classifier itself and does NOT trigger the bail-out, so a query mixing a
    pinned-object triple with a FILTER still gets its triple rewritten.

    Pure string transform — unit-testable with a plain SPARQL string.
    """
    import re

    ent = "<" + re.escape(_ENTITIES_URI) + r"[^>\s]+>"

    # Bail out (return the query unchanged) when an entity IRI sits in a slot the
    # position classifier below cannot safely handle — see the SAFETY BAIL-OUT note.
    if _entity_ref_in_unsafe_slot(sparql, ent):
        return sparql

    # The walk we insert always starts with ``(<…sameAs>`` — the idempotence guard.
    sameas_open = re.escape("(<" + SAME_AS + ">")

    counter = [0]

    def _fresh() -> str:
        v = "?_sa" + str(counter[0])
        counter[0] += 1
        return v

    # --- Subject position: `<E> P O` -> `<E> (path) ?_saN . ?_saN P O` ---
    # `<E>` is a subject when a TERM (not a separator) follows. The negative
    # lookahead on the sameAs path keeps it idempotent; the char class excludes
    # every triple/group separator so an object or a graph name is never matched.
    def _subj(m: "re.Match[str]") -> str:
        v = _fresh()
        return m.group(1) + " " + _SAMEAS_PATH + " " + v + " . " + v + " "

    subj_pat = "(" + ent + r")\s+(?!" + sameas_open + r")(?=[^\s.;,{}()\[\]])"
    sparql = re.sub(subj_pat, _subj, sparql)

    # --- Object position: `S P <E>` -> `S P ?_saN . ?_saN (path) <E>` ---
    # `<E>` is a triple-final object when a `.`/`}`/end follows. `,` and `;`
    # (object-list / predicate-object-list continuations) are intentionally left
    # untouched — splitting them would re-attach the continuation to the wrong
    # subject. The lookbehind skips a reference already carrying the walk.
    def _obj(m: "re.Match[str]") -> str:
        v = _fresh()
        return v + " . " + v + " " + _SAMEAS_PATH + " " + m.group(1)

    obj_pat = r"(?<!\)\* )(" + ent + r")(?=\s*[.}]|\s*$)"
    sparql = re.sub(obj_pat, _obj, sparql)

    return sparql


def add_layer_from_clauses(sparql: str, graph_uris: list[str]) -> str:
    """Add FROM <g> clauses for layer graphs missing from a graph-scoped query.

    Generated NL queries are scoped to one data graph (`FROM <data-graph>`),
    but with ontology layers (ADR 0002 §1) the subClassOf edges that the
    closure path `rdf:type/rdfs:subClassOf*` walks may live in OTHER layer
    graphs (a tenant leaf under a Public parent). Multiple FROM clauses make
    the default graph the union of all of them, so the closure walk sees every
    visible layer.

    Pure string transform, idempotent: a graph already in a FROM clause is not
    added twice. Queries with no FROM and no WHERE (shapes we don't understand)
    are returned unchanged. With an empty `graph_uris` the input is untouched —
    the single-graph call path stays byte-identical.
    """
    import re

    missing = [
        g for g in graph_uris
        if not re.search(rf'FROM\s+<{re.escape(g)}>', sparql)
    ]
    if not missing:
        return sparql
    extra = " ".join(f"FROM <{g}>" for g in missing)

    # Insert after the last existing FROM clause, else just before WHERE.
    from_matches = list(re.finditer(r'FROM\s+<[^>]+>', sparql))
    if from_matches:
        end = from_matches[-1].end()
        return f"{sparql[:end]} {extra}{sparql[end:]}"
    where = re.search(r'\bWHERE\b', sparql, flags=re.IGNORECASE)
    if where:
        start = where.start()
        return f"{sparql[:start]}{extra}\n{sparql[start:]}"
    return sparql
