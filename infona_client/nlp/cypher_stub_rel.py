"""Made-by / hop-out hermetic Cypher fixtures."""

from __future__ import annotations

import re
from typing import Any

from infona_client.nlp.cypher_patterns import (
    HOP_OUT_CYPHER,
    TEMPLATE_HOP_OUT,
    TEMPLATE_RELATED_ENTITIES,
)
from infona_client.nlp.cypher_rel_resolve import _resolve_relationship_attr
from infona_client.nlp.cypher_patterns import (
    _HOP_OF_RE,
    _HOP_THEIR_RE,
    _MADE_BY_FILTER_RE,
    _VIA_REL_RE,
)
from infona_client.nlp.cypher_stub_basic import (
    _clamp_limit,
    _fixture,
    _strip_limit_suffix,
    _strip_order_by_suffix,
)
from infona_client.graph.rdfs_helpers import (
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
    type_names_with_subclasses,
)
from infona_client.nlp.cypher_patterns import TEMPLATE_RELATED_ENTITY_NAME_FILTER
from infona_client.nlp.cypher_types import (
    DEFAULT_LIST_LIMIT,
    _SAFE_PROP_RE,
    _TRAILING_PUNCT_RE,
    resolve_type_name,
)

def try_made_by_filter_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Filter subjects by a related party name (made by / written by / by X)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)
    m = _MADE_BY_FILTER_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    value = _TRAILING_PUNCT_RE.sub("", (m.group("value") or "").strip())
    value, lim_from_value = _strip_limit_suffix(value)
    if lim_from_value is not None:
        limit = lim_from_value
    if not value:
        return None
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None
    # Phrase → preferred leaves (makers / creators only — never has_genre).
    phrase = (m.group(0) or "").lower()
    if "written" in phrase or "authored" in phrase:
        candidates = ("has_author", "written_by", "authored_by")
    elif "published" in phrase:
        candidates = ("has_publisher", "published_by", "publisher")
    elif "sold" in phrase:
        candidates = ("sold_by", "has_seller", "seller")
    elif "made" in phrase or "manufactur" in phrase:
        candidates = (
            "made_by",
            "manufacturer",
            "has_manufacturer",
            "supplied_by",
            "has_supplier",
            "has_vendor",
            "vendor",
        )
    elif "suppl" in phrase or "vendor" in phrase:
        candidates = (
            "supplied_by",
            "has_supplier",
            "has_vendor",
            "vendor",
            "made_by",
        )
    else:
        # bare "by X" — prefer maker/author/supplier leaves present on this type
        candidates = (
            "made_by",
            "supplied_by",
            "has_supplier",
            "has_vendor",
            "has_author",
            "written_by",
            "sold_by",
            "published_by",
            "has_publisher",
        )
    text = ontology_summary or ""
    section = text
    if matched:
        m_sec = re.search(
            rf"(?ims)Type:\s*{re.escape(matched)}\b.*?(?=^Type:|\Z)",
            text,
        )
        if m_sec:
            section = m_sec.group(0)
    rel_attr: str | None = None
    for cand in candidates:
        if re.search(rf"(?i)\b{re.escape(cand)}\b", section):
            rel_attr = cand
            break
    if rel_attr is None:
        # Inverse: Organization.makes -> Product ("products made by Acme").
        inv_candidates = ("makes", "sells", "manufactures", "produces")
        inv_rel = None
        for cand in inv_candidates:
            if re.search(rf"(?i)\b{re.escape(cand)}\b.*relationship", text):
                inv_rel = cand
                break
        if inv_rel is None:
            return None
        expanded = type_names_with_subclasses(
            matched, ontology_summary=ontology_summary, include_subclasses=True
        )
        return _fixture(
            cypher=RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
            params={
                "type_names": expanded,
                "rel_attr": inv_rel,
                "target_name": value,
                "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
            },
            explanation=(
                f"Find {matched} entities that {inv_rel} from maker named "
                f"{value!r} (inverse related_entity_name_filter)."
            ),
            template="related_entity_name_filter_inverse",
        )
    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    # Literal attribute (common for free-text ingest): use equality filter.
    # Relationship edge: related_entity_name_filter.
    is_literal = bool(
        re.search(
            rf"(?i)-\s*{re.escape(rel_attr)}\s*:\s*\w+\s*\(literal",
            section,
        )
    )
    if is_literal:
        # CONTAINS so "Acme" matches "Acme Corp" free-text literals.
        lit_cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL AND toLower(toString(raw)) CONTAINS toLower($needle)
RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type,
       coalesce(e.title, e.name) AS title, raw AS value
ORDER BY e.id
LIMIT $limit
""".strip()
        return _fixture(
            cypher=lit_cypher,
            params={
                "type_names": expanded,
                "prop_key": rel_attr,
                "needle": value,
                "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
            },
            explanation=(
                f"Find {matched} entities where {rel_attr} contains {value!r}."
            ),
            template=None,
        )
    return _fixture(
        cypher=RELATED_ENTITY_NAME_FILTER_CYPHER,
        params={
            "type_names": expanded,
            "rel_attr": rel_attr,
            "target_name": value,
            "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
        },
        explanation=(
            f"Find {matched} entities related via {rel_attr} to "
            f"{value!r} via related_entity_name_filter."
        ),
        template=TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    )


def try_hop_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Simple 1-hop outbound traversal between two types (optional rel attr / LIMIT)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None

    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)

    rel_attr: str | None = None
    via = _VIA_REL_RE.search(q)
    if via:
        rel_attr = via.group("rel")
        q = q[: via.start()].strip()

    source_label: str | None = None
    target_label: str | None = None
    m = _HOP_THEIR_RE.match(q)
    if m:
        source_label = (m.group("source") or "").strip()
        target_label = (m.group("target") or "").strip()
    else:
        m = _HOP_OF_RE.match(q)
        if m:
            # "authors of books" → from Book (source) to Author (target)
            target_label = (m.group("target") or "").strip()
            source_label = (m.group("source") or "").strip()

    if not source_label or not target_label:
        return None

    # Source/target may still carry limit/order fragments from sloppy NL.
    source_label, lim_s = _strip_limit_suffix(source_label)
    target_label, lim_t = _strip_limit_suffix(target_label)
    if lim_s is not None:
        limit = lim_s
    if lim_t is not None:
        limit = lim_t
    source_label, _, _ = _strip_order_by_suffix(source_label)
    target_label, _, _ = _strip_order_by_suffix(target_label)

    from_type = resolve_type_name(source_label, type_names, ontology_summary)
    to_type = resolve_type_name(target_label, type_names, ontology_summary)
    if from_type is None or to_type is None:
        return None
    if from_type == to_type and rel_attr is None:
        # Ambiguous self-hop without a rel name — skip fixture.
        return None

    from_types = type_names_with_subclasses(
        from_type, ontology_summary=ontology_summary, include_subclasses=True
    )
    to_types = type_names_with_subclasses(
        to_type, ontology_summary=ontology_summary, include_subclasses=True
    )
    params: dict[str, Any] = {
        "from_types": from_types,
        "to_types": to_types,
        "rel_attr": rel_attr,
        "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
    }
    expl = f"1-hop relationships from {from_type} to {to_type} via related_entities"
    if rel_attr:
        expl += f" (attr={rel_attr})"
    return _fixture(
        cypher=HOP_OUT_CYPHER,
        params=params,
        explanation=expl + ".",
        template=TEMPLATE_HOP_OUT,
    )



