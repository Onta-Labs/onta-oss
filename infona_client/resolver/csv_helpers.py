"""ADR 0003 v2 shape-checks + small CSV helper functions."""

from __future__ import annotations

import hashlib
import json
import re

from infona_client.graph.ontology_queries import (
    TEXT_KIND_FREE_TEXT,
    TEXT_KIND_NOT_TEXT,
    _safe_id,
)
from infona_client.graph.text_markers import TextCandidacy, classify_text_candidacy

_TEXT_KIND_UNSET = object()
from infona_client.resolver.models import (
    ColumnMapping,
    ColumnProfile,
    CoreSlot,
    CoreSlotTests,
    CSVSchemaMapping,
    DatasetConstant,
    OntologyExtensions,
    RejectedSlot,
    SchemaViolation,
    TypeExtension,
    ValueShape,
)

def _rank_sample_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Sort by descending non-empty field count; stable on ties (preserves
    original order). Used so the LLM gets the most informative rows when the
    head of the CSV is sparse (deleted/empty records). Does not mutate input."""
    def score(row: dict) -> int:
        return sum(
            1 for v in row.values()
            if v is not None and (not isinstance(v, str) or v.strip() != "")
        )
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda t: (-score(t[1]), t[0]))
    return [r for _, r in indexed]


# --- ADR 0003 v2 helpers -----------------------------------------------------


def _strip_code_fences(text: str) -> str:
    """LLMs sometimes wrap JSON in markdown fences despite 'JSON only'."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
        stripped = "\n".join(lines)
    return stripped


def _check_reason_shape(data: dict) -> None:
    """Reject degenerate Pass B (or corrected Pass C) output by raising
    KeyError, which feeds the existing retry-then-422 contract. Only the
    load-bearing structure is enforced; optional fields (why, confidence,
    relationships) may be absent."""
    if not isinstance(data, dict):
        raise KeyError("schema must be a JSON object")
    entities = data.get("entities")
    if not isinstance(entities, list) or not entities:
        raise KeyError("entities")
    for ent in entities:
        if not isinstance(ent, dict) or not ent.get("type_name"):
            raise KeyError("entities[].type_name")
    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        raise KeyError("columns")
    for col in columns:
        if not isinstance(col, dict) or not col.get("column"):
            raise KeyError("columns[].column")


def _check_refute_shape(data: dict, proposed: dict) -> tuple[list[dict], dict]:
    """Validate Pass C output; returns ``(violations, corrected)``.

    The reviewer must echo the schema when nothing is wrong — but a model
    that returns ``violations: []`` without the echo is repaired (the
    proposed schema stands). Violations without a corrected schema are
    degenerate output → KeyError → retry."""
    if not isinstance(data, dict):
        raise KeyError("refute output must be a JSON object")
    violations = data.get("violations")
    if not isinstance(violations, list):
        raise KeyError("violations")
    violations = [v for v in violations if isinstance(v, dict)]
    corrected = data.get("corrected")
    if not isinstance(corrected, dict) or not corrected:
        if violations:
            raise KeyError("corrected")
        corrected = proposed
    _check_reason_shape(corrected)
    return violations, corrected


#: Below this confidence a completion item is held for client-side review
#: (COG-52 wiring note 4); ALL promotions are held regardless of confidence.
COMPLETION_REVIEW_THRESHOLD = 0.7


def _check_complete_shape(data: dict) -> OntologyExtensions:
    """Validate Pass D (COMPLETE) output and convert it to
    :class:`OntologyExtensions`, computing ``held_for_review`` flags.

    Degenerate output raises ``KeyError`` (feeding the retry-then-422
    contract); a type with more than 3 core slots raises pydantic
    ``ValidationError`` from the ``max_length=3`` cap — the boundedness rule
    is enforced structurally, not just prompted. An empty ``types`` list is
    accepted (nothing to extend), but the key itself must be present.

    Held-for-review marking (client-side confirm gate — see the model
    docstrings): every promotion is held; a type or slot with confidence
    below :data:`COMPLETION_REVIEW_THRESHOLD` is held; a dataset constant
    without a usable confidence is held (the prompt mandates one).
    """
    if not isinstance(data, dict):
        raise KeyError("completion output must be a JSON object")
    raw_types = data.get("types")
    if not isinstance(raw_types, list):
        raise KeyError("types")

    parsed: list[TypeExtension] = []
    for t in raw_types:
        if not isinstance(t, dict):
            raise KeyError("types[] entries must be objects")
        type_name = t.get("type") or t.get("type_name")
        if not type_name:
            raise KeyError("types[].type")

        core_slots: list[CoreSlot] = []
        for s in t.get("core_slots") or []:
            if not isinstance(s, dict) or not s.get("name"):
                raise KeyError("core_slots[].name")
            constant: DatasetConstant | None = None
            dc = s.get("dataset_constant")
            if isinstance(dc, dict) and dc.get("value") is not None:
                constant = DatasetConstant(
                    value=str(dc["value"]),
                    confidence=_as_confidence(dc.get("confidence")),
                )
            tests = s.get("tests")
            slot_confidence = _as_confidence(s.get("confidence"))
            held = (
                (slot_confidence is not None and slot_confidence < COMPLETION_REVIEW_THRESHOLD)
                or (constant is not None and (
                    constant.confidence is None
                    or constant.confidence < COMPLETION_REVIEW_THRESHOLD
                ))
            )
            kind = str(s.get("kind") or "attribute").strip().lower()
            core_slots.append(CoreSlot(
                name=str(s["name"]),
                kind="relationship" if kind == "relationship" else "attribute",
                target_type=s.get("target_type") or None,
                why=s.get("why"),
                tests=CoreSlotTests(
                    existence=bool(tests.get("existence")),
                    identity=bool(tests.get("identity")),
                    universality=bool(tests.get("universality")),
                ) if isinstance(tests, dict) else None,
                dataset_constant=constant,
                confidence=slot_confidence,
                held_for_review=held,
            ))

        rejected = [
            RejectedSlot(
                name=str(r.get("name")),
                failed_test=str(r.get("failed_test") or ""),
                why=r.get("why"),
            )
            for r in (t.get("rejected") or [])
            if isinstance(r, dict) and r.get("name")
        ]

        promoted = t.get("promoted_from_attribute") or None
        type_confidence = _as_confidence(t.get("confidence"))
        parsed.append(TypeExtension(
            type_name=str(type_name),
            promoted_from_attribute=promoted,
            core_slots=core_slots,  # >3 → ValidationError (boundedness cap)
            rejected=rejected,
            confidence=type_confidence,
            held_for_review=bool(promoted) or (
                type_confidence is not None
                and type_confidence < COMPLETION_REVIEW_THRESHOLD
            ),
        ))
    return OntologyExtensions(types=parsed)


#: Sentinel for "the REASON/COLUMN-ASSIGN output carried NO text_kind field at
#: all" — distinguishable from an explicit ``null``. Explicit null is a genuine
#: adjudicated NO (the prompt mandates it for structured strings) and persists
#: as the durable ``not_text`` marker (ONTA-173); an absent field means the
#: model never adjudicated the column (old recordings that predate ONTA-177,
#: or columns backfilled deterministically by the wide-table coverage repair)
#: and MUST stay undecided.
_TEXT_KIND_UNSET = object()


def _decide_text_kind(
    candidacy: TextCandidacy | None, llm_verdict: object,
) -> str | None:
    """Combine the name-blind candidacy proposal with the REASON pass's
    name-informed adjudication (ONTA-177 — "profiler proposes, LLM adjudicates";
    ONTA-173 — a decided NO persists durably):

    - ``FREE_TEXT`` (unambiguously long prose) → marked deterministically; the
      LLM output is irrelevant, so old recordings without the field still mark.
    - ``AMBIGUOUS`` → ``"free_text"`` iff the REASON pass emitted it for the
      column (the ONE place the column NAME is consulted — ADR 0003 keeps
      names out of every deterministic layer); ``"not_text"`` when the pass
      EXPLICITLY declined (the field is present but not ``"free_text"``, e.g.
      an explicit null) — the schema pass genuinely decided, and an
      unpersisted NO would be re-sampled by the reconciler forever and could
      later be overruled by the name-blind auto tier; ``None`` (undecided)
      when the field is absent (``_TEXT_KIND_UNSET``: old recordings,
      backfilled columns the model never saw).
    - ``NOT_CANDIDATE`` / unknown column → never marked (neither polarity),
      even when the LLM volunteered the field: candidacy authority stays with
      the value-shape evidence, so a hallucinated ``text_kind`` on a
      code/number column is discarded rather than written into the ontology,
      and non-candidates stay unmarked (the reconciler's cheap heuristic
      re-classifies them itself).
    """
    if candidacy is TextCandidacy.FREE_TEXT:
        return TEXT_KIND_FREE_TEXT
    if candidacy is TextCandidacy.AMBIGUOUS:
        if llm_verdict == TEXT_KIND_FREE_TEXT:
            return TEXT_KIND_FREE_TEXT
        if llm_verdict is not _TEXT_KIND_UNSET:
            return TEXT_KIND_NOT_TEXT
    return None


def _as_violation(v: dict) -> SchemaViolation:
    """Lenient parse of one refute violation entry."""
    return SchemaViolation(
        template=str(v.get("template") or ""),
        location=str(v.get("location") or ""),
        evidence=str(v.get("evidence") or ""),
        severity=str(v.get("severity") or "warning"),
    )


def _as_confidence(value) -> float | None:
    """Coerce a model-emitted confidence to a clamped float (None if junk)."""
    try:
        if value is None:
            return None
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def _is_int(v: str) -> bool:
    try:
        int(v)
    except ValueError:
        return False
    return True


def _datatype_from_profile(col: ColumnProfile | None) -> str:
    """Deterministic datatype from Pass A value-shape evidence. The v2 schema
    carries no datatype field — the profile already measured the values, so
    nothing is gained by asking the LLM to re-guess. Purely structural checks
    (no column-name inspection).

    Currency-wrapped values (``$12.50``) count as NUMBER via the profiler and
    become ``float`` (or ``integer`` when every bare lexical form is integral).
    Non-numeric free text stays string — we never invent money typing from
    column names alone.
    """
    if col is None:
        return "string"
    if col.value_shape == ValueShape.DATE:
        return "datetime"
    if col.value_shape == ValueShape.NUMBER:
        from infona_client.resolver.profiler import strip_money_wrappers

        def _example_is_int(e: str) -> bool:
            bare = strip_money_wrappers(e) or e
            return _is_int(bare)

        return (
            "integer"
            if col.examples and all(_example_is_int(e) for e in col.examples)
            else "float"
        )
    lowered = [e.lower() for e in col.examples]
    if lowered and all(e in ("true", "false") for e in lowered):
        return "boolean"
    if lowered and all(e.startswith(("http://", "https://")) for e in lowered):
        return "uri"
    return "string"


def _pascal_case(name: str) -> str:
    """Mechanical PascalCase of a snake_case handle (fallback target type for
    a relationship column whose target_type the model omitted)."""
    return "".join(p.capitalize() for p in _snake_case(name).split("_")) or "Entity"


def _is_opaque_identifier(value: str) -> bool:
    """True for machine-ish codes that must not pollute ``attrs/name``.

    Display names (``West``, ``Alice Chen``, ``Acme Corp``) return False so
    stubs stay NL-filterable when no dimension table ever arrives. Codes with
    digits or CODE-style dashed tokens (``R-WEST``, ``S-ACME``) return True so
    later dimension-table display names are the sole ``attrs/name`` value
    (dogfood S5 dual-name SUM inflation).
    """
    v = (value or "").strip()
    if not v or len(v) > 64:
        return True  # empty / pathological → do not mint a name
    # Digits almost always mean a code (C1001, O9001, ERP-1, SKU42).
    # Exception: spaced display labels with digits ("Room 101", "Windows 11").
    if any(ch.isdigit() for ch in v) and " " not in v:
        return True
    # Dashed/underscored uppercase codes without spaces: R-WEST, S-ACME.
    if ("-" in v or "_" in v) and " " not in v:
        parts = [p for p in re.split(r"[-_]", v) if p]
        if len(parts) >= 2 and all(p.isalnum() for p in parts):
            if all(p.isupper() for p in parts if p.isalpha()):
                return True
    return False


def _rel_values(raw_value) -> list[str]:
    """Split a relationship cell into one or more target labels (JSON array,
    pipe-delimited, or short comma-delimited). Mirrors the legacy single-entity
    splitting so multi-entity and legacy paths behave identically."""
    if isinstance(raw_value, list):
        return [v.strip() for v in raw_value if isinstance(v, str) and v.strip()]
    raw_value = str(raw_value)
    if "|" in raw_value:
        return [v.strip() for v in raw_value.split("|") if v.strip()]
    if ", " in raw_value:
        parts = [v.strip() for v in raw_value.split(", ") if v.strip()]
        if all(len(p) < 30 for p in parts) and len(parts) >= 2:
            return parts
    return [raw_value.strip()] if raw_value.strip() else []


def _cell(row: dict, column: str) -> str:
    """One cell as a stripped string ('' when missing/empty). Non-string
    values (typed JSON cells) are stringified deterministically."""
    raw = row.get(column, "")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    return raw.strip()


def _synthetic_key(type_name: str, owned_values: dict[str, str]) -> str:
    """Deterministic content-hash key for a row whose natural key resolves
    empty (ADR 0003 §2 — row conservation). Depends only on the entity type
    and the row's owned non-empty column values — never on batch position,
    row index, or anything random — so identical rows collapse into one
    entity (true duplicates) and batched / re-run ingest stays idempotent.
    """
    material = type_name + "|" + "|".join(
        sorted(f"{col}={val}" for col, val in owned_values.items())
    )
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
    return _safe_id(digest)


def _snake_case(name: str) -> str:
    """snake_case a property / column handle.

    Splits camelCase / PascalCase before lowercasing so ``manufacturedBy``
    becomes ``manufactured_by`` (not the underscore-less ``manufacturedby``
    that landed on the Oliver label-compliance Drug type).
    """
    s = (name or "").strip()
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"[^a-zA-Z0-9]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s or "unnamed"


