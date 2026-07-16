"""Tests for the per-entity clean/verify policy (ONTA-348).

Four layers:
  1. CleanPolicy value shape — defaults + ``__post_init__`` axis/knob validation.
  2. apply_clean_policy behavior — unknown token dropped-with-reason; a normal
     value passed/canonicalized per toggles; mode == "off" is a pass-through.
  3. Store round-trip — pure serialize/deserialize AND a save -> get -> list over a
     tiny in-memory fake Neptune that evaluates the handful of SPARQL shapes the
     store emits (mirroring test_normalization.py's rule-store roundtrip).
  4. Shared-shape proof — a minimal P4-style VerifyPolicy extends the SAME
     PolicyBase and reuses the mode axis with ZERO duplication of the enum.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from cograph_client.normalization.policy import (
    DEFAULT_UNKNOWN_TOKENS,
    MODE_AUTO,
    MODE_OFF,
    MODE_ON_DEMAND,
    POLICY_MODES,
    POLICY_TYPE_URI,
    REASON_CANONICALIZED,
    REASON_MODE_OFF,
    REASON_UNCHANGED,
    REASON_UNKNOWN_TOKEN,
    CleanPolicy,
    CleanPolicyStore,
    PolicyBase,
    apply_clean_policy,
    make_policy_id,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TENANT = "t1"
KG = "june-16"


# --------------------------------------------------------------------------- #
# 1. CleanPolicy shape + validation
# --------------------------------------------------------------------------- #
def test_defaults():
    p = CleanPolicy(kg_name=KG, type_name="Person")
    assert p.mode == MODE_AUTO
    assert p.attr is None
    assert p.trim is True
    assert p.collapse_whitespace is True
    assert p.casefold is False
    assert p.nfc is True
    assert p.unknown_tokens == DEFAULT_UNKNOWN_TOKENS
    # identity + uri derive from the (kg, type[, attr]) scope
    assert p.scope_id == make_policy_id(KG, "Person")
    assert p.uri.endswith("/CleanPolicy/" + p.scope_id)


def test_attr_scoped_id_distinct_from_type_wide():
    type_wide = CleanPolicy(kg_name=KG, type_name="Person")
    per_attr = CleanPolicy(kg_name=KG, type_name="Person", attr="email")
    assert type_wide.scope_id != per_attr.scope_id
    assert per_attr.scope_id == make_policy_id(KG, "Person", "email")


@pytest.mark.parametrize("mode", sorted(POLICY_MODES))
def test_valid_modes_accepted(mode):
    assert CleanPolicy(kg_name=KG, type_name="Person", mode=mode).mode == mode


def test_invalid_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        CleanPolicy(kg_name=KG, type_name="Person", mode="sometimes")


@pytest.mark.parametrize("knob", ["trim", "collapse_whitespace", "casefold", "nfc"])
def test_invalid_knob_rejected(knob):
    # bool is an int subclass, so a truthy int is NOT a bool — rejected on purpose.
    with pytest.raises(ValueError, match=knob):
        CleanPolicy(kg_name=KG, type_name="Person", **{knob: 1})


def test_unknown_tokens_coerced_to_frozenset():
    p = CleanPolicy(kg_name=KG, type_name="Person", unknown_tokens={"tbd", "TBD"})
    assert isinstance(p.unknown_tokens, frozenset)
    assert p.unknown_tokens == frozenset({"tbd", "TBD"})


def test_policy_is_frozen():
    p = CleanPolicy(kg_name=KG, type_name="Person")
    with pytest.raises(Exception):
        p.mode = MODE_OFF  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 2. apply_clean_policy behavior
# --------------------------------------------------------------------------- #
def test_unknown_token_dropped_with_reason():
    p = CleanPolicy(kg_name=KG, type_name="Person")
    out = apply_clean_policy("N/A", p)  # matches "n/a" case-insensitively
    assert out.dropped is True
    assert out.value is None
    assert out.reason == REASON_UNKNOWN_TOKEN


def test_unknown_token_matches_around_whitespace():
    p = CleanPolicy(kg_name=KG, type_name="Person")
    assert apply_clean_policy("  none  ", p).dropped is True


def test_normal_value_canonicalized():
    p = CleanPolicy(kg_name=KG, type_name="Person")  # trim + collapse_ws default on
    out = apply_clean_policy("  Ada   Lovelace  ", p)
    assert out.dropped is False
    assert out.value == "Ada Lovelace"
    assert out.changed is True
    assert out.reason == REASON_CANONICALIZED


def test_already_canonical_value_unchanged():
    p = CleanPolicy(kg_name=KG, type_name="Person")
    out = apply_clean_policy("Ada Lovelace", p)
    assert out.value == "Ada Lovelace"
    assert out.changed is False
    assert out.reason == REASON_UNCHANGED


def test_casefold_toggle():
    p = CleanPolicy(kg_name=KG, type_name="Person", casefold=True)
    assert apply_clean_policy("HeLLo", p).value == "hello"


def test_toggles_off_is_verbatim():
    p = CleanPolicy(
        kg_name=KG,
        type_name="Person",
        trim=False,
        collapse_whitespace=False,
        casefold=False,
        nfc=False,
    )
    out = apply_clean_policy("  A  B  ", p)
    assert out.value == "  A  B  "
    assert out.changed is False


def test_mode_off_is_passthrough():
    # A value that WOULD be dropped/cleaned is left untouched when mode == off.
    p = CleanPolicy(kg_name=KG, type_name="Person", mode=MODE_OFF)
    out = apply_clean_policy("  N/A  ", p)
    assert out.dropped is False
    assert out.value == "  N/A  "
    assert out.reason == REASON_MODE_OFF


def test_nfc_normalization():
    # "e" + combining acute accent (NFD) -> single precomposed "é" (NFC).
    p = CleanPolicy(kg_name=KG, type_name="Person", collapse_whitespace=False)
    out = apply_clean_policy("é", p)
    assert out.value == "é"
    assert out.changed is True


# --------------------------------------------------------------------------- #
# 3a. Pure serialize/deserialize round-trip (no Neptune)
# --------------------------------------------------------------------------- #
def test_serialize_deserialize_roundtrip():
    p = CleanPolicy(
        kg_name=KG,
        type_name="Person",
        attr="email",
        mode=MODE_ON_DEMAND,
        trim=False,
        collapse_whitespace=True,
        casefold=True,
        nfc=False,
        unknown_tokens={"n/a", "tbd"},
    )
    triples = CleanPolicyStore._policy_to_triples(p)
    # The lexical object of each field triple, as parse_sparql_results would yield
    # (datatype suffix stripped — the store reads the lexical form).
    fields = {pred: _lexical(obj) for (_s, pred, obj) in triples}
    back = CleanPolicyStore._policy_from_fields(fields)
    assert back is not None
    assert back.kg_name == KG
    assert back.type_name == "Person"
    assert back.attr == "email"
    assert back.mode == MODE_ON_DEMAND
    assert back.trim is False
    assert back.collapse_whitespace is True
    assert back.casefold is True
    assert back.nfc is False
    assert back.unknown_tokens == frozenset({"n/a", "tbd"})


def _lexical(obj: str) -> str:
    """Mimic the store read path: SPARQL returns the lexical form of a typed
    literal (``"true"^^<…boolean>`` binds as ``"true"``)."""
    if "^^" in obj:
        return obj.rsplit("^^", 1)[0]
    return obj


# --------------------------------------------------------------------------- #
# 3b. Store save -> get -> list over an in-memory fake Neptune
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_store_roundtrip():
    neptune = _FakeNeptune()
    store = CleanPolicyStore(neptune)
    policy = CleanPolicy(
        kg_name=KG,
        type_name="Person",
        attr="email",
        mode=MODE_ON_DEMAND,
        casefold=True,
        unknown_tokens={"n/a", "unknown"},
    )
    await store.save(TENANT, policy)

    got = await store.get(TENANT, policy.scope_id)
    assert got is not None
    assert got.kg_name == KG
    assert got.type_name == "Person"
    assert got.attr == "email"
    assert got.mode == MODE_ON_DEMAND
    assert got.casefold is True
    assert got.trim is True  # default preserved
    assert got.unknown_tokens == frozenset({"n/a", "unknown"})

    listed = await store.list(TENANT, kg=KG)
    assert len(listed) == 1 and listed[0].scope_id == policy.scope_id

    # Wrong-KG filter returns nothing.
    assert await store.list(TENANT, kg="other-kg") == []

    # get of a missing policy returns None.
    assert await store.get(TENANT, "nope") is None


@pytest.mark.asyncio
async def test_store_save_is_idempotent_no_stale_fields():
    neptune = _FakeNeptune()
    store = CleanPolicyStore(neptune)
    policy = CleanPolicy(kg_name=KG, type_name="Person", mode=MODE_AUTO)
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
async def test_store_lists_only_policy_typed_subjects():
    """The list query is typed on <CleanPolicy>, so an unrelated subject sharing
    the ontology graph is never mistaken for a policy."""
    neptune = _FakeNeptune()
    neptune.quads.setdefault("https://cograph.tech/graphs/t1", set()).add(
        ("https://cograph.tech/entities/Person/1", RDF_TYPE, "https://cograph.tech/types/Person")
    )
    store = CleanPolicyStore(neptune)
    await store.save(TENANT, CleanPolicy(kg_name=KG, type_name="Person"))
    listed = await store.list(TENANT)
    assert len(listed) == 1 and listed[0].type_name == "Person"


# --------------------------------------------------------------------------- #
# 4. Shared-shape proof: P4 VerifyPolicy extends the SAME base, zero duplication
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _VerifyPolicyStub(PolicyBase):
    """A minimal stand-in for the future P4 VerifyPolicy: it EXTENDS PolicyBase
    with verify-only knobs and reuses the shared ``mode`` axis verbatim. It does
    NOT import or redeclare PolicyMode / POLICY_MODES."""

    min_confidence: float = 0.7
    require_authority: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()  # the SAME mode validation, reused


def test_verify_stub_shares_the_mode_axis():
    # Both policies descend from the ONE base that owns `mode`.
    assert issubclass(CleanPolicy, PolicyBase)
    assert issubclass(_VerifyPolicyStub, PolicyBase)

    # ZERO enum duplication: only PolicyBase declares the `mode` field; neither
    # subclass redeclares it in its own annotations.
    assert "mode" in PolicyBase.__annotations__
    assert "mode" not in CleanPolicy.__dict__.get("__annotations__", {})
    assert "mode" not in _VerifyPolicyStub.__dict__.get("__annotations__", {})


@pytest.mark.parametrize("mode", sorted(POLICY_MODES))
def test_verify_stub_accepts_same_valid_modes(mode):
    v = _VerifyPolicyStub(kg_name=KG, type_name="Person", mode=mode)
    assert v.mode == mode
    # And it carries its own extension fields without touching the mode shape.
    assert v.min_confidence == 0.7


def test_verify_stub_rejects_invalid_mode_via_shared_validator():
    # Same base validator → same ValueError as CleanPolicy for a bogus mode.
    with pytest.raises(ValueError, match="mode"):
        _VerifyPolicyStub(kg_name=KG, type_name="Person", mode="whenever")


def test_both_policies_share_one_mode_source_of_truth():
    # The accepted-mode set is literally the ONE POLICY_MODES constant for both.
    for cls in (CleanPolicy, _VerifyPolicyStub):
        for mode in POLICY_MODES:
            assert cls(kg_name=KG, type_name="Person", mode=mode).mode == mode
        with pytest.raises(ValueError):
            cls(kg_name=KG, type_name="Person", mode="not-a-mode")


# --------------------------------------------------------------------------- #
# In-memory fake Neptune: evaluates only the SPARQL shapes CleanPolicyStore emits
# (INSERT DATA, subject-scoped DELETE + its COUNT, get SELECT ?p ?o, list
# SELECT ?s ?p ?o typed on <CleanPolicy>). Not a general engine — just enough,
# mirroring the FakeNeptune idiom in tests/test_normalization.py.
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
        # list: SELECT ?s ?p ?o ... ?s rdf:type <CleanPolicy> [filters] ?s ?p ?o
        if "SELECT ?s ?p ?o" in sparql and POLICY_TYPE_URI in sparql:
            subjects = {s for (s, p, o) in quads if p == RDF_TYPE and o == POLICY_TYPE_URI}
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
