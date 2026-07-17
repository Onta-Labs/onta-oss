"""Per-entity VERIFY policy — P4's projection of the shared P3∩P4 shape (ONTA-362).

This is the ``VerifyPolicy(PolicyBase)`` the ``normalization/policy.py`` docstring
explicitly anticipated. It gives the P4 Verify stage its own per-``(kg, type[, attr])``
policy by EXTENDING the ONE shared base ONTA-348 already built — with zero
duplication of the operating-mode axis:

* :class:`cograph_client.normalization.policy.PolicyBase` owns the shared surface —
  the ``mode`` axis (:data:`~cograph_client.normalization.policy.POLICY_MODES`,
  validated in ONE ``__post_init__``) plus the ``(kg_name, type_name, attr)``
  identity every per-entity policy is scoped by. This module IMPORTS it and never
  redeclares the enum.
* :class:`CleanPolicy` (P3) extends the base with clean knobs.
* :class:`VerifyPolicy` (THIS module, P4) extends the SAME base with verify knobs:
  whether corroboration must come from an INDEPENDENT source
  (``independent_evidence_required``), a per-entity-class verification budget
  (``max_evidence_sources`` / ``max_cost``), and an evidence-source allow/deny list
  (``allowed_hosts`` / ``denied_hosts``). Its ``__post_init__`` delegates mode
  validation to ``super().__post_init__()`` (the shared validator) then validates
  its own knobs, mirroring :meth:`CleanPolicy.__post_init__`'s idiom.

Because ``VerifyPolicy`` carries the base ``mode`` axis, the P4 orchestrator
(:func:`cograph_client.verification.verifier.verify_clean_facts`) recognizes it as
ON/OFF for free: its ``_policy_enabled`` duck-types a policy's string ``mode``
(``off``/``none``/``disabled``/empty ⇒ OFF), so ``VerifyPolicy(mode="off")`` gates
verification off and ``VerifyPolicy(mode="auto")`` gates it on — no ``enabled`` flag
needed.

Persistence (:class:`VerifyPolicyStore`) mirrors
:class:`~cograph_client.normalization.policy.CleanPolicyStore` EXACTLY: a policy is
stored as ordinary triples in the **tenant ontology graph**
(:func:`~cograph_client.graph.queries.tenant_graph_uri`) — one
``…/entities/VerifyPolicy/<id>`` resource with an ``rdf:type`` plus one predicate per
field, written through the shared converged write path (``kg_writer.delete_facts``
clear-then ``kg_writer.insert_facts``), so the store stays on the converged write
path with no hand-rolled SPARQL insert/delete. The shared base fields
(``kgName`` / ``typeName`` / ``attr`` / ``mode`` / ``createdAt``) reuse the SAME
predicate URIs and the SAME ``make_policy_id`` / ``tenant_graph_uri`` conventions as
:class:`CleanPolicyStore`.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``. No ``from cograph.*``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.kg_writer import delete_facts, insert_facts
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import _escape_literal, tenant_graph_uri

# Reuse the shared P3∩P4 base + everything the mode axis / scoping is built from.
# The mode enum lives ONLY in normalization/policy.py — imported, never redeclared.
from cograph_client.normalization.policy import (
    MODE_AUTO,
    POLICY_MODES,  # re-exported for callers; NOT redeclared here
    POLICY_NS,
    RDF_TYPE,
    XSD_BOOLEAN,
    CleanPolicyStore,
    PolicyBase,
    PolicyMode,
    make_policy_id,
)

# --------------------------------------------------------------------------- #
# Namespaces — mirror CleanPolicyStore's exact shape, but a DISTINCT rdf:type +
# entity prefix so a VerifyPolicy resource is `…/entities/VerifyPolicy/<id>` and
# never collides with a CleanPolicy resource in the same tenant ontology graph.
# The shared base fields reuse the SAME `…/onto/policy/<field>` predicate
# namespace (POLICY_NS) and predicate URIs as CleanPolicy — the subjects differ,
# so there is no collision, and the shared surface stays literally shared.
# --------------------------------------------------------------------------- #
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"

VERIFY_POLICY_TYPE_URI = "https://cograph.tech/types/VerifyPolicy"
VERIFY_POLICY_ENTITY_PREFIX = "https://cograph.tech/entities/VerifyPolicy/"

# Shared base-field predicates (reused verbatim from the CleanPolicy shape).
P_KG = POLICY_NS + "kgName"
P_TYPE = POLICY_NS + "typeName"
P_ATTR = POLICY_NS + "attr"
P_MODE = POLICY_NS + "mode"
P_CREATED_AT = POLICY_NS + "createdAt"

# Verify-specific knob predicates.
P_INDEPENDENT_EVIDENCE_REQUIRED = POLICY_NS + "independentEvidenceRequired"
P_MAX_EVIDENCE_SOURCES = POLICY_NS + "maxEvidenceSources"
P_MAX_COST = POLICY_NS + "maxCost"
P_ALLOWED_HOSTS = POLICY_NS + "allowedHosts"
P_DENIED_HOSTS = POLICY_NS + "deniedHosts"


# --------------------------------------------------------------------------- #
# VerifyPolicy — EXTENDS the shared PolicyBase with the P4 verify knobs.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerifyPolicy(PolicyBase):
    """P4 verify policy: EXTENDS :class:`PolicyBase` with the verify knobs.

    Reuses the base ``mode`` axis unchanged — it does NOT redeclare
    :data:`~cograph_client.normalization.policy.POLICY_MODES`. The knobs:

    * ``independent_evidence_required`` — corroboration must come from at least one
      source DISTINCT from the fact's own (the epistemic core of P4). Default ``True``.
    * ``max_evidence_sources`` — per-entity-class evidence budget: the max number of
      independent sources a verifier may consult for one fact. Default ``3``.
    * ``max_cost`` — per-entity-class cost budget (in the retrieval cost seam's units)
      a verifier may spend corroborating one fact. Default ``1.0``.
    * ``allowed_hosts`` / ``denied_hosts`` — evidence-source allow / deny lists (by
      network host). Empty ``allowed_hosts`` means "any host not denied"; a host in
      ``denied_hosts`` is never used as evidence. Both are frozen tuples of ``str``.

    ``__post_init__`` first delegates mode validation to the base (the ONE shared
    validator), then validates its own knobs — each with the strict type checks the
    P3 :meth:`CleanPolicy.__post_init__` uses (``bool``s must be real ``bool``s;
    ``max_evidence_sources`` a real non-negative ``int`` — a ``bool`` is rejected even
    though it is an ``int`` subclass; ``max_cost`` a real non-negative number; host
    lists coerced to ``tuple[str, ...]``).

    Frozen (like the base) so a policy is an immutable value.
    """

    independent_evidence_required: bool = True
    max_evidence_sources: int = 3
    max_cost: float = 1.0
    allowed_hosts: tuple[str, ...] = ()
    denied_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Reuse the base mode validation verbatim (no duplicated enum).
        super().__post_init__()

        # independent_evidence_required must be a real bool (bool is an int
        # subclass, so `1`/`0` are rejected — the strictness the P3 knobs use).
        if not isinstance(self.independent_evidence_required, bool):
            raise ValueError(
                "VerifyPolicy.independent_evidence_required must be a bool; got "
                f"{self.independent_evidence_required!r}"
            )

        # Budget: max_evidence_sources is a real non-negative int (reject bool).
        n = self.max_evidence_sources
        if not isinstance(n, int) or isinstance(n, bool):
            raise ValueError(
                f"VerifyPolicy.max_evidence_sources must be an int; got {n!r}"
            )
        if n < 0:
            raise ValueError(
                f"VerifyPolicy.max_evidence_sources must be >= 0; got {n!r}"
            )

        # Budget: max_cost is a real non-negative number (reject bool).
        c = self.max_cost
        if not isinstance(c, (int, float)) or isinstance(c, bool):
            raise ValueError(
                f"VerifyPolicy.max_cost must be a number; got {c!r}"
            )
        if c < 0:
            raise ValueError(f"VerifyPolicy.max_cost must be >= 0; got {c!r}")

        # Host allow/deny lists: coerce any iterable to a tuple of str, stay frozen.
        for attr_name in ("allowed_hosts", "denied_hosts"):
            hosts = getattr(self, attr_name)
            if not isinstance(hosts, tuple):
                hosts = tuple(hosts)
                object.__setattr__(self, attr_name, hosts)
            for h in hosts:
                if not isinstance(h, str):
                    raise ValueError(
                        f"VerifyPolicy.{attr_name} entries must be str; got {h!r}"
                    )

    @property
    def uri(self) -> str:
        """This policy's entity IRI: ``…/entities/VerifyPolicy/<scope_id>``.

        Overrides :attr:`PolicyBase.uri` (which is hard-scoped to the CleanPolicy
        prefix) so a VerifyPolicy is minted under its OWN ``VerifyPolicy`` resource
        namespace, never colliding with a CleanPolicy of the same
        ``(kg, type[, attr])`` scope. ``scope_id`` (from the shared
        :func:`make_policy_id`) is reused unchanged.
        """
        return VERIFY_POLICY_ENTITY_PREFIX + self.scope_id


# --------------------------------------------------------------------------- #
# Store — mirrors CleanPolicyStore's exact persistence mechanism.
# --------------------------------------------------------------------------- #
class VerifyPolicyStore:
    """Persist + read :class:`VerifyPolicy`\\ s in the tenant ontology graph.

    A direct mirror of
    :class:`cograph_client.normalization.policy.CleanPolicyStore`: each method is
    async (one Neptune round-trip) and :meth:`save` is idempotent — it clears any
    prior triples for the policy's id via the shared ``kg_writer.delete_facts``
    (subject-scoped) then writes the current field set via ``kg_writer.insert_facts``,
    so re-saving an updated policy never leaves stale field triples behind. No
    ``refresh_after_write`` — a policy row is config metadata (never instance data /
    geometry / schema), so there is no derived-index, ontology-cache, or type-stats
    fan-out to run for it (identical reasoning to the clean-policy / rule stores).

    The boolean-literal serialize/parse helpers are REUSED from
    :class:`CleanPolicyStore` (``_bool_literal`` / ``_parse_bool``) rather than
    copied — the shared surface stays shared on the store side too.
    """

    def __init__(self, neptune: NeptuneClient):
        self._neptune = neptune

    async def save(self, tenant_id: str, policy: VerifyPolicy) -> None:
        graph = tenant_graph_uri(tenant_id)
        # Clear-then-write through the shared converged write path (ADR 0007): the
        # policy is a metadata subject in the tenant ontology graph, so delete_facts
        # drops its prior field triples (subject-scoped) and insert_facts writes the
        # current set. No refresh_after_write (config metadata, not instance data).
        await delete_facts(
            self._neptune, graph, subjects=[policy.uri], reason="verify-policy upsert"
        )
        await insert_facts(self._neptune, graph, self._policy_to_triples(policy))

    async def get(self, tenant_id: str, policy_id: str) -> Optional[VerifyPolicy]:
        graph = tenant_graph_uri(tenant_id)
        uri = VERIFY_POLICY_ENTITY_PREFIX + policy_id
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
    ) -> list[VerifyPolicy]:
        """List policies, optionally filtered by KG name.

        The KG filter is applied as escaped string-literal equality in SPARQL
        (never spliced into an IRI), so it is injection-safe — same discipline as
        :meth:`CleanPolicyStore.list`.
        """
        graph = tenant_graph_uri(tenant_id)
        filters = ""
        if kg is not None:
            filters += f'  ?s <{P_KG}> "{_escape_literal(kg)}" .\n'
        q = (
            f"SELECT ?s ?p ?o FROM <{graph}> WHERE {{\n"
            f"  ?s <{RDF_TYPE}> <{VERIFY_POLICY_TYPE_URI}> .\n"
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
        out: list[VerifyPolicy] = []
        for fields in by_subject.values():
            policy = self._policy_from_fields(fields)
            if policy is not None:
                out.append(policy)
        # Stable default order: by scope id.
        out.sort(key=lambda p: p.scope_id)
        return out

    # --- serialization -------------------------------------------------------

    @classmethod
    def _policy_to_triples(cls, policy: VerifyPolicy) -> list[tuple[str, str, str]]:
        uri = policy.uri
        triples: list[tuple[str, str, str]] = [
            (uri, RDF_TYPE, VERIFY_POLICY_TYPE_URI),
            (uri, P_KG, policy.kg_name),
            (uri, P_TYPE, policy.type_name),
            (uri, P_MODE, policy.mode),
            # bool serializer reused from CleanPolicyStore (shared surface).
            (
                uri,
                P_INDEPENDENT_EVIDENCE_REQUIRED,
                CleanPolicyStore._bool_literal(policy.independent_evidence_required),
            ),
            # Typed numeric literals so a downstream query can sort/filter
            # numerically. Full XSD URIs (not the `xsd:` prefix) — `_escape_value`
            # emits `"<v>"^^<xsd-uri>` verbatim.
            (uri, P_MAX_EVIDENCE_SOURCES, f"{policy.max_evidence_sources}^^{XSD_INTEGER}"),
            (uri, P_MAX_COST, f"{policy.max_cost}^^{XSD_DECIMAL}"),
            # Host lists as JSON blobs (sorted for a stable serialization), exactly
            # how CleanPolicy stores its unknown-token set.
            (uri, P_ALLOWED_HOSTS, json.dumps(sorted(policy.allowed_hosts))),
            (uri, P_DENIED_HOSTS, json.dumps(sorted(policy.denied_hosts))),
            (uri, P_CREATED_AT, policy.created_at),
        ]
        if policy.attr:
            triples.append((uri, P_ATTR, policy.attr))
        return triples

    @classmethod
    def _policy_from_fields(cls, fields: dict[str, str]) -> Optional[VerifyPolicy]:
        if fields.get(RDF_TYPE) != VERIFY_POLICY_TYPE_URI:
            return None

        def _hosts(raw: Optional[str]) -> tuple[str, ...]:
            if not raw:
                return ()
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return ()
            if not isinstance(parsed, list):
                return ()
            return tuple(str(h) for h in parsed)

        def _int(raw: Optional[str], default: int) -> int:
            try:
                return int(str(raw))
            except (TypeError, ValueError):
                return default

        def _float(raw: Optional[str], default: float) -> float:
            try:
                return float(str(raw))
            except (TypeError, ValueError):
                return default

        return VerifyPolicy(
            kg_name=fields.get(P_KG, ""),
            type_name=fields.get(P_TYPE, ""),
            attr=fields.get(P_ATTR) or None,
            mode=fields.get(P_MODE, MODE_AUTO),  # type: ignore[arg-type]
            independent_evidence_required=CleanPolicyStore._parse_bool(
                fields.get(P_INDEPENDENT_EVIDENCE_REQUIRED), True
            ),
            max_evidence_sources=_int(fields.get(P_MAX_EVIDENCE_SOURCES), 3),
            max_cost=_float(fields.get(P_MAX_COST), 1.0),
            allowed_hosts=_hosts(fields.get(P_ALLOWED_HOSTS)),
            denied_hosts=_hosts(fields.get(P_DENIED_HOSTS)),
            created_at=fields.get(P_CREATED_AT, ""),
        )


# Re-export the shared mode axis names so a VerifyPolicy caller can reach them from
# THIS module without also importing normalization/policy.py — but they remain the
# SAME objects declared once in the base module (identity is asserted in the tests).
__all__ = [
    "PolicyBase",
    "PolicyMode",
    "POLICY_MODES",
    "VerifyPolicy",
    "VerifyPolicyStore",
    "make_policy_id",
    "VERIFY_POLICY_TYPE_URI",
    "VERIFY_POLICY_ENTITY_PREFIX",
]
