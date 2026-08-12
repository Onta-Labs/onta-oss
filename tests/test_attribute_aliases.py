"""Attribute alias mechanism tests (COG-40, ADR 0002 §7).

Covers: alias registration/retirement SPARQL, alias-map chain flattening +
cycle guard, the conservative full-IRI query rewriter, the batched lazy
backfill, and the NL pipeline wiring — including the regression-critical
default path (feature OFF / zero aliases => zero behavior change).

All mocked — no live Neptune, no LLM, no network.

**ONTA-527 note.** ``graph/aliases.py`` is still SPARQL-only: every function
below builds a query string and hands it to a ``NeptuneClient``, and there is no
GraphStore equivalent, so on the shipped Neo4j backend none of it can run. The
builder/parser tests keep their value (chain flattening, the cycle guard, the
prefix-overlap rule in the rewriter are backend-independent logic that a port
must preserve) and are left as they are; the pipeline-wiring block at the bottom
was rewritten onto the Cypher path, where it documents that alias resolution
never happens at query time any more.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.aliases import (
    ALIAS_OF,
    AliasStillReferencedError,
    backfill_aliases,
    count_attr_references,
    fetch_alias_map,
    register_alias,
    retire_alias,
    rewrite_query_attrs,
)
from infona_client.graph.client import NeptuneClient
from infona_client.nlp.pipeline import NLQueryPipeline

ONTO_GRAPH = "https://graph.infona.ai/graphs/t-alias"
DATA_GRAPH = "https://graph.infona.ai/graphs/t-alias/kg/main"

PHONE_NUM = "https://graph.infona.ai/types/Guest/attrs/phone_num"
PHONE = "https://graph.infona.ai/types/Guest/attrs/phone"
CONTACT = "https://graph.infona.ai/types/Person/attrs/contact_phone"


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    return client


def _alias_bindings(*pairs: tuple[str, str]) -> dict:
    return {
        "head": {"vars": ["old", "new"]},
        "results": {
            "bindings": [
                {
                    "old": {"type": "uri", "value": old},
                    "new": {"type": "uri", "value": new},
                }
                for old, new in pairs
            ]
        },
    }


def _count_result(n: int) -> dict:
    return {
        "head": {"vars": ["n"]},
        "results": {"bindings": [{"n": {"type": "literal", "value": str(n)}}]},
    }


# ---------------------------------------------------------------------------
# register / retire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_alias_writes_alias_triple(mock_neptune):
    await register_alias(mock_neptune, ONTO_GRAPH, PHONE_NUM, PHONE)
    sparql = mock_neptune.update.call_args.args[0]
    assert "INSERT DATA" in sparql
    assert f"GRAPH <{ONTO_GRAPH}>" in sparql
    assert f"<{PHONE_NUM}> <{ALIAS_OF}> <{PHONE}> ." in sparql


@pytest.mark.asyncio
async def test_register_alias_rejects_self_alias(mock_neptune):
    with pytest.raises(ValueError):
        await register_alias(mock_neptune, ONTO_GRAPH, PHONE, PHONE)
    mock_neptune.update.assert_not_called()


@pytest.mark.asyncio
async def test_retire_alias_deletes_alias_triple(mock_neptune):
    await retire_alias(mock_neptune, ONTO_GRAPH, PHONE_NUM)
    sparql = mock_neptune.update.call_args.args[0]
    assert "DELETE WHERE" in sparql
    assert f"<{PHONE_NUM}> <{ALIAS_OF}> ?new" in sparql


@pytest.mark.asyncio
async def test_retire_alias_refuses_while_refs_remain(mock_neptune):
    """ONTA-407b: real reference check — retirement blocked while count > 0."""
    mock_neptune.query.return_value = _count_result(3)
    with pytest.raises(AliasStillReferencedError) as ei:
        await retire_alias(
            mock_neptune, ONTO_GRAPH, PHONE_NUM, data_graph_uri=DATA_GRAPH,
        )
    assert ei.value.remaining == 3
    assert ei.value.old_attr_uri == PHONE_NUM
    assert ei.value.data_graph_uri == DATA_GRAPH
    mock_neptune.update.assert_not_called()


@pytest.mark.asyncio
async def test_retire_alias_ok_when_zero_refs(mock_neptune):
    mock_neptune.query.return_value = _count_result(0)
    await retire_alias(
        mock_neptune, ONTO_GRAPH, PHONE_NUM, data_graph_uri=DATA_GRAPH,
    )
    sparql = mock_neptune.update.call_args.args[0]
    assert "DELETE WHERE" in sparql


@pytest.mark.asyncio
async def test_count_attr_references(mock_neptune):
    mock_neptune.query.return_value = _count_result(7)
    n = await count_attr_references(mock_neptune, DATA_GRAPH, PHONE_NUM)
    assert n == 7
    assert f"<{PHONE_NUM}>" in mock_neptune.query.call_args.args[0]


# ---------------------------------------------------------------------------
# fetch_alias_map — chain flattening + cycle guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_alias_map_flattens_chains(mock_neptune):
    """a -> b -> c resolves to {a: c, b: c} — every rewrite is one hop."""
    mock_neptune.query.return_value = _alias_bindings(
        (PHONE_NUM, PHONE), (PHONE, CONTACT),
    )
    got = await fetch_alias_map(mock_neptune, ONTO_GRAPH)
    assert got == {PHONE_NUM: CONTACT, PHONE: CONTACT}


@pytest.mark.asyncio
async def test_fetch_alias_map_drops_cycles(mock_neptune):
    """a -> b -> a is nonsensical alias data: both entries dropped, no hang."""
    mock_neptune.query.return_value = _alias_bindings(
        (PHONE_NUM, PHONE), (PHONE, PHONE_NUM),
    )
    got = await fetch_alias_map(mock_neptune, ONTO_GRAPH)
    assert got == {}


@pytest.mark.asyncio
async def test_fetch_alias_map_self_cycle_dropped_others_kept(mock_neptune):
    mock_neptune.query.return_value = _alias_bindings(
        (PHONE, PHONE),          # self-alias: dropped
        (PHONE_NUM, CONTACT),    # independent alias: kept
    )
    got = await fetch_alias_map(mock_neptune, ONTO_GRAPH)
    assert got == {PHONE_NUM: CONTACT}


# ---------------------------------------------------------------------------
# rewrite_query_attrs — full-IRI matches only
# ---------------------------------------------------------------------------


def test_rewrite_query_attrs_rewrites_full_iri():
    q = f"SELECT ?v WHERE {{ ?g <{PHONE_NUM}> ?v }}"
    out = rewrite_query_attrs(q, {PHONE_NUM: PHONE})
    assert f"<{PHONE}>" in out
    assert f"<{PHONE_NUM}>" not in out


def test_rewrite_query_attrs_ignores_prefix_overlap():
    """`<.../attrs/phone>` must NOT fire inside `<.../attrs/phone_num>`."""
    q = f"SELECT ?v WHERE {{ ?g <{PHONE_NUM}> ?v }}"
    assert rewrite_query_attrs(q, {PHONE: CONTACT}) == q


def test_rewrite_query_attrs_single_pass_no_rechaining():
    """An unflattened map (a->b, b->c) must not double-rewrite a to c."""
    q = f"SELECT ?v WHERE {{ ?g <{PHONE_NUM}> ?v }}"
    out = rewrite_query_attrs(q, {PHONE_NUM: PHONE, PHONE: CONTACT})
    assert f"<{PHONE}>" in out and f"<{CONTACT}>" not in out


def test_rewrite_query_attrs_empty_map_is_identity():
    q = f"SELECT ?v WHERE {{ ?g <{PHONE_NUM}> ?v }}"
    assert rewrite_query_attrs(q, {}) == q


# ---------------------------------------------------------------------------
# backfill_aliases — batched DELETE/INSERT WHERE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_emits_batched_delete_insert(mock_neptune):
    """2500 triples at batch_size=1000 => 3 batched updates, count returned."""
    mock_neptune.query.return_value = _count_result(2500)

    rewritten = await backfill_aliases(
        mock_neptune, DATA_GRAPH, {PHONE_NUM: PHONE}, batch_size=1000,
    )

    assert rewritten == 2500
    updates = [c.args[0] for c in mock_neptune.update.call_args_list]
    assert len(updates) == 3
    for sparql in updates:
        assert f"DELETE {{ GRAPH <{DATA_GRAPH}> {{ ?s <{PHONE_NUM}> ?o }} }}" in sparql
        assert f"INSERT {{ GRAPH <{DATA_GRAPH}> {{ ?s <{PHONE}> ?o }} }}" in sparql
        assert "LIMIT 1000" in sparql
    # The count probe ran against the old predicate in the data graph.
    count_sparql = mock_neptune.query.call_args.args[0]
    assert "COUNT" in count_sparql and f"<{PHONE_NUM}>" in count_sparql


@pytest.mark.asyncio
async def test_backfill_zero_triples_no_updates(mock_neptune):
    mock_neptune.query.return_value = _count_result(0)
    rewritten = await backfill_aliases(mock_neptune, DATA_GRAPH, {PHONE_NUM: PHONE})
    assert rewritten == 0
    mock_neptune.update.assert_not_called()


# ---------------------------------------------------------------------------
# NL pipeline wiring (gated by INFONA_ALIASES_ENABLED)
#
# Ported by ONTA-527. These three cases used to stub an LLM into returning
# SPARQL, hand it to `pipeline.ask`, and read the alias rewrite back out of the
# executed query string. Production is Neo4j-only: `ask` dispatches to
# `_ask_cypher` (neo4j_ask_enabled() is unconditionally True) and the SPARQL
# branch — the ONLY place `_fetch_alias_map` and `_fix_common_sparql_issues`'s
# alias pass are wired — never runs. So they are re-expressed on the Cypher
# path: the two that assert the feature costs nothing when it is off or empty
# still pass, and the one that asserts the feature WORKS is a strict xfail,
# because it does not.
# ---------------------------------------------------------------------------


TENANT = "test-tenant"
KG = "guests"
KG_GRAPH = f"https://graph.infona.ai/graphs/{TENANT}/kg/{KG}"
TENANT_GRAPH = f"https://graph.infona.ai/graphs/{TENANT}"
GUEST_ONTOLOGY = "Type: Guest\n  - phone\n  - phone_num"


async def _seeded_store():
    """One Guest whose phone lives under the NEW attribute leaf."""
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.rdf_model import AssertionFact, assert_fact
    from infona_client.graph.scope import GraphScope

    store = MemoryGraphStore()
    session = store.session(GraphScope.for_instance(TENANT, KG))
    guest = "https://graph.infona.ai/entities/Guest/g1"
    await assert_fact(
        session, AssertionFact(subject_id=guest, kind="type", value="Guest"),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=guest, kind="literal", property_leaf="name", value="Ada"
        ),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=guest, kind="literal", property_leaf="phone", value="555-0100"
        ),
        dual_write_cache=True,
    )
    return store


def _recording_neptune():
    """A SPARQL client that answers nothing and records what it was asked.

    The Cypher path still makes one best-effort SPARQL call (`_resolve_uri_labels`
    — a residual NeptuneClient reference ONTA-527's ratchet is counting down), so
    the assertion below is the original one: whatever SPARQL is issued, none of it
    is an alias-map lookup.
    """
    client = MagicMock()
    client.query = AsyncMock(
        return_value={"head": {"vars": []}, "results": {"bindings": []}}
    )
    client.update = AsyncMock(return_value=None)
    return client


def _pipeline(store, neptune):
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._openrouter_key = ""
    pipe._fetch_ontology = AsyncMock(return_value=GUEST_ONTOLOGY)  # type: ignore[method-assign]
    return pipe


async def _ask(pipe, question="list all guests"):
    return await pipe.ask(question, TENANT_GRAPH, instance_graph=KG_GRAPH)


@pytest.mark.asyncio
async def test_pipeline_default_off_issues_no_alias_lookup(monkeypatch):
    """Regression guard: with the flag unset (the default) /ask answers without
    ever looking an alias map up — zero aliases registered, zero cost."""
    monkeypatch.delenv("INFONA_ALIASES_ENABLED", raising=False)
    store = await _seeded_store()
    neptune = _recording_neptune()
    pipe = _pipeline(store, neptune)
    assert pipe._aliases_enabled is False

    fetch_map = AsyncMock(return_value={})
    monkeypatch.setattr(pipe, "_fetch_alias_map", fetch_map)

    result = await _ask(pipe)
    assert result.timing.get("query_language") == "cypher"
    assert "Ada" in result.answer
    fetch_map.assert_not_awaited()
    for call in neptune.query.call_args_list:
        assert "aliasOf" not in call.args[0]


@pytest.mark.asyncio
async def test_pipeline_enabled_zero_aliases_unchanged(monkeypatch):
    """Flag on but nothing registered: byte-identical query and answer.

    The regression this guards is "turning the feature on changes answers even
    when there are no aliases", which must stay false on any backend.
    """
    store = await _seeded_store()
    neptune = _recording_neptune()

    monkeypatch.delenv("INFONA_ALIASES_ENABLED", raising=False)
    off = await _ask(_pipeline(store, neptune))

    monkeypatch.setenv("INFONA_ALIASES_ENABLED", "1")
    pipe_on = _pipeline(store, neptune)
    assert pipe_on._aliases_enabled is True
    monkeypatch.setattr(pipe_on, "_fetch_alias_map", AsyncMock(return_value={}))
    on = await _ask(pipe_on)

    assert on.sparql == off.sparql
    assert on.answer == off.answer


@pytest.mark.xfail(
    reason=(
        "BUG (ONTA-527 port gap): attribute aliases are inert on Neo4j. "
        "nlp/pipeline.py resolves them in exactly one place — the SPARQL branch "
        "of ask() fetches _fetch_alias_map and _fix_common_sparql_issues rewrites "
        "the query text with graph/aliases.rewrite_query_attrs — and that branch "
        "is unreachable now that neo4j_ask_enabled() is unconditionally True. "
        "_ask_cypher never consults an alias map and confine_generated_cypher has "
        "no alias step, so after a rename a question phrased with the old "
        "attribute silently stops answering (ADR 0002 §7's whole point). The "
        "write half is gone too: commit_ontology's GraphStore branch drops "
        "REGISTER_ALIAS/RENAME_ATTRIBUTE, so no alias can be recorded to resolve."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_pipeline_enabled_resolves_aliases_on_the_cypher_path(monkeypatch):
    """Flag on + one alias registered: /ask must resolve it before executing."""
    monkeypatch.setenv("INFONA_ALIASES_ENABLED", "1")
    store = await _seeded_store()
    neptune = _recording_neptune()
    pipe = _pipeline(store, neptune)
    assert pipe._aliases_enabled is True

    fetch_map = AsyncMock(return_value={PHONE_NUM: PHONE})
    monkeypatch.setattr(pipe, "_fetch_alias_map", fetch_map)

    result = await _ask(pipe, "guests where phone_num is 555-0100")
    assert result.timing.get("query_language") == "cypher"
    fetch_map.assert_awaited()
    assert "phone_num" not in result.sparql


def test_fix_common_sparql_issues_default_signature_unchanged():
    """Backward-compat: the two-arg call (no alias_map) still works and the
    alias pass is a no-op."""
    q = f"SELECT ?v WHERE {{ ?g <{PHONE_NUM}> ?v }}"
    out = NLQueryPipeline._fix_common_sparql_issues(q, "")
    assert f"<{PHONE_NUM}>" in out
