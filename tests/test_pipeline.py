"""``NLQueryPipeline.ask`` end to end (ONTA-527/534: the Cypher path).

``POST /ask`` generates **Cypher** now — ``nlp/cypher_generate.py::neo4j_ask_enabled``
returns True unconditionally, so ``ask`` takes ``_ask_cypher`` and executes
through a scoped ``GraphSession``. The SPARQL generator helpers still exist in
``nlp/pipeline.py`` for unit tests that call them directly; ``ask(...,
use_cypher=False)`` is fail-closed (``SparqlAskPathRetired``, ONTA-534).

The three behavioural cases below (an answered question, a refused mutation, an
honest empty) were ported onto the Cypher path against a seeded
``MemoryGraphStore``. The three ontology-retrieval cases could not be: semantic
retrieval and the zero-row ontology escalation are implemented in ``ask``'s
SPARQL branch and ``_ask_cypher`` never runs them — see the xfail reasons.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.nlp.pipeline import NLQueryPipeline, get_embedding_service  # noqa: F401

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

TENANT = "t1"
KG = "places"
TENANT_GRAPH = f"{IRI_BASE}/graphs/{TENANT}"
KG_GRAPH = f"{TENANT_GRAPH}/kg/{KG}"
PLACE_TYPE = f"{IRI_BASE}/types/Place"
PARK = entity_uri("Place", "p1")

ONTOLOGY = "Type: Place\n  - title"


@pytest.fixture
def mock_neptune():
    """Present but never load-bearing: nothing on the Cypher path reads SPARQL.

    ``_ask_cypher`` still receives a client (``ask``'s signature is unchanged)
    and the answer formatter's URI-label lookup still reaches for it, so it
    returns an empty result set rather than raising — a raise would be
    swallowed by that formatter's own guard and prove nothing.
    """
    client = AsyncMock()
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    return client


@pytest.fixture
def store():
    st = MemoryGraphStore()
    asyncio.run(
        insert_facts(
            None,
            KG_GRAPH,
            [
                (PARK, RDF_TYPE, PLACE_TYPE),
                (PARK, LABEL, "Central Park"),
                (PARK, f"{PLACE_TYPE}/attrs/title", "Central Park"),
            ],
            store=st,
        )
    )
    return st


@pytest.fixture
def pipeline(mock_neptune, store):
    p = NLQueryPipeline(mock_neptune, "fake-key", graph_store=store)
    p._fetch_ontology = AsyncMock(return_value=ONTOLOGY)  # type: ignore[method-assign]
    return p


def _llm_cypher(payload: dict):
    """Stand in for ``_try_llm_cypher``: one canned generation, recorded."""
    calls: list[dict] = []

    async def fake(question, ontology, **kw):
        calls.append(kw)
        return dict(payload)

    return fake, calls


@pytest.mark.asyncio
async def test_ask_success(pipeline, store):
    """A generated query runs against the store and its rows become the answer.

    The generator's ``explanation`` rides through untouched, and the executed
    query text is what comes back on ``NLResult.sparql`` (field name historical
    — it carries Cypher on this path).
    """
    fake, _calls = _llm_cypher(
        {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE e.primary_type IN $type_names RETURN count(*) AS n"
            ),
            "params": {"type_names": ["Place"]},
            "template": "entities_of_type_count",
            "explanation": "Finds all names",
            "functions_needed": [],
        }
    )
    pipeline._try_llm_cypher = fake  # type: ignore[method-assign]

    result = await pipeline.ask(
        "tally the places somehow", TENANT_GRAPH, KG_GRAPH
    )

    assert result.answer == "1"
    assert result.explanation == "Finds all names"
    assert "MATCH" in result.sparql
    assert result.timing.get("query_language") == "cypher"
    assert result.timing.get("cypher_exec_path") == "template:entities_of_type_count"
    # User-visible timing row is title-cased from this key ("Cypher Exec").
    assert "cypher_exec_ms" in result.timing
    assert not any(k.startswith("neptune_exec_ms") for k in result.timing)


@pytest.mark.asyncio
async def test_ask_refuses_a_generated_mutation(pipeline, store):
    """A jailbroken generator that emits a WRITE is refused, and writes nothing.

    Ported from ``test_ask_invalid_sparql`` (a generated ``DELETE WHERE``). The
    guard moved from SPARQL shape-checking to
    ``nlp/cypher_scope.py::confine_generated_cypher``'s read-only rule, but the
    property is the same one, and it is now asserted against the STORE rather
    than only against the answer string.
    """
    async def fake(question, ontology, **kw):
        if "delete" in (question or "").lower():
            return {
                "cypher": (
                    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                    "DETACH DELETE e"
                ),
                "params": {},
                "explanation": "Tried to delete",
                "functions_needed": [],
            }
        return {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE e.primary_type IN $type_names RETURN count(*) AS n"
            ),
            "params": {"type_names": ["Place"]},
            "template": "entities_of_type_count",
            "explanation": "count places",
            "functions_needed": [],
        }

    pipeline._try_llm_cypher = fake  # type: ignore[method-assign]

    result = await pipeline.ask("Delete everything", TENANT_GRAPH, KG_GRAPH)

    assert "Could not answer" in result.answer
    assert "read-only" in result.answer.lower()
    assert "DELETE" in result.sparql
    assert result.timing.get("cypher_scope_error") == 1.0

    # The entity is still there: refusal, not a partially applied write.
    survivor = await pipeline.ask("How many places?", TENANT_GRAPH, KG_GRAPH)
    assert survivor.answer == "1"


@pytest.mark.asyncio
async def test_ask_no_results(pipeline):
    """Nothing to answer with ⇒ say so; never invent rows.

    Behaviour changed upstream (fix(ask) #338): a question naming a type the
    ontology does not have is now REFUSED before a query is generated, rather
    than generating one that returns zero rows. Both are honest, and honesty is
    what this test owns — so it accepts either, and fails if the pipeline ever
    answers as though it had data.
    """
    result = await pipeline.ask(
        "list all sprockets", TENANT_GRAPH, KG_GRAPH
    )

    answer = result.answer.lower()
    assert "no results found" in answer or "could not answer" in answer, result.answer
    assert "sprocket" not in answer.replace("sprockets", ""), "fabricated a row"
    rows = result.timing.get("rows")
    # A query ran ⇒ it must have returned nothing. It refused ⇒ no row count.
    assert rows in (0, None), rows


@pytest.mark.asyncio
async def test_ask_requires_a_per_kg_instance_graph(pipeline):
    """A bare tenant URI cannot be scoped to a workspace, and says so.

    The Cypher path derives ``(tenant_id, kg)`` from the instance graph and
    forces both onto the session, so "the tenant graph" is not a thing it can
    read. Pinned because the SPARQL path accepted this shape (the tenant graph
    was just another dataset clause) and several callers still pass it.
    """
    result = await pipeline.ask("How many places?", TENANT_GRAPH, TENANT_GRAPH)
    assert "Could not answer" in result.answer
    assert "per-KG instance graph" in result.answer


# --------------------------------------------------------------------------- #
# Ontology retrieval: implemented in ask()'s SPARQL branch only.
# --------------------------------------------------------------------------- #

_NO_SEMANTIC_RETRIEVAL = (
    "LOST CAPABILITY (ONTA-527): semantic ontology retrieval is wired into "
    "nlp/pipeline.py::ask's SPARQL branch (get_embedding_service().retrieve → "
    "timing['ontology_source'] = 'semantic' | 'full'). _ask_cypher builds its "
    "context from ontology_from_graph_store (ontology_source = "
    "'graph_store_catalog') or _fetch_ontology ('sparql_fetch') and never "
    "consults the embedding service, so the shipped /ask path does no semantic "
    "schema retrieval at all."
)


@pytest.mark.asyncio
async def test_ask_uses_semantic_retrieval(pipeline, mock_neptune):
    """When the embedding service returns an ontology, the pipeline uses it."""
    from unittest.mock import patch

    svc = AsyncMock()
    svc.retrieve.return_value = "Type: Place\n  Attributes: title (string)"
    pipeline._try_llm_cypher = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=svc):
        result = await pipeline.ask("What is the title?", TENANT_GRAPH, KG_GRAPH)

    svc.retrieve.assert_called_once()
    assert result.timing.get("ontology_source") == "semantic"


@pytest.mark.asyncio
async def test_ask_falls_back_when_no_embeddings(pipeline, mock_neptune):
    """No embeddings ⇒ the FULL ontology, marked as such in the timing."""
    from unittest.mock import patch

    svc = AsyncMock()
    svc.retrieve.return_value = None
    pipeline._try_llm_cypher = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with patch("infona_client.nlp.pipeline.get_embedding_service", return_value=svc):
        result = await pipeline.ask("list all places", TENANT_GRAPH, KG_GRAPH)

    assert result.timing.get("ontology_source") == "full"


@pytest.mark.asyncio
async def test_ask_escalates_semantic_to_full_on_zero_rows(pipeline, store):
    """Zero rows under a reduced ontology subset widen to full schema once.

    Oliver demo RCA: retrieval returned a wrong ClinicalTrial shape; the first
    query was valid but empty. Without escalation the pipeline answers "No
    results found" in one attempt.
    """
    from unittest.mock import patch

    attempts: list[str] = []

    async def fake(question, ontology, **kw):
        attempts.append(ontology)
        if len(attempts) == 1:
            # Wrong type: valid, scoped, and empty (list shape — a COUNT of 0
            # still returns one row and is not a zero-row signal).
            return {
                "cypher": (
                    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                    "WHERE e.primary_type IN $type_names "
                    "RETURN e.name AS name LIMIT 25"
                ),
                "params": {"type_names": ["ClinicalTrial"], "limit": 25},
                "explanation": "wrong shape",
                "functions_needed": [],
            }
        return {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE e.primary_type IN $type_names "
                "RETURN e.name AS name LIMIT 25"
            ),
            "params": {"type_names": ["Place"], "limit": 25},
            "explanation": "full schema",
            "functions_needed": [],
        }

    pipeline._try_llm_cypher = fake  # type: ignore[method-assign]

    # Semantic subset first (wrong shape); full fetch on zero-row escalation.
    svc = AsyncMock()
    svc.retrieve.return_value = "Type: ClinicalTrial\n  - title"
    svc.type_names = AsyncMock(return_value={"ClinicalTrial", "Place"})
    pipeline._active_types = AsyncMock(return_value={"Place"})  # type: ignore[method-assign]

    async def fake_exec(session, gen, cypher, forced_params):
        from unittest.mock import MagicMock

        tns = list(
            (forced_params or {}).get("type_names")
            or (gen.get("params") or {}).get("type_names")
            or []
        )
        if "Place" in tns:
            rec = MagicMock()
            rec.keys.return_value = ["name"]
            rec.get.side_effect = lambda k, d=None: (
                "Central Park" if k == "name" else d
            )
            return [rec], "execute_read"
        return [], "execute_read"

    with patch(
        "infona_client.nlp.pipeline.get_embedding_service", return_value=svc
    ), patch.object(
        pipeline, "_execute_confined_cypher", new=fake_exec
    ):
        result = await pipeline.ask(
            "which trial supports this?", TENANT_GRAPH, KG_GRAPH
        )

    assert result.timing.get("ontology_zero_row_escalation") == 1.0
    assert len(attempts) >= 2
    # Place entity surfaces after escalation.
    assert "Central Park" in result.answer or result.timing.get("rows", 0) >= 1
