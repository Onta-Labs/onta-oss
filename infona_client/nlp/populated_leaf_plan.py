"""Honest populated-leaf planning: Assertion SoT, empty aliases, enum > category.

Pure /ask helpers (always-LLM). Empty aliases must not look live. Kind filters
prefer a populated typed enum on the asked type over unstructured related
``category``. No industry vocabulary.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

NO_INSTANCES_MARK = "[no instances]"

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_HEADER_RE = re.compile(r"(?im)^Type:\s*([A-Za-z_][A-Za-z0-9_]*)")
_DASH_LITERAL_RE = re.compile(r"(?im)^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(?P<body>.*)$")
_DOT_PROP_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")
_BRACKET_PROP_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"
)

# Not ontology aliases — never remap.
_STRUCTURAL_SKIP = frozenset(
    "id name display_name primary_type tenant_id kg literal_value subject_id "
    "object_id source_url verified_at confidence run_id kind datatype labels "
    "source label description updated_at created_at uri iri attr".split()
)
_DATETIME_TOKENS = frozenset(
    "date time datetime timestamp start end date_end date_start start_date "
    "end_date started_at ended_at".split()
)
_ENUM_DATATYPES = frozenset({"enum", "select"})
_ENUM_SUFFIXES = ("_type", "_kind", "_status", "_class", "_role")
_UNSTRUCTURED_CATEGORY = frozenset({"category", "categories"})


def _norm(name: str) -> str:
    return (name or "").strip()


def _low(name: str) -> str:
    return _norm(name).lower()


def _is_ident(name: str) -> bool:
    return bool(name) and bool(_SAFE_IDENT_RE.match(name))


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    v = getattr(obj, key, None)
    if v is None and isinstance(obj, dict):
        v = obj.get(key)
    return default if v is None else v


def _is_datetime_family(name: str) -> bool:
    n = _low(name)
    return bool(n) and (
        n in _DATETIME_TOKENS
        or n.endswith(("_at", "_date", "_time", "date_end", "date_start"))
    )


def split_ontology_type_sections(ontology_summary: str) -> dict[str, str]:
    text = ontology_summary or ""
    headers = list(_TYPE_HEADER_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        out[m.group(1)] = text[m.start() : end]
    return out


def classify_literals_in_section(section: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(populated, empty)`` dash-form literal leaves; skip relationships."""
    populated: list[str] = []
    empty: list[str] = []
    seen: set[str] = set()
    for m in _DASH_LITERAL_RE.finditer(section or ""):
        leaf, body = m.group(1), m.group("body") or ""
        if not _is_ident(leaf):
            continue
        if "->" in body or "relationship" in body.lower():
            continue
        key = leaf.lower()
        if key in seen or key in _STRUCTURAL_SKIP:
            continue
        seen.add(key)
        (empty if NO_INSTANCES_MARK in body else populated).append(leaf)
    return tuple(populated), tuple(empty)


def classify_literals_by_type(
    ontology_summary: str,
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    return {
        name: classify_literals_in_section(section)
        for name, section in split_ontology_type_sections(ontology_summary).items()
    }


def classify_literals_from_slots(
    slots: Sequence[Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    populated: list[str] = []
    empty: list[str] = []
    seen: set[str] = set()
    for s in slots or ():
        name = _norm(_attr(s, "name", "") or "")
        if not _is_ident(name) or name.lower() in _STRUCTURAL_SKIP:
            continue
        kind = _low(_attr(s, "kind", "literal") or "literal")
        if kind == "relationship" or _attr(s, "range_type"):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        (populated if _attr(s, "populated") else empty).append(name)
    return tuple(populated), tuple(empty)


def populated_sibling_leaf(
    requested: str,
    *,
    populated: Sequence[str],
    empty: Sequence[str] = (),
    type_hint: str | None = None,
) -> str | None:
    """Suffix ``event_title`` for empty ``title``; datetime family for ``date_end``."""
    req = _norm(requested)
    if not _is_ident(req) or req.lower() in _STRUCTURAL_SKIP:
        return None
    req_l = req.lower()
    pop = [_norm(p) for p in populated if _is_ident(_norm(p))]
    if req_l in {p.lower() for p in pop}:
        return None
    suffix = [p for p in pop if p.lower().endswith("_" + req_l)]
    dt = (
        [p for p in pop if _is_datetime_family(p) and p.lower() != req_l]
        if _is_datetime_family(req)
        else []
    )
    hits = list(dict.fromkeys(suffix + dt))
    if not hits:
        return None
    hint = _low(type_hint or "")
    if hint and len(hits) > 1:
        prefixed = [h for h in hits if h.lower().startswith(hint)]
        if prefixed:
            hits = prefixed
    if len(hits) == 1:
        return hits[0]
    hits.sort(key=lambda h: (len(h), h.lower()))
    return hits[0]


def extract_dot_props(cypher: str) -> tuple[tuple[str, str], ...]:
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rx in (_DOT_PROP_RE, _BRACKET_PROP_RE):
        for m in rx.finditer(cypher or ""):
            var, prop = m.group(1), m.group(2)
            if not _is_ident(var) or not _is_ident(prop):
                continue
            key = (var, prop.lower())
            if key in seen:
                continue
            seen.add(key)
            ordered.append((var, prop))
    return tuple(ordered)


def _type_names_from_params(params: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not params:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for key in ("type_names", "from_types", "to_types"):
        raw = params.get(key)
        items: Sequence[Any]
        if isinstance(raw, str):
            items = (raw,)
        elif isinstance(raw, (list, tuple)):
            items = raw
        else:
            continue
        for item in items:
            s = str(item).strip() if item is not None else ""
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return tuple(out)


def empty_entity_cache_hits(
    cypher: str,
    ontology_summary: str,
    *,
    params: Mapping[str, Any] | None = None,
    slots_by_type: Mapping[str, Sequence[Any]] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Hits are ``(var, empty_leaf, suggested_sibling)``."""
    if slots_by_type:
        by_type = {tn: classify_literals_from_slots(slots) for tn, slots in slots_by_type.items()}
    else:
        by_type = classify_literals_by_type(ontology_summary)
    if not by_type:
        return ()
    asked = _type_names_from_params(params)
    scope = [t for t in asked if t in by_type] or list(by_type)
    hits: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for var, prop in extract_dot_props(cypher):
        if prop.lower() in _STRUCTURAL_SKIP or (var, prop.lower()) in seen:
            continue
        for tn in scope:
            populated, empty = by_type.get(tn, ((), ()))
            if prop.lower() in {p.lower() for p in populated}:
                continue
            empty_l = {e.lower() for e in empty}
            suffix = any(p.lower().endswith("_" + prop.lower()) for p in populated)
            dt = _is_datetime_family(prop) and any(_is_datetime_family(p) for p in populated)
            if prop.lower() not in empty_l and not suffix and not dt:
                continue
            sib = populated_sibling_leaf(
                prop, populated=populated, empty=empty, type_hint=tn
            )
            if not sib:
                continue
            seen.add((var, prop.lower()))
            hits.append((var, prop, sib))
            break
    return tuple(hits)


def empty_cache_schema_reason(hits: Sequence[tuple[str, str, str]]) -> str:
    if not hits:
        return ""
    parts = [
        f"{empty_leaf!r} has no instances in this KG (empty Entity cache / "
        f"empty alias); project {sib!r} via Assertion.literal_value "
        f"(p.name = {sib!r}) or the populated cache, not Entity.{empty_leaf}"
        for _var, empty_leaf, sib in hits
    ]
    return (
        "empty Entity cache projection: "
        + "; ".join(parts)
        + ". Literal source of truth is Assertion.literal_value on a populated leaf."
    )


def remap_empty_cache_projections(
    cypher: str,
    ontology_summary: str = "",
    *,
    params: Mapping[str, Any] | None = None,
    slots_by_type: Mapping[str, Sequence[Any]] | None = None,
    populated: Sequence[str] | None = None,
    empty: Sequence[str] | None = None,
    type_hint: str | None = None,
) -> str:
    """Rewrite ``var.empty_leaf`` → ``var.populated_sibling`` (identifier only)."""
    text = cypher or ""
    if not text:
        return text
    if populated is not None:
        hits = tuple(
            (var, prop, sib)
            for var, prop in extract_dot_props(text)
            if (
                sib := populated_sibling_leaf(
                    prop, populated=populated, empty=empty or (), type_hint=type_hint
                )
            )
        )
    else:
        hits = empty_entity_cache_hits(
            text, ontology_summary, params=params, slots_by_type=slots_by_type
        )
    out = text
    for var, empty_leaf, sib in hits:
        if not _is_ident(empty_leaf) or not _is_ident(sib) or empty_leaf == sib:
            continue
        out = re.sub(
            rf"\b({re.escape(var)})\s*\.\s*{re.escape(empty_leaf)}\b", rf"\1.{sib}", out
        )
        out = re.sub(
            rf"\b({re.escape(var)})\s*\[\s*['\"]{re.escape(empty_leaf)}['\"]\s*\]",
            rf"\1.{sib}",
            out,
        )
    return out


def _slot_name(slot: Any) -> str:
    return _norm(_attr(slot, "name", "") or "")


def _slot_kind(slot: Any) -> str:
    kind = _low(_attr(slot, "kind", "literal") or "literal")
    if kind == "relationship" or _attr(slot, "range_type"):
        return "relationship"
    return "literal"


def looks_typed_enum(slot: Any) -> bool:
    """Populated enum/select or ``*_type``/``*_kind`` leaf. No domain vocab."""
    if _slot_kind(slot) != "literal" or not _attr(slot, "populated"):
        return False
    if _low(_attr(slot, "datatype", "") or "") in _ENUM_DATATYPES:
        return True
    vals = _attr(slot, "enum_values") or ()
    if vals:
        return True
    return any(_slot_name(slot).lower().endswith(sfx) for sfx in _ENUM_SUFFIXES)


def looks_unstructured_category(slot: Any) -> bool:
    if _slot_kind(slot) != "literal":
        return False
    if _slot_name(slot).lower() not in _UNSTRUCTURED_CATEGORY:
        return False
    if _low(_attr(slot, "datatype", "") or "") in _ENUM_DATATYPES:
        return False
    return not _attr(slot, "enum_values")


def prefer_kind_filter_leaf(
    asked_types: Sequence[str],
    slots_by_type: Mapping[str, Sequence[Any]],
) -> str | None:
    """Populated typed enum on the asked type, else None."""
    for tn in (t for t in asked_types if t):
        for slot in slots_by_type.get(tn) or ():
            if looks_typed_enum(slot):
                return f"{tn}.{_slot_name(slot)}"
    return None


def related_unstructured_category_leaves(
    asked_types: Sequence[str],
    slots_by_type: Mapping[str, Sequence[Any]],
) -> tuple[str, ...]:
    asked = {t for t in asked_types if t}
    return tuple(
        f"{tn}.{_slot_name(slot)}"
        for tn, slots in (slots_by_type or {}).items()
        if tn not in asked
        for slot in slots or ()
        if looks_unstructured_category(slot) and _attr(slot, "populated")
    )


def format_leaf_grounding_notes(
    planning_types: Sequence[Any],
    *,
    asked_types: Sequence[str] = (),
) -> str:
    slots_by_type: dict[str, tuple[Any, ...]] = {}
    for t in planning_types or ():
        name = _norm(_attr(t, "name", "") or "")
        if not name:
            continue
        slots = _attr(t, "slots") or ()
        slots_by_type[name] = tuple(slots)
    lines = [
        "Literal source of truth: bind Assertion.literal_value with p.name set to a "
        "populated (unmarked) leaf. Entity denorm cache is only id / name / "
        "primary_type / tenant_id / kg — never assume Entity.title or Entity.date_end "
        "unless that exact key is unmarked as populated.",
    ]
    alias_notes = []
    for tn, slots in slots_by_type.items():
        populated, empty = classify_literals_from_slots(slots)
        for emp in empty:
            sib = populated_sibling_leaf(
                emp, populated=populated, empty=empty, type_hint=tn
            )
            if sib:
                alias_notes.append(
                    f"{tn}.{emp} {NO_INSTANCES_MARK} — use {tn}.{sib} (populated), "
                    "not the empty alias"
                )
    if alias_notes:
        lines.append("Empty aliases (do not project these Entity caches):")
        lines.extend(f"  - {n}" for n in alias_notes[:16])
    asked = list(asked_types) or list(slots_by_type)
    enum_leaf = prefer_kind_filter_leaf(asked, slots_by_type)
    cats = related_unstructured_category_leaves(asked, slots_by_type)
    if enum_leaf and cats:
        lines.append(
            "Kind/class filters: prefer the populated typed enum/select "
            f"{enum_leaf} on the asked type over unstructured related-type "
            f"category ({', '.join(cats[:6])}). Bind exact stored enum values; "
            "do not invent category lists."
        )
    elif enum_leaf:
        lines.append(
            "Kind/class filters: prefer the populated typed enum/select "
            f"{enum_leaf} when the question asks what kind/type of the asked entity."
        )
    return "\n".join(lines)


__all__ = [
    "NO_INSTANCES_MARK",
    "classify_literals_by_type",
    "classify_literals_from_slots",
    "classify_literals_in_section",
    "empty_cache_schema_reason",
    "empty_entity_cache_hits",
    "extract_dot_props",
    "format_leaf_grounding_notes",
    "looks_typed_enum",
    "looks_unstructured_category",
    "populated_sibling_leaf",
    "prefer_kind_filter_leaf",
    "related_unstructured_category_leaves",
    "remap_empty_cache_projections",
    "split_ontology_type_sections",
]
