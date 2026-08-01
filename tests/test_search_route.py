"""Route-level tests for the canonical semantic search (ONTA-178).

``POST /graphs/{tenant}/search`` is the ONE search surface every client
(webapp / CLI / MCP / SDK) rides, so this file locks the documented HTTP
contract end-to-end over the InMemory backend with fake embeddings:

* auth — same ``get_tenant`` dependency as every KG route: 401 without a key,
  403 for a multi-tenant key requesting an unowned path tenant, and the
  legacy single-tenant-key path-scoping behavior (results come from the KEY's
  tenant, never the foreign tenant named in the path);
* validation — blank query → 400; non-integer top_k → 422; out-of-range
  top_k clamped to [1, 50] with the effective value echoed back;
* unknown kg_name → 200 + empty hits (never a 404/500 — see the route docs);
* semantic gate off → lexical DEGRADE (200 + ``degraded=true``), NOT a 503:
  the keyword leg still answers, so we never dead-end the caller;
* degraded shape — no embed key, or an embed failure, yields ``degraded=true``
  lexical-only results, never a 500;
* happy path — the route embeds the query via the shared embed client and
  passes the vector to the index (the index NEVER calls an embedding API —
  the locked ONTA-176 contract), and hits carry
  entity_uri / attrs / snippet / attr / score.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from cograph_client.auth.api_keys import register_external_verifier
from cograph_client.config import settings
from cograph_client.semantic.extract import content_hash
from cograph_client.semantic.memory import InMemorySemanticIndex
from cograph_client.semantic.protocol import SemanticChunk
from cograph_client.semantic.registry import (
    register_semantic_index,
    reset_semantic_index,
)

TENANT = "test-tenant"  # conftest's static-key tenant
KG = "kg1"
DIM = 4

V_SOLAR = [1.0, 0.0, 0.0, 0.0]
V_WIND = [0.0, 1.0, 0.0, 0.0]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    # The gate is on for every test unless a test disables it explicitly.
    monkeypatch.setenv("COGRAPH_SEMANTIC_INDEX_ENABLED", "true")
    # No embed key by default — individual tests opt in to the embed path.
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    reset_semantic_index()
    yield
    reset_semantic_index()
    register_external_verifier(None)


def _chunk(
    uri: str,
    text: str,
    *,
    kg: str = KG,
    tenant: str = TENANT,
    embedding: Optional[list[float]] = None,
    attrs: Optional[dict] = None,
) -> SemanticChunk:
    return SemanticChunk(
        tenant_id=tenant,
        kg_name=kg,
        entity_uri=uri,
        attr="description",
        chunk_ix=0,
        chunk_text=text,
        content_hash=content_hash(text),
        embedding=embedding,
        embed_model="fake-embed-model" if embedding is not None else None,
        attrs=attrs if attrs is not None else {"label": uri, "type": "Report"},
    )


def _seed(*chunks: SemanticChunk) -> InMemorySemanticIndex:
    index = InMemorySemanticIndex()
    register_semantic_index(index)
    asyncio.run(index.upsert_chunks(list(chunks)))
    return index


def _corpus() -> list[SemanticChunk]:
    return [
        _chunk(
            "e:solar",
            "Rooftop solar panel installation subsidies for residential homes.",
            embedding=V_SOLAR,
            attrs={"label": "Solar", "type": "Report"},
        ),
        _chunk(
            "e:wind",
            "Offshore wind turbine blade maintenance schedule.",
            embedding=V_WIND,
            attrs={"label": "Wind", "type": "Article"},
        ),
    ]


def _search(client, payload: dict, headers=None, tenant: str = TENANT):
    return client.post(f"/graphs/{tenant}/search", json=payload, headers=headers)


# --- auth ---------------------------------------------------------------------


def test_search_requires_auth(client):
    resp = _search(client, {"query": "solar"})
    assert resp.status_code in (401, 403)


def test_search_multi_tenant_key_403_for_unowned_tenant(client):
    """The cross-tenant 403 path, exactly as other routes get it from
    get_tenant: a user-scoped key granting [alpha, beta] must NOT search a
    tenant outside its grant — the key is valid, the tenant grant is not."""
    register_external_verifier(lambda key: ["alpha", "beta"])
    resp = _search(
        client,
        {"query": "solar"},
        headers={"X-API-Key": "multi-key"},  # not in the static map → verifier
        tenant="other-tenant",
    )
    assert resp.status_code == 403
    assert "other-tenant" in resp.json()["detail"]


def test_search_static_key_foreign_tenant_is_403(client, auth_headers):
    """A static key used with a foreign path tenant is 403 — never silently
    searches the key's tenant (or the path tenant) under the wrong path."""
    _seed(
        _chunk("e:mine", "confidential solar subsidies report", tenant=TENANT),
        _chunk("e:theirs", "confidential solar subsidies report", tenant="other-tenant"),
    )
    resp = _search(client, {"query": "confidential solar subsidies"},
                   headers=auth_headers, tenant="other-tenant")
    assert resp.status_code == 403
    assert "other-tenant" in resp.json()["detail"]

    # Matching path still searches the key's tenant only.
    ok = _search(client, {"query": "confidential solar subsidies"},
                 headers=auth_headers, tenant=TENANT)
    assert ok.status_code == 200
    uris = [h["entity_uri"] for h in ok.json()["hits"]]
    assert uris == ["e:mine"]


# --- gate + validation ----------------------------------------------------------


def test_search_gate_off_degrades_to_lexical_not_503(monkeypatch, client, auth_headers):
    """Gate off → lexical keyword search with ``degraded=true`` and a 200,
    never a 503. The FTS leg still matches, so the caller gets a usable (if
    reduced-recall) result instead of a dead-end. This is the fix for the
    persona-eval search-503 bug: a disabled semantic index must degrade, not
    refuse."""
    monkeypatch.delenv("COGRAPH_SEMANTIC_INDEX_ENABLED", raising=False)
    _seed(*_corpus())
    resp = _search(client, {"query": "solar panel subsidies"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    # The lexical leg still ranks the matching entity first.
    assert body["hits"][0]["entity_uri"] == "e:solar"


def test_search_gate_off_forces_lexical_even_with_embed_key(monkeypatch, client, auth_headers):
    """With the gate off we must NOT embed the query — even if a key is
    configured — because the vector leg is not maintained. Assert the embed
    client is never called and the result is a 200 lexical degrade."""
    monkeypatch.delenv("COGRAPH_SEMANTIC_INDEX_ENABLED", raising=False)
    _seed(*_corpus())
    monkeypatch.setattr(settings, "openrouter_api_key", "some-key")

    embed_calls: list[list[str]] = []

    async def spy_embed(texts, *, api_key, timeout=30):
        embed_calls.append(texts)
        return [V_SOLAR for _ in texts]

    monkeypatch.setattr("cograph_client.nlp.embed_client.embed_texts", spy_embed)
    resp = _search(client, {"query": "solar panel subsidies"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["degraded"] is True
    # Gate off ⇒ no embedding round-trip at all.
    assert embed_calls == []


def test_search_gate_off_unknown_kg_is_empty_200_not_503(monkeypatch, client, auth_headers):
    """Even an unpopulated / unknown KG under a disabled gate is an
    empty-but-200 ``degraded=true`` result — never a 503."""
    monkeypatch.delenv("COGRAPH_SEMANTIC_INDEX_ENABLED", raising=False)
    _seed(*_corpus())
    resp = _search(
        client, {"query": "solar", "kg_name": "no-such-kg"}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hits"] == []
    assert body["degraded"] is True


@pytest.mark.parametrize("bad_query", ["", "   ", "\n\t "])
def test_search_blank_query_is_400(client, auth_headers, bad_query):
    """Documented choice: a blank query is a caller bug → 400, not empty 200."""
    _seed(*_corpus())
    resp = _search(client, {"query": bad_query}, headers=auth_headers)
    assert resp.status_code == 400
    assert "query" in resp.json()["detail"]


def test_search_missing_query_is_422(client, auth_headers):
    resp = _search(client, {}, headers=auth_headers)
    assert resp.status_code == 422


def test_search_non_integer_top_k_is_422(client, auth_headers):
    _seed(*_corpus())
    resp = _search(client, {"query": "solar", "top_k": "lots"}, headers=auth_headers)
    assert resp.status_code == 422


def test_search_top_k_clamped_to_cap(client, auth_headers):
    """Out-of-range top_k is clamped to [1, 50] and the EFFECTIVE value is
    echoed back (the documented, observable clamp)."""
    _seed(*_corpus())
    resp = _search(client, {"query": "solar", "top_k": 9999}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["top_k"] == 50

    resp = _search(client, {"query": "solar", "top_k": -3}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["top_k"] == 1
    assert len(body["hits"]) <= 1


def test_search_top_k_limits_hits(client, auth_headers):
    _seed(
        *[
            _chunk(f"e:{i}", f"solar panels variant {i}")
            for i in range(5)
        ]
    )
    resp = _search(client, {"query": "solar panels", "top_k": 2}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["hits"]) == 2
    assert body["count"] == 2
    assert body["top_k"] == 2


# --- kg / type scoping -----------------------------------------------------------


def test_search_unknown_kg_returns_empty_not_error(client, auth_headers):
    """Documented choice: an unknown KG is indistinguishable from an unindexed
    one — empty results, never a 404/500 (no Neptune existence round-trip)."""
    _seed(*_corpus())
    resp = _search(
        client, {"query": "solar", "kg_name": "no-such-kg"}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hits"] == []
    assert body["count"] == 0


def test_search_empty_kg_name_means_all_kgs(client, auth_headers):
    """'' normalizes to None (all KGs) — a blank form field must not filter to
    a KG literally named the empty string."""
    _seed(_chunk("e:a", "solar text", kg="kga"), _chunk("e:b", "solar text", kg="kgb"))
    resp = _search(client, {"query": "solar", "kg_name": ""}, headers=auth_headers)
    assert {h["entity_uri"] for h in resp.json()["hits"]} == {"e:a", "e:b"}


def test_search_type_filter(client, auth_headers):
    _seed(*_corpus())
    resp = _search(
        client, {"query": "solar wind", "type": "Article"}, headers=auth_headers
    )
    assert resp.status_code == 200
    uris = {h["entity_uri"] for h in resp.json()["hits"]}
    assert uris == {"e:wind"}


# --- entity_uris allowlist --------------------------------------------------------


def test_search_entity_uris_subset_only_those_rank(client, auth_headers):
    """Structured pre-filter: only allowlisted URIs can appear in hits, even
    when others would rank higher unrestricted. Multi-entity corpus so the
    assertion is not a single-fixture coincidence."""
    _seed(
        _chunk(
            "e:reef-a",
            "coastal reef restoration funding notes",
            attrs={"label": "Reef A", "type": "Report"},
        ),
        _chunk(
            "e:reef-b",
            "coastal reef restoration funding notes",
            attrs={"label": "Reef B", "type": "Report"},
        ),
        _chunk(
            "e:reef-c",
            "coastal reef restoration funding notes",
            attrs={"label": "Reef C", "type": "Article"},
        ),
        _chunk(
            "e:optics",
            "unrelated quantum optics abstract",
            attrs={"label": "Optics", "type": "Article"},
        ),
    )
    # Unrestricted: all three reef docs match the query.
    unrestricted = _search(
        client, {"query": "coastal reef restoration"}, headers=auth_headers
    )
    assert unrestricted.status_code == 200
    assert {h["entity_uri"] for h in unrestricted.json()["hits"]} == {
        "e:reef-a",
        "e:reef-b",
        "e:reef-c",
    }

    # Subset allowlist drops e:reef-b even though it matches the text.
    resp = _search(
        client,
        {
            "query": "coastal reef restoration",
            "entity_uris": ["e:reef-a", "e:reef-c", "e:optics"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    uris = {h["entity_uri"] for h in body["hits"]}
    assert uris == {"e:reef-a", "e:reef-c"}
    assert "e:reef-b" not in uris


def test_search_entity_uris_empty_list_is_empty_200(client, auth_headers):
    """Strict empty allowlist: [] → zero hits, 200 (not 400)."""
    _seed(*_corpus())
    resp = _search(
        client,
        {"query": "solar panel subsidies", "entity_uris": []},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hits"] == []
    assert body["count"] == 0


def test_search_entity_uris_blanks_and_dupes_normalized(client, auth_headers):
    """Blank strings dropped, duplicates collapsed; remaining allowlist applies."""
    _seed(*_corpus())
    resp = _search(
        client,
        {
            "query": "solar wind",
            "entity_uris": ["", "  ", "e:wind", "e:wind", "e:wind"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert {h["entity_uri"] for h in resp.json()["hits"]} == {"e:wind"}


def test_search_entity_uris_combined_with_type(client, auth_headers):
    """entity_uris AND type filter combine (intersection)."""
    _seed(
        _chunk(
            "e:ev1",
            "annual coastal gathering notes",
            attrs={"label": "Ev1", "type": "Event"},
        ),
        _chunk(
            "e:ev2",
            "annual coastal gathering notes",
            attrs={"label": "Ev2", "type": "Event"},
        ),
        _chunk(
            "e:org1",
            "annual coastal gathering notes",
            attrs={"label": "Org1", "type": "Organization"},
        ),
    )
    resp = _search(
        client,
        {
            "query": "annual coastal gathering",
            "type": "Event",
            "entity_uris": ["e:ev2", "e:org1"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    # org1 allowlisted but wrong type; ev1 right type but not allowlisted.
    assert {h["entity_uri"] for h in resp.json()["hits"]} == {"e:ev2"}


def test_search_entity_uris_over_cap_is_400(client, auth_headers):
    """More than ENTITY_URIS_MAX unique URIs → 400 with a clear message."""
    from cograph_client.api.routes.search import ENTITY_URIS_MAX

    _seed(*_corpus())
    too_many = [f"e:entity-{i}" for i in range(ENTITY_URIS_MAX + 1)]
    resp = _search(
        client,
        {"query": "solar", "entity_uris": too_many},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "entity_uris" in detail
    assert str(ENTITY_URIS_MAX) in detail


def test_search_entity_uris_passed_to_index(client, auth_headers):
    """Route hands the cleaned allowlist through to the backend search call."""
    index = _RecordingIndex()
    register_semantic_index(index)
    asyncio.run(index.upsert_chunks(_corpus()))

    resp = _search(
        client,
        {
            "query": "solar",
            "entity_uris": ["e:solar", "", "e:solar", "  e:wind  "],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert len(index.search_calls) == 1
    assert index.search_calls[0]["entity_uris"] == ["e:solar", "e:wind"]


# --- degraded shape ---------------------------------------------------------------


def test_search_degraded_without_embed_key(client, auth_headers):
    """No OpenRouter key → the route passes query_embedding=None → the backend
    runs lexical-only and the response says so (degraded=true, never silent)."""
    _seed(*_corpus())
    resp = _search(client, {"query": "solar panel subsidies"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["hits"][0]["entity_uri"] == "e:solar"


def test_search_degraded_on_embed_failure(monkeypatch, client, auth_headers):
    """Embedding outage degrades recall, it must never 500 the search."""
    _seed(*_corpus())
    monkeypatch.setattr(settings, "openrouter_api_key", "some-key")

    async def boom(texts, *, api_key, timeout=30):
        raise RuntimeError("embed service down")

    monkeypatch.setattr("cograph_client.nlp.embed_client.embed_texts", boom)
    resp = _search(client, {"query": "solar panel subsidies"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["hits"][0]["entity_uri"] == "e:solar"


# --- happy path -------------------------------------------------------------------


class _RecordingIndex(InMemorySemanticIndex):
    """InMemory index that records search kwargs — proves the route passed the
    query embedding DOWN (the index never embeds on its own; locked contract)."""

    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[dict] = []

    async def search(self, tenant_id, query_text, **kwargs):
        self.search_calls.append({"tenant_id": tenant_id, "query_text": query_text, **kwargs})
        return await super().search(tenant_id, query_text, **kwargs)


def test_search_happy_path_hybrid(monkeypatch, client, auth_headers):
    index = _RecordingIndex()
    register_semantic_index(index)
    asyncio.run(index.upsert_chunks(_corpus()))

    monkeypatch.setattr(settings, "openrouter_api_key", "some-key")
    embed_calls: list[list[str]] = []

    async def fake_embed(texts: list[str], *, api_key: str, timeout: float = 30):
        embed_calls.append(texts)
        return [V_SOLAR for _ in texts]

    monkeypatch.setattr("cograph_client.nlp.embed_client.embed_texts", fake_embed)

    resp = _search(
        client, {"query": "  solar panel subsidies  ", "top_k": 5}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()

    # Full hybrid: not degraded, best entity first, count/top_k echoed.
    assert body["degraded"] is False
    assert body["count"] == len(body["hits"]) >= 1
    assert body["top_k"] == 5
    top = body["hits"][0]
    assert top["entity_uri"] == "e:solar"
    assert top["attrs"] == {"label": "Solar", "type": "Report"}
    assert "solar" in top["snippet"].lower()
    assert top["attr"] == "description"
    assert top["score"] > 0

    # The ROUTE embedded the (stripped) query once via the shared client and
    # handed the vector to the index — the index never called an embedding API.
    assert embed_calls == [["solar panel subsidies"]]
    assert len(index.search_calls) == 1
    call = index.search_calls[0]
    assert call["tenant_id"] == TENANT
    assert call["query_text"] == "solar panel subsidies"
    assert call["query_embedding"] == V_SOLAR
    assert call["top_k"] == 5
