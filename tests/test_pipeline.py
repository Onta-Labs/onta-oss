import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnix.nlp.pipeline import NLQueryPipeline, get_embedding_service


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
        result = await pipeline.ask("What places exist?", "https://omnix.dev/graphs/t1")

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
        result = await pipeline.ask("Delete everything", "https://omnix.dev/graphs/t1")

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
        result = await pipeline.ask("Find something", "https://omnix.dev/graphs/t1")

    assert result.answer == "No results found."


@pytest.mark.asyncio
async def test_ask_uses_semantic_retrieval(pipeline, mock_neptune):
    """When embedding service returns ontology, pipeline uses it (not full fetch)."""
    mock_svc = AsyncMock()
    mock_svc.retrieve.return_value = "Type: Property\n  Attributes: price (integer)"

    llm_response = json.dumps({
        "sparql": "SELECT ?price WHERE { ?s <https://omnix.dev/types/Property/attrs/price> ?price }",
        "explanation": "Gets prices",
        "functions_needed": [],
    })
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=llm_response)]

    with patch("omnix.nlp.pipeline.get_embedding_service", return_value=mock_svc):
        with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_message
            result = await pipeline.ask("What is the price?", "https://omnix.dev/graphs/t1")

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

    with patch("omnix.nlp.pipeline.get_embedding_service", return_value=mock_svc):
        with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_message
            result = await pipeline.ask("Find something", "https://omnix.dev/graphs/t1")

    assert result.timing.get("ontology_source") == "full"
    assert result.answer == "Central Park"
