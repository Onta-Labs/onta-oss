"""ONTA-454: say so when the KG the user NAMED contributed nothing to the answer.

The defect, reproduced on production 2026-08-03 before this change:
``POST /graphs/demo-tenant/ask`` with ``kg_name="maral"`` (registered, 96 triples,
8 subjects, all of ONE type) asked "how many product recalls are there?" and
answered **4229**, with an empty ``coverage_caveat``. The generated query's
dataset was the union of ``kg/maral`` + the tenant base graph + the Global public
layer, and every row came from the base graph. The named KG contributed nothing
and nothing in the response said so.

Two properties are asserted throughout, and the second is what keeps this from
being a cure worse than the disease:

1. The caveat FIRES on the bug, reaches the ``answer`` STRING (the one field
   every interface renders), and rides the existing ``coverage_caveat`` field.
2. It stays SILENT on every shape where the union is the honest answer: a
   ``kg_name``-less workspace whose data legitimately IS the base graph, a type
   that does have instances here, a supertype whose SUBTYPES have instances here,
   and a zero-row result (which belongs to ONTA-450/258 and must not be
   double-annotated).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cograph_client.nlp.kg_coverage import (
    coverage_caveat,
    empty_types_for_kg,
    kg_subtype_presence_query,
    referenced_types,
    uncovered_types,
)
from cograph_client.nlp.pipeline import NLQueryPipeline

BASE = "https://graph.onta.sh"
TENANT_GRAPH = f"{BASE}/graphs/t1"
KG_GRAPH = f"{TENANT_GRAPH}/kg/maral"
PUBLIC_LAYER = f"{BASE}/graphs/global/public"

# The production ontology-summary header separator. Spelled as an escape so the
# fixture byte-matches `_fetch_ontology`'s own f-string without the character
# appearing literally in this file.
EM = "—"

ONTOLOGY = "\n".join(
    [
        f"Type: Hospital {EM} URI: <{BASE}/types/Hospital>",
        f"  Attributes: name (string) {EM} URI: <{BASE}/types/Hospital/attrs/name>",
        f"Type: ProductRecall {EM} URI: <{BASE}/types/ProductRecall> [no instances]",
        f"Type: Organization {EM} URI: <{BASE}/types/Organization> [no instances]",
    ]
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"

# The query the production reproduction actually generated, verbatim in shape.
RECALL_SPARQL = (
    "SELECT (COUNT(DISTINCT ?recall) AS ?count) "
    f"FROM <{KG_GRAPH}> FROM <{TENANT_GRAPH}> FROM <{PUBLIC_LAYER}> "
    f"WHERE {{ ?recall <{RDF_TYPE}>/<{SUBCLASS}>* <{BASE}/types/ProductRecall> }}"
)
HOSPITAL_SPARQL = (
    "SELECT (COUNT(DISTINCT ?h) AS ?count) "
    f"FROM <{KG_GRAPH}> FROM <{TENANT_GRAPH}> "
    f"WHERE {{ ?h <{RDF_TYPE}>/<{SUBCLASS}>* <{BASE}/types/Hospital> }}"
)


def _rows(var: str, values: list[str]) -> dict:
    return {
        "head": {"vars": [var]},
        "results": {
            "bindings": [{var: {"type": "literal", "value": v}} for v in values]
        },
    }


def _uri_rows(var: str, uris: list[str]) -> dict:
    return {
        "head": {"vars": [var]},
        "results": {
            "bindings": [{var: {"type": "uri", "value": u}} for u in uris]
        },
    }


def _neptune(*, answer_rows: list[str], subtypes_present: list[str] | None = None,
             probe_raises: bool = False):
    """Store double that dispatches on the query SHAPE, not on call order.

    ``FROM NAMED`` identifies the ONTA-454 subclass-confirmation probe (nothing
    else in the ask path emits one), so the answer query and the probe can be
    answered differently without depending on how many other reads the pipeline
    happens to make.
    """
    client = AsyncMock()

    async def _query(sparql, *args, **kwargs):
        if "FROM NAMED" in sparql:
            if probe_raises:
                raise RuntimeError("neptune unavailable")
            return _uri_rows("type", subtypes_present or [])
        return _rows("count", answer_rows)

    client.query.side_effect = _query
    return client


async def _ask(client, sparql: str, *, kg: bool = True, ontology: str = ONTOLOGY):
    pipeline = NLQueryPipeline(client, "fake-key")
    canned = {"sparql": sparql, "explanation": "", "functions_needed": []}
    with patch.object(
        pipeline, "_generate_sparql", new_callable=AsyncMock, return_value=canned
    ), patch.object(
        pipeline, "_fetch_ontology", new_callable=AsyncMock, return_value=ontology
    ), patch.object(
        pipeline, "_rephrase_via_openrouter", new_callable=AsyncMock, return_value="",
    ):
        return await pipeline.ask(
            "how many product recalls are there?",
            TENANT_GRAPH,
            KG_GRAPH if kg else None,
            # What `/ask` threads in production (ONTA-397): the tenant's visible
            # layer stack. It is also what makes the Global public layer a legal
            # FROM in the generated query, i.e. what produces the three-clause
            # union the reproduction showed.
            layer_graph_uris=[TENANT_GRAPH, PUBLIC_LAYER],
        )


# --------------------------------------------------------------------------- #
# 1. The bug itself.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_answer_from_another_graph_carries_a_coverage_caveat():
    """The reproduction: a real number, and now a sentence saying where it is from."""
    result = await _ask(_neptune(answer_rows=["4229"]), RECALL_SPARQL)

    assert "4229" in result.answer, "the answer is still returned, never refused"
    assert "ProductRecall" in result.coverage_caveat
    assert "maral" in result.coverage_caveat
    # The `answer` STRING is the only field every interface renders (both MCP
    # tools print `Answer:` and nothing else), so a caveat that lives only in a
    # side field would not reach the person who reads the number.
    assert "Coverage note:" in result.answer
    assert "ProductRecall" in result.answer


@pytest.mark.asyncio
async def test_caveat_names_the_named_graph_as_the_one_that_lacks_the_data():
    result = await _ask(_neptune(answer_rows=["4229"]), RECALL_SPARQL)
    caveat = result.coverage_caveat
    assert caveat.startswith("Knowledge graph 'maral' contains no instances of")
    # It must not assert the ANSWER is wrong — it may be exactly what the user
    # wanted from the workspace as a whole. It states provenance, nothing more.
    assert "did not come from 'maral'" in caveat


# --------------------------------------------------------------------------- #
# 2. Silence everywhere the union is the honest answer.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_kg_name_never_gets_a_caveat():
    """A `kg_name`-less workspace's data legitimately IS the tenant base graph.

    Refusing (or caveating) there would break every such workspace, which is why
    this fix is a caveat scoped to a NAMED graph rather than a narrower dataset.
    """
    result = await _ask(
        _neptune(answer_rows=["4229"]), RECALL_SPARQL.replace(f"FROM <{KG_GRAPH}> ", ""),
        kg=False,
    )
    assert result.coverage_caveat == ""
    assert "Coverage note:" not in result.answer


@pytest.mark.asyncio
async def test_populated_type_gets_no_caveat():
    result = await _ask(_neptune(answer_rows=["8"]), HOSPITAL_SPARQL)
    assert result.coverage_caveat == ""
    assert "Coverage note:" not in result.answer


@pytest.mark.asyncio
async def test_ontology_with_no_empty_marks_gets_no_caveat():
    plain = f"Type: Hospital {EM} URI: <{BASE}/types/Hospital>"
    result = await _ask(_neptune(answer_rows=["4229"]), RECALL_SPARQL, ontology=plain)
    assert result.coverage_caveat == ""


@pytest.mark.asyncio
async def test_zero_rows_is_left_entirely_to_the_honest_empty_guard():
    """ONTA-450/258 own the zero-row case; this must not double-annotate it."""
    result = await _ask(_neptune(answer_rows=[]), RECALL_SPARQL)
    assert "Coverage note:" not in result.answer
    assert result.coverage_caveat == ""


# --------------------------------------------------------------------------- #
# 3. Subclass closure: the false-positive this would otherwise ship.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_supertype_whose_subtype_lives_here_is_not_caveated():
    """`[no instances]` is a DIRECT rdf:type fact; the query walks the closure.

    On demo-tenant ``Facility`` and ``University`` are subclasses of
    ``Organization``. A KG holding only ``Facility`` rows is marked "no
    Organization instances" while a closure query answers correctly FROM THAT KG.
    Caveating there would be a confidently wrong caveat on a correct answer.
    """
    org_sparql = RECALL_SPARQL.replace("ProductRecall", "Organization")
    client = _neptune(
        answer_rows=["12"], subtypes_present=[f"{BASE}/types/Organization"]
    )
    result = await _ask(client, org_sparql)
    assert result.coverage_caveat == ""
    assert "Coverage note:" not in result.answer


@pytest.mark.asyncio
async def test_probe_confirming_absence_still_yields_the_caveat():
    org_sparql = RECALL_SPARQL.replace("ProductRecall", "Organization")
    result = await _ask(_neptune(answer_rows=["12"], subtypes_present=[]), org_sparql)
    assert "Organization" in result.coverage_caveat


@pytest.mark.asyncio
async def test_probe_failure_degrades_to_the_verdict_the_planner_already_saw():
    """The probe can only SUPPRESS. A failure must not silence the caveat.

    The ontology summary already told the model "[no instances]"; restating it is
    strictly no worse than the context the answer was generated from.
    """
    result = await _ask(
        _neptune(answer_rows=["4229"], probe_raises=True), RECALL_SPARQL
    )
    assert "ProductRecall" in result.coverage_caveat


@pytest.mark.asyncio
async def test_the_probe_is_the_only_extra_round_trip():
    """Cost control: exactly ONE store call beyond the answer query itself."""
    client = _neptune(answer_rows=["4229"])
    await _ask(client, RECALL_SPARQL)
    queries = [c.args[0] for c in client.query.await_args_list]
    probes = [q for q in queries if "FROM NAMED" in q]
    assert len(probes) == 1, queries


@pytest.mark.asyncio
async def test_a_covered_query_pays_for_no_probe_at_all():
    client = _neptune(answer_rows=["8"])
    await _ask(client, HOSPITAL_SPARQL)
    assert not [c for c in client.query.await_args_list if "FROM NAMED" in c.args[0]]


# --------------------------------------------------------------------------- #
# 4. The pure analysis layer.
# --------------------------------------------------------------------------- #


def test_referenced_types_reduces_the_attribute_form_to_its_type():
    got = referenced_types(
        f"SELECT ?n WHERE {{ ?s <{BASE}/types/Person/attrs/name> ?n }}"
    )
    assert got == {"Person": [f"{BASE}/types/Person"]}


def test_referenced_types_handles_the_layered_form():
    got = referenced_types(f"?s a <{BASE}/types/public/Person>")
    assert list(got) == ["Person"]
    assert got["Person"] == [f"{BASE}/types/public/Person"]


def test_empty_types_unions_the_marks_with_the_per_kg_probe():
    """The semantic subset only carries the top-K chunks it retrieved.

    Its `[no instances]` marks are therefore NOT exhaustive, and a query aimed at
    a type retrieval left out would go uncaveated on marks alone. The probe's own
    `declared - active` difference covers the rest.
    """
    subset = f"Type: Hospital {EM} URI: <{BASE}/types/Hospital>"
    got = empty_types_for_kg(
        subset,
        declared_names=["Hospital", "ProductRecall", "Drug"],
        active_types={"Hospital"},
    )
    assert got == {"ProductRecall", "Drug"}


def test_empty_types_with_no_probe_falls_back_to_marks_only():
    assert empty_types_for_kg(ONTOLOGY) == {"ProductRecall", "Organization"}
    # `active_types=None` means "nothing to scope by" and must contribute
    # nothing, never mark every declared type empty.
    assert (
        empty_types_for_kg(
            ONTOLOGY, declared_names=["Hospital", "Drug"], active_types=None
        )
        == {"ProductRecall", "Organization"}
    )


def test_uncovered_flags_all_when_every_referenced_type_is_absent():
    flagged, all_types = uncovered_types(RECALL_SPARQL, {"ProductRecall"})
    assert set(flagged) == {"ProductRecall"}
    assert all_types is True


def test_uncovered_flags_partial_when_one_leg_is_present():
    joined = (
        f"SELECT ?x WHERE {{ ?h a <{BASE}/types/Hospital> ; "
        f"<{BASE}/onto/recall> ?r . ?r a <{BASE}/types/ProductRecall> }}"
    )
    flagged, all_types = uncovered_types(joined, {"ProductRecall"})
    assert set(flagged) == {"ProductRecall"}
    assert all_types is False
    text = coverage_caveat("maral", list(flagged), all_types=False)
    assert "any part of this result that depends on it" in text


def test_caveat_is_empty_without_a_kg_name_or_without_types():
    assert coverage_caveat("", ["ProductRecall"], all_types=True) == ""
    assert coverage_caveat("maral", [], all_types=True) == ""


def test_caveat_summarises_a_long_type_list():
    text = coverage_caveat("maral", list("ABCDE"), all_types=True)
    assert "and 2 other types" in text


def test_subtype_presence_query_isolates_instances_to_the_kg_graph():
    """The instances must come from the KG ALONE while the closure edges may not.

    A plain union dataset would find the BASE graph's own instances and suppress
    every caveat, which is the bug this module reports. `FROM NAMED` + an explicit
    `GRAPH` block is what keeps the two apart.
    """
    q = kg_subtype_presence_query(
        KG_GRAPH, [TENANT_GRAPH, PUBLIC_LAYER], [f"{BASE}/types/ProductRecall"]
    )
    assert f"FROM <{TENANT_GRAPH}>" in q and f"FROM <{PUBLIC_LAYER}>" in q
    assert f"FROM NAMED <{KG_GRAPH}>" in q
    assert f"GRAPH <{KG_GRAPH}> {{ ?s <{RDF_TYPE}> ?sub }}" in q
    # The closure factor is written FIRST, with the searched type bound, so the
    # engine enumerates the tiny subclass set instead of scanning the KG's whole
    # rdf:type index with ?sub unbound.
    assert q.index("?sub <") < q.index(f"GRAPH <{KG_GRAPH}>")
    assert "LIMIT 1" in q


def test_subtype_presence_query_parses_as_sparql():
    from rdflib.plugins.sparql.parser import parseQuery

    parseQuery(
        kg_subtype_presence_query(
            KG_GRAPH,
            [TENANT_GRAPH, PUBLIC_LAYER],
            [f"{BASE}/types/ProductRecall", f"{BASE}/types/Organization"],
        )
    )
