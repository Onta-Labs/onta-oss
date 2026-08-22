"""Shared constants and pure helpers for normalization rule execution."""

from __future__ import annotations

import re

import structlog

from infona_client.graph.iri import ENTITY_URI_PREFIX
from infona_client.graph.ontology_queries import (
    RDF,
    RDFS,
    TYPE_URI_PREFIX,
    _safe_id,
    attr_uri,
    entity_uri,
)

logger = structlog.stdlib.get_logger("infona.normalization.execute")

RDF_TYPE = f"{RDF}#type"
RDFS_LABEL = f"{RDFS}#label"
RDFS_RANGE = f"{RDFS}#range"
ATTRS_INFIX = "/attrs/"
NAME_ATTR_SUFFIX = "/attrs/name"

# Slug-aware delimiters: the slug "__" is the de-slugified form of a source-list
# separator (", " etc.). We keep it last so we try the longer composite-name
# split form too. Each is a literal substring to split on.
_FALLBACK_DELIMITERS = [", ", "; ", " / ", " | ", " - ", "__"]

# Emoji / pictographic / junk codepoints to strip from text literals
# (strip_emoji). Scoped to the symbol/pictograph blocks so ordinary letters
# (incl. accented), digits, and real-skill-name punctuation (& + - / # . etc.)
# are left ALONE — e.g. "c++", "C#", "Node.js", "café", "R&D" survive intact.
#   U+200D            zero-width joiner (binds emoji sequences)
#   U+FE0E/U+FE0F     variation selectors (text/emoji presentation)
#   U+1F3FB–U+1F3FF   skin-tone modifiers
#   U+1F1E6–U+1F1FF   regional-indicator letters (flags)
#   U+2600–U+27BF     Misc Symbols + Dingbats
#   U+2B00–U+2BFF     Misc Symbols & Arrows (incl. ⭐ stars, ✅-adjacent)
#   U+1F000–U+1FAFF   the emoji/pictograph planes (Emoticons, Misc Symbols &
#                     Pictographs, Transport & Map, Supplemental, Symbols &
#                     Pictographs Extended-A, etc.)
#   U+2190–U+21FF     Arrows (decorative junk that shows up in scraped text)
#   U+2300–U+23FF     Misc Technical (⌚⏰ etc.)
#   U+2B50 etc. fall inside the ranges above.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0000200d"
    "\U0000fe0e\U0000fe0f"
    "\U0001f3fb-\U0001f3ff"
    "\U0001f1e6-\U0001f1ff"
    "\U00002190-\U000021ff"
    "\U00002300-\U000023ff"
    "\U00002600-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U0001f000-\U0001faff"
    "]+"
)
# Collapse the whitespace left behind once emoji are removed.
_WS_PATTERN = re.compile(r"\s+")


def _affected_types(rule) -> tuple[str, ...]:
    """Types whose SCHEMA this rule changed (for ``refresh_after_write`` re-embed).

    Only ``promote_to_node`` alters the schema — it flips an attribute's
    ``rdfs:range`` from a literal type to an entity type and introduces that
    entity type as a new node type — so it returns ``(owning_type, target_type)``.
    ``list_explode`` / ``strip_emoji`` touch only instance data, so they return
    ``()`` (unchanged behavior — no re-embed).
    """
    if rule.rule_type != "promote_to_node":
        return ()
    target_type = str((rule.params or {}).get("target_type") or "").strip()
    types = [rule.type_name]
    if target_type:
        types.append(target_type)
    return tuple(t for t in types if t)


def _list_explode_as_promotion(rule):
    """Adapt a ``list_explode`` (attribute, target=entity) rule to a
    ``promote_to_node`` value-keyed, split promotion.

    A ``list_explode`` rule's ``params`` has no ``target_type`` (that concept is
    new with ``promote_to_node``), so we derive the node type name from the
    predicate leaf (``specialty`` -> ``Specialty``) unless the caller already put
    a ``target_type`` in params. ``key_by`` is forced to ``"value"`` and ``split``
    to ``True`` — a multi-valued cell exploded into SHARED categorical nodes is
    exactly the value-keyed-with-split shape.

    Returns a shallow copy with ``rule_type="promote_to_node"`` and the derived
    params, so the original rule object is left untouched.
    """
    params = dict(rule.params or {})
    target_type = str(params.get("target_type") or "").strip() or _title_type(
        rule.predicate
    )
    params["target_type"] = target_type
    params["key_by"] = "value"
    params["split"] = True
    return rule.model_copy(update={"rule_type": "promote_to_node", "params": params})


def _title_type(pred_leaf: str) -> str:
    """Best-effort node TYPE name from a predicate leaf: ``specialty`` ->
    ``Specialty``, ``home_city`` -> ``HomeCity``.

    Only used for the ``list_explode target=entity`` back-compat path, where no
    explicit ``target_type`` is supplied. Splits on non-alphanumeric runs and
    title-cases each token; falls back to a capitalised leaf, then ``"Value"``.
    """
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", pred_leaf) if t]
    if not tokens:
        return "Value"
    return "".join(t[:1].upper() + t[1:] for t in tokens)


def _summary_mutated(summary: dict) -> bool:
    """True iff this apply actually changed the graph (so a recompute is worth it).

    Covers every summary shape: list_explode's counters, strip_emoji's, and
    promote_to_node's. A purely idempotent re-run reports all-zero and we skip the
    recompute (and, for promote_to_node, the schema re-embed).
    """
    return any(
        int(summary.get(k, 0))
        for k in (
            "edges_rewritten",
            "atomic_created",
            "orphans_dropped",
            "triples_rewritten",
            "literals_cleaned",
            "nodes_created",
            "edges_added",
            "literals_promoted",
        )
    )


def _delimiters(rule) -> list[str]:
    delims = list((rule.params or {}).get("delimiters") or [])
    # Always include the slug "__" — composite entity names use it even when the
    # source literal used ", " (the slugifier maps both to "__").
    for d in _FALLBACK_DELIMITERS:
        if d not in delims:
            delims.append(d)
    # Longest-first so " / " is tried before "/" etc. — avoids splitting inside a
    # token that legitimately contains the shorter delimiter.
    return sorted(set(delims), key=len, reverse=True)


def _split(value: str, delimiters: list[str]) -> list[str]:
    """Split ``value`` on any of the delimiters into trimmed, de-duped atoms.

    Returns the original single-element list when no delimiter is present (i.e.
    the value is already atomic — the idempotency guarantee).
    """
    # Build one regex alternation of the (escaped) delimiters, longest first.
    pattern = "|".join(re.escape(d) for d in sorted(delimiters, key=len, reverse=True))
    if not pattern:
        return [value.strip()] if value.strip() else []
    parts = re.split(pattern, value)
    atoms: list[str] = []
    seen: set[str] = set()
    for p in parts:
        a = p.strip()
        if a and a not in seen:
            seen.add(a)
            atoms.append(a)
    return atoms


def _literal_predicate(row_type: str | None, rule_type: str, leaf: str) -> str | None:
    """The ``types/<T>/attrs/<leaf>`` predicate a literal is written back on.

    Prefers the ROW's own type (a predicate-scoped read spans types, so the
    entity that holds the value names the right one) and falls back to the
    rule's declaring type when the row carries none. ``None`` when neither is a
    usable type name — that value is skipped rather than written under a
    fabricated predicate.

    **Always an ``attrs/`` form, never ``onto/<leaf>``.** ``kg_writer``'s
    predicate-scoped clear treats an ``onto/`` predicate as possibly-relational
    and drops the subject's RELATIONSHIP edges on that leaf as well
    (``kg_writer_mutate._delete_facts_store``), so writing a literal back under
    ``onto/<leaf>`` would let a later clear destroy unrelated node-valued data.
    """
    for candidate in (row_type, rule_type):
        if not candidate:
            continue
        try:
            return attr_uri(candidate, leaf)
        except Exception:  # noqa: BLE001 — an unusable name costs THAT row only
            continue
    logger.debug("literal_predicate_unresolved", type_name=row_type, predicate=leaf)
    return None


def _group_store_literals(rows, rule, leaf: str) -> dict[tuple[str, str], list]:
    """``LiteralRow``s → ``{(subject, predicate): [value, …]}`` in store order.

    Values stay in their NATIVE store type. Ingest writes a typed literal
    (``"4.6"^^xsd:float``) and the store keeps it as a real float, so a caller
    that rewrites a leaf must hand the untouched siblings back exactly as it
    found them — stringifying them here would silently retype a column the rule
    never even matched.
    """
    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        pred = _literal_predicate(row.type_name, rule.type_name, leaf)
        if pred is None:
            continue
        groups.setdefault((row.subject, pred), []).append(row.value)
    return groups


def _group_sparql_literals(raw) -> dict[tuple[str, str], list]:
    """Residual-arm ``?s ?p ?o`` bindings → the same grouped shape."""
    groups: dict[tuple[str, str], list] = {}
    for r in raw:
        s, p, o = r.get("s", ""), r.get("p", ""), r.get("o", "")
        if not s or not p or o is None or o == "":
            continue
        groups.setdefault((s, p), []).append(o)
    return groups


def _atom_uri(target_type: str, atom: str) -> str:
    """Canonical atomic entity IRI for ``atom`` of ``target_type``:
    ``…/entities/<TargetType>/<slug>``.

    Minted through the ONE shared ``entity_uri`` (graph/ontology_queries) so an
    atom's IRI is byte-identical to how ingestion/discovery mint the composite's
    own IRI. Used both to RE-POINT an edge at the clean atomic node and to decide
    idempotency — the skip check compares an atom's canonical IRI to the
    composite's own IRI, so they MUST be minted the same way for the equality to
    be exact (COG-118). Sharing the minter makes that guarantee structural.
    """
    return entity_uri(target_type, atom)


def _target_type_from_uri(composite_uri: str) -> str | None:
    """``…/entities/<TargetType>/<slug>`` → ``<TargetType>``."""
    if not composite_uri.startswith(ENTITY_URI_PREFIX):
        return None
    tail = composite_uri[len(ENTITY_URI_PREFIX):]
    head = tail.split("/", 1)[0]
    return head or None


def _target_type_from_type_uri(t_uri: str) -> str | None:
    """``…/types/<TargetType>`` → ``<TargetType>``."""
    prefix = TYPE_URI_PREFIX
    if not t_uri.startswith(prefix):
        return None
    tail = t_uri[len(prefix):].strip("/")
    return tail or None


def _subject_local_id(subject_uri: str) -> str:
    """The part of a subject URI after the last ``/`` — the owner's local id.

    Used only by the ``key_by="owner"`` node-identity strategy: the Rating node
    for ``…/entities/CoffeeShop/shop-1`` keys on ``shop-1`` so each owner gets its
    OWN measurement node. Trailing slashes are stripped first so a URI that ends
    in ``/`` still yields its real last segment.
    """
    return subject_uri.rstrip("/").rsplit("/", 1)[-1]


def _node_uri_value(target_type: str, value: str) -> str:
    """Value-keyed node IRI: ``…/entities/<TargetType>/<slug(value)>``.

    SHARED across every owner with the same value (free dedup) — the categorical
    strategy. Minted through the ONE shared ``entity_uri`` (graph/ontology_queries),
    the SAME minter ``_atom_uri`` / ``list_explode`` use, so a promoted categorical
    node coincides exactly with the node ``list_explode`` would mint for the same
    value (cross-rail consistency)."""
    return entity_uri(target_type, value)


def _node_uri_owner(target_type: str, subject_uri: str, pred_leaf: str) -> str:
    """Owner-keyed node IRI: ``…/entities/<TargetType>/<slug(owner_id)>-<leaf>``.

    One node PER OWNER (two shops rated 4.6 are NOT the same Rating). The owner's
    local id disambiguates, and the ``-<leaf>`` suffix keeps two owner-keyed
    promotions on DIFFERENT predicates of the same owner from colliding (a shop's
    ``rating`` node vs its ``price`` node). The base ``…/entities/<TargetType>/
    <slug(owner_id)>`` is the shared ``entity_uri`` (so it coincides with every
    other rail's node for that owner id); the ``-<slug(leaf)>`` suffix is appended
    exactly as before — byte-identical to the old ``ENTITY_URI_PREFIX`` + ``_slug``
    form."""
    return f"{entity_uri(target_type, _subject_local_id(subject_uri))}-{_safe_id(pred_leaf)}"


def _strip_emoji_value(value: str) -> str:
    """Remove emoji / pictographic junk from one text value, collapse whitespace.

    Pure + deterministic. ``"🎨 design"`` → ``"design"``; ``"design 🚀"`` →
    ``"design"``; ``"ai 🚀 growth"`` → ``"ai growth"``; a pure-emoji value → ``""``
    (the caller drops empties). A value with no emoji is returned UNCHANGED after
    a no-op whitespace collapse, so re-running is idempotent and ordinary names
    (``"c++"``, ``"café"``, ``"R&D"``) are never touched.
    """
    stripped = _EMOJI_PATTERN.sub(" ", value)
    return _WS_PATTERN.sub(" ", stripped).strip()


def _decode_local_name(uri: str) -> str:
    """The local-name of an entity URI, percent-decoded (best-effort)."""
    from urllib.parse import unquote

    tail = uri.rstrip("/").split("/")[-1]
    return unquote(tail)


def _sparql_str(s: str) -> str:
    """Escape a Python string for embedding inside a SPARQL double-quoted literal.

    Used for CONTAINS/STRENDS argument literals — the only place we splice a
    delimiter/suffix into a query. Escapes backslash, quote, and newline.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _host():
    """Call-time lookup of the public ``execute`` module.

    Tests may monkeypatch ``insert_facts`` / ``delete_facts`` /
    ``refresh_after_write`` / ``logger`` on ``normalization.execute``.
    Siblings look those up at call time.
    """
    from infona_client.normalization import execute as _mod

    return _mod
