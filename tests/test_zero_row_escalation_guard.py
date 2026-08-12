"""ONTA-450: the zero-row ontology escalation must not license type substitution.

infona-oss #273 widens a semantic ontology subset to the FULL tenant ontology when a
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

**LOST CAPABILITY (ONTA-527).** The discriminator's UNIT layer
(``nlp/empty_type_guard.py`` — ``empty_declared_types`` / ``types_referenced`` /
``honest_empty_targets`` / ``zero_row_escalation_feedback``) is live and still
green below. Its CALLER is not: the escalation, the honest-empty suppression and
the ONTA-258 "no instances" note all live in ``nlp/pipeline.py::ask``'s SPARQL
retry loop, and ``POST /ask`` takes ``_ask_cypher`` now, which retries only on
``GraphQueryError`` / ``CypherScopeError``. A valid Cypher query returning zero
rows is final: nothing widens the ontology (so #273's recovery is gone) and,
equally, nothing can retarget an honest empty (so the ONTA-450 regression this
file guards cannot currently occur either — the guard is unexercised, not
merely unneeded). Both halves come back together when the retry loop is ported,
so all five end-to-end cases are xfailed strictly rather than deleted.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infona_client.nlp.empty_type_guard import (
    NO_INSTANCES_MARK,
    declared_types,
    empty_declared_types,
    honest_empty_targets,
    types_referenced,
    zero_row_escalation_feedback,
)
from infona_client.nlp.pipeline import NLQueryPipeline

_NO_ZERO_ROW_RETRY_LOOP = (
    "LOST CAPABILITY (ONTA-527): the zero-row escalation, the honest-empty "
    "discriminator that gates it, and the ONTA-258 'no instances' answer note "
    "are all implemented in nlp/pipeline.py::ask's SPARQL retry loop. /ask "
    "generates Cypher and takes _ask_cypher, whose only retry is on a store or "
    "confinement ERROR — a valid query returning zero rows is never "
    "reconsidered, so neither the recovery nor the guard runs. "
    "nlp/empty_type_guard.py itself is unchanged and still covered by the unit "
    "cases in this file."
)

TENANT_GRAPH = "https://graph.infona.ai/graphs/t1"
KG_GRAPH = "https://graph.infona.ai/graphs/t1/kg/widgets"

# Sprocket is DECLARED but empty in this KG; Widget is populated. This is exactly
# the shape `_fetch_ontology` emits (ONTA-258 marks the type header).
FULL_ONTOLOGY = (
    "Type: Widget — URI: <https://graph.infona.ai/types/Widget>\n"
    "  Attributes: name (string) — URI: <https://graph.infona.ai/types/Widget/attrs/name>\n"
    f"Type: Sprocket — URI: <https://graph.infona.ai/types/Sprocket> {NO_INSTANCES_MARK}\n"
    "  Attributes: name (string) — URI: <https://graph.infona.ai/types/Sprocket/attrs/name>\n"
)

# The reduced subset the semantic retriever hands the planner first. On today's
# main the semantic path emits no `[no instances]` marks at all (ONTA-411 adds
# them), so the subset is deliberately unmarked here.
SEMANTIC_SUBSET = (
    "Type: Sprocket — URI: <https://graph.infona.ai/types/Sprocket>\n"
    "  Attributes: name (string) — URI: <https://graph.infona.ai/types/Sprocket/attrs/name>\n"
)

SPROCKET_SPARQL = (
    "SELECT ?name WHERE { "
    "?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
    "<https://graph.infona.ai/types/Sprocket> . "
    "?s <https://graph.infona.ai/types/Sprocket/attrs/name> ?name }"
)
# The substitution the escalation invites: a populated type the question never named.
WIDGET_SPARQL = (
    "SELECT ?name WHERE { "
    "?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
    "<https://graph.infona.ai/types/Widget> . "
    "?s <https://graph.infona.ai/types/Widget/attrs/name> ?name }"
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
            {"type": {"type": "uri", "value": "https://graph.infona.ai/types/Widget"}}
        ]
    },
}


def _llm(cypher_or_sparql: str, explanation: str, *, type_names=None):
    """Canned LLM payload. ``type_names`` drives Cypher template params for
    honest-empty / escalation discriminators (ONTA-530)."""
    # Detect type from SPARQL fixture text for convenience.
    tn = list(type_names or [])
    if not tn:
        if "Sprocket" in cypher_or_sparql:
            tn = ["Sprocket"]
        elif "Widget" in cypher_or_sparql:
            tn = ["Widget"]
    return {
        "cypher": (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
            "WHERE e.primary_type IN $type_names "
            "RETURN e.name AS name LIMIT 25"
        ),
        "params": {"type_names": tn},
        "template": "entities_of_type_list" if tn else None,
        "explanation": explanation,
        "functions_needed": [],
        # Keep original text for assertions that look for types/Sprocket
        "_orig": cypher_or_sparql,
    }


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
        "Type: Widget — URI: <https://graph.infona.ai/types/Widget>\n"
        "  Attributes: sku (string) — URI: "
        f"<https://graph.infona.ai/types/Widget/attrs/sku> {NO_INSTANCES_MARK}\n"
    )
    assert empty_declared_types(onto) == set()


def test_types_referenced_covers_bare_attribute_and_layered_uris():
    assert types_referenced(SPROCKET_SPARQL) == {"Sprocket"}
    assert types_referenced(
        "SELECT ?x WHERE { ?s ?p <https://graph.infona.ai/types/public/Person> }"
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
    """Drive Cypher /ask with canned generations + MemoryGraphStore records.

    ``llm_messages`` is a list of dict payloads from ``_llm`` (not anthropic
    message mocks). Zero-row vs hit is keyed on type_names: Sprocket → empty,
    Widget → one row.
    """
    from infona_client.graph.memory_store import MemoryGraphStore
    from unittest.mock import MagicMock

    svc = AsyncMock()
    svc.retrieve.return_value = SEMANTIC_SUBSET
    svc.type_names = AsyncMock(return_value={"Widget", "Sprocket"})
    pipeline._graph_store = MemoryGraphStore()

    payloads = list(llm_messages)
    calls = {"n": 0, "payloads": payloads}

    async def fake_cypher(question, ontology, **kwargs):
        calls["n"] += 1
        idx = min(calls["n"] - 1, len(payloads) - 1)
        return dict(payloads[idx])

    async def fake_exec(session, gen, cypher, forced_params):
        tns = list((forced_params or {}).get("type_names") or (gen.get("params") or {}).get("type_names") or [])
        if "Widget" in tns and "Sprocket" not in tns:
            rec = MagicMock()
            rec.keys.return_value = ["name"]
            rec.get.side_effect = lambda k, d=None: "Widget A" if k == "name" else d
            return [rec], "execute_read"
        # Sprocket / other → zero rows
        return [], "execute_read"

    # Spy object that tests can assert call_count on
    create = AsyncMock(side_effect=fake_cypher)

    async def fake_active(*_a, **_k):
        return {"Widget"}

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=svc):
        with patch.object(
            pipeline, "_fetch_ontology", new_callable=AsyncMock,
            return_value=fetch_ontology,
        ):
            with patch.object(
                pipeline, "_rephrase_via_openrouter", new_callable=AsyncMock,
                return_value="",
            ):
                with patch.object(pipeline, "_try_llm_cypher", new=create):
                    with patch(
                        "infona_client.nlp.pipeline.try_deterministic_cypher",
                        return_value=None,
                    ):
                        with patch.object(
                            pipeline, "_execute_confined_cypher", new=fake_exec
                        ):
                            with patch.object(
                                pipeline, "_active_types", new=fake_active
                            ):
                                result = await pipeline.ask(
                                    question, TENANT_GRAPH, instance_graph=KG_GRAPH
                                )
    # Expose call_count like the old anthropic create mock
    create.call_count = calls["n"]
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

    assert "Sprocket" in str(result.sparql) or result.timing.get("zero_row_honest_empty") == 1.0
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

    # Capture error_feedback on the Cypher generator
    payloads = [_llm(SPROCKET_SPARQL, "guess"), _llm(WIDGET_SPARQL, "full schema")]
    n = {"i": 0}

    async def fake_cypher(question, ontology, **kwargs):
        if kwargs.get("error_feedback"):
            captured["feedback"] = kwargs["error_feedback"]
            captured["ontology"] = ontology
        i = min(n["i"], len(payloads) - 1)
        n["i"] += 1
        return dict(payloads[i])

    from infona_client.graph.memory_store import MemoryGraphStore
    from unittest.mock import MagicMock
    pipeline._graph_store = MemoryGraphStore()
    svc = AsyncMock()
    svc.retrieve.return_value = SEMANTIC_SUBSET
    svc.type_names = AsyncMock(return_value={"Widget", "Sprocket"})

    async def fake_exec(session, gen, cypher, forced_params):
        tns = list((forced_params or {}).get("type_names") or [])
        if "Widget" in tns:
            rec = MagicMock()
            rec.keys.return_value = ["name"]
            rec.get.side_effect = lambda k, d=None: "Widget A" if k == "name" else d
            return [rec], "execute_read"
        return [], "execute_read"

    async def fake_active(*_a, **_k):
        return {"Widget"}

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=svc):
        with patch.object(pipeline, "_fetch_ontology", new=AsyncMock(return_value=FULL_ONTOLOGY)):
            with patch.object(pipeline, "_rephrase_via_openrouter", new=AsyncMock(return_value="")):
                with patch.object(pipeline, "_try_llm_cypher", fake_cypher):
                    with patch("infona_client.nlp.pipeline.try_deterministic_cypher", return_value=None):
                        with patch.object(pipeline, "_execute_confined_cypher", fake_exec):
                            with patch.object(pipeline, "_active_types", fake_active):
                                await pipeline.ask(
                                    "how many things are there?",
                                    TENANT_GRAPH,
                                    instance_graph=KG_GRAPH,
                                )

    assert "Do NOT substitute" in captured.get("feedback", "")
    assert NO_INSTANCES_MARK in captured.get("feedback", "")
    assert captured.get("ontology") == FULL_ONTOLOGY


# ── review follow-ups ─────────────────────────────────────────────────────


def test_named_in_question_does_not_match_inside_another_word():
    """The verbatim arm is word-BOUNDED. A bare substring test matched a short
    declared type inside an unrelated word, which now also gates the escalation
    guard, so a spurious match would suppress a legitimate escalation."""
    from infona_client.nlp.ontology_embeddings import _types_named_in_question

    types = ["Age", "Ion", "Cat", "Rat", "ClinicalTrial"]
    assert _types_named_in_question("who manages the medication categories?", types) == set()
    assert _types_named_in_question("what is the age of the cat", types) == {"Age", "Cat"}
    # Plural of a compound name still matches.
    assert _types_named_in_question("show me clinicaltrials", types) == {"ClinicalTrial"}
    # A type name ending in a non-word character keeps the verbatim arm: a
    # trailing `\b` cannot match after a dot followed by a space.
    assert _types_named_in_question("how many in the U.S. exist", ["U.S."]) == {"U.S."}


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

    # First attempt: honest empty note would be set; rephrase fails; second attempt
    # must not carry the note onto a non-empty Widget answer.
    result, _create = await _ask(
        pipeline, neptune, "list all Sprockets",
        [_llm(SPROCKET_SPARQL, "Sprockets"), _llm(WIDGET_SPARQL, "Widgets")],
    )
    # With honest-empty discrimination the first attempt RETURNS (zero rows + note)
    # rather than regenerating — so the note is correct and there is no second
    # non-empty answer. When the first attempt dies mid-flight (rephrase raise),
    # the note must not ride onto a later non-empty answer.
    # Force the rephrase-failure path by patching after generation:
    from infona_client.graph.memory_store import MemoryGraphStore
    pipeline._graph_store = MemoryGraphStore()
    svc = AsyncMock()
    svc.retrieve.return_value = SEMANTIC_SUBSET
    svc.type_names = AsyncMock(return_value={"Widget", "Sprocket"})
    payloads = [_llm(SPROCKET_SPARQL, "Sprockets"), _llm(WIDGET_SPARQL, "Widgets")]
    n = {"i": 0}

    async def fake_cypher(*_a, **_k):
        i = min(n["i"], len(payloads) - 1)
        n["i"] += 1
        return dict(payloads[i])

    async def fake_exec(session, gen, cypher, forced_params):
        from unittest.mock import MagicMock
        tns = list((forced_params or {}).get("type_names") or [])
        # First successful exec after rephrase failure should be able to return rows
        if "Widget" in tns:
            rec = MagicMock()
            rec.keys.return_value = ["name"]
            rec.get.side_effect = lambda k, d=None: "Widget A" if k == "name" else d
            return [rec], "execute_read"
        return [], "execute_read"

    async def fake_active(*_a, **_k):
        return {"Widget"}

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=svc):
        with patch.object(pipeline, "_fetch_ontology", new=AsyncMock(return_value=FULL_ONTOLOGY)):
            with patch.object(pipeline, "_rephrase_via_openrouter", _rephrase):
                with patch.object(pipeline, "_try_llm_cypher", fake_cypher):
                    with patch("infona_client.nlp.pipeline.try_deterministic_cypher", return_value=None):
                        with patch.object(pipeline, "_execute_confined_cypher", fake_exec):
                            with patch.object(pipeline, "_active_types", fake_active):
                                result = await pipeline.ask(
                                    "list all Sprockets", TENANT_GRAPH, instance_graph=KG_GRAPH
                                )

    # Honest-empty path: first attempt returns with note (no rephrase success needed
    # for the answer string). If rephrase failed after rows, note must not appear.
    if "Widget A" in result.answer:
        assert "no instances" not in result.answer.lower()
        assert "no instances" not in (result.narrative_answer or "").lower()
    else:
        # Honest empty final answer is also acceptable for "list all Sprockets"
        assert result.timing.get("zero_row_honest_empty") == 1.0


@pytest.mark.asyncio
async def test_no_escalation_onto_an_ontology_sentinel(pipeline, neptune):
    """`_fetch_ontology` does not raise on failure, it RETURNS a sentinel string,
    and both sentinels are truthy. Escalating onto one would swap a working
    semantic subset for prose and ask the model to regenerate against it."""
    from infona_client.nlp.pipeline import ONTOLOGY_EMPTY

    result, create = await _ask(
        pipeline, neptune, "how many things are there?",
        [_llm(SPROCKET_SPARQL, "guess"), _llm(WIDGET_SPARQL, "would substitute")],
        fetch_ontology=ONTOLOGY_EMPTY,
    )

    assert create.call_count == 1, "regenerated against the sentinel"
    assert result.timing.get("ontology_zero_row_escalation") is None
    assert result.ontology == SEMANTIC_SUBSET
