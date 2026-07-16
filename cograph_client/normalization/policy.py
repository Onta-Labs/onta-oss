"""Per-entity clean/verify policy — the SHARED P3∩P4 policy shape (ONTA-348).

A per-``(kg, type[, attr])`` policy configures how the pipeline cleans (P3) and —
in a future wave — verifies (P4) instance data. The contract's operating-mode
axis is ``auto`` / ``on_demand`` / ``off``, and it must be declared exactly
ONCE so P3's :class:`CleanPolicy` and P4's future ``VerifyPolicy`` never diverge
on what "mode" means. That single declaration is :class:`PolicyBase`:

* :class:`PolicyBase` owns the shared surface — the ``mode`` axis
  (:data:`POLICY_MODES`, validated in ONE ``__post_init__``) plus the
  ``(kg_name, type_name, attr)`` identity every per-entity policy is scoped by.
* :class:`CleanPolicy` (THIS ticket, P3) EXTENDS the base with the clean knobs:
  a null/unknown token set (:data:`DEFAULT_UNKNOWN_TOKENS`) and the
  canonicalization toggles (``trim`` / ``collapse_whitespace`` / ``casefold`` /
  ``nfc``).
* A future P4 ``VerifyPolicy(PolicyBase)`` will EXTEND the SAME base with verify
  knobs (min-confidence, required-authority, …) and reuse ``mode`` /
  ``POLICY_MODES`` verbatim — it never redeclares the enum. The
  ``tests/test_clean_policy.py`` "shared-shape proof" pins this: a stub
  ``VerifyPolicy`` inherits :class:`PolicyBase` and gets identical mode
  validation with zero duplication.

Persistence mirrors :class:`cograph_client.normalization.rules.NormalizationRuleStore`
exactly: a policy is stored as ordinary triples in the **tenant ontology graph**
(:func:`tenant_graph_uri`) — one ``…/entities/CleanPolicy/<id>`` resource with an
``rdf:type`` plus one predicate per field, written through the shared write path
(``kg_writer.delete_facts`` clear-then ``insert_facts``), so the store stays on
the converged write path (no hand-rolled SPARQL insert/delete).

:func:`apply_clean_policy` is a PURE application helper that demonstrates the
policy is usable: it drops an unknown/null token with a reason and otherwise
canonicalizes per the toggles. It is standalone — the actual clean-stage wiring
that CALLS it is owned by a sibling ticket (``normalization/clean.py``), untouched
here.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``. No ``from cograph.*``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.kg_writer import delete_facts, insert_facts
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import _escape_literal, tenant_graph_uri

# --------------------------------------------------------------------------- #
# The ONE shared mode axis (P3 ∩ P4). Declared here exactly once; P4's
# VerifyPolicy reuses PolicyMode / POLICY_MODES verbatim — it never redeclares
# the enum.
# --------------------------------------------------------------------------- #
MODE_AUTO = "auto"
MODE_ON_DEMAND = "on_demand"
MODE_OFF = "off"

PolicyMode = Literal["auto", "on_demand", "off"]
POLICY_MODES: frozenset[str] = frozenset({MODE_AUTO, MODE_ON_DEMAND, MODE_OFF})

# The default null/unknown token set: values that mean "no value" and should be
# DROPPED rather than canonicalized. Compared case-insensitively (see
# :func:`apply_clean_policy`), so listing the lowercase form covers all casings.
DEFAULT_UNKNOWN_TOKENS: frozenset[str] = frozenset(
    {
        "",
        "-",
        "--",
        "n/a",
        "na",
        "n.a.",
        "nil",
        "none",
        "null",
        "unknown",
        "unspecified",
        "not available",
        "not applicable",
    }
)

# apply_clean_policy outcome reasons (named so callers/tests branch on constants,
# not magic strings) — mirrors the conflict module's REASON_* idiom.
REASON_MODE_OFF = "mode_off"  # policy.mode == "off": no cleaning applied
REASON_UNKNOWN_TOKEN = "unknown_token"  # value matched the null/unknown set → dropped
REASON_CANONICALIZED = "canonicalized"  # value survived, changed by a toggle
REASON_UNCHANGED = "unchanged"  # value survived, already canonical


# --------------------------------------------------------------------------- #
# Namespaces — mirror NormalizationRuleStore's exact shape (an entity resource
# with a dedicated `…/onto/policy/<field>` predicate namespace so fields never
# collide with real ontology predicates).
# --------------------------------------------------------------------------- #
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
POLICY_TYPE_URI = "https://cograph.tech/types/CleanPolicy"
POLICY_ENTITY_PREFIX = "https://cograph.tech/entities/CleanPolicy/"
POLICY_NS = "https://cograph.tech/onto/policy/"

P_KG = POLICY_NS + "kgName"
P_TYPE = POLICY_NS + "typeName"
P_ATTR = POLICY_NS + "attr"
P_MODE = POLICY_NS + "mode"
P_TRIM = POLICY_NS + "trim"
P_COLLAPSE_WS = POLICY_NS + "collapseWhitespace"
P_CASEFOLD = POLICY_NS + "casefold"
P_NFC = POLICY_NS + "nfc"
P_UNKNOWN_TOKENS = POLICY_NS + "unknownTokens"
P_CREATED_AT = POLICY_NS + "createdAt"

_CANON_TOGGLES = ("trim", "collapse_whitespace", "casefold", "nfc")


def make_policy_id(kg_name: str, type_name: str, attr: Optional[str] = None) -> str:
    """Deterministic id for a ``(kg, type[, attr])`` policy so re-saving is idempotent.

    Mirrors :func:`cograph_client.normalization.rules.make_rule_id`: a type-wide
    policy keys on ``<kg>__<type>``; a per-attribute policy appends ``__<attr>``,
    so the type-wide default and each per-attribute override get DISTINCT ids and
    never clobber each other in the store. Sanitized to URI-safe chars so it slots
    straight into the entity IRI without further escaping.
    """
    raw = f"{kg_name}__{type_name}"
    if attr:
        raw = f"{raw}__{attr}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:200] or "policy"


# --------------------------------------------------------------------------- #
# The shared base (P3 ∩ P4) + the P3 CleanPolicy that extends it.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyBase:
    """Shared base for per-entity clean/verify policies — the P3∩P4 surface.

    Carries the ONE ``mode`` axis (``auto`` / ``on_demand`` / ``off``) that BOTH
    the P3 :class:`CleanPolicy` and the future P4 ``VerifyPolicy`` share, plus the
    ``(kg_name, type_name, attr)`` identity every per-entity policy is scoped by.
    Mode validation lives in this ONE ``__post_init__``; a subclass extends the
    base and calls ``super().__post_init__()`` to reuse it verbatim — the enum is
    never redeclared downstream. ``attr is None`` means the type-wide default;
    a non-empty ``attr`` is a per-attribute override.

    Frozen (like :class:`cograph_client.pipeline.conflict.ConflictPolicy`) so a
    policy is an immutable value and application is a pure function of its inputs.
    """

    kg_name: str = ""
    type_name: str = ""
    attr: Optional[str] = None
    mode: PolicyMode = MODE_AUTO  # type: ignore[assignment]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if self.mode not in POLICY_MODES:
            raise ValueError(
                f"policy mode {self.mode!r} is invalid; "
                f"allowed: {sorted(POLICY_MODES)}"
            )

    @property
    def scope_id(self) -> str:
        """Deterministic store id for this policy's ``(kg, type[, attr])`` scope."""
        return make_policy_id(self.kg_name, self.type_name, self.attr)

    @property
    def uri(self) -> str:
        return POLICY_ENTITY_PREFIX + self.scope_id


@dataclass(frozen=True)
class CleanPolicy(PolicyBase):
    """P3 clean policy: EXTENDS :class:`PolicyBase` with the clean knobs.

    Adds a null/unknown token set (values meaning "no value" that
    :func:`apply_clean_policy` DROPS) and the canonicalization toggles applied to
    a surviving value:

    * ``trim`` — strip leading/trailing whitespace.
    * ``collapse_whitespace`` — fold internal whitespace runs to a single space.
    * ``casefold`` — Unicode-aware lowercasing.
    * ``nfc`` — Unicode NFC normalization (compose combining sequences).

    Reuses the base ``mode`` axis unchanged — it does NOT redeclare
    :data:`POLICY_MODES`. ``__post_init__`` first delegates mode validation to the
    base, then validates its own knobs (each toggle must be a real ``bool``;
    ``unknown_tokens`` is coerced to a ``frozenset`` of ``str``), mirroring the
    axis-validation idiom of ``ConflictPolicy.__post_init__``.
    """

    trim: bool = True
    collapse_whitespace: bool = True
    casefold: bool = False
    nfc: bool = True
    unknown_tokens: frozenset[str] = DEFAULT_UNKNOWN_TOKENS

    def __post_init__(self) -> None:
        # Reuse the base mode validation verbatim (no duplicated enum).
        super().__post_init__()
        # Coerce unknown_tokens to a frozenset of str (ergonomic: accept any
        # iterable at construction, stay frozen afterward).
        if not isinstance(self.unknown_tokens, frozenset):
            object.__setattr__(
                self, "unknown_tokens", frozenset(self.unknown_tokens)
            )
        for tok in self.unknown_tokens:
            if not isinstance(tok, str):
                raise ValueError(
                    f"CleanPolicy.unknown_tokens entries must be str; got {tok!r}"
                )
        # Reject an invalid knob: each toggle must be a real bool (note: bool is an
        # int subclass, so `1`/`0` are rejected — the strictness the ticket wants).
        for knob in _CANON_TOGGLES:
            val = getattr(self, knob)
            if not isinstance(val, bool):
                raise ValueError(
                    f"CleanPolicy.{knob} must be a bool; got {val!r}"
                )


# --------------------------------------------------------------------------- #
# Pure application helper — demonstrates the policy is usable.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CleanOutcome:
    """The result of applying a :class:`CleanPolicy` to one value.

    ``value`` is the cleaned value, or ``None`` when it was DROPPED (an
    unknown/null token). ``dropped`` / ``changed`` are convenience flags and
    ``reason`` names WHY (one of the ``REASON_*`` constants) so a caller can log /
    surface the decision. Immutable — application is a pure function.
    """

    value: Optional[str]
    dropped: bool
    changed: bool
    reason: str


def apply_clean_policy(value: str, policy: CleanPolicy) -> CleanOutcome:
    """Apply ``policy`` to ``value`` — the pure P3 clean step.

    * ``mode == "off"`` → pass the value through untouched (the policy is disabled).
    * A value matching the null/unknown token set (case-insensitively, after a
      light trim) → DROPPED with :data:`REASON_UNKNOWN_TOKEN`.
    * Otherwise → canonicalized per the toggles (NFC, trim, whitespace-collapse,
      casefold); :data:`REASON_CANONICALIZED` if that changed it, else
      :data:`REASON_UNCHANGED`.

    Deterministic: same ``(value, policy)`` → same outcome. This is standalone —
    the clean-stage wiring that calls it lives in ``normalization/clean.py`` (a
    sibling ticket), not here.
    """
    if policy.mode == MODE_OFF:
        return CleanOutcome(value=value, dropped=False, changed=False, reason=REASON_MODE_OFF)

    # Unknown/null detection is case-insensitive regardless of the casefold toggle
    # (a null-token set exists precisely to catch "N/A" / "n/a" / "NA" alike). Use
    # a stripped view for the match so surrounding whitespace never hides a token.
    if value.strip().casefold() in {t.casefold() for t in policy.unknown_tokens}:
        return CleanOutcome(value=None, dropped=True, changed=True, reason=REASON_UNKNOWN_TOKEN)

    canonical = value
    if policy.nfc:
        canonical = unicodedata.normalize("NFC", canonical)
    if policy.trim:
        canonical = canonical.strip()
    if policy.collapse_whitespace:
        canonical = re.sub(r"\s+", " ", canonical)
    if policy.casefold:
        canonical = canonical.casefold()

    changed = canonical != value
    return CleanOutcome(
        value=canonical,
        dropped=False,
        changed=changed,
        reason=REASON_CANONICALIZED if changed else REASON_UNCHANGED,
    )


# --------------------------------------------------------------------------- #
# Store — mirrors NormalizationRuleStore's exact persistence mechanism.
# --------------------------------------------------------------------------- #
class CleanPolicyStore:
    """Persist + read :class:`CleanPolicy`\\ s in the tenant ontology graph.

    A direct mirror of
    :class:`cograph_client.normalization.rules.NormalizationRuleStore`: each
    method is async (one Neptune round-trip) and :meth:`save` is idempotent — it
    clears any prior triples for the policy's id via the shared
    ``kg_writer.delete_facts`` (subject-scoped) then writes the current field set
    via ``kg_writer.insert_facts``, so re-saving an updated policy never leaves
    stale field triples behind. No ``refresh_after_write`` — a policy row is config
    metadata (never instance data / geometry / schema), so there is no
    derived-index, ontology-cache, or type-stats fan-out to run for it (identical
    reasoning to the rule store).
    """

    def __init__(self, neptune: NeptuneClient):
        self._neptune = neptune

    async def save(self, tenant_id: str, policy: CleanPolicy) -> None:
        graph = tenant_graph_uri(tenant_id)
        await delete_facts(
            self._neptune, graph, subjects=[policy.uri], reason="clean-policy upsert"
        )
        await insert_facts(self._neptune, graph, self._policy_to_triples(policy))

    async def get(self, tenant_id: str, policy_id: str) -> Optional[CleanPolicy]:
        graph = tenant_graph_uri(tenant_id)
        uri = POLICY_ENTITY_PREFIX + policy_id
        q = (
            f"SELECT ?p ?o FROM <{graph}> WHERE {{\n"
            f"  <{uri}> ?p ?o .\n"
            f"}}"
        )
        _, rows = parse_sparql_results(await self._neptune.query(q))
        if not rows:
            return None
        fields = {r["p"]: r["o"] for r in rows if "p" in r and "o" in r}
        return self._policy_from_fields(fields)

    async def list(
        self, tenant_id: str, kg: Optional[str] = None
    ) -> list[CleanPolicy]:
        """List policies, optionally filtered by KG name.

        The KG filter is applied as escaped string-literal equality in SPARQL
        (never spliced into an IRI), so it is injection-safe — same discipline as
        ``NormalizationRuleStore.list``.
        """
        graph = tenant_graph_uri(tenant_id)
        filters = ""
        if kg is not None:
            filters += f'  ?s <{P_KG}> "{_escape_literal(kg)}" .\n'
        q = (
            f"SELECT ?s ?p ?o FROM <{graph}> WHERE {{\n"
            f"  ?s <{RDF_TYPE}> <{POLICY_TYPE_URI}> .\n"
            f"{filters}"
            f"  ?s ?p ?o .\n"
            f"}}"
        )
        _, rows = parse_sparql_results(await self._neptune.query(q))
        by_subject: dict[str, dict[str, str]] = {}
        for r in rows:
            s, p, o = r.get("s"), r.get("p"), r.get("o")
            if not s or not p:
                continue
            by_subject.setdefault(s, {})[p] = o
        out: list[CleanPolicy] = []
        for fields in by_subject.values():
            policy = self._policy_from_fields(fields)
            if policy is not None:
                out.append(policy)
        # Stable default order: by scope id.
        out.sort(key=lambda p: p.scope_id)
        return out

    # --- serialization -------------------------------------------------------

    @staticmethod
    def _bool_literal(value: bool) -> str:
        # Typed xsd:boolean literal (RDF-correct); `_escape_value` emits
        # `"true"^^<…#boolean>`. Read-side `_parse_bool` reads the lexical form.
        return f"{'true' if value else 'false'}^^{XSD_BOOLEAN}"

    @staticmethod
    def _parse_bool(raw: Optional[str], default: bool) -> bool:
        if raw is None or raw == "":
            return default
        return str(raw).strip().casefold() in {"true", "1"}

    @classmethod
    def _policy_to_triples(cls, policy: CleanPolicy) -> list[tuple[str, str, str]]:
        uri = policy.uri
        triples: list[tuple[str, str, str]] = [
            (uri, RDF_TYPE, POLICY_TYPE_URI),
            (uri, P_KG, policy.kg_name),
            (uri, P_TYPE, policy.type_name),
            (uri, P_MODE, policy.mode),
            (uri, P_TRIM, cls._bool_literal(policy.trim)),
            (uri, P_COLLAPSE_WS, cls._bool_literal(policy.collapse_whitespace)),
            (uri, P_CASEFOLD, cls._bool_literal(policy.casefold)),
            (uri, P_NFC, cls._bool_literal(policy.nfc)),
            # Token set as a JSON blob (sorted for a stable serialization), exactly
            # how the rule store stores params / sample_values.
            (uri, P_UNKNOWN_TOKENS, json.dumps(sorted(policy.unknown_tokens))),
            (uri, P_CREATED_AT, policy.created_at),
        ]
        if policy.attr:
            triples.append((uri, P_ATTR, policy.attr))
        return triples

    @classmethod
    def _policy_from_fields(cls, fields: dict[str, str]) -> Optional[CleanPolicy]:
        if fields.get(RDF_TYPE) != POLICY_TYPE_URI:
            return None

        def _tokens(raw: str) -> frozenset[str]:
            if not raw:
                return DEFAULT_UNKNOWN_TOKENS
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return DEFAULT_UNKNOWN_TOKENS
            if not isinstance(parsed, list):
                return DEFAULT_UNKNOWN_TOKENS
            return frozenset(str(t) for t in parsed)

        return CleanPolicy(
            kg_name=fields.get(P_KG, ""),
            type_name=fields.get(P_TYPE, ""),
            attr=fields.get(P_ATTR) or None,
            mode=fields.get(P_MODE, MODE_AUTO),  # type: ignore[arg-type]
            trim=cls._parse_bool(fields.get(P_TRIM), True),
            collapse_whitespace=cls._parse_bool(fields.get(P_COLLAPSE_WS), True),
            casefold=cls._parse_bool(fields.get(P_CASEFOLD), False),
            nfc=cls._parse_bool(fields.get(P_NFC), True),
            unknown_tokens=_tokens(fields.get(P_UNKNOWN_TOKENS, "")),
            created_at=fields.get(P_CREATED_AT, ""),
        )


__all__ = [
    "MODE_AUTO",
    "MODE_ON_DEMAND",
    "MODE_OFF",
    "PolicyMode",
    "POLICY_MODES",
    "DEFAULT_UNKNOWN_TOKENS",
    "REASON_MODE_OFF",
    "REASON_UNKNOWN_TOKEN",
    "REASON_CANONICALIZED",
    "REASON_UNCHANGED",
    "PolicyBase",
    "CleanPolicy",
    "CleanOutcome",
    "apply_clean_policy",
    "CleanPolicyStore",
    "make_policy_id",
]
