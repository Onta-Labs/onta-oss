"""Hermetic proof that the README hero question is grounded in trials.csv.

Contract + data + query path (not the LLM):

1. ``examples/trials.csv`` still contains the hero fact (AstraZeneca runs
   FLAURA2 as Phase 3 NSCLC). Guards README / demo drift.
2. Those rows are written through ``insert_facts`` into MemoryGraphStore.
3. ``NLQueryPipeline.ask`` is run with a mocked Cypher generator that emits
   valid ADR-0013 template Cypher. MemoryGraphStore executes it. The answer
   mentions FLAURA2 (or the synthetic trial id).

Production ``/ask`` stays always-LLM — this file never short-circuits that
path and does not call OpenRouter.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE, ONTO_PRED_PREFIX, TYPE_URI_PREFIX
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import env_neo4j_configured
from infona_client.nlp.cypher_generate import (
    try_filter_query,
    try_related_name_filter_query,
)
from infona_client.nlp.pipeline import NLQueryPipeline
from tests._hermetic import live_llm_opted_in

REPO = Path(__file__).resolve().parents[1]
HERO_CSV = REPO / "examples" / "trials.csv"
README = REPO / "README.md"

HERO_QUESTION = "Which Phase 3 NSCLC trials is AstraZeneca running?"
HERO_TRIAL = "FLAURA2"
HERO_TRIAL_ID = "TRIAL-FLAURA2"
HERO_SPONSOR = "AstraZeneca"
HERO_PHASE = "Phase 3"
HERO_INDICATION = "NSCLC"

TENANT = "demo-tenant"
KG = "trials"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Planning text the mocked generator (and fixture helpers) resolve against.
# Mirrors the ingest-shaped types: Trial, Sponsor, Indication + product edges.
TRIALS_ONTOLOGY = (
    "Type: Trial\n"
    "  - phase: string (literal, key=phase)\n"
    "  - status: string (literal, key=status)\n"
    "  - sponsor -> Sponsor (relationship, key=sponsor)\n"
    "  - indication -> Indication (relationship, key=indication)\n"
    "Type: Sponsor\n"
    "  - display_name: string (literal, key=display_name)\n"
    "  - runs -> Trial (relationship, key=runs)\n"
    "Type: Indication\n"
    "  - display_name: string (literal, key=display_name)\n"
)


def _load_trial_rows() -> list[dict[str, str]]:
    assert HERO_CSV.is_file(), f"missing hero CSV: {HERO_CSV}"
    with HERO_CSV.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _hero_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        r
        for r in rows
        if r.get("trial") == HERO_TRIAL
        and r.get("sponsor") == HERO_SPONSOR
        and r.get("phase") == HERO_PHASE
        and r.get("indication") == HERO_INDICATION
    ]


def _triples_from_rows(rows: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    """Ingest-shaped facts: Trial + Sponsor + Indication, onto/runs + onto/indication.

    Trial--sponsor-->Sponsor is the ADR-0013-queryable direction (subject is the
    type we return). Sponsor--runs-->Trial is the README product edge.
    """
    triples: list[tuple[str, str, str]] = []
    seen_sponsors: set[str] = set()
    seen_indications: set[str] = set()
    for row in rows:
        trial_id = (row.get("trial_id") or "").strip()
        trial_name = (row.get("trial") or "").strip()
        sponsor_name = (row.get("sponsor") or "").strip()
        indication_name = (row.get("indication") or "").strip()
        phase = (row.get("phase") or "").strip()
        status = (row.get("status") or "").strip()
        if not trial_id or not trial_name:
            continue
        trial = entity_uri("Trial", trial_id)
        triples.append((trial, RDF_TYPE, f"{TYPE_URI_PREFIX}Trial"))
        triples.append((trial, RDFS_LABEL, trial_name))
        if phase:
            triples.append((trial, f"{TYPE_URI_PREFIX}Trial/attrs/phase", phase))
        if status:
            triples.append((trial, f"{TYPE_URI_PREFIX}Trial/attrs/status", status))
        if sponsor_name:
            sponsor = entity_uri("Sponsor", sponsor_name)
            if sponsor_name not in seen_sponsors:
                seen_sponsors.add(sponsor_name)
                triples.append((sponsor, RDF_TYPE, f"{TYPE_URI_PREFIX}Sponsor"))
                triples.append((sponsor, RDFS_LABEL, sponsor_name))
            triples.append((sponsor, f"{ONTO_PRED_PREFIX}runs", trial))
            triples.append((trial, f"{ONTO_PRED_PREFIX}sponsor", sponsor))
        if indication_name:
            indication = entity_uri("Indication", indication_name)
            if indication_name not in seen_indications:
                seen_indications.add(indication_name)
                triples.append((indication, RDF_TYPE, f"{TYPE_URI_PREFIX}Indication"))
                triples.append((indication, RDFS_LABEL, indication_name))
            triples.append((trial, f"{ONTO_PRED_PREFIX}indication", indication))
    return triples


async def _seed_trials(store: MemoryGraphStore) -> list[dict[str, str]]:
    rows = _load_trial_rows()
    triples = _triples_from_rows(rows)
    assert triples, "hero CSV produced no insert_facts triples"
    await insert_facts(None, GRAPH, triples, store=store)
    return rows


def _llm_payload(question: str, ontology: str) -> dict:
    """ADR-0013 payload the mocked generator returns (not called by product /ask).

    The hero question itself is not a fixture string. Helpers build the same
    templates MemoryGraphStore can execute: sponsor / indication name filter,
    or phase literal equality.
    """
    q = (question or "").strip()
    payload = None
    if HERO_INDICATION in q and HERO_SPONSOR not in q:
        payload = try_related_name_filter_query(
            f"trials with indication {HERO_INDICATION}", ontology
        )
    elif HERO_PHASE in q and HERO_SPONSOR not in q and HERO_INDICATION not in q:
        payload = try_filter_query(f"trials where phase is {HERO_PHASE}", ontology)
    else:
        # Default / hero: AstraZeneca running → Trial--sponsor-->AstraZeneca.
        payload = try_related_name_filter_query(
            f"trials with sponsor {HERO_SPONSOR}", ontology
        )
    assert payload is not None, f"fixture helper produced no Cypher for {q!r}"
    out = dict(payload)
    out.pop("fixture", None)
    out.pop("stub", None)
    return out


def _wire_hero_llm(pipe: NLQueryPipeline) -> list[str]:
    questions: list[str] = []

    async def fake(question: str, ontology: str, **kw):
        questions.append(question)
        return _llm_payload(question, ontology)

    pipe._try_llm_cypher = fake  # type: ignore[method-assign]
    return questions


def _pipe(store: MemoryGraphStore) -> tuple[NLQueryPipeline, MagicMock, list[str]]:
    # Residual SPARQL is only used for post-answer URI→label lookup, not /ask.
    neptune = MagicMock()
    neptune.query = AsyncMock(
        return_value={"head": {"vars": []}, "results": {"bindings": []}}
    )
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=TRIALS_ONTOLOGY)  # type: ignore[method-assign]
    llm_qs = _wire_hero_llm(pipe)
    return pipe, neptune, llm_qs


def _mentions_hero(text: str) -> bool:
    blob = text or ""
    return HERO_TRIAL in blob or HERO_TRIAL_ID in blob


# ---------------------------------------------------------------------------
# CSV / README fixture contract
# ---------------------------------------------------------------------------


def test_hero_csv_contains_astrazeneca_flaura2_fact():
    rows = _load_trial_rows()
    assert rows, f"{HERO_CSV} is empty"
    matches = _hero_rows(rows)
    assert matches, (
        f"{HERO_CSV} must contain AstraZeneca + {HERO_PHASE} + {HERO_INDICATION} "
        f"+ {HERO_TRIAL} (README hero fact drifted)"
    )
    row = matches[0]
    assert row["trial_id"] == HERO_TRIAL_ID
    assert row["status"] in {"Active", "Completed"}
    # FLAURA is the sibling AZ Phase 3 NSCLC program named in the README.
    flaura = [
        r
        for r in rows
        if r.get("trial") == "FLAURA"
        and r.get("sponsor") == HERO_SPONSOR
        and r.get("phase") == HERO_PHASE
        and r.get("indication") == HERO_INDICATION
    ]
    assert flaura, f"{HERO_CSV} must still include sibling FLAURA as {HERO_PHASE} NSCLC"


def test_readme_still_asks_the_hero_question():
    text = README.read_text(encoding="utf-8")
    assert HERO_QUESTION in text
    assert HERO_TRIAL in text
    assert "examples/trials.csv" in text


# ---------------------------------------------------------------------------
# Hermetic ingest-shaped write + /ask (MemoryGraphStore executes Cypher)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hero_ask_executes_adr0013_cypher_against_seeded_store():
    store = MemoryGraphStore()
    await _seed_trials(store)
    assert store.entity_count(tenant_id=TENANT, kg=KG) >= 3

    pipe, _neptune, llm_qs = _pipe(store)
    result = await pipe.ask(
        HERO_QUESTION,
        graph_uri=f"{IRI_BASE}/graphs/{TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )

    assert llm_qs == [HERO_QUESTION]
    assert result.timing.get("query_language") == "cypher"
    assert (
        result.timing.get("cypher_exec_path")
        == "template:related_entity_name_filter"
    )
    assert int(result.timing.get("rows") or 0) >= 1
    assert "$tenant_id" in (result.sparql or "")
    assert _mentions_hero(result.answer), (
        f"hero ask must mention {HERO_TRIAL} (or {HERO_TRIAL_ID}); "
        f"got {result.answer!r}"
    )


@pytest.mark.asyncio
async def test_hero_indication_filter_excludes_sclc():
    """Product edge onto/indication is queryable: NSCLC hits FLAURA2, not CASPIAN."""
    store = MemoryGraphStore()
    await _seed_trials(store)
    pipe, _, _ = _pipe(store)
    result = await pipe.ask(
        f"Which trials have indication {HERO_INDICATION}?",
        graph_uri=f"{IRI_BASE}/graphs/{TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )
    assert result.timing.get("cypher_exec_path") == (
        "template:related_entity_name_filter"
    )
    assert _mentions_hero(result.answer)
    assert "CASPIAN" not in (result.answer or "")


@pytest.mark.asyncio
async def test_hero_phase_literal_includes_flaura2_not_phase2():
    store = MemoryGraphStore()
    await _seed_trials(store)
    pipe, _, _ = _pipe(store)
    result = await pipe.ask(
        f"trials where phase is {HERO_PHASE}",
        graph_uri=f"{IRI_BASE}/graphs/{TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )
    assert result.timing.get("cypher_exec_path") == "template:literal_values"
    assert _mentions_hero(result.answer)
    assert "DESTINY-Lung01" not in (result.answer or "")


@pytest.mark.asyncio
async def test_hero_ask_never_calls_deterministic_fixtures(monkeypatch):
    """Product rule: user-facing /ask does not short-circuit to fixture helpers."""
    store = MemoryGraphStore()
    await _seed_trials(store)
    pipe, _, llm_qs = _pipe(store)

    det_calls: list[tuple] = []

    def spy_det(*args, **kwargs):
        det_calls.append((args, kwargs))
        raise AssertionError("try_deterministic_cypher must not run on /ask")

    monkeypatch.setattr(
        "infona_client.nlp.cypher_generate.try_deterministic_cypher", spy_det
    )
    monkeypatch.setattr(
        "infona_client.nlp.pipeline.try_deterministic_cypher",
        spy_det,
        raising=False,
    )

    result = await pipe.ask(
        HERO_QUESTION,
        graph_uri=f"{IRI_BASE}/graphs/{TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )
    assert not det_calls
    assert llm_qs == [HERO_QUESTION]
    assert _mentions_hero(result.answer)


# ---------------------------------------------------------------------------
# Optional live Neo4j — skipped in default CI (no NEO4J_URI / OpenRouter)
# ---------------------------------------------------------------------------


def _live_hero_ready() -> bool:
    has_openrouter = bool(
        (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        or (os.environ.get("INFONA_OPENROUTER_API_KEY") or "").strip()
    )
    return env_neo4j_configured() and has_openrouter and live_llm_opted_in()


@pytest.mark.neo4j
@pytest.mark.integration
@pytest.mark.skipif(
    not _live_hero_ready(),
    reason=(
        "needs NEO4J_URI + NEO4J_PASSWORD, OPENROUTER_API_KEY, and "
        "INFONA_TEST_ALLOW_LIVE_LLM=1 (default CI stays green)"
    ),
)
@pytest.mark.asyncio
async def test_hero_ask_live_neo4j_seeded_store():
    """Same data + ADR-0013 Cypher path against live Neo4j. LLM still mocked."""
    from infona_client.graph.neo4j_store import Neo4jGraphStore
    from infona_client.graph.scope import GraphScope

    uri = os.environ["NEO4J_URI"].strip()
    user = (os.environ.get("NEO4J_USER") or "neo4j").strip() or "neo4j"
    password = os.environ["NEO4J_PASSWORD"]
    database = (os.environ.get("NEO4J_DATABASE") or "").strip() or None
    store = Neo4jGraphStore(uri=uri, user=user, password=password, database=database)
    if not await store.health():
        await store.close()
        pytest.skip("Neo4j not reachable at NEO4J_URI")

    tenant = "hero-live"
    kg = "trials"
    graph = f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"
    try:
        await store.bootstrap_schema()
        rows = _load_trial_rows()
        await insert_facts(None, graph, _triples_from_rows(rows), store=store)

        neptune = MagicMock()
        neptune.query = AsyncMock(
            return_value={"head": {"vars": []}, "results": {"bindings": []}}
        )
        pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
        pipe._fetch_ontology = AsyncMock(  # type: ignore[method-assign]
            return_value=TRIALS_ONTOLOGY
        )
        _wire_hero_llm(pipe)
        result = await pipe.ask(
            HERO_QUESTION,
            graph_uri=f"{IRI_BASE}/graphs/{tenant}",
            instance_graph=graph,
            use_cypher=True,
        )
        assert result.timing.get("query_language") == "cypher"
        assert _mentions_hero(result.answer)
    finally:
        try:
            session = store.session(GraphScope.for_instance(tenant, kg))
            await session.execute_write(
                "MATCH (n {tenant_id: $tenant_id, kg: $kg}) DETACH DELETE n",
                {"tenant_id": tenant, "kg": kg},
            )
        except Exception:
            pass
        await store.close()
