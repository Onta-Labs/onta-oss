"""Subclass-attribute bind helpers (INF-599).

Kept off :mod:`infona_client.graph.rdfs_helpers_templates` so that module
stays under the new-file hard cap. The public facade
(:mod:`infona_client.graph.rdfs_helpers`) re-exports these names.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from infona_client.graph.rdfs_helpers_templates import (
    _TYPE_LINE_RE,
    type_names_with_subclasses,
)

_ATTR_DASH_RE = re.compile(
    r"(?im)^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:"
)
_ATTR_KEY_RE = re.compile(r"\bkey=([A-Za-z_][A-Za-z0-9_]*)")


def declared_attr_leaves_by_type(ontology_summary: str) -> dict[str, set[str]]:
    """Parse ``Type:`` blocks into type → declared literal leaf names.

    Used so Person.first_name binds Contact.first_name but not Staff.first_name
    when Staff only declared ``name`` (INF-599).
    """
    out: dict[str, set[str]] = {}
    current: str | None = None
    for line in (ontology_summary or "").splitlines():
        tm = _TYPE_LINE_RE.match(line)
        if tm:
            current = tm.group(1)
            out.setdefault(current, set())
            continue
        if current is None:
            continue
        am = _ATTR_DASH_RE.match(line)
        if am:
            out[current].add(am.group(1))
        for km in _ATTR_KEY_RE.finditer(line):
            out[current].add(km.group(1))
    return out


def subclass_attribute_predicates(
    parent_type: str,
    attr_leaf: str,
    *,
    child_to_parent: Mapping[str, str] | None = None,
    ontology_summary: str = "",
    declared_by_type: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Bind ``types/<Child>/attrs/<leaf>`` for a parent and its descendants.

    Instance triples stay on the asserted leaf (ADR 0001): Contact.first_name
    is ``types/Contact/attrs/first_name``, never rewritten onto Person. A
    Person ask that only binds ``types/Person/attrs/first_name`` returns
    empty names. Callers must bind the child predicates too.

    When ``declared_by_type`` / ``ontology_summary`` is provided, a descendant
    is included only if it actually declares ``attr_leaf`` — Staff with
    ``name`` (not ``first_name``) is omitted.

    Order matches :func:`type_names_with_subclasses` (parent first, then
    children alphabetically).
    """
    from infona_client.graph.ontology_queries_uris import attr_uri

    if not parent_type or not attr_leaf:
        return []
    types = type_names_with_subclasses(
        parent_type,
        ontology_summary=ontology_summary,
        child_to_parent=child_to_parent,
        include_subclasses=True,
    )
    declared = declared_by_type
    if declared is None and ontology_summary:
        declared = declared_attr_leaves_by_type(ontology_summary)
    leaf_l = attr_leaf.strip().lower()
    out: list[str] = []
    for t in types:
        if declared is not None:
            leaves = {str(x).strip().lower() for x in (declared.get(t) or ())}
            if leaf_l not in leaves:
                continue
        try:
            out.append(attr_uri(t, attr_leaf))
        except ValueError:
            continue
    return out


def bind_subclass_attribute(
    parent_type: str,
    attr_leaf: str,
    *,
    child_to_parent: Mapping[str, str] | None = None,
    ontology_summary: str = "",
    declared_by_type: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, object]:
    """Planner bind for a parent-type attribute ask (count/list/filter).

    ``type_names`` drives subclass-closure membership (how many people?).
    ``predicates`` is the SPARQL-era attr IRI list the NL layer must use
    instead of only ``types/<Parent>/attrs/<leaf>``. ``prop_key`` is the
    Neo4j leaf (property-graph Facts flatten the type segment).
    """
    types = type_names_with_subclasses(
        parent_type,
        ontology_summary=ontology_summary,
        child_to_parent=child_to_parent,
        include_subclasses=True,
    )
    predicates = subclass_attribute_predicates(
        parent_type,
        attr_leaf,
        child_to_parent=child_to_parent,
        ontology_summary=ontology_summary,
        declared_by_type=declared_by_type,
    )
    return {
        "type_names": types,
        "attr_leaf": attr_leaf,
        "predicates": predicates,
        "prop_key": attr_leaf,
    }
