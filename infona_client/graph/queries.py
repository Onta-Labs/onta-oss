from infona_client.graph.iri import GRAPH_URI_PREFIX, IRI_BASE
import re

# Characters that CANNOT appear in a well-formed absolute IRI reference and would
# let a value break out of the ``<…>`` wrapper generated SPARQL puts it in: the
# angle brackets that delimit it, any whitespace, and control chars. SPARQL's own
# IRIREF grammar forbids exactly these, so a value carrying one cannot produce a
# query the store would accept ANYWAY — rejecting it turns a store-side parse
# error (an opaque 500, or worse an injection) into an honest 422 and never
# rejects a name that works today.
#
# Defined at the top because it is now the shared basis of THREE things: the
# IRI branch of :func:`_escape_value`, :func:`is_valid_type_name` (ONTA-425) and
# :func:`is_valid_tenant_id` (ONTA-422).
#
# ``\x7f-\x9f`` (DEL + the C1 controls) is part of the same class and was missing
# while this range only guarded ``_escape_value``: RFC 3987's ``ucschar`` starts
# at U+00A0, so every codepoint below it that is not already covered by
# ``\x00-\x20`` is equally illegal in an IRI. Left out, a name carrying one
# passed validation and then failed at the STORE — the opaque 500 this whole
# family exists to convert into an honest 422. Checked against the live registry:
# no real type or attribute name contains one.
_IRI_FORBIDDEN = re.compile(r'[<>"{}|\^`\\\x00-\x20\x7f-\x9f]')


class InvalidGraphIdentifier(ValueError):
    """Base of the "this name cannot legally sit inside a generated IRI" family.

    ``api/app.py`` registers ONE 422 handler on this base (Starlette resolves a
    handler by walking the exception's MRO), so a new member — a fourth kind of
    caller-supplied IRI segment — is rendered as 422 the moment it is added,
    rather than surfacing as an opaque 500 until someone remembers to register
    another handler. Subclasses stay distinct so a caller can still catch just
    the kind it cares about.

    Deliberately a ``ValueError`` subclass, like :class:`InvalidKGName` was
    before it gained this base, so pre-existing ``except ValueError`` blocks keep
    catching it.
    """


def is_iri_safe_segment(value: object) -> bool:
    """Whether ``value`` may be interpolated verbatim inside a ``<…>`` IRI.

    The STRUCTURAL predicate: non-empty, and free of every character SPARQL's
    IRIREF production forbids. It says nothing about a value being a sensible
    name — that is each identifier family's own, stricter business (see
    :func:`is_valid_kg_name`). What it guarantees is the only thing security
    depends on: the value cannot terminate the IRI it is embedded in, so it can
    never become SPARQL syntax.
    """
    return (
        isinstance(value, str)
        and value != ""
        and _IRI_FORBIDDEN.search(value) is None
    )


class InvalidTenantId(InvalidGraphIdentifier):
    """A ``tenant_id`` that cannot legally appear inside a graph IRI (ONTA-422)."""


def is_valid_tenant_id(tenant_id: object) -> bool:
    """Whether ``tenant_id`` may be interpolated into a graph IRI.

    Deliberately WEAKER than ``auth.tenant_directory.TENANT_ID_RE`` (the 3–40
    char lowercase slug rule self-serve creation enforces). That rule governs
    what a NEW workspace may be called; this one governs what may be
    interpolated, and the two populations differ: a self-hosted deployment in
    open-access mode picks its own workspace id with nothing to validate it, and
    ids predating the slug rule keep working. Enforcing the slug shape here would
    make an existing workspace's data unreachable to fix a problem it does not
    have — the ONTA-414 → onta-oss#274 mistake, where a stricter-than-necessary
    read-side pattern 422'd a whole listing over one pre-existing name.

    So the rule is exactly "cannot break out of, or repoint, the IRI":

    * no IRIREF-illegal character (see :func:`is_iri_safe_segment`) — a ``>``
      closes ``<https://graph.onta.sh/graphs/{tenant}>`` early and the remainder
      becomes SPARQL. On the ontology/ingest WRITE paths that remainder lands in
      a ``client.update``, where ``;`` starts a second operation: ``DROP ALL``
      needs no IRI of its own and destroys every graph in the store;
    * no ``/`` — a tenant id is ONE path segment by construction, and a smuggled
      slash repoints the IRI at a different graph (``victim/kg/secret``) without
      ever leaving the ``<…>``;
    * no ``%`` — percent-encoding could spell either of the above after the
      store's own IRI resolution, and ``sparql_scope.tenant_owns_graph`` already
      refuses it for exactly that reason;
    * not a dot segment — RFC 3986 ``remove_dot_segments`` applies to a
      reference even when it carries a scheme, so ``..`` starts inside our
      namespace and lands outside it.
    """
    if not is_iri_safe_segment(tenant_id):
        return False
    assert isinstance(tenant_id, str)  # narrowed by is_iri_safe_segment
    if "/" in tenant_id or "%" in tenant_id:
        return False
    return tenant_id not in (".", "..")


def require_valid_tenant_id(tenant_id: object) -> str:
    if not is_valid_tenant_id(tenant_id):
        raise InvalidTenantId(
            f"Invalid workspace id {tenant_id!r}: must be a single non-empty "
            "path segment with no whitespace, control character, '/', '%', or "
            "any of <>\"{}|^`\\"
        )
    assert isinstance(tenant_id, str)
    return tenant_id


# The namespace every workspace graph URI is minted under. A named constant
# because ONE caller (``text_markers.invalidate_for_graph``) wants the PREFIX,
# not a URI, and used to spell it ``tenant_graph_uri("")`` — an idiom that
# silently depends on the builder accepting a name it should reject. Asking for
# the prefix directly is both honest and unbreakable by validation.


def tenant_graph_uri(tenant_id: str) -> str:
    """Base graph URI for a tenant. Used as the ontology graph.

    ONTA-422: validates ``tenant_id`` HERE, the same way :func:`kg_graph_uri`
    validates ``kg_name``, because when NO auth provider is configured
    (``auth/api_keys._resolve_tenant``'s open-access branch, the self-hosted
    mode) the tenant is whatever the caller put in the URL and reaches this
    f-string unchecked. ``auth`` rejects it at the door too; this is the
    structural backstop that covers every other caller, including premium ones
    and any future writer that mints a graph IRI without going through a route.
    """
    return f"{GRAPH_URI_PREFIX}{require_valid_tenant_id(tenant_id)}"


# A KG name that may legally be interpolated into a graph IRI. Deliberately the
# SAME pattern ``KGCreate.name`` enforces on create (api/routes/knowledge_graphs.py)
# and that ``kg_writer.ensure_kg_registered`` enforces before registering: a name
# that could never be created must never reach a generated SPARQL string.
#
# ``\Z``, not ``$``: Python's ``$`` also matches immediately BEFORE a final
# newline, so ``re.match(r"^[a-zA-Z0-9_-]+$", "kg\n")`` succeeds and a trailing
# ``%0A`` on a path or query param would have slipped through. Nothing can follow
# that newline (so it was not itself an injection), but it broke the stated
# invariant that this is exactly the pattern create enforces: pydantic compiles
# its patterns with Rust regex, whose ``$`` is a strict end-of-text, so
# ``KGCreate.name`` rejects ``"kg\n"``. ``\Z`` makes the two agree.
_KG_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+\Z")


def is_valid_kg_name(kg_name: object) -> bool:
    """Whether ``kg_name`` may be interpolated into a graph IRI.

    THE predicate. Callers that must not raise (the best-effort registration in
    the shared write path) branch on this instead of keeping a second copy of the
    pattern that can drift, as one did before ONTA-414.
    """
    return isinstance(kg_name, str) and _KG_NAME_RE.match(kg_name) is not None


class InvalidKGName(InvalidGraphIdentifier):
    """A ``kg_name`` that cannot legally appear inside a graph IRI (ONTA-414).

    Mapped to HTTP 422 by the app-level handler in ``api/app.py`` so every route
    that funnels user input into :func:`kg_graph_uri` rejects it identically,
    instead of each route re-deriving its own validation (or forgetting to).
    """


class InvalidTypeName(InvalidGraphIdentifier):
    """A type / attribute name that cannot legally sit inside an IRI (ONTA-425)."""


def is_valid_type_name(name: object) -> bool:
    """Whether a type or attribute name may be interpolated into an ontology IRI.

    THE predicate for both halves of ``types/<Type>/attrs/<attr>``: an attribute
    leaf is interpolated into exactly the same IRI and needs exactly the same
    guarantee.

    Deliberately just :func:`is_iri_safe_segment`, NOT a slug pattern, and that
    is the whole point of this function existing rather than a regex inlined at
    the call site. Type and attribute names are minted by an LLM from real data,
    not chosen from a keyboard-safe alphabet, so any pattern narrower than
    "cannot break the IRI" risks rejecting names that exist and work. Checked
    against the live registry rather than by grepping this repo (the ONTA-414
    verification mistake that produced onta-oss#274): of 158 type names and 714
    attribute names in the two live workspaces, ZERO carry an IRIREF-illegal
    character, while TWO attribute names contain ``/`` (``city/town``,
    ``county/parish``). A ``[A-Za-z0-9_-]+`` rule of the kind ``kg_name`` uses
    would therefore have broken two attributes that are in production today,
    for no security gain: a ``/`` cannot escape ``<…>``.
    """
    return is_iri_safe_segment(name)


def require_valid_type_name(name: object, kind: str = "type name") -> str:
    """Raise flavour: for a name the CALLER supplied and can fix.

    Use on a single-resource operation (one type's summary, one ontology
    mutation). For a read that fans out over STORED names use
    :func:`skip_invalid_type_name` instead — see its docstring for why the
    difference is load-bearing.
    """
    if not is_valid_type_name(name):
        raise InvalidTypeName(
            f"Invalid {kind} {name!r}: must be non-empty and free of whitespace, "
            "control characters, and any of <>\"{}|^`\\ — none of which can "
            "appear in an IRI"
        )
    assert isinstance(name, str)
    return name


def skip_invalid_type_name(name: object, op: str) -> bool:
    """Fail-soft flavour: whether to SKIP ``name``, logging it as it goes.

    THE helper for any read that enumerates names coming back FROM THE STORE:
    the ontology summary the NL planner builds, the Explorer's type search, the
    ontology embedding chunks. All of them fan out over every declared name, so
    letting :func:`require_valid_type_name` raise on ONE corrupt row would take
    the whole answer down for every healthy type — the all-or-nothing failure
    onta-oss#274 had to fix after ONTA-414 (a single pre-existing malformed
    ``kg_name`` 422'd an entire workspace's KG listing).

    Shared rather than copied per module BECAUSE of that history: the enumeration
    sites are spread across ``nlp/``, ``api/routes/`` and ``graph/``, and a
    per-module copy is how the two ``kg_name`` patterns drifted in the first
    place. The ``kg_name`` twin, ``knowledge_graphs._skip_invalid_kg_name``,
    stays separate only because its message and predicate differ.

    A corrupt row does not need out-of-band DB access to arrive:
    ``POST /graphs/{tenant}/triples`` writes arbitrary triples into the same
    tenant base graph the ontology lives in, and SPARQL literal escaping does not
    escape ``>``, so a caller with write access to their OWN workspace can plant
    a ``rdfs:label`` that later reads back here. The skip stays LOUD for exactly
    that reason: a silent drop would make the corruption unfindable.
    """
    if is_valid_type_name(name):
        return False
    # Per-call logger, matching knowledge_graphs._skip_invalid_kg_name: a
    # module-level structlog proxy is frozen at import by
    # cache_logger_on_first_use, after which structlog.testing.capture_logs can
    # no longer intercept it. Not hot — the valid-name fast path returns above.
    import structlog

    structlog.get_logger("cograph.graph").warning(
        "type_name_invalid_skipped", type_name=name, op=op
    )
    return True


def kg_graph_uri(tenant_id: str, kg_name: str) -> str:
    """Named graph URI for a specific knowledge graph within a tenant.

    ONTA-414: validates ``kg_name`` HERE rather than at each of the ~20 call
    sites, several of which take the name straight off a request body. The
    returned URI is interpolated verbatim into generated SPARQL inside an IRI
    (``FROM <...>``, ``GRAPH <...>``), so a name carrying ``>`` closes the IRI
    early and lets the caller append a second ``FROM`` naming ANOTHER tenant's
    graph. That is a tenant-isolation break, not a cosmetic bug, so this fails
    closed with :class:`InvalidKGName` instead of emitting a malformed IRI.

    ONTA-422: the ``tenant_id`` half is validated for the same reason. It was
    the unchecked half of this two-line f-string — a caller-supplied tenant in
    open-access mode breaks out of the IRI exactly as a ``kg_name`` did.
    f"""
    tenant_id = require_valid_tenant_id(tenant_id)
    if not is_valid_kg_name(kg_name):
        raise InvalidKGName(
            f"Invalid kg_name {kg_name!r}: must be one or more of [a-zA-Z0-9_-] "
            "with nothing else, including no trailing whitespace or newline"
        )
    return f"{GRAPH_URI_PREFIX}{tenant_id}/kg/{kg_name}"


# Registry record every KG is announced with in the tenant's BASE graph. Written
# by ``create_kg`` (the Explorer's "New KG" button) and by the shared write path
# (``kg_writer.ensure_kg_registered``, which covers CLI / MCP / agent writers);
# read by ``list_kgs`` and by the ONTA-413 existence probe. Canonical here so the
# three producers/consumers cannot drift on the URI or predicate shape.
KG_NAME_PRED = f"{IRI_BASE}/onto/kg_name"


def kg_meta_uri(tenant_id: str, kg_name: str) -> str:
    """Subject URI of a KGf's registration record in the tenant base graph.

    The ``kg_name`` half stays deliberately UNVALIDATED (see the note in
    ``api/routes/knowledge_graphs.py``: callers branch on ``is_valid_kg_name``
    and skip, because a bad name here means a corrupt registry row that must not
    take a listing down). The ``tenant_id`` half is validated (ONTA-422) — that
    argument never comes from a registry row, only from an authenticated
    ``TenantContext``, so there is no listing to fail soft for and nothing to
    gain from emitting an IRI that cannot parse.
    """
    return f"{IRI_BASE}/kgs/{require_valid_tenant_id(tenant_id)}/{kg_name}"


# The kg segment is anchored to a single path component ([^/]+, no slashes) so a
# COMPANION graph URI — e.g. a provenance graph ".../kg/<kg>/provenance" — does NOT
# greedily parse to kg_name="<kg>/provenance"; it correctly returns None (matching
# the docstring contract). KG names can't contain "/" (KGCreate enforces
# ^[a-zA-Z0-9_-]+$), so this never rejects a real KG.
_KG_GRAPH_RE = re.compile(
    rf"^{re.escape(IRI_BASE)}/graphs/(?P<tenant>[^/]+)/kg/(?P<kg>[^/]+)$"
)


def parse_kg_graph_uri(graph_uri: str) -> tuple[str, str] | None:
    """Inverse of :func:`kg_graph_uri`: ``(tenant_id, kg_name)`` or ``None``.

    Returns ``None`` for anything that is not a per-KG instance-graph URI (e.g. the
    tenant ontology graph or a provenance companion graph), so callers can detect
    a non-KG graph and skip per-KG work rather than mis-deriving a scope.
    """
    if not isinstance(graph_uri, str):
        return None
    m = _KG_GRAPH_RE.match(graph_uri)
    if not m:
        return None
    return m.group("tenant"), m.group("kg")


# ``_IRI_FORBIDDEN`` (defined at the top of this module) is what stops a
# user-supplied ``subject``/``predicate`` here from smuggling a ``>``, which would
# terminate the IRI early and inject arbitrary SPARQL (e.g. a
# ``GRAPH <other-tenant>`` block → cross-tenant read). It used to be defined
# twice in this file, character-for-character identical; ONTA-425 collapsed the
# copies onto the one at the top rather than letting a third grow beside them.


def _escape_value(value: str) -> str:
    """Wrap a value as a URI (<...>), typed literal ("..."^^<xsd:type>), or plain literal ("...").

    Typed literal convention: "500000^^xsd:integer" → "500000"^^<xsd:integer>

    The ``http(s)://`` URI branch REJECTS a value carrying an IRIREF-forbidden
    character (``<`` ``>`` whitespace / control / ``"{}|^`\\``) rather than wrapping
    it verbatim: a ``>`` inside the value would close the ``<…>`` early and let a
    crafted subject/predicate inject SPARQL (a tenant-isolation break on the read
    routes that pass user input here — the history + provenance readers). A
    well-formed absolute IRI never contains these, so this only ever rejects an
    injection attempt.
    """
    if value.startswith("http://") or value.startswith("https://"):
        if _IRI_FORBIDDEN.search(value):
            raise ValueError(f"invalid IRI (illegal character): {value!r}")
        return f"<{value}>"
    if value.startswith("<") and value.endswith(">"):
        inner = value[1:-1]
        if _IRI_FORBIDDEN.search(inner):
            raise ValueError(f"invalid IRI (illegal character): {value!r}")
        return value
    # Check for typed literal: value^^xsd:type
    if "^^" in value:
        literal, xsd_type = value.rsplit("^^", 1)
        return f'"{_escape_literal(literal)}"^^<{xsd_type}>'
    return f'"{_escape_literal(value)}"'


def sparql_string_literal(value: str) -> str:
    """Escape ``value`` for embedding INSIDE a SPARQL ``"…"`` string literal.

    THE one hardened string-literal escaper (ONTA-416). Returns the escaped
    *body* — the caller supplies the surrounding quotes — so it composes with
    both plain (``"…"``) and typed (``"…"^^<xsd:…>``) literal forms.

    SPARQL's ``STRING_LITERAL2`` production forbids a raw ``"``, ``\\``, LF and
    CR inside the quotes, so every one of them MUST be escaped: a value carrying
    an interior newline (a pasted multi-line string, a CSV cell with an embedded
    line break, a search needle) otherwise produces an UNTERMINATED literal and a
    hard parse error at the store — surfacing to the caller as an opaque 500
    rather than the honest 400 the input deserves. ``\\t`` is legal verbatim but
    escaped anyway so the emitted query stays single-line and greppable.

    Order matters: backslash FIRST, so the escape sequences added afterwards are
    not themselves re-escaped. (That ordering was already correct in the three
    partial copies this function replaces, which is why none of them was ever a
    breakout/injection hole — the gap was only the missing ``\\r``/``\\t``/``\\n``
    coverage, i.e. malformed SPARQL rather than injected SPARQL.)
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _escape_literal(value: str) -> str:
    """Back-compat alias for :func:`sparql_string_literal`.

    Kept because the write path (``kg_writer``, ``normalization``, ``history``,
    ``ontology_changelog``) imports this name; it must never grow a second,
    divergent escaping rule — delegate, don't copy.
    """
    return sparql_string_literal(value)


def insert_triples(graph_uri: str, triples: list[tuple[str, str, str]]) -> str:
    triple_strs = []
    for s, p, o in triples:
        triple_strs.append(f"  {_escape_value(s)} {_escape_value(p)} {_escape_value(o)} .")
    body = "\n".join(triple_strs)
    return f"INSERT DATA {{\n  GRAPH <{graph_uri}> {{\n{body}\n  }}\n}}"


def batched_insert_triples(
    graph_uri: str, triples: list[tuple[str, str, str]], batch_size: int = 500,
) -> list[str]:
    """Split triples into batched SPARQL INSERT DATA statements."""
    if not triples:
        return []
    return [
        insert_triples(graph_uri, triples[i : i + batch_size])
        for i in range(0, len(triples), batch_size)
    ]


def delete_triples(graph_uri: str, triples: list[tuple[str, str, str]]) -> str:
    triple_strs = []
    for s, p, o in triples:
        triple_strs.append(f"  {_escape_value(s)} {_escape_value(p)} {_escape_value(o)} .")
    body = "\n".join(triple_strs)
    return f"DELETE DATA {{\n  GRAPH <{graph_uri}> {{\n{body}\n  }}\n}}"


def batched_delete_triples(
    graph_uri: str, triples: list[tuple[str, str, str]], batch_size: int = 500,
) -> list[str]:
    """Split concrete-triple deletes into batched ``DELETE DATA`` statements.

    Mirror of :func:`batched_insert_triples` on the removal side, so a large
    concrete-triple delete is chunked rather than emitted as one oversized
    statement. Used by ``kg_writer.delete_facts`` for its ``triples=`` mode.
    """
    if not triples:
        return []
    return [
        delete_triples(graph_uri, triples[i : i + batch_size])
        for i in range(0, len(triples), batch_size)
    ]


def delete_subjects_query(graph_uri: str, subjects: list[str]) -> str:
    """One batched ``DELETE`` of every triple whose subject is in ``subjects``.

    Bounded by ``len(subjects)`` (a ``VALUES ?s`` list), not by triple count, so a
    subject with thousands of triples is still a single statement. Used by
    ``kg_writer.delete_facts`` for its ``subjects=`` mode; the caller chunks the
    subject list so the statement stays bounded.
    """
    values = " ".join(f"<{s}>" for s in subjects)
    return (
        f"DELETE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}\n"
        f"WHERE {{ GRAPH <{graph_uri}> {{ VALUES ?s {{ {values} }} ?s ?p ?o }} }}"
    )


def delete_subject_predicates_query(
    graph_uri: str, sp_pairs: list[tuple[str, str]]
) -> str:
    """One batched predicate-scoped ``DELETE`` of every ``(subject, predicate)`` object.

    Removes every object of each ``(?s, ?p)`` pair (the "clear this attribute
    before writing the new value" case — e.g. a lambda re-invoke) via a
    ``VALUES (?s ?p)`` list, so the old value is dropped regardless of its literal
    serialization (no fragile object round-trip). Used by
    ``kg_writer.delete_facts`` for ``triples=`` entries whose object is ``None``.
    """
    tuples = " ".join(f"(<{s}> <{p}>)" for s, p in sp_pairs)
    return (
        f"DELETE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}\n"
        f"WHERE {{ GRAPH <{graph_uri}> {{ VALUES (?s ?p) {{ {tuples} }} ?s ?p ?o }} }}"
    )


def delete_node_predicates_query(
    graph_uri: str, node: str, predicates: list[str]
) -> str:
    """One batched ``DELETE`` of the given ``predicates`` (with any objects) on a
    single ``node`` in a graph.

    Removes every object of ``(<node>, ?p)`` for each ``?p`` in ``predicates`` via a
    ``VALUES ?p`` list, so a subset of a node's predicates is cleared in one
    statement regardless of how many objects each carries. Idempotent — a predicate
    the node does not carry simply matches nothing. Bounded by ``len(predicates)``.

    Used by ``graph/validity.py::reopen_interval_update`` (routed through
    ``kg_writer.insert_facts(reopen_facts=…)``) to clear a validity interval node's
    prior CLOSURE predicates (``val:validTo`` / ``val:supersededBy`` / ``val:status``)
    when a previously-closed value is re-asserted as current — the "value
    resurrection" fix. ``node`` and ``predicates`` are internally-minted URIs.
    """
    if not predicates:
        return ""
    values = " ".join(f"<{p}>" for p in predicates)
    return (
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{node}> ?p ?o }} }}\n"
        f"WHERE {{ GRAPH <{graph_uri}> {{ VALUES ?p {{ {values} }} <{node}> ?p ?o }} }}"
    )


def count_subjects_query(graph_uri: str, subjects: list[str]) -> str:
    """COUNT of triples that ``delete_subjects_query`` would remove (removed-count)."""
    values = " ".join(f"<{s}>" for s in subjects)
    return (
        f"SELECT (COUNT(*) AS ?n) FROM <{graph_uri}>\n"
        f"WHERE {{ VALUES ?s {{ {values} }} ?s ?p ?o }}"
    )


def count_subject_predicates_query(
    graph_uri: str, sp_pairs: list[tuple[str, str]]
) -> str:
    """COUNT of triples that ``delete_subject_predicates_query`` would remove."""
    tuples = " ".join(f"(<{s}> <{p}>)" for s, p in sp_pairs)
    return (
        f"SELECT (COUNT(*) AS ?n) FROM <{graph_uri}>\n"
        f"WHERE {{ VALUES (?s ?p) {{ {tuples} }} ?s ?p ?o }}"
    )


def select_subject_predicate_objects_query(
    graph_uri: str, sp_pairs: list[tuple[str, str]]
) -> str:
    """SELECT the CURRENT ``?o`` of every ``(subject, predicate)`` in ``sp_pairs``.

    The read half of an attribute UPDATE: before a predicate-scoped delete drops
    the old value, ``kg_writer.delete_facts`` reads it here so a value CHANGE can
    be versioned (ONTA-236 value history). One ``VALUES (?s ?p)`` batch — the same
    batching style as ``count_subject_predicates_query`` — so a large update reads
    in bounded statements. ``?s ?p`` are projected too so the caller can key the
    returned objects back to their pair.
    """
    tuples = " ".join(f"(<{s}> <{p}>)" for s, p in sp_pairs)
    return (
        f"SELECT ?s ?p ?o FROM <{graph_uri}>\n"
        f"WHERE {{ VALUES (?s ?p) {{ {tuples} }} ?s ?p ?o }}"
    )


def rewrite_subject_update(graph_uri: str, old_uri: str, new_uri: str) -> str:
    """SPARQL update that renames ``old_uri`` to ``new_uri`` in one graph.

    Moves every triple that references ``old_uri`` as SUBJECT and every triple
    that references it as OBJECT onto ``new_uri``, as two ``;``-separated
    operations in one request. Idempotent under RDF set semantics (re-running on
    already-rewritten data is a no-op). This is the single-place SPARQL for
    ``kg_writer.rewrite_subject`` (which adds the provenance ``rewrite`` event and
    the derived-index re-key); ER merge composes it via that primitive.
    """
    return (
        f"WITH <{graph_uri}>\n"
        f"DELETE {{ <{old_uri}> ?p ?o }} INSERT {{ <{new_uri}> ?p ?o }} "
        f"WHERE {{ <{old_uri}> ?p ?o }} ;\n"
        f"WITH <{graph_uri}>\n"
        f"DELETE {{ ?s ?p <{old_uri}> }} INSERT {{ ?s ?p <{new_uri}> }} "
        f"WHERE {{ ?s ?p <{old_uri}> }}"
    )


def rewrite_predicate_update(graph_uri: str, old_pred: str, new_pred: str) -> str:
    """SPARQL update that re-keys every ``(s, old_pred, o)`` onto ``new_pred``.

    The predicate mirror of :func:`rewrite_subject_update`: one server-side
    ``DELETE/INSERT/WHERE`` so the object term keeps its EXACT datatype (a typed
    ``xsd:dateTime`` freshness stamp must not degrade to a plain string on the
    way through — the ONTA-247 lesson; a client-side read-then-reinsert through
    ``parse_sparql_results`` would drop the datatype). Idempotent under RDF set
    semantics. This is the single-place SPARQL for ``kg_writer.rewrite_predicates``
    (the attr_meta companion migration, ONTA-262).
    """
    return (
        f"WITH <{graph_uri}>\n"
        f"DELETE {{ ?s <{old_pred}> ?o }} INSERT {{ ?s <{new_pred}> ?o }} "
        f"WHERE {{ ?s <{old_pred}> ?o }}"
    )


def select_triples(
    graph_uri: str,
    subject: str | None = None,
    predicate: str | None = None,
    obj: str | None = None,
    limit: int = 100,
) -> str:
    s = _escape_value(subject) if subject else "?s"
    p = _escape_value(predicate) if predicate else "?p"
    o = _escape_value(obj) if obj else "?o"
    return (
        f"SELECT ?s ?p ?o FROM <{graph_uri}>\n"
        f"WHERE {{ ?s ?p ?o .\n"
        f"  FILTER(?s = {s} || {s} = ?s)\n"
        f"  FILTER(?p = {p} || {p} = ?p)\n"
        f"  FILTER(?o = {o} || {o} = ?o)\n"
        f"}}\nLIMIT {limit}"
    ) if any([subject, predicate, obj]) else (
        f"SELECT ?s ?p ?o FROM <{graph_uri}>\n"
        f"WHERE {{ ?s ?p ?o . }}\n"
        f"LIMIT {limit}"
    )


def resolve_function_attachment(
    entity_type: str,
    *,
    layer: "Layer | None" = None,
) -> tuple["Layer", str]:
    """Resolve ``(layer, type_uri)`` for attaching a function to a type.

    ``entity_type`` may be:

    * a bare type name (``"Person"``) → Tenant namespace
      (``types/Person``) unless ``layer`` is passed explicitly;
    * a path-shaped name (``"x/Person"`` / ``"public/Person"``) → the layer
      encoded by that path under the ``types/`` prefix;
    * a full type URI in any layer namespace.

    Explicit ``layer`` wins when the input is a bare name; a path-shaped or
    full-URI input that resolves to a different layer is a ValueError so a
    caller cannot smuggle Public via ``layer=ENHANCED``.
    """
    # Local imports: keep queries.py import-light (see register_function_triple).
    from infona_client.graph.layers import (
        Layer,
        layer_from_uri,
        layer_type_uri,
        type_name_from_uri,
    )

    raw = (entity_type or "").strip()
    if not raw:
        raise ValueError("entity_type must not be empty")

    if raw.startswith("http://") or raw.startswith("https://"):
        detected = layer_from_uri(raw)
        if detected is None:
            # Outside every layer namespace — treat as an opaque tenant URI so
            # legacy absolute IRIs keep working rather than being rejected.
            if layer is not None and layer is not Layer.TENANT:
                raise ValueError(
                    f"entity_type URI {raw!r} is outside every layer namespace "
                    f"but layer={layer.value!r} was requested"
                )
            return Layer.TENANT, raw
        if layer is not None and layer is not detected:
            raise ValueError(
                f"entity_type URI {raw!r} is in the {detected.value} namespace "
                f"but layer={layer.value!r} was requested"
            )
        return detected, raw

    # Path-shaped (contains '/') or bare name. Mint under types/ and classify.
    candidate = f"{IRI_BASE}/types/{raw}"
    detected = layer_from_uri(candidate)
    if detected is Layer.PUBLIC or detected is Layer.ENHANCED:
        if layer is not None and layer is not detected:
            raise ValueError(
                f"entity_type {raw!r} resolves to the {detected.value} layer "
                f"but layer={layer.value!r} was requested"
            )
        return detected, candidate

    # Bare name (or non-public/non-enhanced path that still lands in TENANT).
    resolved = layer if layer is not None else Layer.TENANT
    bare = type_name_from_uri(candidate) or raw
    return resolved, layer_type_uri(resolved, bare)


def register_function_triple(
    graph_uri: str,
    entity_type: str,
    function_name: str,
    endpoint_url: str,
    description: str = "",
    *,
    layer: "Layer | None" = None,
) -> str:
    """Build the SPARQL INSERT that attaches a function to a type.

    **Layer-aware attachment identity (ONTA-399).** The type URI is minted via
    :func:`resolve_function_attachment` / :func:`~infona_client.graph.layers.layer_type_uri`
    so Enhanced (``types/x/<T>``) and Tenant (``types/<T>``) attachments land on
    the correct subject. When the resolved layer is Enhanced, the INSERT targets
    the Enhanced named graph (``graphs/global/enhanced``) regardless of the
    caller-supplied ``graph_uri`` — a workspace write must not smuggle Enhanced
    triples into a tenant graph. Tenant-layer attachments keep the caller's
    ``graph_uri``.

    **Public is refused** (ONTA-400 / ``LAYER_CONTENT_MATRIX``): Public is
    attributes + relationships only. Path-shaped ``"public/Person"`` and full
    Public type URIs raise :class:`~infona_client.graph.layer_content.LayerContentError`.

    Runtime is unchanged: this only decides *where* the attachment triple lands.
    Execution still goes through the existing dual model (endpoint-URL registry
    here + lambda executor in ``functions/executor.py``).
    """
    # Local import: queries.py is a low-level SPARQL builder; keep the module
    # importable without pulling the full layer stack at import time, and avoid
    # a cycle with anything that imports queries from layer helpers.
    from infona_client.graph.layer_content import (
        ContentKind,
        assert_permits,
    )
    from infona_client.graph.layers import Layer, enhanced_graph_uri

    resolved_layer, type_uri_val = resolve_function_attachment(
        entity_type, layer=layer
    )
    assert_permits(
        resolved_layer,
        ContentKind.FUNCTIONS,
        what=f"register_function_triple entity_type={entity_type!r}",
    )
    # Global Enhanced always writes into its own named graph so attachment
    # identity and graph location cannot drift. Tenant keeps the caller graph.
    if resolved_layer is Layer.ENHANCED:
        graph_uri = enhanced_graph_uri()

    func_uri = f"{IRI_BASE}/functions/{function_name}"
    triples = [
        (func_uri, f"{IRI_BASE}/onto/attachedTo", type_uri_val),
        (func_uri, f"{IRI_BASE}/onto/endpointUrl", endpoint_url),
        (func_uri, f"{IRI_BASE}/onto/name", function_name),
    ]
    if description:
        triples.append((func_uri, f"{IRI_BASE}/onto/description", description))
    return insert_triples(graph_uri, triples)


BATCH_PREDICATE = f"{IRI_BASE}/onto/batch_id"


def delete_batch_query(graph_uri: str, batch_id: str) -> str:
    """Delete all triples whose subject belongs to a given batch.

    This removes: (1) the batch provenance triple itself, and
    (2) all other triples sharing the same subject.
    """
    return (
        f"DELETE {{\n"
        f"  GRAPH <{graph_uri}> {{ ?s ?p ?o }}\n"
        f"}} WHERE {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f"    ?s <{BATCH_PREDICATE}> \"{_escape_literal(batch_id)}\" .\n"
        f"    ?s ?p ?o .\n"
        f"  }}\n"
        f"}}"
    )


def list_functions_query(
    graph_uri: str,
    entity_type: str | None = None,
    *,
    layer: "Layer | None" = None,
) -> str:
    """List functions in ``graph_uri``, optionally filtered by attached type.

    ``entity_type`` uses the same resolution rules as
    :func:`register_function_triple` (bare name / path / full URI + optional
    ``layer``) so a filter for an Enhanced type matches the layer-qualified
    attachment subject.
    """
    type_filter = ""
    if entity_type:
        _, type_uri_val = resolve_function_attachment(entity_type, layer=layer)
        type_filter = f'  FILTER(?type = <{type_uri_val}>)\n'
    return (
        f"SELECT ?name ?type ?endpoint ?desc FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?func <{IRI_BASE}/onto/name> ?name .\n"
        f"  ?func <{IRI_BASE}/onto/attachedTo> ?type .\n"
        f"  ?func <{IRI_BASE}/onto/endpointUrl> ?endpoint .\n"
        f"  OPTIONAL {{ ?func <{IRI_BASE}/onto/description> ?desc }}\n"
        f"{type_filter}}}"
    )
