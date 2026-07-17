"""Tests for the per-entity VERIFY policy (ONTA-362).

Four layers, mirroring ``tests/test_clean_policy.py``:
  1. Extension + ZERO-duplication proof — ``VerifyPolicy`` EXTENDS the shared
     ``PolicyBase`` and reuses the mode axis verbatim (a bad ``mode`` raises via the
     INHERITED ``__post_init__``; the enum lives only in the base).
  2. Verify-knob axis validation — bad types on the verify knobs raise.
  3. Store round-trip — pure serialize/deserialize AND a save -> get -> list over a
     tiny in-memory fake Neptune (the same idiom ``test_clean_policy.py`` uses for
     ``CleanPolicyStore``), routed through the converged write path.
  4. Policy gates verification — ``mode == "off"`` passes every fact through
     UNVERIFIABLE without consulting the verifier; ``mode == "auto"`` + a registered
     stub verifier flips the output to SUPPORTED (the LOAD-BEARING control proving
     the policy's ON/OFF actually gates).
"""

from __future__ import annotations

import re

import pytest

from cograph_client.normalization.policy import (
    POLICY_MODES,
    CleanPolicy,
    CleanPolicyStore,
    PolicyBase,
    make_policy_id,
)
from cograph_client.resolver.models import CleanFact, CleanOutcome

# Import the sibling-owned verifier surface via its FULL submodule path (do not rely
# on / edit verification/__init__.py re-exports).
from cograph_client.verification.policy import (
    VERIFY_POLICY_ENTITY_PREFIX,
    VERIFY_POLICY_TYPE_URI,
    VerifyPolicy,
    VerifyPolicyStore,
)
from cograph_client.verification.types import TruthVerdict, VerifierResult
from cograph_client.verification.verifier import (
    register_fact_verifier,
    verify_clean_facts,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
MODE_AUTO = "auto"
MODE_OFF = "off"
MODE_ON_DEMAND = "on_demand"
TENANT = "t1"
KG = "june-16"


# --------------------------------------------------------------------------- #
# 1. Extension + ZERO-duplication proof (the ticket's headline requirement)
# --------------------------------------------------------------------------- #
def test_verify_policy_extends_policy_base():
    assert issubclass(VerifyPolicy, PolicyBase)


def test_mode_enum_lives_only_in_the_base_zero_duplication():
    # The `mode` field is declared ONLY on PolicyBase; VerifyPolicy inherits it and
    # never redeclares it in its own annotations (so the enum is single-sourced).
    assert "mode" in PolicyBase.__annotations__
    assert "mode" not in VerifyPolicy.__dict__.get("__annotations__", {})
    # And VerifyPolicy shares the SAME POLICY_MODES object as CleanPolicy — one
    # source of truth for the accepted modes, imported not copied.
    from cograph_client.normalization import policy as base_mod
    from cograph_client.verification import policy as verify_mod

    assert verify_mod.POLICY_MODES is base_mod.POLICY_MODES


def test_bad_mode_raises_via_inherited_post_init():
    # The ValueError comes from PolicyBase.__post_init__ (the ONE validator) —
    # VerifyPolicy adds no mode logic of its own.
    with pytest.raises(ValueError, match="mode"):
        VerifyPolicy(kg_name=KG, type_name="Person", mode="whenever")


@pytest.mark.parametrize("mode", sorted(POLICY_MODES))
def test_valid_modes_accepted(mode):
    v = VerifyPolicy(kg_name=KG, type_name="Person", mode=mode)
    assert v.mode == mode


def test_both_policies_share_one_mode_source_of_truth():
    # The accepted-mode set is literally the ONE POLICY_MODES constant for both.
    for cls in (CleanPolicy, VerifyPolicy):
        for mode in POLICY_MODES:
            assert cls(kg_name=KG, type_name="Person", mode=mode).mode == mode
        with pytest.raises(ValueError):
            cls(kg_name=KG, type_name="Person", mode="not-a-mode")


# --------------------------------------------------------------------------- #
# 2. VerifyPolicy shape + verify-knob validation
# --------------------------------------------------------------------------- #
def test_defaults():
    v = VerifyPolicy(kg_name=KG, type_name="Person")
    assert v.mode == MODE_AUTO
    assert v.attr is None
    assert v.independent_evidence_required is True
    assert v.max_evidence_sources == 3
    assert v.max_cost == 1.0
    assert v.allowed_hosts == ()
    assert v.denied_hosts == ()
    # identity + uri derive from the (kg, type[, attr]) scope, under the VerifyPolicy
    # resource namespace (NOT the CleanPolicy one).
    assert v.scope_id == make_policy_id(KG, "Person")
    assert v.uri == VERIFY_POLICY_ENTITY_PREFIX + v.scope_id
    assert v.uri.endswith("/VerifyPolicy/" + v.scope_id)


def test_uri_namespace_distinct_from_clean_policy():
    # Same scope, different resource namespace → no collision in one ontology graph.
    v = VerifyPolicy(kg_name=KG, type_name="Person")
    c = CleanPolicy(kg_name=KG, type_name="Person")
    assert v.scope_id == c.scope_id
    assert v.uri != c.uri
    assert "/VerifyPolicy/" in v.uri and "/CleanPolicy/" in c.uri


def test_attr_scoped_id_distinct_from_type_wide():
    type_wide = VerifyPolicy(kg_name=KG, type_name="Person")
    per_attr = VerifyPolicy(kg_name=KG, type_name="Person", attr="email")
    assert type_wide.scope_id != per_attr.scope_id
    assert per_attr.scope_id == make_policy_id(KG, "Person", "email")


def test_independent_evidence_required_must_be_bool():
    # bool is an int subclass, so a truthy int is NOT a bool — rejected on purpose.
    with pytest.raises(ValueError, match="independent_evidence_required"):
        VerifyPolicy(kg_name=KG, type_name="Person", independent_evidence_required=1)


@pytest.mark.parametrize("bad", [True, 2.0, "3", None])
def test_max_evidence_sources_must_be_int(bad):
    with pytest.raises(ValueError, match="max_evidence_sources"):
        VerifyPolicy(kg_name=KG, type_name="Person", max_evidence_sources=bad)


def test_max_evidence_sources_must_be_non_negative():
    with pytest.raises(ValueError, match="max_evidence_sources"):
        VerifyPolicy(kg_name=KG, type_name="Person", max_evidence_sources=-1)


@pytest.mark.parametrize("bad", [True, "1.0", None])
def test_max_cost_must_be_a_number(bad):
    with pytest.raises(ValueError, match="max_cost"):
        VerifyPolicy(kg_name=KG, type_name="Person", max_cost=bad)


def test_max_cost_must_be_non_negative():
    with pytest.raises(ValueError, match="max_cost"):
        VerifyPolicy(kg_name=KG, type_name="Person", max_cost=-0.5)


def test_max_cost_accepts_int_and_float():
    assert VerifyPolicy(kg_name=KG, type_name="Person", max_cost=0).max_cost == 0
    assert VerifyPolicy(kg_name=KG, type_name="Person", max_cost=2).max_cost == 2
    assert VerifyPolicy(kg_name=KG, type_name="Person", max_cost=0.25).max_cost == 0.25


def test_hosts_coerced_to_tuple():
    v = VerifyPolicy(
        kg_name=KG,
        type_name="Person",
        allowed_hosts=["a.com", "b.com"],
        denied_hosts=["evil.example"],
    )
    assert isinstance(v.allowed_hosts, tuple)
    assert v.allowed_hosts == ("a.com", "b.com")
    assert isinstance(v.denied_hosts, tuple)
    assert v.denied_hosts == ("evil.example",)


def test_non_str_host_rejected():
    with pytest.raises(ValueError, match="allowed_hosts"):
        VerifyPolicy(kg_name=KG, type_name="Person", allowed_hosts=[123])
    with pytest.raises(ValueError, match="denied_hosts"):
        VerifyPolicy(kg_name=KG, type_name="Person", denied_hosts=[object()])


def test_policy_is_frozen():
    v = VerifyPolicy(kg_name=KG, type_name="Person")
    with pytest.raises(Exception):
        v.mode = MODE_OFF  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 3a. Pure serialize/deserialize round-trip (no Neptune)
# --------------------------------------------------------------------------- #
def _lexical(obj: str) -> str:
    """Mimic the store read path: SPARQL returns the lexical form of a typed
    literal (``"3"^^<…integer>`` binds as ``"3"``)."""
    if "^^" in obj:
        return obj.rsplit("^^", 1)[0]
    return obj


def test_serialize_deserialize_roundtrip():
    v = VerifyPolicy(
        kg_name=KG,
        type_name="Person",
        attr="email",
        mode=MODE_ON_DEMAND,
        independent_evidence_required=False,
        max_evidence_sources=5,
        max_cost=0.75,
        allowed_hosts=("wikipedia.org", "irs.gov"),
        denied_hosts=("spam.example",),
    )
    triples = VerifyPolicyStore._policy_to_triples(v)
    fields = {pred: _lexical(obj) for (_s, pred, obj) in triples}
    back = VerifyPolicyStore._policy_from_fields(fields)
    assert back is not None
    assert back.kg_name == KG
    assert back.type_name == "Person"
    assert back.attr == "email"
    assert back.mode == MODE_ON_DEMAND
    assert back.independent_evidence_required is False
    assert back.max_evidence_sources == 5
    assert back.max_cost == 0.75
    assert back.allowed_hosts == ("irs.gov", "wikipedia.org")  # stored sorted
    assert back.denied_hosts == ("spam.example",)


def test_from_fields_rejects_wrong_rdf_type():
    # A subject that is not rdf:type VerifyPolicy never deserializes as one.
    assert VerifyPolicyStore._policy_from_fields({RDF_TYPE: "https://x/Other"}) is None


# --------------------------------------------------------------------------- #
# 3b. Store save -> get -> list over an in-memory fake Neptune
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_store_roundtrip():
    neptune = _FakeNeptune()
    store = VerifyPolicyStore(neptune)
    policy = VerifyPolicy(
        kg_name=KG,
        type_name="Person",
        attr="email",
        mode=MODE_ON_DEMAND,
        max_evidence_sources=4,
        max_cost=2.5,
        allowed_hosts=("wikipedia.org",),
    )
    await store.save(TENANT, policy)

    got = await store.get(TENANT, policy.scope_id)
    assert got is not None
    assert got.kg_name == KG
    assert got.type_name == "Person"
    assert got.attr == "email"
    assert got.mode == MODE_ON_DEMAND
    assert got.independent_evidence_required is True  # default preserved
    assert got.max_evidence_sources == 4
    assert got.max_cost == 2.5
    assert got.allowed_hosts == ("wikipedia.org",)
    assert got == policy  # full value equality

    listed = await store.list(TENANT, kg=KG)
    assert len(listed) == 1 and listed[0].scope_id == policy.scope_id

    # Wrong-KG filter returns nothing.
    assert await store.list(TENANT, kg="other-kg") == []

    # get of a missing policy returns None.
    assert await store.get(TENANT, "nope") is None


@pytest.mark.asyncio
async def test_store_save_is_idempotent_no_stale_fields():
    neptune = _FakeNeptune()
    store = VerifyPolicyStore(neptune)
    policy = VerifyPolicy(kg_name=KG, type_name="Person", mode=MODE_AUTO)
    await store.save(TENANT, policy)

    # Re-save with a flipped mode: the clear-then-write upsert must leave exactly
    # one mode triple (no stale "auto" left behind).
    from dataclasses import replace

    await store.save(TENANT, replace(policy, mode=MODE_OFF))

    graph = neptune.quads["https://cograph.tech/graphs/t1"]
    mode_triples = [t for t in graph if t[0] == policy.uri and t[1].endswith("/mode")]
    assert len(mode_triples) == 1 and mode_triples[0][2] == MODE_OFF

    got = await store.get(TENANT, policy.scope_id)
    assert got is not None and got.mode == MODE_OFF


@pytest.mark.asyncio
async def test_store_lists_only_verify_policy_typed_subjects():
    """The list query is typed on <VerifyPolicy>, so neither an unrelated subject nor
    a CleanPolicy sharing the ontology graph is ever mistaken for a VerifyPolicy."""
    neptune = _FakeNeptune()
    g = neptune.quads.setdefault("https://cograph.tech/graphs/t1", set())
    g.add(
        ("https://cograph.tech/entities/Person/1", RDF_TYPE, "https://cograph.tech/types/Person")
    )
    # A CleanPolicy in the same graph must NOT be listed as a VerifyPolicy.
    await CleanPolicyStore(neptune).save(TENANT, CleanPolicy(kg_name=KG, type_name="Person"))

    store = VerifyPolicyStore(neptune)
    await store.save(TENANT, VerifyPolicy(kg_name=KG, type_name="Person"))
    listed = await store.list(TENANT)
    assert len(listed) == 1 and listed[0].type_name == "Person"
    assert all(p.uri.startswith(VERIFY_POLICY_ENTITY_PREFIX) for p in listed)


# --------------------------------------------------------------------------- #
# 4. Policy gates verification (the P4 wiring proof)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_verifier_registry():
    """Never leak a registered verifier across tests."""
    register_fact_verifier(None)
    yield
    register_fact_verifier(None)


def _clean(entity_id="W1", attribute="rating", datatype="float",
           raw_value="4.6", clean_value="4"):
    return CleanFact(
        datatype=datatype,
        raw_value=raw_value,
        clean_value=clean_value,
        outcome=CleanOutcome.TRANSFORMED,
        entity_id=entity_id,
        attribute=attribute,
    )


class _SupportingStub:
    """A verifier that ALWAYS returns SUPPORTED and records whether it was consulted —
    so ON/OFF gating is observable both by the flipped verdict AND the call count."""

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, fact, context=None) -> VerifierResult:
        self.calls += 1
        return VerifierResult(
            verdict=TruthVerdict.SUPPORTED,
            confidence=0.9,
            reason="stub says supported",
        )


def test_verify_policy_off_passes_through_without_consulting_verifier():
    """VerifyPolicy(mode="off") gates verification OFF: every fact passes through
    UNVERIFIABLE and the registered verifier is NEVER consulted."""
    stub = _SupportingStub()
    register_fact_verifier(stub)

    facts = [_clean(), _clean(entity_id="W2", attribute="name")]
    out = verify_clean_facts(facts, VerifyPolicy(kg_name=KG, type_name="Widget", mode="off"))

    assert len(out) == 2
    for v in out:
        assert v.verdict is TruthVerdict.UNVERIFIABLE
    # LOAD-BEARING: the verifier that WOULD have said SUPPORTED was not called at all.
    assert stub.calls == 0


def test_verify_policy_auto_gates_verification_on():
    """LOAD-BEARING control: with VerifyPolicy(mode="auto") AND a stub verifier
    registered (returns SUPPORTED), verify_clean_facts output CHANGES from the OFF
    passthrough (UNVERIFIABLE) to SUPPORTED — proving the policy's mode actually
    gates verification ON, and the same registered verifier is consulted."""
    facts = [_clean()]

    # Baseline: OFF → UNVERIFIABLE (verifier not consulted).
    off_stub = _SupportingStub()
    register_fact_verifier(off_stub)
    off = verify_clean_facts(facts, VerifyPolicy(kg_name=KG, type_name="Widget", mode="off"))
    assert off[0].verdict is TruthVerdict.UNVERIFIABLE
    assert off_stub.calls == 0

    # Same stub, mode flipped to auto → the verdict CHANGES to SUPPORTED.
    on_stub = _SupportingStub()
    register_fact_verifier(on_stub)
    on = verify_clean_facts(facts, VerifyPolicy(kg_name=KG, type_name="Widget", mode="auto"))
    assert on[0].verdict is TruthVerdict.SUPPORTED
    assert on[0].confidence == 0.9
    assert on_stub.calls == 1
    # The output visibly differs between the two policy modes.
    assert on[0].verdict != off[0].verdict


# --------------------------------------------------------------------------- #
# In-memory fake Neptune: evaluates only the SPARQL shapes the policy stores emit
# (INSERT DATA, subject-scoped DELETE + its COUNT, get SELECT ?p ?o, list
# SELECT ?s ?p ?o typed on <VerifyPolicy>/<CleanPolicy>). Not a general engine —
# mirrors the FakeNeptune idiom in tests/test_clean_policy.py verbatim.
# --------------------------------------------------------------------------- #
_TERM = r'(<[^>]+>|"(?:[^"\\]|\\.)*"(?:\^\^<[^>]+>)?)'


class _FakeNeptune:
    def __init__(self) -> None:
        self.quads: dict[str, set[tuple[str, str, str]]] = {}

    def _g(self, uri: str) -> set:
        return self.quads.setdefault(uri, set())

    @staticmethod
    def _graph(sparql: str) -> str:
        m = re.search(r"GRAPH <([^>]+)>", sparql) or re.search(r"FROM <([^>]+)>", sparql)
        return m.group(1) if m else ""

    async def update(self, sparql: str) -> None:
        op = sparql.strip()
        graph = self._graph(op)
        if op.startswith("INSERT DATA"):
            for t in _parse_data_triples(op):
                self._g(graph).add(t)
        elif op.startswith("DELETE"):
            vals = re.search(r"VALUES \?s \{([^}]*)\}", op)
            if vals and "?s ?p ?o" in op:
                subs = set(re.findall(r"<([^>]+)>", vals.group(1)))
                for t in list(self._g(graph)):
                    if t[0] in subs:
                        self._g(graph).discard(t)

    async def query(self, sparql: str) -> dict:
        rows = self._select(sparql, self._g(self._graph(sparql)))
        variables: list[str] = []
        for r in rows:
            for k in r:
                if k not in variables:
                    variables.append(k)
        return {
            "head": {"vars": variables},
            "results": {"bindings": [{k: {"value": v} for k, v in r.items()} for r in rows]},
        }

    def _select(self, sparql: str, quads: set) -> list[dict]:
        # delete_facts removed-count.
        if "COUNT(*)" in sparql:
            vals = re.search(r"VALUES \?s \{([^}]*)\}", sparql)
            subs = set(re.findall(r"<([^>]+)>", vals.group(1))) if vals else set()
            return [{"n": str(sum(1 for (s, _p, _o) in quads if s in subs))}]
        # get: SELECT ?p ?o WHERE { <uri> ?p ?o }
        m = re.search(r"SELECT \?p \?o FROM <[^>]+> WHERE \{\s*<([^>]+)> \?p \?o", sparql)
        if m:
            uri = m.group(1)
            return [{"p": p, "o": o} for (s, p, o) in quads if s == uri]
        # list: SELECT ?s ?p ?o ... ?s rdf:type <SomeType> [filters] ?s ?p ?o
        if "SELECT ?s ?p ?o" in sparql:
            m2 = re.search(r"\?s <" + re.escape(RDF_TYPE) + r"> <([^>]+)>", sparql)
            type_uri = m2.group(1) if m2 else None
            subjects = {s for (s, p, o) in quads if p == RDF_TYPE and o == type_uri}
            for fp, fv in re.findall(r'\?s <([^>]+)> "([^"]*)" \.', sparql):
                subjects = {s for s in subjects if (s, fp, fv) in quads}
            return [{"s": s, "p": p, "o": o} for (s, p, o) in quads if s in subjects]
        return []


def _parse_data_triples(op: str) -> list[tuple[str, str, str]]:
    body = re.search(r"GRAPH <[^>]+> \{(.*)\}\s*\}", op, re.DOTALL)
    if not body:
        return []
    out = []
    for line in body.group(1).split("\n"):
        line = line.strip().rstrip(".").strip()
        if not line:
            continue
        m = re.match(rf"^{_TERM}\s+{_TERM}\s+(.*)$", line)
        if m:
            out.append((_term(m.group(1)), _term(m.group(2)), _term(m.group(3).strip())))
    return out


def _term(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        return raw[1:-1]
    if raw.startswith('"'):
        if "^^" in raw:
            raw = raw.rsplit("^^", 1)[0].strip()
        inner = raw[1:-1] if raw.endswith('"') else raw[1:]
        return inner.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
    return raw
