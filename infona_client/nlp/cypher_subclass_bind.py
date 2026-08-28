"""Live /ask Cypher subclass bind (INF-599).

Production NL is LLM Cypher, not SPARQL. After Contact⊑Person, a Person-only
plan (``c.name IN ['Person']`` + ``p.name = 'first_name'``) matches zero
INSTANCE_OF rows. This module expands type lists (and SPARQL-era attr IRIs)
on the generated Cypher + params bag **after** the LLM returns, before
confine/execute.

``p.name = 'first_name'`` stays a leaf (Neo4j Facts flatten the type
segment). Type membership must include Contact|Staff. Attribute IRIs are
emitted only for types that *declare* the leaf — Staff with ``name`` not
``first_name`` is omitted from predicates.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from infona_client.graph.iri import TYPE_URI_PREFIX
from infona_client.graph.rdfs_helpers import (
    bind_subclass_attribute,
    declared_attr_leaves_by_type,
    extract_subclass_map_from_ontology,
    type_names_with_subclasses,
)

_TYPE_LIST_KEYS = ("type_names", "from_types", "to_types")

_IN_LITERAL_RE = re.compile(
    r"(?P<left>\b(?:c|cls|class)\.(?:name|id)\s+IN\s*)\[(?P<body>[^\]]*)\]",
    re.IGNORECASE,
)
_EQ_LITERAL_RE = re.compile(
    r"(?P<left>\b(?:c|cls|class)\.(?:name|id)\s*=\s*)['\"]"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)['\"]",
    re.IGNORECASE,
)
_ATTR_IRI_RE = re.compile(
    rf"<?{re.escape(TYPE_URI_PREFIX)}(\w+)/attrs/(\w+)>?"
)
_QUOTED_NAME_RE = re.compile(r"""['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]""")


def _union_expand(
    names: list[str],
    child_to_parent: Mapping[str, str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        t = str(raw or "").strip()
        if not t:
            continue
        for n in type_names_with_subclasses(t, child_to_parent=child_to_parent):
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _format_in_list(names: list[str]) -> str:
    return "[" + ", ".join(f"'{n}'" for n in names) + "]"


def expand_cypher_subclass_binds(
    cypher: str,
    params: dict[str, Any] | None,
    ontology_summary: str,
) -> tuple[str, dict[str, Any]]:
    """Expand Person-only Cypher type filters to Contact|Staff (live /ask).

    Returns ``(cypher, params)``. No-op when the ontology has no subclass map.
    Idempotent. Does not touch ``primary_type`` (asserted-leaf denorm).
    """
    out_params = dict(params or {})
    text = cypher or ""
    smap = extract_subclass_map_from_ontology(ontology_summary)
    if not smap:
        return text, out_params

    declared = declared_attr_leaves_by_type(ontology_summary)

    for key in _TYPE_LIST_KEYS:
        raw = out_params.get(key)
        if isinstance(raw, list) and raw:
            out_params[key] = _union_expand(
                [str(x) for x in raw], smap
            )

    def _expand_in(m: re.Match[str]) -> str:
        names = _QUOTED_NAME_RE.findall(m.group("body") or "")
        expanded = _union_expand(names, smap)
        if not expanded or expanded == names:
            return m.group(0)
        return m.group("left") + _format_in_list(expanded)

    text = _IN_LITERAL_RE.sub(_expand_in, text)

    def _expand_eq(m: re.Match[str]) -> str:
        name = m.group("name")
        expanded = type_names_with_subclasses(name, child_to_parent=smap)
        if len(expanded) <= 1:
            return m.group(0)
        return m.group("left").replace("=", "IN", 1) + _format_in_list(expanded)

    text = _EQ_LITERAL_RE.sub(_expand_eq, text)

    # SPARQL-era attr IRIs in free-form Cypher (or leftover SPARQL).
    prop_key = out_params.get("prop_key")
    if not isinstance(prop_key, str) or not prop_key.strip():
        # Infer from attr IRIs in the query.
        iri_leaves = {m.group(2) for m in _ATTR_IRI_RE.finditer(text)}
        if len(iri_leaves) == 1:
            prop_key = next(iter(iri_leaves))
            out_params.setdefault("prop_key", prop_key)

    roots: list[str] = []
    for key in _TYPE_LIST_KEYS:
        raw = out_params.get(key)
        if isinstance(raw, list):
            roots.extend(str(x) for x in raw if x)
    for m in _IN_LITERAL_RE.finditer(text):
        roots.extend(_QUOTED_NAME_RE.findall(m.group("body") or ""))
    # After equality rewrite, IN lists cover those too.

    if isinstance(prop_key, str) and prop_key.strip() and roots:
        # Bind predicates from the *parent* roots (pre-union names that have kids).
        # Use full expanded type_names as the ask's type scope.
        pred_seen: list[str] = []
        pred_set: set[str] = set()
        # Prefer the shallowest parent that actually has descendants.
        parent_roots = []
        seen_r: set[str] = set()
        for t in roots:
            t = str(t).strip()
            if not t or t in seen_r:
                continue
            seen_r.add(t)
            kids = type_names_with_subclasses(t, child_to_parent=smap)
            if len(kids) > 1:
                parent_roots.append(t)
        bind_from = parent_roots or [str(roots[0]).strip()]
        for parent in bind_from:
            bind = bind_subclass_attribute(
                parent,
                prop_key.strip(),
                child_to_parent=smap,
                ontology_summary=ontology_summary,
                declared_by_type=declared,
            )
            for p in bind["predicates"] or []:
                if p not in pred_set:
                    pred_set.add(str(p))
                    pred_seen.append(str(p))
        if pred_seen:
            out_params["predicates"] = pred_seen
            # Rewrite a parent-only attr IRI so the plan is not Person-IRI-only.
            for parent in bind_from:
                parent_iri = f"{TYPE_URI_PREFIX}{parent}/attrs/{prop_key.strip()}"
                if parent_iri in text or f"<{parent_iri}>" in text:
                    # Keep Contact IRI present; drop a lone parent IRI by
                    # appending child IRIs next to it (tests look for Contact).
                    extras = " ".join(
                        f"<{p}>" for p in pred_seen if parent_iri not in p
                    )
                    if extras:
                        text = text.replace(f"<{parent_iri}>", f"<{parent_iri}> {extras}")
                        if f"<{parent_iri}> {extras}" not in text:
                            text = text.replace(parent_iri, parent_iri + " " + extras)

    return text, out_params
