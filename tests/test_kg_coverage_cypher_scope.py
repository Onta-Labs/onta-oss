"""ONTA-534 — the coverage caveat's UNION premise is gone under Cypher.

Signals B and C of the ONTA-454 caveat (``nlp/pipeline_format._kg_coverage_caveat``)
both say the result "counts data from '<kg>' together with the workspace base
graph and any shared layers". That was true of a SPARQL answer whose dataset
really was that union — ``/ask`` spliced those graphs in as extra ``FROM``
clauses. Generated Cypher is confined to ONE ``(tenant_id, kg)`` scope by
``confine_generated_cypher`` + ``assert_cypher_is_scoped``, so on the shipped
backend the sentence would be FALSE about a correct, correctly-scoped answer.

These pin silence there, and they FAIL without the gate. Note the asymmetry with
production: the ``.ask()`` double RETURNS ``True`` rather than raising, so on
``main`` the hermetic suite emits the caveat, while production ``main`` is silent
(a real ``NeptuneClient.ask`` raises ``SparqlClientRetired`` whenever a process
GraphStore is configured, and the ``except`` swallows it into ``False``). So the
user-visible change on the shipped backend is nil — which is the point, making
the probe real must not also make a stale claim reachable — but these are real
regression tests for the gate, not documentation of a no-op.

The signal LOGIC itself stays pinned in ``tests/test_kg_coverage_caveat.py``
behind its ``union_dataset`` fixture.
"""

from __future__ import annotations

import pytest

from tests.test_kg_coverage_caveat import (
    BASE,
    EM,
    RECALL_SPARQL,
    UNSCOPED_SPARQL,
    _ask,
    _neptune,
)


@pytest.mark.asyncio
async def test_unanchored_query_is_silent_on_the_cypher_path():
    """No `union_dataset` fixture: this is the shipped configuration."""
    client = _neptune(answer_rows=["19582"], base_has_instances=True)
    result = await _ask(client, UNSCOPED_SPARQL)
    assert result.coverage_caveat == ""
    assert "19582" in result.answer


@pytest.mark.asyncio
async def test_unanchored_silence_costs_no_probe_at_all_on_the_cypher_path():
    """Not merely silent — it must not ASK either.

    The gate short-circuits before `other_graphs_hold_instances`, so a
    confined-Cypher answer pays nothing for a signal that cannot apply to it.
    This is also what drops the `other_graph_instance_probe_failed` warnings
    that ONTA-534 left spamming production.
    """
    client = _neptune(answer_rows=["19582"], base_has_instances=True)
    await _ask(client, UNSCOPED_SPARQL)
    assert client.ask.await_args_list == []


@pytest.mark.asyncio
async def test_undetermined_semantic_signal_is_silent_on_the_cypher_path():
    unmarked = f"Type: ProductRecall {EM} URI: <{BASE}/types/ProductRecall>"
    result = await _ask(
        _neptune(answer_rows=["4229"], base_has_instances=True),
        RECALL_SPARQL,
        ontology=unmarked,
        semantic_probe_failed=True,
    )
    assert result.coverage_caveat == ""


@pytest.mark.asyncio
async def test_a_confirmed_absence_still_caveats_on_the_cypher_path():
    """The fail-closed rule must not silence signal A when the probe DID answer.

    Guards against over-correcting: `_types_present_in_kg` returning an empty
    set ("ran, cleared nothing") is a measurement and must still yield the
    caveat. Only `None` ("could not answer") suppresses.
    """
    org_sparql = RECALL_SPARQL.replace("ProductRecall", "Organization")
    result = await _ask(_neptune(answer_rows=["12"], subtypes_present=[]), org_sparql)
    assert "Organization" in result.coverage_caveat
