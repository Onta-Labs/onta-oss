"""Attribute alias mechanism tests (COG-40, ADR 0002 §7).

Covers: alias registration/retirement (GraphStore companion bag), alias-map
chain flattening + cycle guard, the conservative full-IRI query rewriter, the
batched lazy backfill (SPARQL batch builder, gated when a process store is
present), and the NL pipeline wiring — including the regression-critical
default path (feature OFF / zero aliases => zero behavior change).

Hermetic suite injects a ``MemoryGraphStore`` for every test
(``tests/conftest.py::_hermetic_graph_store``). Production alias authoring
therefore lands on the ontology companion bag (ONTA-531), not SPARQL
``INSERT DATA`` / ``DELETE WHERE``. The register / retire / fetch / count
tests exercise that path. The pure SPARQL batch-builder test for backfill
clears the process store so the SPARQL mock path remains unit-testable.

**ONTA-530 / ONTA-531 note.** Alias *authoring* lands on the ontology companion
bag (ONTA-531 GraphStore register/retire). At /ask time, when
``INFONA_ALIASES_ENABLED`` and a non-empty alias map are present, ``_ask_cypher``
rewrite-only renames leaf tokens in the generated Cypher string
(``_rewrite_cypher_alias_leaves``); empty map / flag off ⇒ zero behavior change.
The builder/parser tests keep their value (chain flattening, cycle guard,
prefix-overlap rule).
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
from infona_client.graph.ontology_companion import (
    get_ontology_companion,
    live_graph_uri,
)
from infona_client.graph.store import get_graph_store, reset_graph_store_for_tests
from infona_client.nlp.pipeline import NLQueryPipeline

ONTO_GRAPH = "https://graph.infona.ai/graphs/t-alias"
DATA_GRAPH = "https://graph.infona.ai/graphs/t-alias/kg/main"
TENANT = "t-alias"
KG = "main"

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


def _seed_entity_props(leaf: str, n: int) -> None:
    """Plant ``n`` MemoryGraphStore entities carrying ``leaf`` as a prop.

    Used by the GraphStore reference-count path
    (``_count_attr_references_graph_store``).
    """
    from infona_client.graph.memory_store import _EntityRow

    store = get_graph_store()
    for i in range(n):
        eid = f"https://graph.infona.ai/entities/Guest/g{i}"
        store._entities[(TENANT, KG, eid)] = _EntityRow(  # type: ignore[attr-defined]
            tenant_id=TENANT,
            kg=KG,
            id=eid,
            primary_type="Guest",
            props={leaf: f"val-{i}"},
        )


def _companion_aliases() -> dict[str, str]:
    live = live_graph_uri(ONTO_GRAPH)
    return get_ontology_companion().aliases.get(live) or {}


# ---------------------------------------------------------------------------
# register / retire (GraphStore companion bag — ONTA-531)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_alias_writes_alias_triple(mock_neptune):
    """With a process store present, register lands on the companion bag."""
    await register_alias(mock_neptune, ONTO_GRAPH, PHONE_NUM, PHONE)
    mock_neptune.update.assert_not_called()
    assert _companion_aliases() == {PHONE_NUM: PHONE}


@pytest.mark.asyncio
async def test_register_alias_rejects_self_alias(mock_neptune):
    with pytest.raises(ValueError):
        await register_alias(mock_neptune, ONTO_GRAPH, PHONE, PHONE)
    mock_neptune.update.assert_not_called()
    assert _companion_aliases() == {}


@pytest.mark.asyncio
async def test_retire_alias_deletes_alias_triple(mock_neptune):
    await register_alias(mock_neptune, ONTO_GRAPH, PHONE_NUM, PHONE)
    assert PHONE_NUM in _companion_aliases()

    await retire_alias(mock_neptune, ONTO_GRAPH, PHONE_NUM)
    mock_neptune.update.assert_not_called()
    assert PHONE_NUM not in _companion_aliases()


@pytest.mark.asyncio
async def test_retire_alias_refuses_while_refs_remain(mock_neptune):
    """ONTA-407b: real reference check — retirement blocked while count > 0."""
    _seed_entity_props("phone_num", 3)
    await register_alias(mock_neptune, ONTO_GRAPH, PHONE_NUM, PHONE)

    with pytest.raises(AliasStillReferencedError) as ei:
        await retire_alias(
            mock_neptune, ONTO_GRAPH, PHONE_NUM, data_graph_uri=DATA_GRAPH,
        )
    assert ei.value.remaining == 3
    assert ei.value.old_attr_uri == PHONE_NUM
    assert ei.value.data_graph_uri == DATA_GRAPH
    mock_neptune.update.assert_not_called()
    # Alias remains recorded until a successful retire.
    assert _companion_aliases().get(PHONE_NUM) == PHONE


@pytest.mark.asyncio
async def test_retire_alias_ok_when_zero_refs(mock_neptune):
    await register_alias(mock_neptune, ONTO_GRAPH, PHONE_NUM, PHONE)
    # No instance props seeded → GraphStore count is 0.
    await retire_alias(
        mock_neptune, ONTO_GRAPH, PHONE_NUM, data_graph_uri=DATA_GRAPH,
    )
    mock_neptune.update.assert_not_called()
    assert PHONE_NUM not in _companion_aliases()


@pytest.mark.asyncio
async def test_count_attr_references(mock_neptune):
    _seed_entity_props("phone_num", 7)
    n = await count_attr_references(mock_neptune, DATA_GRAPH, PHONE_NUM)
    assert n == 7
    # GraphStore path — no SPARQL count probe.
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_alias_map — chain flattening + cycle guard (companion edges)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_alias_map_flattens_chains(mock_neptune):
    """a -> b -> c resolves to {a: c, b: c} — every rewrite is one hop."""
    live = live_graph_uri(ONTO_GRAPH)
    get_ontology_companion().aliases[live] = {
        PHONE_NUM: PHONE,
        PHONE: CONTACT,
    }
    got = await fetch_alias_map(mock_neptune, ONTO_GRAPH)
    assert got == {PHONE_NUM: CONTACT, PHONE: CONTACT}
    mock_neptune.query.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_alias_map_drops_cycles(mock_neptune):
    """a -> b -> a is nonsensical alias data: both entries dropped, no hang."""
    live = live_graph_uri(ONTO_GRAPH)
    get_ontology_companion().aliases[live] = {
        PHONE_NUM: PHONE,
        PHONE: PHONE_NUM,
    }
    got = await fetch_alias_map(mock_neptune, ONTO_GRAPH)
    assert got == {}


@pytest.mark.asyncio
async def test_fetch_alias_map_self_cycle_dropped_others_kept(mock_neptune):
    live = live_graph_uri(ONTO_GRAPH)
    get_ontology_companion().aliases[live] = {
        PHONE: PHONE,            # self-alias: dropped
        PHONE_NUM: CONTACT,      # independent alias: kept
    }
    got = await fetch_alias_map(mock_neptune, ONTO_GRAPH)
    assert got == {PHONE_NUM: CONTACT}


# ---------------------------------------------------------------------------
# rewrite_query_attrs — full-IRI matches only (backend-independent)
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
# backfill_aliases — batched DELETE/INSERT WHERE (SPARQL batch builder)
#
# Instance rewrites are still SPARQL-shaped; count/register now prefer the
# process GraphStore. Clear the store so this unit test exercises the SPARQL
# batch path with a mocked Neptune client (same pattern as other residual
# SPARQL unit tests under the hermetic GraphStore fixture).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_emits_batched_delete_insert(mock_neptune):
    """2500 triples at batch_size=1000 => 3 batched updates, count returned."""
    reset_graph_store_for_tests()
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
    # GraphStore present, zero instance props → count 0, no SPARQL updates.
    rewritten = await backfill_aliases(mock_neptune, DATA_GRAPH, {PHONE_NUM: PHONE})
    assert rewritten == 0
    mock_neptune.update.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_uses_graph_store_count_then_sparql_batches(mock_neptune):
    """Hybrid path: GraphStore counts refs; rewrite batches still hit Neptune."""
    _seed_entity_props("phone_num", 5)
    rewritten = await backfill_aliases(
        mock_neptune, DATA_GRAPH, {PHONE_NUM: PHONE}, batch_size=2,
    )
    assert rewritten == 5
    assert len(mock_neptune.update.call_args_list) == 3  # ceil(5/2)
    mock_neptune.query.assert_not_called()  # count came from the store


# ---------------------------------------------------------------------------
# NL pipeline wiring (gated by INFONA_ALIASES_ENABLED)
#
# ONTA-530: `_ask_cypher` fetches the alias map when the flag is on and, when
# the map is non-empty, rewrite-only renames old attribute leaves in the
# Cypher text (`_rewrite_cypher_alias_leaves`). Flag off / empty map still
# cost nothing. The positive case asserts map fetch + leaf rewrite on the
# Cypher path (no longer SPARQL `_fix_common_sparql_issues`).
# ---------------------------------------------------------------------------

PIPE_TENANT = "test-tenant"
PIPE_KG = "guests"
KG_GRAPH = f"https://graph.infona.ai/graphs/{PIPE_TENANT}/kg/{PIPE_KG}"
TENANT_GRAPH = f"https://graph.infona.ai/graphs/{PIPE_TENANT}"
GUEST_ONTOLOGY = "Type: Guest\n  - phone\n  - phone_num"


async def _seeded_store():
    """One Guest whose phone lives under the NEW attribute leaf."""
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.rdf_model import AssertionFact, assert_fact
    from infona_client.graph.scope import GraphScope

    store = MemoryGraphStore()
    session = store.session(GraphScope.for_instance(PIPE_TENANT, PIPE_KG))
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


@pytest.mark.asyncio
async def test_pipeline_enabled_resolves_aliases_on_the_cypher_path(monkeypatch):
    """Flag on + one alias registered: /ask rewrite-only renames leaves in Cypher.

    When the map is present, `_rewrite_cypher_alias_leaves` replaces old attr
    leaves in the generated Cypher text before confine/execute. Empty map is a
    no-op (covered by ``test_pipeline_enabled_zero_aliases_unchanged``).
    """
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
