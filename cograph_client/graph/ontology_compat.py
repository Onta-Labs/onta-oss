"""Backward-compatibility classifier + publish gate (ONTA-404).

Backward compatibility is a **publish gate over a typed diff**
(:class:`~cograph_client.models.ontology.ChangeRecord` from ONTA-406). The
classifier is pure (no I/O); the gate is enforced when materializing a
``kind="release"`` snapshot via
:func:`~cograph_client.graph.ontology_snapshots.execute_snapshot`.

Vocabulary
----------
:class:`CompatClass` is the stored / overall class:

* ``ANNOTATIVE`` — comments, text-kind, non-structural core-slot → semver **patch**
* ``ADDITIVE`` — new type/attr/rel/subclass, ``RENAME_WITH_ALIAS`` → **minor**
* ``DEPRECATING`` — ``DEPRECATE`` with optional ``superseded_by`` → **minor**
* ``BREAKING`` — removes, range changes, rename-without-alias, unsafe re-parent → **major**

Open rulings (ONTA-404)
-----------------------
* **Widening** a datatype or relationship range is **breaking** (conservative:
  safe for readers but breaks writers who emit the wider set). Narrowing is also
  breaking. Only an equal range is non-breaking for ``CHANGE_RANGE``.
* **Re-parent to an ancestor** of the current parent is **not** breaking (stays
  within the ancestor chain). Re-parent to a sibling / unrelated type is
  **breaking**.
* **Adversarial rename** (``REMOVE_TYPE`` + ``ADD_TYPE`` under a new name in one
  release) is **breaking**. Only explicit ``RENAME_WITH_ALIAS`` is non-breaking.

The gate **refuses** a breaking release unless the caller sets
``declare_major=True``. A quiet bypass is not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from cograph_client.graph.ontology_commit import OntologyShape
from cograph_client.models.ontology import ChangeKind, ChangeRecord

# Literal / XSD-ish primitives used by the datatype lattice. Anything else is
# treated as a relationship range (bare type name) — same convention as
# ontology_snapshots._LITERAL_DATATYPES.
_LITERAL_DATATYPES = frozenset({
    "string", "integer", "float", "boolean", "datetime", "uri", "geo",
    "double", "date", "decimal", "long", "int", "number", "anyURI",
    "dateTime", "time",
})

# Widening lattice rank for *messaging* only. Non-equal ranges are always
# BREAKING (both directions); rank only distinguishes "widened" vs "narrowed"
# in human-readable summaries.
_DT_RANK: dict[str, int] = {
    "boolean": 10,
    "integer": 20,
    "int": 20,
    "long": 20,
    "float": 30,
    "double": 30,
    "decimal": 30,
    "number": 30,
    "date": 40,
    "datetime": 40,
    "dateTime": 40,
    "time": 40,
    "uri": 50,
    "anyURI": 50,
    "geo": 50,
    "string": 60,
}


class CompatClass(str, Enum):
    """Overall / per-record backward-compatibility class (ONTA-404)."""

    ANNOTATIVE = "annotative"
    ADDITIVE = "additive"
    DEPRECATING = "deprecating"
    BREAKING = "breaking"


# Severity for overall = worst-of. Deprecating ranks above additive so a release
# that both adds and deprecates reports ``deprecating`` (both map to minor).
_SEVERITY: dict[CompatClass, int] = {
    CompatClass.ANNOTATIVE: 0,
    CompatClass.ADDITIVE: 1,
    CompatClass.DEPRECATING: 2,
    CompatClass.BREAKING: 3,
}

_SEMVER: dict[CompatClass, str] = {
    CompatClass.ANNOTATIVE: "patch",
    CompatClass.ADDITIVE: "minor",
    CompatClass.DEPRECATING: "minor",
    CompatClass.BREAKING: "major",
}


@dataclass(frozen=True)
class ClassifiedChange:
    """One :class:`ChangeRecord` with its classified :class:`CompatClass`."""

    record: ChangeRecord
    compat_class: CompatClass
    note: str = ""


@dataclass(frozen=True)
class CompatVerdict:
    """Aggregate classification of a typed ontology diff."""

    overall: CompatClass
    requires_major: bool
    classified: tuple[ClassifiedChange, ...] = ()
    summary: tuple[str, ...] = ()

    @property
    def semver_bump(self) -> str:
        """``patch`` / ``minor`` / ``major`` for the overall class."""
        return _SEMVER[self.overall]

    @property
    def stored_compat_class(self) -> str:
        """Value written onto the release record's ``compatClass`` predicate."""
        return self.overall.value


class OntologyCompatError(Exception):
    """Raised when a release is breaking and ``declare_major`` was not set.

    Message is HTTP 409-ready: stable, human-readable, includes the verdict
    summary so a client can surface why publish was refused.
    """

    def __init__(self, verdict: CompatVerdict, message: str | None = None):
        self.verdict = verdict
        super().__init__(message or self._default_message(verdict))

    @staticmethod
    def _default_message(verdict: CompatVerdict) -> str:
        bits = list(verdict.summary[:8])
        tail = "; ".join(bits) if bits else "(no detail)"
        if len(verdict.summary) > 8:
            tail += f"; …(+{len(verdict.summary) - 8} more)"
        return (
            f"ontology release is {verdict.overall.value} and requires "
            f"declare_major=True to publish (semver {verdict.semver_bump}). "
            f"Changes: {tail}"
        )


# ---------------------------------------------------------------------------
# Ancestry + range helpers
# ---------------------------------------------------------------------------


def ancestors_of(shape: OntologyShape | None, type_name: str | None) -> set[str]:
    """Walk ``parent_of`` upward from ``type_name`` (exclusive of self)."""
    if not shape or not type_name:
        return set()
    out: set[str] = set()
    seen: set[str] = set()
    cur: str | None = shape.parent_of.get(type_name)
    while cur and cur not in seen:
        out.add(cur)
        seen.add(cur)
        cur = shape.parent_of.get(cur)
    return out


def is_ancestor(
    shape: OntologyShape | None,
    *,
    of: str | None,
    ancestor: str | None,
) -> bool:
    """True iff ``ancestor`` appears on the parent chain of ``of``."""
    if not of or not ancestor:
        return False
    if of == ancestor:
        return True
    return ancestor in ancestors_of(shape, of)


def _normalize_range(value: str | None) -> str:
    return (value or "").strip()


def _is_literal(dt: str) -> bool:
    return dt in _LITERAL_DATATYPES


def describe_range_change(old: str | None, new: str | None) -> str:
    """Human note for a non-equal CHANGE_RANGE (both directions are breaking)."""
    o, n = _normalize_range(old), _normalize_range(new)
    if not o or not n:
        return f"range {o or '?'} → {n or '?'} (non-equal → breaking)"
    if _is_literal(o) and _is_literal(n):
        ro, rn = _DT_RANK.get(o), _DT_RANK.get(n)
        if ro is not None and rn is not None and ro != rn:
            direction = "widened" if rn > ro else "narrowed"
            return f"datatype {direction} {o} → {n} (breaking)"
        return f"datatype changed {o} → {n} (breaking)"
    if not _is_literal(o) and not _is_literal(n):
        return f"relationship range {o} → {n} (breaking)"
    return f"range kind change {o} → {n} (literal↔node; breaking)"


# ---------------------------------------------------------------------------
# Per-record classification
# ---------------------------------------------------------------------------


def classify_change(
    record: ChangeRecord,
    *,
    parent_shape: OntologyShape | None = None,
) -> ClassifiedChange:
    """Classify one :class:`ChangeRecord`.

    ``parent_shape`` is only consulted for subclass / ancestry context when
    classifying a lone subclass edge; re-parent pairs are resolved in
    :func:`classify_diff`.
    """
    kind = record.kind

    if kind in (
        ChangeKind.ADD_TYPE,
        ChangeKind.ADD_ATTRIBUTE,
        ChangeKind.ADD_RELATIONSHIP,
        ChangeKind.ADD_SUBCLASS,
        ChangeKind.RENAME_WITH_ALIAS,
    ):
        note = _additive_note(record)
        return ClassifiedChange(record, CompatClass.ADDITIVE, note)

    if kind in (
        ChangeKind.CHANGE_COMMENT,
        ChangeKind.CHANGE_TEXT_KIND,
        ChangeKind.CHANGE_CORE_SLOT,
    ):
        return ClassifiedChange(
            record,
            CompatClass.ANNOTATIVE,
            f"{kind.value} on {_subject_label(record)}",
        )

    if kind is ChangeKind.DEPRECATE:
        sub = _subject_label(record)
        if record.superseded_by:
            note = f"deprecate {sub} superseded_by={record.superseded_by}"
        else:
            note = f"deprecate {sub}"
        return ClassifiedChange(record, CompatClass.DEPRECATING, note)

    if kind in (
        ChangeKind.REMOVE_TYPE,
        ChangeKind.REMOVE_ATTRIBUTE,
        ChangeKind.REMOVE_RELATIONSHIP,
        ChangeKind.REMOVE_SUBCLASS,
    ):
        return ClassifiedChange(
            record,
            CompatClass.BREAKING,
            f"{kind.value} {_subject_label(record)} (removal is breaking)",
        )

    if kind is ChangeKind.CHANGE_RANGE:
        old, new = _normalize_range(record.old_value), _normalize_range(record.new_value)
        if old == new:
            # Equal range should not appear in a real diff; treat as no-op annotative.
            return ClassifiedChange(
                record,
                CompatClass.ANNOTATIVE,
                f"CHANGE_RANGE equal {old or '(empty)'} (no-op)",
            )
        note = describe_range_change(old, new)
        return ClassifiedChange(record, CompatClass.BREAKING, note)

    # Unknown future kinds — fail closed.
    return ClassifiedChange(
        record,
        CompatClass.BREAKING,
        f"unknown ChangeKind {kind!r} (fail-closed → breaking)",
    )


def _subject_label(record: ChangeRecord) -> str:
    parts: list[str] = []
    if record.type_name:
        parts.append(record.type_name)
    if record.slot_name:
        parts.append(f".{record.slot_name}" if parts else record.slot_name)
    if record.parent_type and record.kind in (
        ChangeKind.ADD_SUBCLASS,
        ChangeKind.REMOVE_SUBCLASS,
    ):
        parts.append(f"⊏{record.parent_type}")
    if record.from_name or record.to_name:
        parts.append(f"{record.from_name or '?'}→{record.to_name or '?'}")
    return "".join(parts) if parts else "(unnamed)"


def _additive_note(record: ChangeRecord) -> str:
    if record.kind is ChangeKind.RENAME_WITH_ALIAS:
        return (
            f"rename_with_alias {record.from_name or '?'}→{record.to_name or '?'} "
            f"(explicit alias; non-breaking)"
        )
    return f"{record.kind.value} {_subject_label(record)}"


# ---------------------------------------------------------------------------
# Diff-level classification (re-parent pairing + overall)
# ---------------------------------------------------------------------------


def classify_diff(
    records: Sequence[ChangeRecord],
    *,
    parent_shape: OntologyShape | None = None,
    child_shape: OntologyShape | None = None,
) -> CompatVerdict:
    """Classify a full typed diff; overall = worst of the set.

    Empty diff → ``ADDITIVE`` / not requiring major (first release or no-op).

    ``REMOVE_SUBCLASS`` + ``ADD_SUBCLASS`` on the **same** ``type_name`` are
    treated as a re-parent event: re-parent to an ancestor of the old parent is
    non-breaking (annotative); any other re-parent is breaking.

    ``parent_shape`` / ``child_shape`` are optional ancestry context (pre/post
    shapes). Ancestry walks use ``parent_shape`` first, then ``child_shape``.
    """
    del child_shape  # reserved for future cardinality / post-state checks
    items = list(records)
    if not items:
        return CompatVerdict(
            overall=CompatClass.ADDITIVE,
            requires_major=False,
            classified=(),
            summary=("empty diff (additive / ok)",),
        )

    # Pair re-parent events before per-record classification so a safe
    # re-parent is not poisoned by the lone REMOVE_SUBCLASS → BREAKING rule.
    removes: dict[str, list[ChangeRecord]] = {}
    adds: dict[str, list[ChangeRecord]] = {}
    for r in items:
        if r.kind is ChangeKind.REMOVE_SUBCLASS and r.type_name:
            removes.setdefault(r.type_name, []).append(r)
        elif r.kind is ChangeKind.ADD_SUBCLASS and r.type_name:
            adds.setdefault(r.type_name, []).append(r)

    paired_ids: set[int] = set()
    classified: list[ClassifiedChange] = []

    for type_name in sorted(set(removes) & set(adds)):
        # Pair one remove with one add (diff emits at most one each per type).
        rem = removes[type_name][0]
        add = adds[type_name][0]
        paired_ids.add(id(rem))
        paired_ids.add(id(add))
        old_parent = rem.parent_type
        new_parent = add.parent_type
        if is_ancestor(parent_shape, of=old_parent, ancestor=new_parent):
            note = (
                f"re-parent {type_name}: {old_parent} → {new_parent} "
                f"(new parent is ancestor of old; non-breaking)"
            )
            # Emit a single synthetic classification on the ADD record; the
            # REMOVE is covered by the same note (paired, not double-counted).
            classified.append(
                ClassifiedChange(add, CompatClass.ANNOTATIVE, note)
            )
            classified.append(
                ClassifiedChange(rem, CompatClass.ANNOTATIVE, note)
            )
        else:
            note = (
                f"re-parent {type_name}: {old_parent} → {new_parent} "
                f"(outside ancestor chain; breaking)"
            )
            classified.append(ClassifiedChange(rem, CompatClass.BREAKING, note))
            classified.append(ClassifiedChange(add, CompatClass.BREAKING, note))

    for r in items:
        if id(r) in paired_ids:
            continue
        classified.append(classify_change(r, parent_shape=parent_shape))

    overall = CompatClass.ANNOTATIVE
    for c in classified:
        if _SEVERITY[c.compat_class] > _SEVERITY[overall]:
            overall = c.compat_class

    summary = tuple(c.note for c in classified if c.note)
    return CompatVerdict(
        overall=overall,
        requires_major=(overall is CompatClass.BREAKING),
        classified=tuple(classified),
        summary=summary,
    )


def assert_publishable(
    records: Sequence[ChangeRecord],
    *,
    declare_major: bool = False,
    parent_shape: OntologyShape | None = None,
    child_shape: OntologyShape | None = None,
) -> CompatVerdict:
    """Classify ``records`` and raise :class:`OntologyCompatError` if blocked.

    Used by the release publish path. Callers that only need classification
    without enforcement should call :func:`classify_diff` directly.
    """
    verdict = classify_diff(
        records, parent_shape=parent_shape, child_shape=child_shape,
    )
    if verdict.requires_major and not declare_major:
        raise OntologyCompatError(verdict)
    return verdict
