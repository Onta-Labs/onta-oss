"""ONTA-450: the zero-row ontology escalation must not license type substitution.

onta-oss #273 widens a semantic ontology subset to the FULL tenant ontology when a
valid SPARQL returns zero rows, then regenerates. That helps a genuine retrieval
miss (the planner built its query from types/predicates that do not exist in this
KG). It is unsafe for the OTHER situation that produces the identical signal: the
question named a type that IS declared and simply has no instances, where zero rows
is the correct answer (ONTA-258).

Handing the model every populated type in the tenant right after telling it the
query produced nothing is an invitation to answer about a different type. These
tests pin the discriminator and the hardened retry feedback.

Everything is on invented tokens (Sprocket / Widget) so the tests assert the
mechanism, not a domain.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cograph_client.nlp.empty_type_guard import (
    NO_INSTANCES_MARK,
    declared_types,
    empty_declared_types,
    honest_empty_targets,
    types_referenced,
    zero_row_escalation_feedback,
)
from cograph_client.nlp.pipeline import NLQueryPipeline

TENANT_GRAPH = "https://cograph.tech/graphs/t1"
KG_GRAPH = "https://cograph.tech/graphs/t1/kg/widgets"

# Sprocket is DECLARED but empty in this KG; Widget is populated. This is exactly
# the shape `_fetch_ontology` emits (ONTA-258 marks the type header).
FULL_ONTOLOGY = (
    "Type: Widget — URI: <https://cograph.tech/types/Widget>\n"
    "  Attributes: name (string) — URI: <https://cograph.tech/types/Widget/attrs/name>\n"
    f"Type: Sprocket — URI: <https://cograph.tech/types/Sprocket> {NO_INSTANCES_MARK}\n"
    "  Attributes: name (string) — URI: <https://cograph.tech/types/Sprocket/attrs/name>\n"
)

# The reduced subset the semantic retriever hands the planner first. On today's
# main the semantic path emits no `[no instances]` marks at all (ONTA-411 adds
# them), so the subset is deliberately unmarked here.
SEMANTIC_SUBSET = (
    "Type: Sprocket — URI: <https://cograph.tech/types/Sprocket>\n"
    "  Attributes: name (string) — URI: <https://cograph.tech/types/Sprocket/attrs/name>\n"
)

SPROCKET_SPARQL = (
    "SELECT ?name WHERE { "
    "?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
    "<https://cograph.tech/types/Sprocket> . "
    "?s <https://cograph.tech/types/Sprocket/attrs/name> ?name }"
)
# The substitution the escalation invites: a populated type the question never named.
WIDGET_SPARQL = (
    "SELECT ?name WHERE { "
    "?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
    "<https://cograph.tech/types/Widget> . "
    "?s <https://cograph.tech/types/Widget/attrs/name> ?name }"
)

EMPTY_RESULT = {"head": {"vars": ["name"]}, "results": {"bindings": []}}
WIDGET_ROWS = {
    "head": {"vars": ["name"]},
    "results": {"bindings": [{"name": {"type": "literal", "value": "Widget A"}}]},
}
# The `SELECT DISTINCT ?type` instance probe: Widget carries instances here,
# Sprocket does not. Read by `_fetch_ontology`, and by `_active_types` once
# ONTA-411 lands.
ACTIVE_TYPE_PROBE = {
    "head": {"vars": ["type"]},
    "results": {
        "bindings": [
            {"type": {"type": "uri", "value": "https://cograph.tech/types/Widget"}}
        ]
    },
}


def _llm(sparql: str, explanation: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps({
        "sparql": sparql, "explanation": explanation, "functions_needed": [],
    }))]
    return msg


@pytest.fixture
def neptune():
    return AsyncMock()


@pytest.fixture
def pipeline(neptune):
    return NLQueryPipeline(neptune, "fake-key")


# ── unit: the discriminator ───────────────────────────────────────────────


def test_empty_declared_types_reads_the_type_level_mark():
    assert empty_declared_types(FULL_ONTOLOGY) == {"Sprocket"}
    assert declared_types(FULL_ONTOLOGY) == {"Widget", "Sprocket"}


def test_attribute_level_mark_is_not_a_type_level_mark():
    """ONTA-248 marks an empty ATTRIBUTE of a POPULATED type. That is not an
    honest-empty target and must not suppress escalation."""
    onto = (
        "Type: Widget — URI: <https://cograph.tech/types/Widget>\n"
        "  Attributes: sku (string) — URI: "
        f"<https://cograph.tech/types/Widget/attrs/sku> {NO_INSTANCES_MARK}\n"
    )
    assert empty_declared_types(onto) == set()


def test_types_referenced_covers_bare_attribute_and_layered_uris():
    assert types_referenced(SPROCKET_SPARQL) == {"Sprocket"}
    assert types_referenced(
        "SELECT ?x WHERE { ?s ?p <https://cograph.tech/types/public/Person> }"
    ) == {"Person"}
    assert types_referenced("SELECT ?x WHERE { ?s ?p ?o }") == set()


def test_honest_empty_target_requires_named_declared_and_empty():
    # Named + declared + empty + referenced -> honest empty.
    assert honest_empty_targets(
        "list all Sprockets", SPROCKET_SPARQL, FULL_ONTOLOGY
    ) == {"Sprocket"}
    # Populated target -> not honest-empty, escalation stays available.
    assert honest_empty_targets(
        "list all Widgets", WIDGET_SPARQL, FULL_ONTOLOGY
    ) == set()
    # Empty type the question never named (the ONTA-411 foreign-KG shape): the
    # planner chose it, so widening the ontology is still the right recovery.
    assert honest_empty_targets(
        "how many things are there?", SPROCKET_SPARQL, FULL_ONTOLOGY
    ) == set()
    # No marks at all (a bare tenant-graph ask) -> never suppress.
    assert honest_empty_targets(
        "list all Sprockets", SPROCKET_SPARQL, SEMANTIC_SUBSET
    ) == set()


def test_escalation_feedback_never_licenses_substitution():
    fb = zero_row_escalation_feedback(full_ontology_has_marks=True)
    assert NO_INSTANCES_MARK in fb
    assert "Do NOT substitute" in fb
    assert "does not exist" in fb
    # The pre-ONTA-450 accusation ("your query used types not in this schema")
    # is what read as permission to retarget. It must be gone.
    assert "may have used types or predicates that are not" not in fb


# ── end to end through ask() ──────────────────────────────────────────────


async def _ask(pipeline, neptune, question, llm_messages, fetch_ontology=FULL_ONTOLOGY):
    svc = AsyncMock()
    svc.retrieve.return_value = SEMANTIC_SUBSET

    async def _query(sparql="", *_a, **_k):
        # Keyed on the query TEXT, not a call counter: the pipeline issues a
        # varying number of housekeeping queries around the generated one (the
        # name-lookup broaden probe, and the active-type probe ONTA-411 adds), and
        # a positional stub silently hands one of those the result meant for the
        # generated SPARQL.
        if "DISTINCT ?type" in sparql:
            return ACTIVE_TYPE_PROBE
        return WIDGET_ROWS if "types/Widget" in sparql else EMPTY_RESULT

    neptune.query.side_effect = _query

    with patch("cograph_client.nlp.pipeline.get_embedding_service", return_value=svc):
        with patch.object(
            pipeline, "_fetch_ontology", new_callable=AsyncMock,
            return_value=fetch_ontology,
        ):
            with patch.object(
                # NLResult.narrative_answer is a str, so "" (not None) is the
                # "no narrative" value; None makes NLResult() raise and the
                # attempt silently retries.
                pipeline, "_rephrase_via_openrouter", new_callable=AsyncMock,
                return_value="",
            ):
                with patch.object(
                    pipeline.anthropic.messages, "create", new_callable=AsyncMock
                ) as create:
                    create.side_effect = llm_messages
                    result = await pipeline.ask(
                        question, TENANT_GRAPH, instance_graph=KG_GRAPH
                    )
    return result, create


@pytest.mark.asyncio
async def test_declared_but_empty_named_type_is_not_escalated_away(pipeline, neptune):
    """THE ONTA-450 REGRESSION.

    "list all Sprockets" against a declared-but-empty Sprocket must answer about
    SPROCKET (zero rows), not be widened to the full ontology and re-answered
    about the populated Widget. Before the fix the #273 escalation fires here,
    a second generation runs, and the answer is "Widget A".
    """
    result, create = await _ask(
        pipeline, neptune, "list all Sprockets",
        [_llm(SPROCKET_SPARQL, "Sprockets"), _llm(WIDGET_SPARQL, "Widgets")],
    )

    assert "types/Sprocket" in result.sparql, "the query was retargeted"
    assert "types/Widget" not in result.sparql
    assert "Widget A" not in result.answer
    assert create.call_count == 1, "the escalation regenerated on an honest empty"
    assert result.timing.get("ontology_zero_row_escalation") is None
    assert result.timing.get("zero_row_honest_empty") == 1.0
    # ONTA-258: say so plainly rather than only "No results found."
    assert "no instances" in result.answer.lower()


@pytest.mark.asyncio
async def test_escalation_still_fires_for_a_genuine_retrieval_miss(pipeline, neptune):
    """#273's own case is preserved: the question names no empty type, the first
    query returns zero rows, so the ontology widens and regeneration happens."""
    result, create = await _ask(
        pipeline, neptune, "how many things are there?",
        [_llm(SPROCKET_SPARQL, "guess"), _llm(WIDGET_SPARQL, "full schema")],
    )

    assert result.timing.get("ontology_zero_row_escalation") == 1.0
    assert create.call_count >= 2


@pytest.mark.asyncio
async def test_escalation_feedback_reaches_the_regeneration(pipeline, neptune):
    """When escalation DOES fire, the anti-substitution guard is in the prompt."""
    captured: dict = {}
    original = NLQueryPipeline._generate_sparql

    async def _spy(self, question, ontology, graph_uri, **kwargs):
        if "error_feedback" in kwargs:
            captured["feedback"] = kwargs["error_feedback"]
            captured["ontology"] = ontology
        return await original(self, question, ontology, graph_uri, **kwargs)

    with patch.object(NLQueryPipeline, "_generate_sparql", _spy):
        await _ask(
            pipeline, neptune, "how many things are there?",
            [_llm(SPROCKET_SPARQL, "guess"), _llm(WIDGET_SPARQL, "full schema")],
        )

    assert "Do NOT substitute" in captured["feedback"]
    assert NO_INSTANCES_MARK in captured["feedback"]
    assert captured["ontology"] == FULL_ONTOLOGY


# ── review follow-ups ─────────────────────────────────────────────────────


def test_named_in_question_does_not_match_inside_another_word():
    """The verbatim arm is word-BOUNDED. A bare substring test matched a short
    declared type inside an unrelated word, which now also gates the escalation
    guard, so a spurious match would suppress a legitimate escalation."""
    from cograph_client.nlp.ontology_embeddings import _types_named_in_question

    types = ["Age", "Ion", "Cat", "Rat", "ClinicalTrial"]
    assert _types_named_in_question("who manages the medication categories?", types) == set()
    assert _types_named_in_question("what is the age of the cat", types) == {"Age", "Cat"}
    # Plural of a compound name still matches.
    assert _types_named_in_question("show me clinicaltrials", types) == {"ClinicalTrial"}


@pytest.mark.asyncio
async def test_honest_note_does_not_ride_onto_a_later_non_empty_answer(
    pipeline, neptune
):
    """The note is per-ATTEMPT. An attempt that sets it and then dies before
    returning must not leave it to be appended to a later attempt's rows, which
    would assert "no instances" over actual data."""
    calls = {"n": 0}

    async def _rephrase(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient rephrase failure")
        return ""

    with patch.object(pipeline, "_rephrase_via_openrouter", _rephrase):
        svc = AsyncMock()
        svc.retrieve.return_value = SEMANTIC_SUBSET

        async def _query(sparql="", *_a, **_k):
            if "DISTINCT ?type" in sparql:
                return ACTIVE_TYPE_PROBE
            return WIDGET_ROWS if "types/Widget" in sparql else EMPTY_RESULT

        neptune.query.side_effect = _query
        with patch("cograph_client.nlp.pipeline.get_embedding_service", return_value=svc):
            with patch.object(
                pipeline, "_fetch_ontology", new_callable=AsyncMock,
                return_value=FULL_ONTOLOGY,
            ):
                with patch.object(
                    pipeline.anthropic.messages, "create", new_callable=AsyncMock
                ) as create:
                    create.side_effect = [
                        _llm(SPROCKET_SPARQL, "Sprockets"),
                        _llm(WIDGET_SPARQL, "Widgets"),
                    ]
                    result = await pipeline.ask(
                        "list all Sprockets", TENANT_GRAPH, instance_graph=KG_GRAPH
                    )

    assert "Widget A" in result.answer, "second attempt should have returned rows"
    assert "no instances" not in result.answer.lower()
    assert "no instances" not in (result.narrative_answer or "").lower()


@pytest.mark.asyncio
async def test_no_escalation_onto_an_ontology_sentinel(pipeline, neptune):
    """`_fetch_ontology` does not raise on failure, it RETURNS a sentinel string,
    and both sentinels are truthy. Escalating onto one would swap a working
    semantic subset for prose and ask the model to regenerate against it."""
    from cograph_client.nlp.pipeline import ONTOLOGY_EMPTY

    result, create = await _ask(
        pipeline, neptune, "how many things are there?",
        [_llm(SPROCKET_SPARQL, "guess"), _llm(WIDGET_SPARQL, "would substitute")],
        fetch_ontology=ONTOLOGY_EMPTY,
    )

    assert create.call_count == 1, "regenerated against the sentinel"
    assert result.timing.get("ontology_zero_row_escalation") is None
    assert result.ontology == SEMANTIC_SUBSET
