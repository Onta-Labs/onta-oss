"""OntologyShape + fingerprint / load path for ontology commits.

Looks up patched names on :mod:`infona_client.graph.ontology_commit` via
``_host()``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import structlog

from infona_client.graph.aliases import fetch_alias_map
from infona_client.graph.iri import TYPE_URI_PREFIX
from infona_client.graph.ontology_commit_core import (
    DEPRECATED_AT,
    SUPERSEDED_BY,
)
from infona_client.graph.ontology_queries import (
    full_ontology_detail_query,
    ontology_version,
    parent_map_query,
    text_kind_map_query,
)
from infona_client.graph.parser import parse_sparql_results

logger = structlog.stdlib.get_logger("infona.graph.ontology_commit")


def _host():
    """Call-time lookup of the public ontology_commit module (monkeypatch surface)."""
    from infona_client.graph import ontology_commit as _mod

    return _mod


@dataclass
class OntologyShape:
    """Identity-bearing ontology snapshot used by fingerprint + ONTA-406 diff.

    Companions under ``attr_meta/`` are never loaded here — they are not
    ontology content (plan §2). Deprecation markers (ONTA-404) *are* schema
    identity and are included in :meth:`fingerprint`.
    """

    types: dict[str, str] = field(default_factory=dict)  # name -> comment
    attrs: dict[str, dict[str, str]] = field(default_factory=dict)  # type -> attr -> dt
    parent_of: dict[str, str] = field(default_factory=dict)
    # Nested attr comments: {type: {attr: text}} — type comments live in types.
    attr_comments: dict[str, dict[str, str]] = field(default_factory=dict)
    core_slots: list[tuple[str, str]] = field(default_factory=list)
    text_kinds: dict[tuple[str, str], str] = field(default_factory=dict)
    alias_map: dict[str, str] = field(default_factory=dict)
    # Deprecation (ONTA-404): type_name -> superseded_by ("" if unmarked replacement).
    deprecated_types: dict[str, str] = field(default_factory=dict)
    # (type_name, slot_name) -> superseded_by ("" if unmarked).
    deprecated_slots: dict[tuple[str, str], str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Same digest :func:`fingerprint_ontology` would return for this shape."""
        comments: dict = {}
        if self.attr_comments:
            comments.update(self.attr_comments)
        base = ontology_version(
            self.types,
            self.attrs,
            self.parent_of,
            comments=comments or None,
            core_slots=self.core_slots or None,
            text_kinds=self.text_kinds or None,
        )
        if not self.alias_map and not self.deprecated_types and not self.deprecated_slots:
            return base
        h = hashlib.sha256()
        h.update(base.encode("utf-8"))
        h.update(b"\n")
        for old in sorted(self.alias_map):
            h.update(b"AL:")
            h.update(old.encode("utf-8"))
            h.update(b"=")
            h.update(self.alias_map[old].encode("utf-8"))
            h.update(b"\n")
        for t in sorted(self.deprecated_types):
            h.update(b"DEP:")
            h.update(t.encode("utf-8"))
            sup = self.deprecated_types[t] or ""
            if sup:
                h.update(b"=")
                h.update(sup.encode("utf-8"))
            h.update(b"\n")
        for (t, slot) in sorted(self.deprecated_slots):
            h.update(b"DEPA:")
            h.update(t.encode("utf-8"))
            h.update(b".")
            h.update(slot.encode("utf-8"))
            sup = self.deprecated_slots[(t, slot)] or ""
            if sup:
                h.update(b"=")
                h.update(sup.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()[:16]


def shape_to_dict(shape: OntologyShape) -> dict:
    """JSON-serializable form of :class:`OntologyShape` for frozen snapshots."""
    return {
        "types": dict(shape.types),
        "attrs": {t: dict(a) for t, a in shape.attrs.items()},
        "parent_of": dict(shape.parent_of),
        "attr_comments": {t: dict(a) for t, a in shape.attr_comments.items()},
        "core_slots": [list(p) for p in shape.core_slots],
        "text_kinds": {
            f"{t}.{a}": k for (t, a), k in shape.text_kinds.items()
        },
        "alias_map": dict(shape.alias_map),
        "deprecated_types": dict(shape.deprecated_types),
        "deprecated_slots": {
            f"{t}.{a}": s for (t, a), s in shape.deprecated_slots.items()
        },
    }


def shape_from_dict(data: dict | None) -> OntologyShape:
    """Inverse of :func:`shape_to_dict`."""
    if not data:
        return OntologyShape()
    text_kinds: dict[tuple[str, str], str] = {}
    for key, kind in (data.get("text_kinds") or {}).items():
        if "." in key:
            t, a = key.split(".", 1)
            text_kinds[(t, a)] = kind
    deprecated_slots: dict[tuple[str, str], str] = {}
    for key, sup in (data.get("deprecated_slots") or {}).items():
        if "." in key:
            t, a = key.split(".", 1)
            deprecated_slots[(t, a)] = sup
    core_slots = [tuple(p) for p in (data.get("core_slots") or [])]
    return OntologyShape(
        types=dict(data.get("types") or {}),
        attrs={t: dict(a) for t, a in (data.get("attrs") or {}).items()},
        parent_of=dict(data.get("parent_of") or {}),
        attr_comments={
            t: dict(a) for t, a in (data.get("attr_comments") or {}).items()
        },
        core_slots=list(core_slots),  # type: ignore[arg-type]
        text_kinds=text_kinds,
        alias_map=dict(data.get("alias_map") or {}),
        deprecated_types=dict(data.get("deprecated_types") or {}),
        deprecated_slots=deprecated_slots,
    )


async def load_ontology_shape(neptune, graph_uri: str) -> OntologyShape:
    """Read the live ontology shape from ``graph_uri`` (ONTA-403/406).

    Shared by :func:`fingerprint_ontology` and the ONTA-406 diff/snapshot path
    so the two cannot disagree on what counts as ontology content.

    On Neo4j / GraphStore (always, in production) the shape is loaded from the
    ontology catalog + companion bag. Frozen snapshot graphs (``…/v{N}``,
    ``…/revisions/r{N}``) read the companion's stored shape JSON.
    """
    from infona_client.graph.store import GraphConfigError, get_graph_store

    try:
        get_graph_store()
    except GraphConfigError:
        pass
    else:
        # GraphStore is configured — never fall through to SPARQL (ONTA-531).
        return await _host()._load_ontology_shape_graph_store(graph_uri)

    types: dict[str, str] = {}
    attrs: dict[str, dict[str, str]] = {}
    attr_comments: dict[str, dict[str, str]] = {}
    core_slots: list[tuple[str, str]] = []

    try:
        raw = await neptune.query(full_ontology_detail_query(graph_uri))
        _, rows = parse_sparql_results(raw)
    except Exception:
        logger.warning("ontology_shape_fetch_failed", graph_uri=graph_uri, exc_info=True)
        rows = []

    for row in rows:
        tlabel = row.get("typeLabel") or ""
        if not tlabel:
            continue
        if tlabel not in types:
            types[tlabel] = row.get("typeComment") or ""
            attrs[tlabel] = {}
        if row.get("typeComment") and not types[tlabel]:
            types[tlabel] = row["typeComment"]
        alabel = row.get("attrLabel") or ""
        if alabel:
            range_str = row.get("range") or ""
            datatype = _range_to_datatype(range_str)
            attrs[tlabel][alabel] = datatype
            ac = row.get("attrComment") or ""
            if ac:
                attr_comments.setdefault(tlabel, {})[alabel] = ac
            core = row.get("core") or ""
            if core and str(core).lower() in ("true", "1"):
                core_slots.append((tlabel, alabel))

    parent_of: dict[str, str] = {}
    try:
        raw_p = await neptune.query(parent_map_query(graph_uri))
        _, prows = parse_sparql_results(raw_p)
        for row in prows:
            child_uri = row.get("child") or ""
            parent_uri = row.get("parent") or ""
            if child_uri and parent_uri:
                child = child_uri.rsplit("/", 1)[-1]
                parent = parent_uri.rsplit("/", 1)[-1]
                if child and parent:
                    parent_of[child] = parent
    except Exception:
        logger.warning("ontology_shape_parent_map_failed", graph_uri=graph_uri, exc_info=True)

    text_kinds: dict[tuple[str, str], str] = {}
    try:
        raw_t = await neptune.query(text_kind_map_query(graph_uri))
        _, trows = parse_sparql_results(raw_t)
        for row in trows:
            a_uri = row.get("attr") or ""
            kind = row.get("kind") or ""
            if not a_uri or not kind:
                continue
            parts = a_uri.split("/types/", 1)
            if len(parts) != 2 or "/attrs/" not in parts[1]:
                continue
            type_part, attr_part = parts[1].split("/attrs/", 1)
            type_name = type_part.rsplit("/", 1)[-1]
            attr_name = attr_part
            if type_name and attr_name:
                text_kinds[(type_name, attr_name)] = kind
    except Exception:
        logger.warning("ontology_shape_text_kinds_failed", graph_uri=graph_uri, exc_info=True)

    try:
        alias_map = await fetch_alias_map(neptune, graph_uri)
    except Exception:
        logger.warning("ontology_shape_aliases_failed", graph_uri=graph_uri, exc_info=True)
        alias_map = {}

    deprecated_types: dict[str, str] = {}
    deprecated_slots: dict[tuple[str, str], str] = {}
    try:
        raw_d = await neptune.query(
            f"SELECT ?s ?dep ?sup FROM <{graph_uri}> WHERE {{\n"
            f"  ?s <{DEPRECATED_AT}> ?dep .\n"
            f"  OPTIONAL {{ ?s <{SUPERSEDED_BY}> ?sup }}\n"
            f"}}"
        )
        _, drows = parse_sparql_results(raw_d)
        for row in drows:
            s = (row.get("s") or "").strip()
            if not s:
                continue
            sup = (row.get("sup") or "").strip()
            if "/attrs/" in s and "/types/" in s:
                try:
                    after = s.split("/types/", 1)[1]
                    type_part, attr_part = after.split("/attrs/", 1)
                    t_name = type_part.rsplit("/", 1)[-1]
                    a_name = attr_part
                    if t_name and a_name:
                        deprecated_slots[(t_name, a_name)] = (
                            sup.rsplit("/", 1)[-1] if sup else ""
                        )
                except ValueError:
                    continue
            elif "/types/" in s:
                t_name = s.rsplit("/", 1)[-1]
                if t_name:
                    deprecated_types[t_name] = (
                        sup.rsplit("/", 1)[-1] if sup else ""
                    )
    except Exception:
        logger.warning(
            "ontology_shape_deprecations_failed", graph_uri=graph_uri, exc_info=True
        )

    return OntologyShape(
        types=types,
        attrs=attrs,
        parent_of=parent_of,
        attr_comments=attr_comments,
        core_slots=core_slots,
        text_kinds=text_kinds,
        alias_map=alias_map or {},
        deprecated_types=deprecated_types,
        deprecated_slots=deprecated_slots,
    )


async def _load_ontology_shape_graph_store(graph_uri: str) -> OntologyShape:
    """Load OntologyShape from catalog + companion (Neo4j product path)."""
    from infona_client.graph import ontology_catalog as oc
    from infona_client.graph.ontology_companion import (
        catalog_session_kwargs,
        catalog_target_from_graph_uri,
        get_ontology_companion,
    )

    bag = get_ontology_companion()

    # Frozen snapshot graph — return the stored shape (empty if never written).
    if _host().is_immutable_version_graph(graph_uri):
        frozen = bag.frozen_shapes.get(graph_uri.rstrip("/"))
        if frozen is None:
            # Unreadable / missing parent content → empty shape so the B1
            # fingerprint mismatch fails closed at the publish gate.
            return OntologyShape()
        return _host().shape_from_dict(frozen)

    target = catalog_target_from_graph_uri(graph_uri)
    live = target.live_graph_uri
    cat_kw = catalog_session_kwargs(target, for_write=False)

    types_list = await oc.list_types(**cat_kw)
    attrs_list = await oc.list_attributes(**cat_kw)

    types: dict[str, str] = {}
    parent_of: dict[str, str] = {}
    deprecated_types: dict[str, str] = {}
    for t in types_list:
        types[t.name] = t.description or ""
        if t.parent_type:
            parent_of[t.name] = t.parent_type
        if t.deprecated_at:
            deprecated_types[t.name] = t.superseded_by or ""

    attrs: dict[str, dict[str, str]] = {name: {} for name in types}
    attr_comments: dict[str, dict[str, str]] = {}
    core_slots: list[tuple[str, str]] = []
    text_kinds: dict[tuple[str, str], str] = {}
    deprecated_slots: dict[tuple[str, str], str] = {}
    for a in attrs_list:
        attrs.setdefault(a.domain, {})
        if a.kind == "relationship" and a.range_type:
            attrs[a.domain][a.name] = a.range_type
        else:
            attrs[a.domain][a.name] = a.datatype or "string"
        if a.description:
            attr_comments.setdefault(a.domain, {})[a.name] = a.description
        if a.core_slot:
            core_slots.append((a.domain, a.name))
        if a.text_kind:
            text_kinds[(a.domain, a.name)] = a.text_kind
        if a.deprecated_at:
            deprecated_slots[(a.domain, a.name)] = a.superseded_by or ""

    # Alias map (flatten chains for fingerprint parity with SPARQL path).
    raw_aliases = dict(bag.aliases.get(live) or {})
    alias_map = _flatten_alias_map(raw_aliases)

    return OntologyShape(
        types=types,
        attrs=attrs,
        parent_of=parent_of,
        attr_comments=attr_comments,
        core_slots=core_slots,
        text_kinds=text_kinds,
        alias_map=alias_map,
        deprecated_types=deprecated_types,
        deprecated_slots=deprecated_slots,
    )


def _flatten_alias_map(edges: dict[str, str]) -> dict[str, str]:
    """Flatten a→b→c chains; drop cycles (same as fetch_alias_map)."""
    resolved: dict[str, str] = {}
    for old in edges:
        target = edges[old]
        seen = {old}
        while target in edges:
            if target in seen:
                target = ""
                break
            seen.add(target)
            target = edges[target]
        if target and target != old:
            resolved[old] = target
    return resolved


async def fingerprint_ontology(neptune, graph_uri: str) -> str:
    """Read the live ontology shape from ``graph_uri`` and return its fingerprint.

    Covers types, attributes (with ranges), subclass edges, comments, core-slot
    markers, text-kinds (ONTA-403 extended surface), and attribute aliases
    (ONTA-407a — alias registration must shift the concurrency token).
    """
    shape = await _host().load_ontology_shape(neptune, graph_uri)
    return shape.fingerprint()


def _range_to_datatype(range_str: str) -> str:
    if not range_str:
        return "string"
    type_uri_prefix = TYPE_URI_PREFIX
    if range_str.startswith(type_uri_prefix):
        return range_str[len(type_uri_prefix):].rsplit("/", 1)[-1]
    if "#" in range_str:
        fragment = range_str.split("#")[-1]
        dt_map = {
            "string": "string",
            "integer": "integer",
            "float": "float",
            "double": "float",
            "boolean": "boolean",
            "dateTime": "datetime",
            "anyURI": "uri",
            "Resource": "uri",
            "wktLiteral": "geo",
        }
        return dt_map.get(fragment, "string")
    return "string"
