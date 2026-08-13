"""Post-gen schema predicate validation for free-form Cypher.

Persona round 9b3e3a / analytics-trust P0: free-form LLM Cypher invents
relationship labels and attribute keys that are **not** in the ontology
(e.g. ``HAS_OFFERED_IN`` when the real leaf is ``offered_in`` → Neo4j type
``OFFERED_IN`` via :func:`sanitize_rel_type`). The plan still "looks"
filtered, coverage sees filter tokens bound, and aggregates return **0 with
high confidence**.

This module is pure + hermetic:

1. Extract relationship / attribute **leaves** from ontology summary text
   (dash-literal catalog form + production ``Attributes:`` /
   ``Relationships:`` lines).
2. Parse free-form Cypher for typed rel patterns and property keys.
3. Reject plans that use non-schema hops / leaves (fail closed).

**Allowlisted ADR 0013 structural edges** (``INSTANCE_OF``, ``PREDICATE``,
``SUBJECT``, ``OBJECT``, ``SUBCLASS_OF``) always pass. Typed dual-write
shortcuts are valid **only** when ``sanitize_rel_type(leaf)`` (or the leaf
itself) matches a declared relationship leaf — never invent ``HAS_<LEAF>``
when the leaf is bare ``leaf``.

**Product rules:** always-LLM (regenerate, never fixture short-circuit);
fail closed over high-conf zeros; anti-overfit (synthetic leaves only in tests).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from infona_client.graph.facts import sanitize_rel_type
from infona_client.nlp.cypher_generate import (
    _ontology_section_for_type,
    _relationship_specs_in_section,
    extract_type_names_from_ontology,
)
from infona_client.nlp.numeric_attr_resolve import (
    _literal_leaves_from_section,
    normalize_leaf_key,
)

# ---------------------------------------------------------------------------
# Structural allowlists (ADR 0013 model edges / entity fields)
# ---------------------------------------------------------------------------

# Graph topology edges that are not ontology relationship leaves.
STRUCTURAL_REL_TYPES: frozenset[str] = frozenset(
    {
        "INSTANCE_OF",
        "SUBCLASS_OF",
        "SUBJECT",
        "OBJECT",
        "PREDICATE",
        # Catalog / provenance topology (when present).
        "SCOPED_TO",
        "PROV_EVENT",
        "HAS_PROVENANCE",
    }
)

# Entity / Assertion / Class fields that free-form may read without ontology.
STRUCTURAL_PROP_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "display_name",
        "title",
        "primary_type",
        "tenant_id",
        "kg",
        "literal_value",
        "subject_id",
        "object_id",
        "source_url",
        "verified_at",
        "confidence",
        "run_id",
        "attr",
        "kind",
        "datatype",
        "labels",
        "source",
        "label",
        "description",
        "updated_at",
        "created_at",
        "embedding",
        "uri",
        "iri",
    }
)

# Templates whose bodies are known-good Assertion shapes (no free-form invent).
# Params (prop_key / rel_attr) are still schema-checked when present.
_KNOWN_TEMPLATE_BODIES = frozenset(
    {
        "literal_values",
        "literal_compare",
        "literal_aggregate",
        "related_entity_name_filter",
        "related_entity_name_filter_inverse",
        "related_entities",
        "entities_of_type",
        "entities_of_type_count",
        "entity_count_total",
        "entity_count_by_type",
        "assertions_for_subject",
        "subclass_of_closure",
    }
)

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Cypher typed relationship: -[:REL] / -[r:REL] / -[:`REL`] / optional * / props
_REL_TYPE_IN_PATTERN_RE = re.compile(
    r"\[\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*)?:\s*`?([A-Za-z_][A-Za-z0-9_]*)`?"
)

# p.name = 'leaf' / p.name = "leaf"
_P_NAME_LITERAL_RE = re.compile(
    r"(?i)\bp\.name\s*=\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
)

# Entity denorm / free prop access used as a filter: e.prop = / e.prop < …
# Variable is intentionally broad (e, a, n, …) but requires a compare op.
_ENTITY_PROP_COMPARE_RE = re.compile(
    r"(?ix)\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:=|<>|!=|<=|>=|<|>|=~|CONTAINS|STARTS\s+WITH|ENDS\s+WITH)"
)

# e['prop'] / e["prop"]
_ENTITY_BRACKET_LITERAL_RE = re.compile(
    r"(?ix)\b[A-Za-z_][A-Za-z0-9_]*\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"
)

# Production Relationships: line bodies
_RELATIONSHIPS_LINE_RE = re.compile(r"(?im)^\s*Relationships:\s*(.+)$")


# ---------------------------------------------------------------------------
# Ontology leaf inventory
# ---------------------------------------------------------------------------


def _leaves_from_relationships_line_body(body: str) -> list[str]:
    """Extract relationship leaf names from a ``Relationships: …`` line body.

    Accepts production shapes::

        offered_in → Term, has_phase → Phase
        offered_in -> Term (relationship, key=offered_in)
        offered_in — predicate URI: <…>
    """
    if not body or not body.strip():
        return []
    cleaned = body.strip()
    if re.match(r"(?i)^\(see\b", cleaned):
        return []
    # Drop [annotations]
    cleaned = re.sub(r"\[[^\]]*\]", "", cleaned)
    # Drop URI suffixes
    cleaned = re.sub(r"[—\-]\s*(?:predicate\s+)?URI:\s*<[^>]*>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bURI:\s*<[^>]*>", "", cleaned, flags=re.I)
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(leaf: str) -> None:
        if not leaf or not _SAFE_IDENT_RE.match(leaf):
            return
        key = leaf.lower()
        if key in seen or key in {
            "relationships",
            "none",
            "uri",
            "type",
            "attributes",
            "see",
            "related",
            "types",
        }:
            return
        seen.add(key)
        ordered.append(leaf)

    # Prefer key=leaf when present
    for m in re.finditer(r"\bkey\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", cleaned):
        _add(m.group(1))
    # Split on commas for "leaf → Range" / "leaf -> Range" fragments
    for part in re.split(r",", cleaned):
        frag = part.strip()
        if not frag:
            continue
        m = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:→|->|—|-)?",
            frag,
        )
        if m:
            _add(m.group(1))
    return ordered


def extract_relationship_leaves(ontology_summary: str) -> list[str]:
    """Ordered unique relationship leaves declared in ontology summary text."""
    text = ontology_summary or ""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(leaf: str) -> None:
        if not leaf or not _SAFE_IDENT_RE.match(leaf):
            return
        key = leaf.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(leaf)

    # Per-type dash / production arrow forms
    type_names = extract_type_names_from_ontology(text)
    if type_names:
        for tn in type_names:
            section = _ontology_section_for_type(tn, text)
            for leaf, _rng in _relationship_specs_in_section(section):
                _add(leaf)
            for m in _RELATIONSHIPS_LINE_RE.finditer(section):
                for leaf in _leaves_from_relationships_line_body(m.group(1)):
                    _add(leaf)
    else:
        for leaf, _rng in _relationship_specs_in_section(text):
            _add(leaf)

    for m in _RELATIONSHIPS_LINE_RE.finditer(text):
        for leaf in _leaves_from_relationships_line_body(m.group(1)):
            _add(leaf)
    return ordered


def extract_attribute_leaves(ontology_summary: str) -> list[str]:
    """Ordered unique literal / attribute leaves from ontology summary text."""
    text = ontology_summary or ""
    type_names = extract_type_names_from_ontology(text)
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(leaf: str) -> None:
        if not leaf or not _SAFE_IDENT_RE.match(leaf):
            return
        key = leaf.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(leaf)

    if type_names:
        for tn in type_names:
            section = _ontology_section_for_type(tn, text)
            for leaf in _literal_leaves_from_section(section):
                _add(leaf)
    else:
        for leaf in _literal_leaves_from_section(text):
            _add(leaf)
    return ordered


@dataclass(frozen=True)
class OntologyLeafInventory:
    """Schema inventory extracted from ontology summary text."""

    relationship_leaves: tuple[str, ...] = ()
    attribute_leaves: tuple[str, ...] = ()
    type_names: tuple[str, ...] = ()
    # Uppercase Neo4j dual-write tokens allowed for typed rel hops.
    allowed_rel_tokens: frozenset[str] = field(default_factory=frozenset)
    # Lowercased attribute + relationship leaves for prop-key checks.
    allowed_prop_keys: frozenset[str] = field(default_factory=frozenset)
    empty: bool = True

    @classmethod
    def from_ontology(cls, ontology_summary: str) -> OntologyLeafInventory:
        rels = extract_relationship_leaves(ontology_summary)
        attrs = extract_attribute_leaves(ontology_summary)
        types = extract_type_names_from_ontology(ontology_summary or "")
        tokens: set[str] = set(STRUCTURAL_REL_TYPES)
        prop_keys: set[str] = {k.lower() for k in STRUCTURAL_PROP_KEYS}
        for leaf in rels:
            prop_keys.add(leaf.lower())
            prop_keys.add(normalize_leaf_key(leaf))
            try:
                tokens.add(sanitize_rel_type(leaf))
            except Exception:  # noqa: BLE001 — bad leaf stays out of allow-set
                pass
            # Also accept the leaf as written uppercased (LLM sometimes copies
            # the leaf name into a type token without HAS_ invent).
            if _SAFE_IDENT_RE.match(leaf):
                tokens.add(leaf.upper())
        for leaf in attrs:
            prop_keys.add(leaf.lower())
            prop_keys.add(normalize_leaf_key(leaf))
        empty = not rels and not attrs and not types
        return cls(
            relationship_leaves=tuple(rels),
            attribute_leaves=tuple(attrs),
            type_names=tuple(types),
            allowed_rel_tokens=frozenset(tokens),
            allowed_prop_keys=frozenset(prop_keys),
            empty=empty,
        )


# ---------------------------------------------------------------------------
# Cypher parse
# ---------------------------------------------------------------------------


def extract_cypher_rel_types(cypher: str) -> list[str]:
    """Typed relationship labels used in MATCH / OPTIONAL MATCH patterns."""
    c = cypher or ""
    out: list[str] = []
    seen: set[str] = set()
    for m in _REL_TYPE_IN_PATTERN_RE.finditer(c):
        name = m.group(1)
        if not name:
            continue
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def extract_cypher_prop_keys(
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
) -> list[str]:
    """Property / predicate keys referenced in free-form Cypher + params.

    Collects ``p.name = '…'`` literals, entity compare / bracket props, and
    non-empty ``prop_key`` / ``rel_attr`` params. Structural keys are included
    so callers can filter; validation subtracts the structural allowlist.
    """
    c = cypher or ""
    params = params or {}
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(name: str | None) -> None:
        if not name or not isinstance(name, str):
            return
        name = name.strip()
        if not name or not _SAFE_IDENT_RE.match(name):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(name)

    for m in _P_NAME_LITERAL_RE.finditer(c):
        _add(m.group(1))
    for m in _ENTITY_PROP_COMPARE_RE.finditer(c):
        _add(m.group(1))
    for m in _ENTITY_BRACKET_LITERAL_RE.finditer(c):
        _add(m.group(1))

    for pk in ("prop_key", "rel_attr"):
        v = params.get(pk)
        if isinstance(v, str):
            _add(v)
    return ordered


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaValidResult:
    """Outcome of schema predicate validation."""

    ok: bool
    reason: str = ""
    invented_rel_types: tuple[str, ...] = ()
    invented_prop_keys: tuple[str, ...] = ()
    inventory: OntologyLeafInventory | None = None

    def to_timing(self) -> dict[str, float | str]:
        out: dict[str, float | str] = {}
        if not self.ok:
            out["schema_valid_cypher_fail"] = 1.0
            out["schema_valid_cypher_reason"] = (self.reason or "")[:500]
            if self.invented_rel_types:
                out["invented_rel_types"] = ", ".join(self.invented_rel_types)[:200]
            if self.invented_prop_keys:
                out["invented_prop_keys"] = ", ".join(self.invented_prop_keys)[:200]
        else:
            out["schema_valid_cypher_ok"] = 1.0
        return out


def _rel_token_allowed(token: str, inventory: OntologyLeafInventory) -> bool:
    t = (token or "").strip()
    if not t:
        return True
    upper = t.upper()
    if upper in STRUCTURAL_REL_TYPES:
        return True
    if upper in inventory.allowed_rel_tokens:
        return True
    # Leaf form as written (case-insensitive) against declared leaves.
    low = t.lower()
    for leaf in inventory.relationship_leaves:
        if leaf.lower() == low:
            return True
        try:
            if sanitize_rel_type(leaf) == upper:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _prop_key_allowed(key: str, inventory: OntologyLeafInventory) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    low = k.lower()
    if low in STRUCTURAL_PROP_KEYS or low in inventory.allowed_prop_keys:
        return True
    n = normalize_leaf_key(k)
    if n in inventory.allowed_prop_keys:
        return True
    return False


def check_schema_valid_cypher(
    cypher: str,
    ontology_summary: str,
    *,
    params: dict[str, Any] | None = None,
    template: str | None = None,
    inventory: OntologyLeafInventory | None = None,
) -> SchemaValidResult:
    """Return schema-validity of free-form Cypher against ontology leaves.

    ``ok=True`` when:

    * ontology inventory is empty (cannot validate — fail open so a missing
      ontology summary does not brick /ask), OR
    * every typed rel and non-structural prop key is schema-grounded.

    Known ADR 0013 **templates** still validate ``prop_key`` / ``rel_attr``
    params (a template name does not license inventing a leaf), but free-form
    pattern rel types are the primary invent class.
    """
    inv = inventory or OntologyLeafInventory.from_ontology(ontology_summary)
    if inv.empty:
        return SchemaValidResult(ok=True, reason="empty ontology inventory", inventory=inv)

    params = dict(params or {})
    tmpl = (template or "").strip()
    rels_used = extract_cypher_rel_types(cypher)
    props_used = extract_cypher_prop_keys(cypher, params=params)

    invented_rels = [r for r in rels_used if not _rel_token_allowed(r, inv)]
    invented_props = [p for p in props_used if not _prop_key_allowed(p, inv)]

    # Template bodies are Assertion-shaped; still reject invented params.
    if tmpl in _KNOWN_TEMPLATE_BODIES:
        # Only param-driven leaves matter for templates (bodies don't invent
        # HAS_* typed hops — RELATED_* use Assertion + p.name = $rel_attr).
        param_props: list[str] = []
        for pk in ("prop_key", "rel_attr"):
            v = params.get(pk)
            if isinstance(v, str) and v.strip() and _SAFE_IDENT_RE.match(v.strip()):
                if not _prop_key_allowed(v.strip(), inv):
                    param_props.append(v.strip())
        if param_props:
            return SchemaValidResult(
                ok=False,
                reason=(
                    f"template {tmpl} params reference non-schema leaf(s) "
                    f"{param_props!r}; only use attribute/relationship names "
                    "declared in the ontology schema"
                ),
                invented_prop_keys=tuple(param_props),
                inventory=inv,
            )
        # Free-form rel types on a mislabeled template body still checked.
        if invented_rels:
            return SchemaValidResult(
                ok=False,
                reason=(
                    "Cypher uses relationship type(s) not in the ontology: "
                    + ", ".join(invented_rels)
                    + ". Dual-write typed rels must match sanitize_rel_type(leaf) "
                    "for a declared relationship leaf (e.g. offered_in → OFFERED_IN, "
                    "not HAS_OFFERED_IN). Prefer Assertion SUBJECT/PREDICATE/OBJECT "
                    "with p.name = $rel_attr from the schema."
                ),
                invented_rel_types=tuple(invented_rels),
                inventory=inv,
            )
        return SchemaValidResult(ok=True, reason="template schema ok", inventory=inv)

    if invented_rels or invented_props:
        parts: list[str] = []
        if invented_rels:
            parts.append(
                "relationship type(s) not in ontology: " + ", ".join(invented_rels)
            )
        if invented_props:
            parts.append(
                "attribute/predicate key(s) not in ontology: " + ", ".join(invented_props)
            )
        return SchemaValidResult(
            ok=False,
            reason=(
                "schema-invalid free-form Cypher ("
                + "; ".join(parts)
                + "). Use only relationship and attribute leaves declared in the "
                "ontology (or ADR 0013 structural edges INSTANCE_OF / SUBJECT / "
                "OBJECT / PREDICATE). Typed dual-write rels are "
                "sanitize_rel_type(leaf) of a declared relationship — never invent "
                "HAS_<something> that is not a real leaf. Prefer templates "
                "related_entity_name_filter / literal_values / literal_compare "
                "with schema-grounded $rel_attr / $prop_key."
            ),
            invented_rel_types=tuple(invented_rels),
            invented_prop_keys=tuple(invented_props),
            inventory=inv,
        )

    return SchemaValidResult(ok=True, reason="schema predicates ok", inventory=inv)


def schema_valid_feedback(result: SchemaValidResult, *, previous_cypher: str = "") -> str:
    """Build LLM error_feedback for a schema-validity rejection."""
    parts = [
        "SCHEMA PREDICATE FAILURE (invented relationship/attribute risk):",
        result.reason or "Cypher uses leaves not declared in the ontology",
        "",
        "Rewrite rules (REQUIRED):",
        "1. ONLY use relationship and attribute names that appear in the "
        "ontology schema text provided.",
        "2. Typed dual-write relationship tokens are the UPPER_SNAKE form of a "
        "declared leaf via sanitize_rel_type (offered_in → OFFERED_IN). Do NOT "
        "invent HAS_OFFERED_IN / HAS_* when the leaf is not literally has_*.",
        "3. Prefer ADR 0013 templates: related_entity_name_filter with "
        "$rel_attr from the schema + $target_name; literal_values / "
        "literal_compare with $prop_key from Attributes.",
        "4. Assertion path is always valid: MATCH (a:Assertion)-[:SUBJECT]->(e) "
        "MATCH (a)-[:OBJECT]->(t) MATCH (a)-[:PREDICATE]->(p:Property) "
        "WHERE p.name = $rel_attr — p.name must be a declared leaf.",
        "5. Fail closed: honest empty / clarification beats a silent zero from "
        "an invented hop.",
    ]
    if result.inventory and result.inventory.relationship_leaves:
        parts.append(
            "Declared relationship leaves: "
            + ", ".join(result.inventory.relationship_leaves[:40])
        )
    if result.inventory and result.inventory.attribute_leaves:
        parts.append(
            "Declared attribute leaves (sample): "
            + ", ".join(result.inventory.attribute_leaves[:40])
        )
    if previous_cypher and previous_cypher.strip():
        parts.extend(["", f"Rejected query was:\n{previous_cypher.strip()}"])
    return "\n".join(parts)


def fail_closed_schema_answer(result: SchemaValidResult) -> str:
    """User-facing honest answer when schema validation fails after retries."""
    parts = [
        "Could not answer with confidence: the generated plan used "
        "relationship or attribute names that are not in the ontology schema, "
        "so executing it risked a silent empty/wrong result.",
    ]
    if result.invented_rel_types:
        parts.append(
            "Invented relationship type(s): "
            + ", ".join(f"'{t}'" for t in result.invented_rel_types)
            + "."
        )
    if result.invented_prop_keys:
        parts.append(
            "Invented attribute key(s): "
            + ", ".join(f"'{t}'" for t in result.invented_prop_keys)
            + "."
        )
    if result.inventory and result.inventory.relationship_leaves:
        parts.append(
            "Declared relationships include: "
            + ", ".join(result.inventory.relationship_leaves[:12])
            + "."
        )
    parts.append(
        "Prefer clarifying which schema field or relationship to use over "
        "returning a high-confidence zero from an invalid hop."
    )
    if result.reason:
        parts.append(f"Reason: {result.reason}")
    return " ".join(parts)


__all__ = [
    "OntologyLeafInventory",
    "STRUCTURAL_PROP_KEYS",
    "STRUCTURAL_REL_TYPES",
    "SchemaValidResult",
    "check_schema_valid_cypher",
    "extract_attribute_leaves",
    "extract_cypher_prop_keys",
    "extract_cypher_rel_types",
    "extract_relationship_leaves",
    "fail_closed_schema_answer",
    "schema_valid_feedback",
]
