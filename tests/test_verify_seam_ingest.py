"""ONTA-370: the A4 Verify seam is wired into the live discovery ingest path,
DEFAULT-OFF.

The Wave-6 verifier (`verification/`) was fully UNWIRED — no live-path file
called `verify_clean_facts`. This wires it in `schema_resolver._resolve_and_insert`,
between the A3 clean ledger (`result.clean_report`, ONTA-373) and the shared
`insert_facts` write, gated by a per-resolver `VerifyPolicy` (DEFAULT None = OFF).

The two load-bearing regression controls (the acceptance bar, NOT decorative):

(1) DEFAULT-OFF is a byte-identical NO-OP. With no policy and no side effect the
    seam short-circuits BEFORE constructing a verifier or iterating facts — so
    the written graph is exactly what the pre-370 (== ONTA-373) write produced,
    `result.verified_facts` is empty, and a spy verifier that RAISES if touched is
    NEVER called. Zero LLM / network / cost when off.

(2) The ENABLED path produces VerifiedFacts. With a `VerifyPolicy` turned on the
    same ingest yields one `VerifiedFact` (verdict + A4 lineage) per A3 clean fact
    on `result.verified_facts` — with the OSS offline default (all UNVERIFIABLE)
    AND, when a `FactVerifier` is registered via `register_fact_verifier`, that
    verifier's verdicts. The write stays unchanged either way (verify is read-only
    and sits before the write; it does not fork the converged writer).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import time

import pytest

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.ontology_queries import attr_uri, type_uri  # noqa: E402
from cograph_client.graph.queries import kg_graph_uri  # noqa: E402
from cograph_client.resolver.models import (  # noqa: E402
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402
from cograph_client.verification.policy import VerifyPolicy  # noqa: E402
from cograph_client.verification.types import (  # noqa: E402
    EvidenceRef,
    TruthVerdict,
    VerifiedFact,
    VerifierResult,
)
from cograph_client.verification.verifier import register_fact_verifier  # noqa: E402

TENANT = "onta370"
KG = "providers"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
SRC = "https://example.com/roster"

# The SAME hand-crafted fixture ONTA-373 uses: one entity, three primitive
# attributes landing one-per-partition (PASSED / TRANSFORMED / DROPPED) so the A3
# ledger has exactly 3 clean facts to verify.
EXTRACTION = ExtractionResult(
    entities=[
        ExtractedEntity(
            type_name="Physician",
            id="dr-alice",
            attributes=[
                ExtractedAttribute(name="specialty", value="Cardiology", datatype="string"),
                ExtractedAttribute(name="years_experience", value="4.6", datatype="integer"),
                ExtractedAttribute(name="npi", value="twelve", datatype="integer"),
            ],
        ),
    ],
    relationships=[],
)
N_A3_FACTS = 3  # passed + transformed + dropped

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)

    async def batch_exists(self, sparql: str) -> set[str]:
        data = await self.query(sparql)
        rows = data.get("results", {}).get("bindings", [])
        return {r["entity"]["value"] for r in rows if "entity" in r}


@pytest.fixture(autouse=True)
def _er_disabled(monkeypatch):
    """Deterministic URIs (no signal-hash suffixes) via a scoped env set."""
    monkeypatch.setenv("COGRAPH_ER_ENABLED", "0")


@pytest.fixture(autouse=True)
def _clear_registered_verifier():
    """The fact-verifier registry is a PROCESS GLOBAL — clear it before and after
    every test so a registration never leaks across tests (or into the rest of the
    suite, which relies on the OSS offline default)."""
    register_fact_verifier(None)
    try:
        yield
    finally:
        register_fact_verifier(None)


def _make_resolver(neptune, *, verify_policy=None) -> SchemaResolver:
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"onta370_{time.time_ns()}.json"
    return SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=JsonVerdictCache(cache_path),
        embedding_service=None,
        verify_policy=verify_policy,
    )


def _stub_extract(resolver: SchemaResolver) -> None:
    async def fake_extract(content, content_type, existing_types=None, constraint=None):
        await asyncio.sleep(0)
        return EXTRACTION

    resolver._extract = fake_extract


async def _instance_triples(neptune) -> set[tuple[str, str, str]]:
    data = await neptune.query(
        f"SELECT ?s ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ ?s ?p ?o }} }}"
    )
    return {
        (b["s"]["value"], b["p"]["value"], b["o"]["value"])
        for b in data["results"]["bindings"]
    }


def _assert_write_is_pre370(triples: set[tuple[str, str, str]]) -> None:
    """The write is exactly the ONTA-373 (pre-370) write: PASSED verbatim,
    TRANSFORMED canonicalized, DROPPED absent, entity typed. Proves the seam did
    not change the graph, in either the off or the enabled (read-only) case."""
    by_pred: dict[str, set[str]] = {}
    for _s, p, o in triples:
        by_pred.setdefault(p, set()).add(o)
    assert by_pred.get(attr_uri("Physician", "specialty")) == {"Cardiology"}
    assert by_pred.get(attr_uri("Physician", "years_experience")) == {"4"}
    assert attr_uri("Physician", "npi") not in by_pred, "dropped value must never be written"
    assert (RDF_TYPE, type_uri("Physician")) in {(p, o) for (_s, p, o) in triples}


# --------------------------------------------------------------------------- #
# (1) DEFAULT-OFF: byte-identical no-op, verifier NEVER invoked.
# --------------------------------------------------------------------------- #
class _RaisingSpyVerifier:
    """A FactVerifier that FAILS the test if ever consulted. Registered on the
    DEFAULT-OFF path to prove the seam short-circuits before touching a verifier."""

    def __init__(self) -> None:
        self.called = 0

    def verify(self, fact, context=None) -> VerifierResult:  # pragma: no cover
        self.called += 1
        raise AssertionError(
            "verifier was invoked on the DEFAULT-OFF path — the seam did NOT "
            "short-circuit (this must add zero cost when no policy is configured)"
        )


@pytest.mark.asyncio
async def test_verify_seam_default_off_is_noop_and_verifier_never_called():
    """DEFAULT-OFF (no verify_policy): the seam is a provable NO-OP PASSTHROUGH.

    Even with a spy verifier REGISTERED, an ingest with no policy must never call
    it, must leave `verified_facts` empty, and must write the exact pre-370 graph.
    """
    spy = _RaisingSpyVerifier()
    register_fact_verifier(spy)  # registered but must stay untouched when off

    neptune = PyoxiNeptune()
    resolver = _make_resolver(neptune)  # verify_policy=None => OFF (the default)
    _stub_extract(resolver)

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH,
    )

    # The verifier was NEVER consulted (no LLM / cost / behavior change).
    assert spy.called == 0
    # No verify field populated — byte-identical result modulo the empty default.
    assert result.verified_facts == []
    # And the A3 ledger is still assembled (ONTA-373 not regressed).
    assert result.clean_report.counts() == {"passed": 1, "transformed": 1, "dropped": 1, "total": 3}
    # The written graph is exactly the pre-370 write.
    _assert_write_is_pre370(await _instance_triples(neptune))


@pytest.mark.asyncio
async def test_verify_seam_off_by_disabled_policy_is_noop():
    """A policy present but with mode='off' is ALSO OFF — the same duck-typed gate
    (`_policy_enabled`) the orchestrator uses. No verdicts, verifier never run."""
    spy = _RaisingSpyVerifier()
    register_fact_verifier(spy)

    neptune = PyoxiNeptune()
    off_policy = VerifyPolicy(kg_name=KG, type_name="Physician", mode="off")
    resolver = _make_resolver(neptune, verify_policy=off_policy)
    _stub_extract(resolver)

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH,
    )
    assert spy.called == 0
    assert result.verified_facts == []
    _assert_write_is_pre370(await _instance_triples(neptune))


# --------------------------------------------------------------------------- #
# (2) ENABLED: the same ingest produces VerifiedFacts on the result.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_verify_seam_enabled_offline_default_produces_verified_facts():
    """With an ENABLED VerifyPolicy (mode='auto') and NO registered verifier, the
    OSS offline default runs: one VerifiedFact per A3 clean fact, all UNVERIFIABLE,
    each stamped with an A4 lineage envelope carrying the run's workspace_id."""
    neptune = PyoxiNeptune()
    policy = VerifyPolicy(kg_name=KG, type_name="Physician", mode="auto")
    resolver = _make_resolver(neptune, verify_policy=policy)
    _stub_extract(resolver)

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH,
    )

    verified = result.verified_facts
    assert len(verified) == N_A3_FACTS
    assert all(isinstance(v, VerifiedFact) for v in verified)
    # Offline default: everything is UNVERIFIABLE (never SUPPORTED on its own source).
    assert {v.verdict for v in verified} == {TruthVerdict.UNVERIFIABLE}
    # A4 lineage: the run envelope's workspace_id (== tenant_id, ONTA-372) is
    # threaded onto every verdict, and all share ONE run_id.
    assert {v.envelope.workspace_id for v in verified} == {TENANT}
    assert len({v.envelope.run_id for v in verified}) == 1
    assert all(v.envelope.run_id for v in verified)
    # The ledger's clean facts are the ones verified (identity threads through).
    assert {v.attribute for v in verified} == {"specialty", "years_experience", "npi"}
    # Verify is read-only + before the write: the graph is unchanged.
    _assert_write_is_pre370(await _instance_triples(neptune))


class _SupportingVerifier:
    """A premium-style FactVerifier: every fact SUPPORTED by an independent source."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify(self, fact, context=None) -> VerifierResult:
        self.calls.append(fact.attribute)
        return VerifierResult(
            verdict=TruthVerdict.SUPPORTED,
            confidence=0.9,
            evidence=(EvidenceRef.from_url("https://independent.example/cite", "snippet"),),
            reason="corroborated by an independent source",
        )


@pytest.mark.asyncio
async def test_verify_seam_enabled_runs_registered_verifier():
    """With an ENABLED policy AND a registered FactVerifier, that verifier runs on
    every A3 clean fact and its verdicts flow onto the result — proving the
    register_fact_verifier plugin seam reaches the live ingest path."""
    verifier = _SupportingVerifier()
    register_fact_verifier(verifier)

    neptune = PyoxiNeptune()
    policy = VerifyPolicy(kg_name=KG, type_name="Physician", mode="auto")
    resolver = _make_resolver(neptune, verify_policy=policy)
    _stub_extract(resolver)

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH,
    )

    # The registered verifier ran once per A3 clean fact.
    assert sorted(verifier.calls) == ["npi", "specialty", "years_experience"]
    verified = result.verified_facts
    assert len(verified) == N_A3_FACTS
    assert {v.verdict for v in verified} == {TruthVerdict.SUPPORTED}
    assert all(v.confidence == pytest.approx(0.9) for v in verified)
    assert all(v.evidence and v.evidence[0].host == "independent.example" for v in verified)
    # Verify never touched the write.
    _assert_write_is_pre370(await _instance_triples(neptune))
