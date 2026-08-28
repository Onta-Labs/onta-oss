"""Semver + ``acquisition_revision`` signal table (INF-560 C4 / INF-563).

Semver answers only: does this break an installed consumer's *schema*?
Acquisition and freshness *instruction* changes are a different signal —
they increment ``acquisition_revision``, not a silent MINOR.
"""

from __future__ import annotations

from typing import Literal

ChangeClass = Literal["major", "minor", "patch", "acquisition_revision"]

#: Removing a concept, renaming, narrowing a range, flipping literal ↔
#: type-ranged, making an optional attribute required, or removing a source
#: binding breaks installed consumers.
SEMVER_MAJOR: frozenset[str] = frozenset(
    {
        "remove_concept",
        "rename_concept",
        "rename_identity_key",
        "narrow_range",
        "make_optional_required",
        "literal_to_type_ranged",
        "type_ranged_to_literal",
        "remove_source_binding",
    }
)

#: Additive. Existing installs keep working.
SEMVER_MINOR: frozenset[str] = frozenset(
    {
        "add_optional_attribute",
        "add_optional_concept",
        "add_source_binding",
        "add_skill",
        "add_function",
        "add_question",
        "add_eval",
        "change_er_threshold",
    }
)

#: Prose, fixtures, sample refresh, documentation. No change to what an
#: installed graph *is*.
SEMVER_PATCH: frozenset[str] = frozenset(
    {
        "wording",
        "eval_fixture",
        "sample_refresh",
        "documentation",
    }
)

#: Seed query, page cap, first-pull vs refresh, disappeared-row, conflict,
#: or a freshness window. Schema still matches; what the next pull contains
#: (and what "current" means) does not.
ACQUISITION_REVISION_CHANGES: frozenset[str] = frozenset(
    {
        "change_acquisition_instruction",
        "change_seed_query",
        "change_page_cap",
        "change_first_pull",
        "change_later_refresh",
        "change_disappeared_row",
        "change_conflict_instruction",
        "change_freshness_window",
    }
)

_TABLE: dict[str, ChangeClass] = {}
_TABLE.update(dict.fromkeys(SEMVER_MAJOR, "major"))
_TABLE.update(dict.fromkeys(SEMVER_MINOR, "minor"))
_TABLE.update(dict.fromkeys(SEMVER_PATCH, "patch"))
_TABLE.update(dict.fromkeys(ACQUISITION_REVISION_CHANGES, "acquisition_revision"))


def classify_manifest_change(change: str) -> ChangeClass:
    """Return the version signal for a named change kind.

    Raises ``ValueError`` on an unknown kind — the table is the freeze.
    """
    try:
        return _TABLE[change]
    except KeyError:
        raise ValueError(
            f"unknown manifest change {change!r}; not in the v1-frozen table"
        ) from None


__all__ = [
    "ACQUISITION_REVISION_CHANGES",
    "ChangeClass",
    "SEMVER_MAJOR",
    "SEMVER_MINOR",
    "SEMVER_PATCH",
    "classify_manifest_change",
]
