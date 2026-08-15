"""Filter-token bind API + prompt formatting for the dim registry."""

from __future__ import annotations

from typing import Sequence

from infona_client.nlp.dim_registry_models import (
    BIND_AMBIGUITY_MARGIN,
    BIND_MIN_SCORE,
    EDIT_DISTANCE_MAX,
    EDIT_DISTANCE_MAX_LEN,
    MAX_VALUES_IN_PROMPT,
    DimBind,
    DimBindResult,
    DimRegistry,
    DimValue,
    _QUOTED_RE,
    _TOKEN_RE,
    normalize_dim_token,
)


def _host():
    """Call-time lookup of the public ``dim_registry`` module."""
    from infona_client.nlp import dim_registry as _mod

    return _mod


def _edit_distance(a: str, b: str) -> int:
    """Classic Levenshtein; only used for short strings (caller gates length)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Two-row DP
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _token_overlap(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _score_value_match(token_norm: str, value: DimValue) -> tuple[float, str] | None:
    """Score one dim value against a normalized token. None = no match."""
    if not token_norm or not value.normalized:
        return None
    if token_norm == value.normalized:
        return 1.0, "exact"
    # Compact form without spaces (northfleet vs north fleet).
    t_c = token_norm.replace(" ", "")
    v_c = value.normalized.replace(" ", "")
    if t_c and t_c == v_c:
        return 0.98, "normalized"
    # Token overlap for multi-word labels.
    ov = _token_overlap(token_norm, value.normalized)
    if ov >= 1.0:
        return 0.95, "normalized"
    if ov >= 0.67 and min(len(token_norm), len(value.normalized)) >= 3:
        return 0.7 + 0.2 * ov, "normalized"
    # Cheap edit distance for short labels only.
    if (
        len(t_c) <= EDIT_DISTANCE_MAX_LEN
        and len(v_c) <= EDIT_DISTANCE_MAX_LEN
        and abs(len(t_c) - len(v_c)) <= EDIT_DISTANCE_MAX
    ):
        dist = _edit_distance(t_c, v_c)
        if dist <= EDIT_DISTANCE_MAX and max(len(t_c), len(v_c)) >= 3:
            return 0.75 - 0.1 * dist, "fuzzy"
    return None


def rank_filter_token_dims(
    token: str,
    registry: DimRegistry | None,
    *,
    type_hint: str | None = None,
) -> list[DimBind]:
    """Rank all dim-value matches for ``token`` (highest score first)."""
    if registry is None or not token or not str(token).strip():
        return []
    token_norm = normalize_dim_token(token)
    if not token_norm:
        return []
    dims = registry.for_type(type_hint) if type_hint else registry.dims
    out: list[DimBind] = []
    for dim in dims:
        best_for_dim: DimBind | None = None
        for val in dim.values:
            scored = _score_value_match(token_norm, val)
            if scored is None:
                continue
            score, kind = scored
            if score < BIND_MIN_SCORE:
                continue
            cand = DimBind(
                token=str(token).strip(),
                dim=dim,
                matched_value=val,
                score=score,
                match_kind=kind,
            )
            if best_for_dim is None or cand.score > best_for_dim.score:
                best_for_dim = cand
        if best_for_dim is not None:
            out.append(best_for_dim)
    out.sort(
        key=lambda b: (
            -b.score,
            -b.dim.coverage,
            b.dim.subject_type,
            b.dim.leaf,
        )
    )
    return out


def bind_filter_token(
    token: str,
    *,
    tenant_id: str | None = None,
    kg: str | None = None,
    type_hint: str | None = None,
    registry: DimRegistry | None = None,
) -> DimBind | None:
    """Bind a filter token to a unique dim, or ``None`` if none / ambiguous.

    Fail-closed: when the top-2 candidates are within
    :data:`BIND_AMBIGUITY_MARGIN`, returns ``None`` rather than silent pick.

    Prefer passing ``registry=`` in hermetic tests. When omitted, looks up the
    process cache for ``(tenant_id, kg)``.
    """
    result = bind_filter_token_result(
        token,
        tenant_id=tenant_id,
        kg=kg,
        type_hint=type_hint,
        registry=registry,
    )
    return result.unique


def bind_filter_token_result(
    token: str,
    *,
    tenant_id: str | None = None,
    kg: str | None = None,
    type_hint: str | None = None,
    registry: DimRegistry | None = None,
) -> DimBindResult:
    """Full bind result including ambiguity flag and ranked candidates."""
    reg = registry
    if reg is None and tenant_id and kg:
        reg = _host().get_cached_dim_registry(tenant_id, kg)
    ranked = rank_filter_token_dims(token, reg, type_hint=type_hint)
    if not ranked:
        return DimBindResult(token=str(token or ""), unique=None, candidates=())
    if len(ranked) == 1:
        return DimBindResult(
            token=str(token).strip(),
            unique=ranked[0],
            candidates=tuple(ranked),
            ambiguous=False,
        )
    top, second = ranked[0], ranked[1]
    if (top.score - second.score) < BIND_AMBIGUITY_MARGIN:
        return DimBindResult(
            token=str(token).strip(),
            unique=None,
            candidates=tuple(ranked),
            ambiguous=True,
        )
    return DimBindResult(
        token=str(token).strip(),
        unique=top,
        candidates=tuple(ranked),
        ambiguous=False,
    )


def extract_filter_tokens(question: str) -> list[str]:
    """Cheap filter-token candidates from a question (no ontology)."""
    if not question or not isinstance(question, str):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _QUOTED_RE.finditer(question):
        tok = m.group(1).strip()
        key = normalize_dim_token(tok)
        if key and key not in seen:
            seen.add(key)
            out.append(tok)
    for m in _TOKEN_RE.finditer(question):
        raw = m.group("tok") or ""
        tok = raw.strip().strip("'\"").strip()
        # Drop pure stopwords / very short.
        key = normalize_dim_token(tok)
        if not key or len(key) < 2:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def bind_tokens_in_question(
    question: str,
    registry: DimRegistry | None,
    *,
    type_hint: str | None = None,
) -> list[DimBind]:
    """Unique binds for filter tokens found in ``question``."""
    if registry is None:
        return []
    binds: list[DimBind] = []
    for tok in extract_filter_tokens(question):
        b = bind_filter_token(tok, registry=registry, type_hint=type_hint)
        if b is not None:
            binds.append(b)
    return binds


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


def format_dims_for_prompt(
    registry: DimRegistry | None,
    *,
    binds: Sequence[DimBind] = (),
    max_dims: int = 24,
) -> str:
    """Short 'known dimensions' block for the Cypher planning prompt."""
    if registry is None or not registry.dims:
        # Still surface unique binds if provided without a full registry dump.
        if not binds:
            return ""
    lines: list[str] = [
        "Known low-cardinality dimensions (data-driven — bind filter tokens",
        "to these leaves/rels with EXACT stored values; still emit Cypher via LLM;",
        "do NOT invent dim values not listed):",
    ]
    shown = 0
    if registry is not None:
        for dim in registry.dims:
            if shown >= max_dims:
                lines.append(f"  … ({len(registry.dims) - shown} more dims omitted)")
                break
            vals = ", ".join(
                f'"{v.display}"' for v in dim.values[:MAX_VALUES_IN_PROMPT]
            )
            more = ""
            if dim.distinct_count > len(dim.values[:MAX_VALUES_IN_PROMPT]):
                more = f" … ({dim.distinct_count} distinct)"
            if dim.kind == "entity_dim":
                range_bit = f" → {dim.range_type}" if dim.range_type else ""
                lines.append(
                    f"  - {dim.subject_type}.{dim.leaf}{range_bit} "
                    f"(entity_dim, n={dim.distinct_count}): {vals}{more}"
                )
            else:
                lines.append(
                    f"  - {dim.subject_type}.{dim.leaf} "
                    f"(literal_enum, n={dim.distinct_count}): {vals}{more}"
                )
            shown += 1
    if binds:
        lines.append("Bound filter tokens from the question (use these when confident):")
        for b in binds:
            d = b.dim
            target = (
                f"{d.leaf}→{d.range_type}" if d.kind == "entity_dim" else d.leaf
            )
            lines.append(
                f"  - {b.token!r} → {d.subject_type}.{target} "
                f"= {b.matched_value.display!r} "
                f"({b.match_kind}, score={b.score:.2f})"
            )
    return "\n".join(lines) + "\n"
