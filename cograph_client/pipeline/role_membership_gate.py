"""Role-membership gate (discovery quality R2) — batch-relative role inversion.

When discovery stamps every scraped row with a single ``focus_type``, role
entities that only appear as *values* on real instances (provider, manufacturer,
organization, …) get minted as the same type as the instances. Example:

* batch of Models includes ``{"name": "acme/widget", "provider": "Acme"}`` and
  also a bare row ``{"name": "Acme"}`` → the bare row is the provider, not a Model.

This module drops those mistaken-instance rows using **batch-relative structural
evidence only**:

1. **Role inversion** — row A's key (alnum-normalized) equals row B's value for a
   role-like attribute, and A is not a stronger catalog-path identity than B →
   drop A.
2. **Sparse self-role row** — row whose name equals its own role field, is sparse
   (only key + role-like fields filled), while the batch has catalog-path
   instances that use that name as a role value → drop.
3. **Uncertain → keep** — no brand/voice/platform denylists; no focus_type name
   lists. Absence of batch evidence never drops a row.

Pure OSS: stdlib only, no I/O, no ``from cograph.*``. Wired post-A1 via
``web_ingest_cap.apply_post_a1_structural_gates`` (ONTA-465 / WS6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "DEFAULT_ROLE_ATTRIBUTES",
    "RoleVerdict",
    "alnum_norm",
    "is_catalog_path",
    "identity_rank",
    "screen_role_membership",
]

# Schema-role vocabulary: attribute *names* that hold a role entity (provider,
# manufacturer, …), never entity names / brand tokens. Matched case-insensitively
# against the attribute leaf exactly (not substrings of provenance fields).
#
# High-precision slots only (ONTA-465 review). Hierarchical / dual-use leaves
# like ``parent``, ``source``, ``owner`` are omitted from the default set —
# free-text instances that also appear as intermediate hierarchy nodes would
# otherwise false-drop under equal-rank Rule 1. Callers may still pass them via
# ``role_attributes=``.
DEFAULT_ROLE_ATTRIBUTES: frozenset[str] = frozenset({
    "provider",
    "organization",
    "manufacturer",
    "vendor",
    "publisher",
})

# Internal / provenance keys that never count as filled plan content when judging
# sparseness, and never as role slots even if the leaf is "source".
_SKIP_ATTRS = frozenset({
    "source_url",
    "source_urls",
    "provenance",
    "observed_at",
    "confidence",
    "row_id",
    "uri",
})


def _norm_ws(value: object) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def alnum_norm(value: object) -> str:
    """Casefold + strip to alphanumeric only for structural equality of keys/values.

    ``"UCSF Medical Center"`` → ``"ucsfmedicalcenter"``;
    ``"openai/gpt-4"`` → ``"openaigpt4"``.
    """
    s = _norm_ws(value).casefold()
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s)


def is_catalog_path(value: object) -> bool:
    """True when the key is a structural catalog id (``org/slug``, …).

    Delegates to :func:`discovery_quality.catalog_path_segments` so R1 identity
    and R2 role-rank share one definition: reject URLs, whitespace segments, and
    single-segment paths. No brand/domain vocabulary.
    """
    from cograph_client.pipeline.discovery_quality import catalog_path_segments

    return catalog_path_segments(value) is not None


def identity_rank(value: object) -> int:
    """Higher = stronger identity form. Catalog path > free-text > empty."""
    if not _norm_ws(value):
        return 0
    if is_catalog_path(value):
        return 2
    return 1


def _attr_leaf(attr: object) -> str:
    return str(attr if attr is not None else "").casefold().strip()


def _is_role_attr(attr: object, role_attributes: frozenset[str]) -> bool:
    leaf = _attr_leaf(attr)
    if not leaf or leaf in _SKIP_ATTRS:
        return False
    # Exact leaf match only — ``source_url`` is not ``source``.
    return leaf in role_attributes


def _cell_str(value: object) -> str:
    return _norm_ws(value)


def _nonempty_fields(row: dict, key_attr: str) -> list[str]:
    """Attribute leaves with a non-empty value (excluding skip/provenance)."""
    out: list[str] = []
    for k, v in row.items():
        leaf = _attr_leaf(k)
        if not leaf or leaf in _SKIP_ATTRS:
            continue
        if not _cell_str(v):
            continue
        out.append(leaf)
    return out


def _is_sparse_self_role(
    row: dict,
    key_attr: str,
    role_attributes: frozenset[str],
) -> bool:
    """True when the only filled fields are the key and role-like attrs, and the
    key equals at least one of those role values (name == provider pattern).
    """
    key_leaf = _attr_leaf(key_attr)
    key_val = _cell_str(row.get(key_attr))
    if not key_val:
        return False
    key_n = alnum_norm(key_val)
    if not key_n:
        return False

    filled = _nonempty_fields(row, key_attr)
    if not filled:
        return False

    role_hits = 0
    for leaf in filled:
        if leaf == key_leaf:
            continue
        if leaf not in role_attributes:
            return False  # has a non-role, non-key field → not sparse-self-role
        role_hits += 1
    if role_hits == 0:
        return False

    # Confirm name equals at least one own role field value.
    for k, v in row.items():
        if not _is_role_attr(k, role_attributes):
            continue
        if alnum_norm(v) == key_n:
            return True
    return False


@dataclass
class RoleVerdict:
    """Outcome of batch-relative role-membership screening."""

    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def screen_role_membership(
    rows: list[dict],
    *,
    key_attr: str,
    role_attributes: Optional[frozenset[str]] = None,
    focus_type: Optional[str] = None,  # reserved; unused (no type-specific lists)
) -> RoleVerdict:
    """Drop rows that are role entities mistaken for instances in this batch.

    Parameters
    ----------
    rows:
        Extracted row dicts (post-A1). Never mutated.
    key_attr:
        Attribute holding the instance key / name.
    role_attributes:
        Schema-role attribute leaves. Defaults to
        :data:`DEFAULT_ROLE_ATTRIBUTES`.
    focus_type:
        Optional; currently unused. Reserved for future ontology-driven role
        attribute selection — must never carry brand/name denylists.

    Uncertain rows are kept. Pure function; stdlib only.
    """
    del focus_type  # reserved, deliberately unused
    if not rows:
        return RoleVerdict()

    role_attrs = frozenset(
        a.casefold().strip()
        for a in (role_attributes if role_attributes is not None else DEFAULT_ROLE_ATTRIBUTES)
        if a and str(a).strip()
    )
    if not role_attrs:
        role_attrs = DEFAULT_ROLE_ATTRIBUTES

    # Index rows we can inspect.
    indexed: list[tuple[int, dict]] = [
        (i, r) for i, r in enumerate(rows) if isinstance(r, dict)
    ]
    if not indexed:
        return RoleVerdict(kept=list(rows))

    # role_value_norm → list of (row_index, identity_rank of that instance's key)
    role_value_owners: dict[str, list[tuple[int, int]]] = {}
    # role_value_norm → True if some catalog-path instance uses it as a role value
    role_value_on_catalog: set[str] = set()
    # Any catalog-path key in the batch (inventory with org/slug ids present).
    batch_has_catalog_path = False

    for i, row in indexed:
        key_raw = row.get(key_attr)
        rank = identity_rank(key_raw)
        if rank >= 2:
            batch_has_catalog_path = True
        for attr, raw in row.items():
            if not _is_role_attr(attr, role_attrs):
                continue
            vn = alnum_norm(raw)
            if not vn:
                continue
            role_value_owners.setdefault(vn, []).append((i, rank))
            if is_catalog_path(key_raw):
                role_value_on_catalog.add(vn)

    kept: list[dict] = []
    dropped: list[dict] = []
    reasons: list[str] = []

    for i, row in indexed:
        key_raw = row.get(key_attr)
        key_n = alnum_norm(key_raw)
        key_display = _cell_str(key_raw) or f"row[{i}]"
        a_rank = identity_rank(key_raw)

        drop_reason: Optional[str] = None

        # --- Rule 1: role inversion (batch-relative) ---
        if key_n:
            owners = role_value_owners.get(key_n) or []
            for j, b_rank in owners:
                if j == i:
                    continue
                # Drop A when it is not a *stronger* identity than B.
                if a_rank <= b_rank:
                    drop_reason = (
                        f"role-inversion: {key_display!r} equals a role-attribute "
                        f"value on another instance (rank {a_rank}<={b_rank})"
                    )
                    break

        # --- Rule 2: sparse self-role (name == own provider/org/…) ---
        # Two evidence levels (both require sparse self-role shape):
        # 2a. Cross-row: some *other* catalog-path instance uses this name as a
        #     role value (classic provider brand duplicated as a row).
        # 2b. Batch inventory: the batch already has any catalog-path instance
        #     (typed inventory with org/slug ids). A sparse free-text row whose
        #     name equals its own role field is then a bare brand/role token,
        #     not another catalog instance — e.g. "ElevenLabs" next to
        #     "fish-audio/s1". Without 2b those brands never drop because no
        #     other row lists them as provider.
        # Pure company lists (no catalog-path keys) keep sparse self-role rows.
        if drop_reason is None and key_n and _is_sparse_self_role(
            row, key_attr, role_attrs
        ):
            owners = role_value_owners.get(key_n) or []
            if any(j != i and b_rank >= 2 for j, b_rank in owners):
                drop_reason = (
                    f"sparse-self-role: {key_display!r} equals its own role "
                    f"field and is used as provider/role on catalog-path instances"
                )
            elif a_rank < 2 and batch_has_catalog_path:
                # Catalog-path inventory present; this free-text row is name==role.
                drop_reason = (
                    f"sparse-self-role: {key_display!r} equals its own role "
                    f"field in a batch that already has catalog-path instances"
                )

        if drop_reason is not None:
            dropped.append(dict(row))
            if len(reasons) < 40:
                reasons.append(drop_reason)
        else:
            kept.append(dict(row))

    # Preserve non-dict inputs at the end of kept (defensive; callers pass dicts).
    for r in rows:
        if not isinstance(r, dict):
            kept.append(r)  # type: ignore[arg-type]

    return RoleVerdict(kept=kept, dropped=dropped, reasons=reasons)

