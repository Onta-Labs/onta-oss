"""ONTA-375: persist the A4 TruthVerdict as a companion + read it at answer time.

Two halves, each with a load-bearing regression control:

(a) PERSIST — when the ONTA-370 A4 Verify seam produced ``verified_facts`` (a
    ``VerifyPolicy`` is enabled), the discovery write stamps each WRITTEN fact's
    epistemic ``TruthVerdict`` as a per-attribute ``attr_meta/`` companion. The
    control asserts the companion (i) lands on an INTERNAL predicate
    (``is_internal_predicate`` True — so it is invisible to Explorer / type-stats /
    NL dumps), (ii) is minted by the SHARED ``graph/provenance.py`` minter (not a
    bespoke triple), (iii) is QUERYABLE, and (iv) is EXCLUDED where the surface-form
    / confidence companions are excluded. DROPPED facts (no domain triple) get none.

(b) READ — ``build_citations`` reads that companion into a NEW ``FactCitation``
    field (``truth_verdict``), DISTINCT from the recency/validity ``verdict``. The
    control asserts BOTH fields are present and independently populated (a fact can
    be recency ``superseded`` AND epistemic ``supported`` at once), and a fact with
    no companion reads ``truth_verdict == ""`` (never fabricated).

(c) DEFAULT byte-identical — no ``verified_facts`` (the default, verify off) ⇒ NO
    verdict companion is written (the graph is identical to pre-375); and the new
    ``FactCitation`` field defaults empty so the answer path is back-compatible.
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
from cograph_client.graph.predicates import is_internal_predicate  # noqa: E402
from cograph_client.graph.provenance import (  # noqa: E402
    TRUTH_VERDICT_SUFFIX,
    attr_provenance_companion_uri,
    build_truth_verdict_companion,
    companion_predicate_for,
)
from cograph_client.graph.queries import kg_graph_uri  # noqa: E402
from cograph_client.models.query import FactCitation  # noqa: E402
from cograph_client.nlp.answer_meta import build_citations  # noqa: E402
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
    VerifierResult,
)
from cograph_client.verification.verifier import register_fact_verifier  # noqa: E402

TENANT = "onta375"
KG = "providers"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
SRC = "https://example.com/roster"

# Same hand-crafted fixture as ONTA-370/373: one entity, three primitive attributes
# landing one-per-partition (PASSED / TRANSFORMED / DROPPED).
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

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TRUTH_VERDICT_PRED_SPECIALTY = attr_provenance_companion_uri(
    "Physician", "specialty", TRUTH_VERDICT_SUFFIX
)


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
    """Deterministic URIs (no signal-hash suffixes)."""
    monkeypatch.setenv("COGRAPH_ER_ENABLED", "0")


@pytest.fixture(autouse=True)
def _clear_registered_verifier():
    """The fact-verifier registry is a PROCESS GLOBAL — clear before and after."""
    register_fact_verifier(None)
    try:
        yield
    finally:
        register_fact_verifier(None)


def _make_resolver(neptune, *, verify_policy=None) -> SchemaResolver:
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"onta375_{time.time_ns()}.json"
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


class _SupportingVerifier:
    """A premium-style FactVerifier: every fact SUPPORTED by an independent source."""

    def verify(self, fact, context=None) -> VerifierResult:
        return VerifierResult(
            verdict=TruthVerdict.SUPPORTED,
            confidence=0.9,
            evidence=(EvidenceRef.from_url("https://independent.example/cite", "snip"),),
            reason="corroborated by an independent source",
        )


# --------------------------------------------------------------------------- #
# (a) PERSIST — enabled seam stamps a queryable, internal-predicate companion.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_persist_verdict_companion_on_internal_predicate_via_shared_minter():
    register_fact_verifier(_SupportingVerifier())
    neptune = PyoxiNeptune()
    policy = VerifyPolicy(kg_name=KG, type_name="Physician", mode="auto")
    resolver = _make_resolver(neptune, verify_policy=policy)
    _stub_extract(resolver)

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC, instance_graph=INSTANCE_GRAPH,
    )
    # The seam ran and produced verdicts (guard: the persist has something to do).
    assert len(result.verified_facts) == 3

    triples = await _instance_triples(neptune)
    entity_uri = "https://cograph.tech/entities/Physician/dr-alice"

    # (i) The verdict companion for each WRITTEN fact (PASSED + TRANSFORMED) is
    #     present with the SUPPORTED verdict, keyed by (subject, Type, attribute).
    verdict_triples = {
        (s, p, o) for (s, p, o) in triples if p.endswith(f"/{TRUTH_VERDICT_SUFFIX}")
    }
    assert (entity_uri, TRUTH_VERDICT_PRED_SPECIALTY, TruthVerdict.SUPPORTED.value) in verdict_triples
    years_pred = attr_provenance_companion_uri("Physician", "years_experience", TRUTH_VERDICT_SUFFIX)
    assert (entity_uri, years_pred, TruthVerdict.SUPPORTED.value) in verdict_triples
    # DROPPED npi (no domain triple was written) gets NO verdict companion.
    npi_pred = attr_provenance_companion_uri("Physician", "npi", TRUTH_VERDICT_SUFFIX)
    assert not any(p == npi_pred for (_s, p, _o) in verdict_triples)

    # (ii) It comes from the SHARED provenance minter, NOT a bespoke triple.
    assert TRUTH_VERDICT_PRED_SPECIALTY == attr_provenance_companion_uri(
        "Physician", "specialty", TRUTH_VERDICT_SUFFIX
    )
    assert build_truth_verdict_companion(
        entity_uri, "Physician", "specialty", TruthVerdict.SUPPORTED.value
    ) == [(entity_uri, TRUTH_VERDICT_PRED_SPECIALTY, TruthVerdict.SUPPORTED.value)]

    # (iii) It is an INTERNAL predicate — invisible where surface-form / confidence
    #       companions are excluded (whole attr_meta/ namespace), on every surface.
    for (_s, p, _o) in verdict_triples:
        assert is_internal_predicate(p) is True
        assert is_internal_predicate(p, is_relationship=True) is True

    # (iv) The DOMAIN facts themselves stay visible (the companion hid nothing): the
    #      canonicalized attribute values are present on the real attribute predicate.
    by_pred: dict[str, set[str]] = {}
    for (_s, p, o) in triples:
        by_pred.setdefault(p, set()).add(o)
    assert by_pred.get(attr_uri("Physician", "specialty")) == {"Cardiology"}
    assert by_pred.get(attr_uri("Physician", "years_experience")) == {"4"}
    assert is_internal_predicate(attr_uri("Physician", "specialty")) is False


# --------------------------------------------------------------------------- #
# (c) DEFAULT byte-identical — no policy ⇒ no verified_facts ⇒ no companion.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_default_off_writes_no_verdict_companion():
    neptune = PyoxiNeptune()
    resolver = _make_resolver(neptune)  # verify_policy=None => OFF (the default)
    _stub_extract(resolver)

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC, instance_graph=INSTANCE_GRAPH,
    )
    assert result.verified_facts == []

    triples = await _instance_triples(neptune)
    # Not a single truth-verdict companion anywhere: byte-identical to the pre-375
    # write. (Pre-375 surface-form companions on attr_meta/ for the TRANSFORMED value
    # legitimately remain — 375 adds ONLY the truth_verdict companion, and only when
    # the seam runs.) The domain facts landed exactly as before.
    assert not any(p.endswith(f"/{TRUTH_VERDICT_SUFFIX}") for (_s, p, _o) in triples)
    by_pred: dict[str, set[str]] = {}
    for (_s, p, o) in triples:
        by_pred.setdefault(p, set()).add(o)
    assert by_pred.get(attr_uri("Physician", "specialty")) == {"Cardiology"}
    assert attr_uri("Physician", "npi") not in by_pred  # dropped, unwritten


def test_fact_citation_new_field_defaults_empty_and_is_additive():
    """(c) READ back-compat: the new epistemic field is additive — a default
    FactCitation carries an empty ``truth_verdict`` and the recency ``verdict`` is a
    SEPARATE field, so the flag-OFF answer path is unchanged."""
    c = FactCitation()
    assert c.truth_verdict == ""
    assert c.verdict == ""
    # The two are genuinely distinct attributes, not an alias.
    assert "truth_verdict" in FactCitation.model_fields
    assert "verdict" in FactCitation.model_fields


# --------------------------------------------------------------------------- #
# (b) READ — build_citations populates the DISTINCT epistemic field.
# --------------------------------------------------------------------------- #
from cograph_client.graph.kg_writer import insert_facts  # noqa: E402
from cograph_client.graph.parser import parse_sparql_results  # noqa: E402
from cograph_client.graph.provenance import build_provenance_triples  # noqa: E402
from cograph_client.graph.validity import (  # noqa: E402
    STATUS_SUPERSEDED,
    build_closed_interval_triples,
    build_open_interval_triples,
)

ACME = "https://cograph.tech/entities/Company/acme"
HQ_PRED = attr_uri("Company", "headquarters")  # types/Company/attrs/headquarters
FOUNDED_PRED = attr_uri("Company", "founded")


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)


async def _describe_bindings(n: PyoxiNeptune, pred: str):
    raw = await n.query(
        f"SELECT ?s ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ "
        f"?s ?p ?o . FILTER(?s = <{ACME}> && ?p = <{pred}>) }} }}"
    )
    return parse_sparql_results(raw)


@pytest.mark.asyncio
async def test_read_populates_epistemic_field_distinct_from_recency_verdict():
    """A fact that is recency-``superseded`` AND epistemic-``supported`` surfaces
    BOTH — the recency ``verdict`` and the new ``truth_verdict`` are independently
    populated and carry different values, proving they are not conflated."""
    n = PyoxiNeptune()
    # headquarters "San Jose": CLOSED (superseded) recency interval + provenance, and
    # a SUPPORTED epistemic truth-verdict companion (rides the shared instance write).
    instance = [
        (ACME, RDF_TYPE, type_uri("Company")),
        (ACME, HQ_PRED, "San Jose"),
    ]
    instance += build_truth_verdict_companion(
        ACME, "Company", "headquarters", TruthVerdict.SUPPORTED.value
    )
    validity = build_closed_interval_triples(
        ACME, HQ_PRED, "San Jose",
        valid_to="2026-07-01T00:00:00+00:00", valid_from="2025-01-01T00:00:00+00:00",
        superseded_by="stmt-x", status=STATUS_SUPERSEDED, graph_uri=INSTANCE_GRAPH,
    )
    provenance = build_provenance_triples(
        ACME, HQ_PRED, "San Jose", "old_wiki", confidence=0.6, graph_uri=INSTANCE_GRAPH,
    )
    await insert_facts(
        n, INSTANCE_GRAPH, instance,
        provenance_triples=provenance, validity_triples=validity,
    )

    variables, bindings = await _describe_bindings(n, HQ_PRED)
    citations = await build_citations(n, INSTANCE_GRAPH, variables, bindings)

    assert len(citations) == 1
    c = citations[0]
    # Recency verdict (validity interval) — unchanged, closed ⇒ superseded.
    assert c.verdict == "superseded"
    assert c.is_current is False
    # Epistemic verdict (A4 companion) — SEPARATE field, SUPPORTED.
    assert c.truth_verdict == TruthVerdict.SUPPORTED.value
    # Both present and genuinely distinct.
    assert c.verdict and c.truth_verdict
    assert c.verdict != c.truth_verdict


@pytest.mark.asyncio
async def test_read_epistemic_field_empty_when_no_companion():
    """LOAD-BEARING control: a fact with NO verdict companion reads
    ``truth_verdict == ""`` (never fabricated), while its recency ``verdict`` is
    still populated — so the epistemic field is driven by the companion, not faked."""
    n = PyoxiNeptune()
    await insert_facts(
        n, INSTANCE_GRAPH,
        [
            (ACME, RDF_TYPE, type_uri("Company")),
            (ACME, FOUNDED_PRED, "1998"),
        ],
        validity_triples=build_open_interval_triples(
            ACME, FOUNDED_PRED, "1998",
            valid_from="2026-07-01T00:00:00+00:00", graph_uri=INSTANCE_GRAPH,
        ),
    )

    variables, bindings = await _describe_bindings(n, FOUNDED_PRED)
    citations = await build_citations(n, INSTANCE_GRAPH, variables, bindings)

    assert len(citations) == 1
    assert citations[0].verdict == "current"  # recency still populated
    assert citations[0].truth_verdict == ""    # epistemic absent, not fabricated


def test_companion_predicate_for_reverses_only_literal_attr_predicates():
    """The read-side reverse mapping mints the SAME companion the write did for a
    ``types/<Type>/attrs/<leaf>`` predicate, and returns None for a relationship
    (``onto/<leaf>``) predicate (which carries no literal-attribute companion)."""
    assert companion_predicate_for(HQ_PRED, TRUTH_VERDICT_SUFFIX) == attr_provenance_companion_uri(
        "Company", "headquarters", TRUTH_VERDICT_SUFFIX
    )
    assert companion_predicate_for("https://cograph.tech/onto/hasCEO", TRUTH_VERDICT_SUFFIX) is None
    assert companion_predicate_for("https://cograph.tech/onto/ingested_at", TRUTH_VERDICT_SUFFIX) is None
