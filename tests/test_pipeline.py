import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infona_client.nlp.pipeline import NLQueryPipeline, get_embedding_service


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {
            "bindings": [
                {"name": {"type": "literal", "value": "Central Park"}},
            ]
        },
    }
    return client


@pytest.fixture
def pipeline(mock_neptune):
    return NLQueryPipeline(mock_neptune, "fake-key")


@pytest.mark.asyncio
async def test_ask_success(pipeline, mock_neptune):
    llm_response = json.dumps({
        "sparql": "SELECT ?name WHERE { ?s <https://schema.org/name> ?name }",
        "explanation": "Finds all names",
        "functions_needed": [],
    })
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=llm_response)]

    with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_message
        result = await pipeline.ask("What places exist?", "https://graph.onta.sh/graphs/t1")

    assert result.answer == "Central Park"
    assert "SELECT" in result.sparql
    assert result.explanation == "Finds all names"


@pytest.mark.asyncio
async def test_ask_invalid_sparql(pipeline):
    llm_response = json.dumps({
        "sparql": "DELETE WHERE { ?s ?p ?o }",
        "explanation": "Tried to delete",
        "functions_needed": [],
    })
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=llm_response)]

    with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_message
        result = await pipeline.ask("Delete everything", "https://graph.onta.sh/graphs/t1")

    assert "Could not" in result.answer
    assert "DELETE" in result.answer or "DELETE" in result.sparql


@pytest.mark.asyncio
async def test_ask_no_results(pipeline, mock_neptune):
    mock_neptune.query.side_effect = [
        {"head": {"vars": ["p"]}, "results": {"bindings": []}},
        {"head": {"vars": ["name"]}, "results": {"bindings": []}},
    ]
    llm_response = json.dumps({
        "sparql": "SELECT ?name WHERE { ?s <https://schema.org/name> ?name }",
        "explanation": "Search",
        "functions_needed": [],
    })
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=llm_response)]

    with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_message
        result = await pipeline.ask("Find something", "https://graph.onta.sh/graphs/t1")

    assert result.answer == "No results found."


@pytest.mark.asyncio
async def test_ask_uses_semantic_retrieval(pipeline, mock_neptune):
    """When embedding service returns ontology, pipeline uses it (not full fetch)."""
    mock_svc = AsyncMock()
    mock_svc.retrieve.return_value = "Type: Property\n  Attributes: price (integer)"

    llm_response = json.dumps({
        "sparql": "SELECT ?price WHERE { ?s <https://graph.onta.sh/types/Property/attrs/price> ?price }",
        "explanation": "Gets prices",
        "functions_needed": [],
    })
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=llm_response)]

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=mock_svc):
        with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_message
            result = await pipeline.ask("What is the price?", "https://graph.onta.sh/graphs/t1")

    assert result.answer == "Central Park"  # mock_neptune returns this
    mock_svc.retrieve.assert_called_once()
    assert result.timing.get("ontology_source") == "semantic"


@pytest.mark.asyncio
async def test_ask_falls_back_when_no_embeddings(pipeline, mock_neptune):
    """When embedding service returns None, pipeline falls back to full ontology."""
    mock_svc = AsyncMock()
    mock_svc.retrieve.return_value = None

    llm_response = json.dumps({
        "sparql": "SELECT ?name WHERE { ?s <https://schema.org/name> ?name }",
        "explanation": "Finds names",
        "functions_needed": [],
    })
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=llm_response)]

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=mock_svc):
        with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_message
            result = await pipeline.ask("Find something", "https://graph.onta.sh/graphs/t1")

    assert result.timing.get("ontology_source") == "full"
    assert result.answer == "Central Park"


@pytest.mark.asyncio
async def test_ask_escalates_semantic_to_full_on_zero_rows(pipeline, mock_neptune):
    """Zero rows under a semantic ontology subset widen to full schema once.

    Oliver demo RCA: semantic retrieval returned a wrong ClinicalTrial shape
    (interventions/conditions); the first SPARQL was valid but empty. Without
    escalation the pipeline answered "No results found" in one attempt.
    """
    mock_svc = AsyncMock()
    mock_svc.retrieve.return_value = (
        "Type: ClinicalTrial\n  Attributes: name, nct_id\n"
        "  Relationships: interventions, conditions"
    )

    empty = {"head": {"vars": ["x"]}, "results": {"bindings": []}}
    hit = {
        "head": {"vars": ["name"]},
        "results": {
            "bindings": [{"name": {"type": "literal", "value": "IMvigor011"}}]
        },
    }
    # Empty first attempt (+ any name-lookup broaden probes); hit on retry.
    calls = {"n": 0}

    async def _query(sparql="", *_a, **_k):
        calls["n"] += 1
        # Only the post-escalation full-schema query (attrs/name) has data. Keyed
        # on the query TEXT rather than a call counter, which drifted with the
        # number of name-lookup broaden probes.
        return hit if "attrs/name" in sparql else empty

    mock_neptune.query.side_effect = _query

    bad = json.dumps({
        "sparql": (
            "SELECT ?x WHERE { "
            "?t a <https://graph.onta.sh/types/ClinicalTrial> . "
            "?t <https://graph.onta.sh/onto/interventions> ?x }"
        ),
        "explanation": "wrong shape",
        "functions_needed": [],
    })
    good = json.dumps({
        "sparql": (
            "SELECT ?name WHERE { "
            "?t a <https://graph.onta.sh/types/ClinicalTrial> . "
            "?t <https://graph.onta.sh/types/ClinicalTrial/attrs/name> ?name }"
        ),
        "explanation": "full schema",
        "functions_needed": [],
    })
    msg_bad = MagicMock()
    msg_bad.content = [MagicMock(text=bad)]
    msg_good = MagicMock()
    msg_good.content = [MagicMock(text=good)]

    full_ont = (
        "Type: Drug\n  Attributes: brand_name\n"
        "Type: Indication\n  Attributes: disease, label_status\n"
        "Type: ClinicalTrial\n  Attributes: name, nct_id\n"
        "  Relationships: supported_by_trial (via Indication)\n"
    )

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=mock_svc):
        with patch.object(
            pipeline, "_fetch_ontology", new_callable=AsyncMock, return_value=full_ont
        ):
            with patch.object(
                pipeline.anthropic.messages, "create", new_callable=AsyncMock
            ) as mock_create:
                mock_create.side_effect = [msg_bad, msg_good]
                with patch.object(
                    # "" and not None: NLResult.narrative_answer is a str, so
                    # None makes NLResult() raise and the attempt retries, which
                    # made the assertions below pass vacuously off the
                    # "Could not answer after 3 attempts" fallback.
                    pipeline, "_rephrase_via_openrouter", new_callable=AsyncMock, return_value=""
                ):
                    result = await pipeline.ask(
                        "What trial supports Tecentriq after bladder surgery?",
                        "https://graph.onta.sh/graphs/t1",
                    )

    assert result.timing.get("ontology_zero_row_escalation") == 1.0
    assert result.timing.get("ontology_source") == "full" or result.timing.get(
        "ontology_escalated_to_full_attempt"
    )
    assert mock_create.call_count >= 2
    # Eventually answered from the second SPARQL hit.
    assert "IMvigor011" in result.answer
