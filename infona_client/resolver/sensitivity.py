"""Sensitivity tags + v1 enforcement (ADR 0002 §5).

Attributes carry schema-level sensitivity tags ('pii' | 'secret' | 'public'),
stored as the 'sensitivity' entry of a type's strategy bundle
(resolver/strategy.py): a dict of attr_name -> tag. An attribute absent from
the map defaults to 'public'; 'pii' and 'secret' both count as sensitive.

Inheritance differs from resolve_entry's nearest-ancestor-wins: sensitivity
maps MERGE down the subclass chain, child-overrides-parent, so a subtype can
re-tag a single attribute without redeclaring its ancestors' whole map
(resolve_sensitivity_map). At each type in the chain, registries are still
checked in precedence order (tenant > enhanced > public) and the first one
defining 'sensitivity' for that type wins for that type — the same shadowing
resolve_entry applies.

v1 enforces exactly three rules, each a pure reusable utility:

  1. guard_enrichment_payload — never send a sensitive value to an external
     enrichment service.
  2. filter_response_attrs   — never return a sensitive value without
     entitlement.
  3. redact_for_log          — replace sensitive values with '[REDACTED]'
     before logging.

Nothing in the OSS resolver currently logs attribute values with the type +
parent-map context these utilities need (resolve_attribute's
attr_type_mismatch warning is a pure function with no ontology context), so
the pipeline is not wired here — callers with that context (the API response
layer for rule 2, enrichment adapters/executor for rule 1, any structured log
of attribute records for rule 3) route through these utilities. With no
'sensitivity' entries registered anywhere, all three are exact no-ops.

Value-level detection (per-instance sensitivity) is explicitly deferred by
the ADR; the tag vocabulary leaves room for it without reworking this module.

Name-based privileged-column redaction (``is_privileged_column_name`` /
``redact_privileged_sample_rows``) is a separate pre-ontology path used by
CSV schema inference: sample values for columns named like ``ssn``,
``privileged*``, ``secret*``, passwords, API keys, … are replaced with
``[REDACTED]`` before they are embedded in any LLM prompt. Schema-level tags
cannot apply yet (inference invents the type); the deny list is the v1 guard.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from infona_client.resolver.er.types import ancestor_chain
from infona_client.resolver.strategy import StrategyRegistry

# Tag vocabulary (ADR 0002 §5).
PII = "pii"
SECRET = "secret"
PUBLIC = "public"
SENSITIVE_TAGS = frozenset({PII, SECRET})

# The strategy-bundle entry key sensitivity maps live under.
SENSITIVITY_ENTRY = "sensitivity"

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Name-based privileged-column deny list (CSV schema-inference samples)
# ---------------------------------------------------------------------------
# Schema inference has no type/registry context yet (it is inventing the type),
# so the ADR-tagged map above cannot apply. Before any sample value is embedded
# in an LLM prompt we redact columns whose *names* look privileged — SSN,
# secrets, passwords, API keys, anything starting with "privileged"/"secret".
# Keys (column names) are kept so the model can still map the column; only
# values become REDACTED. Profile examples that would leak the same values are
# scrubbed the same way.
#
# Conservative on purpose: false positives cost one opaque sample cell; false
# negatives send secrets to a cloud LLM.
_PRIVILEGED_EXACT = frozenset(
    {
        "ssn",
        "social_security",
        "social_security_number",
        "password",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "private_key",
        "secret_key",
        "credential",
        "credentials",
        "credit_card",
        "card_number",
        "cvv",
        "pin",
    }
)

# Prefixes: privileged*, secret*, password*, ssn*, api_key*, private_key*, ...
_PRIVILEGED_PREFIXES = (
    "privileged",
    "secret",
    "password",
    "passwd",
    "ssn",
    "api_key",
    "apikey",
    "private_key",
    "access_token",
    "auth_token",
    "credential",
    "credit_card",
)

# Substring tokens that flag compound names like customer_ssn, user_password_hash.
_PRIVILEGED_TOKENS = frozenset(
    {
        "ssn",
        "password",
        "passwd",
        "secret",
        "privileged",
        "api_key",
        "apikey",
        "private_key",
        "access_token",
        "auth_token",
        "credential",
        "credentials",
        "credit_card",
        "cvv",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize_column_name(name: str) -> str:
    """Lowercase + collapse non-alnum runs to ``_`` for deny-list matching."""
    return _NON_ALNUM.sub("_", (name or "").lower()).strip("_")


def is_privileged_column_name(name: str) -> bool:
    """True when a CSV/header name looks like it holds privileged/secret data.

    Matches exact names (``ssn``, ``password``, …), prefixes
    (``privileged_note``, ``secret_token``), and token membership in a
    snake/kebab/camel-split name (``customer_ssn``, ``userPassword``).
    """
    n = _normalize_column_name(name)
    if not n:
        return False
    if n in _PRIVILEGED_EXACT:
        return True
    for prefix in _PRIVILEGED_PREFIXES:
        if n == prefix or n.startswith(prefix + "_"):
            return True
        # CamelCase collapse: PrivilegedFlag → "privilegedflag". Require a
        # reasonably long prefix so short ones like "ssn" don't match
        # "ssnumber"-style false friends via bare startswith alone — those
        # still match via exact / token rules when intended.
        if len(prefix) >= 6 and n.startswith(prefix):
            return True
    # Tokenize on underscores; also split camelCase leftovers already collapsed.
    tokens = [t for t in n.split("_") if t]
    if any(t in _PRIVILEGED_TOKENS for t in tokens):
        return True
    return False


def redact_privileged_sample_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    placeholder: str = REDACTED,
) -> list[dict[str, Any]]:
    """Return a deep-enough copy of ``rows`` with privileged column VALUES redacted.

    Column *names* are preserved so schema inference can still invent a mapping
    for them; only values become ``placeholder``. Non-privileged cells pass
    through unchanged. Input rows are never mutated.
    """
    if not rows:
        return []
    # Cache name decisions — headers are shared across the sample.
    privileged_cache: dict[str, bool] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        redacted: dict[str, Any] = {}
        for k, v in row.items():
            key = str(k)
            if key not in privileged_cache:
                privileged_cache[key] = is_privileged_column_name(key)
            redacted[key] = placeholder if privileged_cache[key] else v
        out.append(redacted)
    return out


def redact_privileged_profile_examples(
    profile: Any,
    *,
    placeholder: str = REDACTED,
) -> Any:
    """Scrub privileged-column ``examples`` on a :class:`TableProfile` for LLM prompts.

    Mutates ``profile.columns[*].examples`` in place when a column name matches
    the deny list; returns the same profile for chaining. Completeness /
    cardinality stats are left alone (they do not leak cell values).
    """
    columns = getattr(profile, "columns", None) or []
    for col in columns:
        name = getattr(col, "name", "") or ""
        if is_privileged_column_name(name):
            examples = getattr(col, "examples", None)
            if examples:
                col.examples = [placeholder for _ in examples]
    return profile


def is_sensitive(tag: str) -> bool:
    """True for 'pii' and 'secret'; 'public' (and anything unknown) is not."""
    return tag in SENSITIVE_TAGS


def resolve_sensitivity_map(
    type_name: str,
    parent_of: dict[str, str],
    registries: list[StrategyRegistry],
) -> dict[str, str]:
    """Effective attr_name -> tag map for a type.

    Walks ancestor_chain(type_name, parent_of) root-first and merges each
    type's 'sensitivity' entry over its ancestors', so a child re-tagging an
    attribute overrides the parent while inheriting everything else. At each
    type, registries are checked in precedence order and the first bundle
    defining 'sensitivity' wins for that type (resolve_entry shadowing).

    Pure: no Neptune, no I/O. Cycle-guarded via ancestor_chain. No entries
    anywhere in the chain => empty map => everything defaults to 'public'.
    """
    merged: dict[str, str] = {}
    for ancestor in reversed(ancestor_chain(type_name, parent_of)):
        for registry in registries:
            bundle = registry.get(ancestor)
            if bundle is not None and SENSITIVITY_ENTRY in bundle:
                merged.update(bundle[SENSITIVITY_ENTRY])
                break
    return merged


def guard_enrichment_payload(
    payload: dict[str, Any],
    type_name: str,
    parent_of: dict[str, str],
    registries: list[StrategyRegistry],
) -> dict[str, Any]:
    """Rule 1: never send a sensitive attribute's value to external enrichment.

    Returns a NEW dict with pii/secret attributes removed entirely (key and
    value — an external service should not learn the attribute exists on this
    record). Untagged attributes default to 'public' and pass through, so an
    empty registry list is an exact no-op copy.
    """
    smap = resolve_sensitivity_map(type_name, parent_of, registries)
    return {k: v for k, v in payload.items() if not is_sensitive(smap.get(k, PUBLIC))}


def filter_response_attrs(
    attrs: dict[str, Any],
    type_name: str,
    parent_of: dict[str, str],
    registries: list[StrategyRegistry],
    entitled: bool,
) -> dict[str, Any]:
    """Rule 2: never return a sensitive attribute without entitlement.

    Entitled callers get an unchanged copy; non-entitled callers get the
    payload with pii/secret attributes removed. Always returns a new dict so
    callers can mutate the result without touching the source record.
    """
    if entitled:
        return dict(attrs)
    smap = resolve_sensitivity_map(type_name, parent_of, registries)
    return {k: v for k, v in attrs.items() if not is_sensitive(smap.get(k, PUBLIC))}


def redact_for_log(
    record: dict[str, Any],
    type_name: str,
    parent_of: dict[str, str],
    registries: list[StrategyRegistry],
) -> dict[str, Any]:
    """Rule 3: redact sensitive VALUES before logging.

    Unlike rules 1-2 the keys are kept — log records keep a stable shape and
    show that a redaction happened — but every pii/secret value is replaced
    with '[REDACTED]'. Returns a new dict; the source record is untouched.
    """
    smap = resolve_sensitivity_map(type_name, parent_of, registries)
    return {
        k: REDACTED if is_sensitive(smap.get(k, PUBLIC)) else v
        for k, v in record.items()
    }
