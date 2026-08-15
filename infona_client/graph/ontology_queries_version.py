"""Deterministic ontology content fingerprint (ONTA-270 / ONTA-403)."""

import hashlib

from infona_client.graph.ontology_queries_uris import attr_uri, type_uri


def ontology_version(
    types,
    attrs,
    parent_of=None,
    *,
    comments=None,
    core_slots=None,
    text_kinds=None,
) -> str:
    """Deterministic content fingerprint of an ontology SNAPSHOT (ONTA-270 / ONTA-403).

    A stable hash over the ontology's identity-bearing shape — the SORTED set of
    existing type URIs, attribute declarations (type + attribute + range/datatype),
    and ``rdfs:subClassOf`` edges — so the same ontology state always yields the
    same version and ANY change (a new type, a new attribute, a new subclass edge)
    yields a different one. Order-independent (everything sorted) and free of
    timestamps / nonces, so it is safe to freeze into a characterization fixture
    and to compare across processes.

    ONTA-403 extends the fingerprint to also cover (when present):

    * **type comments** — non-empty ``rdfs:comment`` on a type (from ``types``
      values that are strings, or from ``comments`` ``{type: text}``);
    * **attribute comments** — non-empty ``schema.description`` or
      ``comments`` ``{(type, attr): text}`` / nested ``{type: {attr: text}}``;
    * **relationship ranges** — already covered by the attribute ``datatype``
      (a type name vs an XSD primitive);
    * **core-slot markers** — ``core_slots`` as an iterable of ``(type, slot)``
      pairs or a nested ``{type: iterable[slot]}``;
    * **text-kind markers** — ``text_kinds`` as ``{(type, attr): kind}`` or
      nested ``{type: {attr: kind}}``.

    Empty / ``None`` extensions contribute nothing, so the empty-ontology
    fingerprint (``ontology_version({}, {})``) stays the frozen constant
    ``e3b0c44298fc1c14``.

    Used as the optimistic-concurrency token stamped onto an A5 Placement Plan:
    P5 stamps the version it READ the ontology at; P6 rejects/recomputes a plan
    whose stamp no longer matches the current ontology (a concurrent run advanced
    it T→T+1 between plan and apply). See
    :attr:`infona_client.pipeline.envelope.ArtifactEnvelope.ontology_version`.

    ``types`` is any mapping keyed by type NAME. Non-empty string values are
    treated as type comments (ONTA-403); non-string values are ignored for the
    comment channel (legacy callers pass ``""`` placeholders). ``attrs`` is
    ``{type_name: {attr_name: schema}}`` where ``schema`` may carry a ``.datatype``
    (the resolver's in-memory ``AttributeSchema``) or be a plain datatype string —
    both hash identically for the same (type, attr, datatype). ``parent_of`` is the
    ``{child_name: parent_name}`` subclass map (``None`` / ``{}`` = no edges). The
    return is a short hex digest — a change-detector token, not a security hash.
    """
    h = hashlib.sha256()
    for t in sorted(types or {}):
        h.update(b"T:")
        h.update(type_uri(t).encode("utf-8"))
        h.update(b"\n")
    for t in sorted(attrs or {}):
        for a in sorted(attrs[t] or {}):
            schema = attrs[t][a]
            datatype = getattr(schema, "datatype", schema)
            h.update(b"A:")
            h.update(attr_uri(t, a).encode("utf-8"))
            h.update(b"=")
            h.update(str(datatype).encode("utf-8"))
            h.update(b"\n")
    for child in sorted(parent_of or {}):
        h.update(b"S:")
        h.update(type_uri(child).encode("utf-8"))
        h.update(b"<")
        h.update(type_uri(parent_of[child]).encode("utf-8"))
        h.update(b"\n")

    # --- ONTA-403 extensions (contribute only when non-empty) -----------------
    # Type comments from the types mapping values (string descriptions).
    for t in sorted(types or {}):
        val = types[t]
        if isinstance(val, str) and val:
            h.update(b"TC:")
            h.update(type_uri(t).encode("utf-8"))
            h.update(b"=")
            h.update(val.encode("utf-8"))
            h.update(b"\n")
    # Explicit comments map: {type: text} and/or {(type, attr): text} /
    # {type: {attr: text}}. Type-level keys that duplicate a types-value
    # comment are written once here only when not already covered above —
    # callers should prefer one channel; double-writing the same text is
    # harmless only if they use the same source of truth.
    if comments:
        type_comments: dict[str, str] = {}
        attr_comments: dict[tuple[str, str], str] = {}
        for key, val in comments.items():
            if not val:
                continue
            if isinstance(key, tuple) and len(key) == 2:
                attr_comments[(str(key[0]), str(key[1]))] = str(val)
            elif isinstance(val, dict):
                for a, c in val.items():
                    if c:
                        attr_comments[(str(key), str(a))] = str(c)
            else:
                type_comments[str(key)] = str(val)
        for t in sorted(type_comments):
            # Skip if already hashed from types[t] string value.
            existing = types.get(t) if types else None
            if isinstance(existing, str) and existing == type_comments[t]:
                continue
            h.update(b"TC:")
            h.update(type_uri(t).encode("utf-8"))
            h.update(b"=")
            h.update(type_comments[t].encode("utf-8"))
            h.update(b"\n")
        for (t, a) in sorted(attr_comments):
            h.update(b"AC:")
            h.update(attr_uri(t, a).encode("utf-8"))
            h.update(b"=")
            h.update(attr_comments[(t, a)].encode("utf-8"))
            h.update(b"\n")
    # Attribute descriptions carried on schema objects.
    for t in sorted(attrs or {}):
        for a in sorted(attrs[t] or {}):
            schema = attrs[t][a]
            desc = getattr(schema, "description", None)
            if isinstance(desc, str) and desc:
                h.update(b"AC:")
                h.update(attr_uri(t, a).encode("utf-8"))
                h.update(b"=")
                h.update(desc.encode("utf-8"))
                h.update(b"\n")
    # Core-slot markers.
    if core_slots:
        pairs: list[tuple[str, str]] = []
        if isinstance(core_slots, dict):
            for t, slots in core_slots.items():
                for s in slots or ():
                    pairs.append((str(t), str(s)))
        else:
            for item in core_slots:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    pairs.append((str(item[0]), str(item[1])))
        for t, s in sorted(set(pairs)):
            h.update(b"CS:")
            h.update(attr_uri(t, s).encode("utf-8"))
            h.update(b"\n")
    # Text-kind markers.
    if text_kinds:
        kind_pairs: list[tuple[str, str, str]] = []
        if all(isinstance(k, tuple) for k in text_kinds.keys()):
            for (t, a), kind in text_kinds.items():
                if kind:
                    kind_pairs.append((str(t), str(a), str(kind)))
        else:
            for t, inner in text_kinds.items():
                if isinstance(inner, dict):
                    for a, kind in inner.items():
                        if kind:
                            kind_pairs.append((str(t), str(a), str(kind)))
        for t, a, kind in sorted(kind_pairs):
            h.update(b"TK:")
            h.update(attr_uri(t, a).encode("utf-8"))
            h.update(b"=")
            h.update(kind.encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()[:16]
