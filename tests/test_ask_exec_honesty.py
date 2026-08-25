"""Ask execution honesty: template vs execute_read, unbound confidence, labels.

Generic SynthEvent / SynthPerson fixtures — no tenant/kg/question gold.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.rdfs_helpers_templates import RELATED_ENTITIES_CYPHER
from infona_client.graph.store import GraphRecord
from infona_client.models.query import NLQuery
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.pipeline_cypher_exec import (
    PipelineCypherExecMixin,
    _cypher_is_assertion_shaped,
)
from infona_client.nlp.pipeline_llm import skip_narrative_rephrase

SYNTH_ONTO = (
    "Type: SynthEvent\n"
    "  - event_title: string (literal, key=event_title)\n"
    "  - date: string (literal, key=date)\n"
    "  - attendee: relationship → SynthPerson\n"
    "Type: SynthPerson\n"
    "  - person_name: string (literal, key=person_name)\n"
)

ASSERTION_LIST = """
MATCH (from_e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(fc:Class)
WHERE fc.name IN $from_types
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from_e)
MATCH (a)-[:OBJECT]->(to_e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
OPTIONAL MATCH (da:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from_e)
OPTIONAL MATCH (da)-[:PREDICATE]->(dp:Property)
WHERE dp.name = $date_prop
RETURN to_e.name AS person_name, da.literal_value AS date
""".strip()

KG = f"{IRI_BASE}/graphs/demo-tenant/kg/syn-events"


def _pipe() -> NLQueryPipeline:
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYNTH_ONTO)  # type: ignore[method-assign]
    pipe._openrouter_key = ""
    return pipe


@pytest.mark.asyncio
async def test_ask_assertion_cypher_keeps_generated_return_despite_template():
    """Generated Assertion Cypher + template=related_entities → execute_read."""
    pipe = _pipe()
    fake_session_reads: list = []

    async def fake_llm(question, ontology, **kw):
        return {
            "cypher": ASSERTION_LIST,
            "template": "related_entities",
            "params": {
                "from_types": ["SynthEvent"],
                "to_types": ["SynthPerson"],
                "rel_attr": "attendee",
                "date_prop": "date",
            },
            "explanation": "people and times",
            "functions_needed": [],
        }

    real_exec = PipelineCypherExecMixin._execute_confined_cypher

    class _Sess:
        async def execute_read(self, cypher, params):
            fake_session_reads.append((cypher, params))
            return [
                GraphRecord(
                    data={"person_name": "Ada Lovelace", "date": "2024-06-01"}
                )
            ]

        async def execute_template(self, name, params):
            raise AssertionError("template must not supersede valid Assertion Cypher")

    async def call_real(session, gen, cypher, forced):
        return await real_exec(pipe, _Sess(), gen, cypher, forced)

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._execute_confined_cypher = call_real  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(  # type: ignore[method-assign]
        return_value="The most recent event was with Zeta Capital."
    )

    result = await pipe.ask(
        "share the names and times they were met",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=KG,
        use_cypher=True,
    )

    assert str(result.timing.get("cypher_exec_path") or "").startswith("execute_read")
    assert fake_session_reads
    assert "Ada Lovelace" in result.answer
    assert "2024-06-01" in result.answer
    assert "Ada_Lovelace" not in result.answer


@pytest.mark.asyncio
async def test_unbound_projection_is_not_high_confidence_and_skips_fluent_cover():
    """Unbound RETURN columns → not query_confidence high; rephrase adds no facts."""
    pipe = _pipe()

    async def fake_llm(question, ontology, **kw):
        return {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})"
                "-[:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE c.name = 'SynthEvent' "
                "OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})"
                "-[:SUBJECT]->(e) "
                "OPTIONAL MATCH (a)-[:PREDICATE]->(p:Property) "
                "WHERE p.name = 'date' "
                "RETURN e.name AS event_title, a.literal_value AS date"
            ),
            "params": {},
            "explanation": "titles and dates",
            "functions_needed": [],
        }

    rec = GraphRecord(data={"event_title": "Kickoff", "date": None})

    async def fake_exec(session, gen, cypher, forced):
        return [rec], "execute_read"

    invented = AsyncMock(
        return_value="The most recent event was with Zeta Capital on Tuesday."
    )
    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._execute_confined_cypher = fake_exec  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = invented  # type: ignore[method-assign]

    result = await pipe.ask(
        "list event titles and dates",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=KG,
        use_cypher=True,
    )

    assert result.timing.get("unbound_projection_vars") == "date"
    assert (result.query_confidence or "").lower() != "high"
    assert result.query_confidence == "low"
    invented.assert_not_awaited()
    assert result.narrative_answer == ""
    assert "Zeta Capital" not in (result.narrative_answer or "")
    assert "Zeta Capital" not in (result.answer or "")
    assert "not present" in result.answer


def test_related_entities_cypher_projects_display_label_not_only_slug():
    """Template RETURN coalesces human display fields over Entity.name slugs."""
    body = " ".join(RELATED_ENTITIES_CYPHER.split())
    assert "coalesce(from_e.display_name, from_e.title, from_e.name) AS from_name" in body
    assert "coalesce(to_e.display_name, to_e.title, to_e.name) AS to_name" in body
    assert "from_e.name AS from_name" not in body

    def _coalesce(*vals: str | None) -> str:
        for v in vals:
            if v:
                return str(v)
        return ""

    assert _coalesce("Ada Lovelace", None, "Ada_Lovelace") == "Ada Lovelace"
    assert _coalesce(None, None, "Ada_Lovelace") == "Ada_Lovelace"


def test_pronoun_followup_has_no_session_coref_on_ask():
    """`/ask` has no session_id; do not invent a coref engine this sprint."""
    assert "session_id" not in NLQuery.model_fields
    assert "session" not in NLQuery.model_fields
    assert "conversation_id" not in NLQuery.model_fields


@pytest.mark.asyncio
async def test_pronoun_question_is_forwarded_verbatim_to_generator():
    captured: dict[str, str] = {}

    async def fake_llm(question, ontology, **kw):
        captured["question"] = question
        return {
            "cypher": ASSERTION_LIST,
            "template": "related_entities",
            "params": {
                "from_types": ["SynthEvent"],
                "to_types": ["SynthPerson"],
                "rel_attr": "attendee",
                "date_prop": "date",
            },
            "explanation": "ok",
            "functions_needed": [],
        }

    pipe = _pipe()
    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [GraphRecord(data={"person_name": "Ada Lovelace", "date": "2024-06-01"})],
            "execute_read",
        )
    )
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]

    q = "what were their names and when were they met"
    await pipe.ask(
        q,
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=KG,
        use_cypher=True,
    )
    assert captured["question"] == q


def test_assertion_shape_helper_and_rephrase_skip():
    assert _cypher_is_assertion_shaped(ASSERTION_LIST) is True
    assert _cypher_is_assertion_shaped(
        "MATCH (a)-[:lead_sponsor]->(b) RETURN b.name AS name"
    ) is False
    assert skip_narrative_rephrase(["date"]) is True
    assert skip_narrative_rephrase([]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    [
        "related_entity_name_filter",
        "literal_values",
        "literal_compare",
        "entities_of_type",
    ],
)
async def test_constrained_templates_supersede_assertion_cypher(template: str):
    """Assertion-shaped body skips only related_entities, not constrained helpers."""

    class _Sess:
        def __init__(self) -> None:
            self.reads: list = []
            self.templates: list = []

        async def execute_read(self, cypher, params):
            self.reads.append((cypher, params))
            raise AssertionError(f"{template} must still supersede Assertion Cypher")

        async def execute_template(self, name, params):
            self.templates.append((name, params))
            return [GraphRecord(data={"id": "e1"})]

    session = _Sess()
    gen = {
        "cypher": ASSERTION_LIST,
        "template": template,
        "params": {
            "type_names": ["SynthEvent"],
            "rel_attr": "attendee",
            "target_name": "Ada Lovelace",
            "prop_key": "date",
            "prop_value": "2024-06-01",
            "op": "ge",
            "threshold": 1.0,
            "limit": 25,
        },
    }
    records, path = await PipelineCypherExecMixin()._execute_confined_cypher(
        session, gen, ASSERTION_LIST, {"tenant_id": "t", "kg": "k"}
    )
    assert path == f"template:{template}"
    assert session.reads == []
    assert session.templates and session.templates[0][0] == template
    assert records
