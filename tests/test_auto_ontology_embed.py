"""OSS auto ontology embedding: GraphStore catalog + OPENROUTER key resolution.

Anti-overfit: synthetic type names only; fake embedder (no live OpenRouter).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import numpy as np
import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests
from infona_client.nlp import pipeline as pipeline_mod
from infona_client.nlp.ontology_embeddings import OntologyEmbeddingService


TENANT = "embed-test-tenant"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}"


@pytest.fixture
def store(tmp_path):
    s = MemoryGraphStore()
    configure_graph_store(s)
    yield s
    asyncio.run(s.close())
    reset_graph_store_for_tests()


@pytest.fixture
def emb_svc(tmp_path, monkeypatch):
    """Service with deterministic fake vectors (dim-consistent, type-stable)."""
    pipeline_mod.reset_embedding_service_for_tests()
    svc = OntologyEmbeddingService(
        openrouter_api_key="test-key",
        local_dir=str(tmp_path / "emb"),
    )

    async def fake_embed(texts, api_key=None):
        out = []
        for t in texts:
            # Hash-ish stable vector in EMBEDDING_DIM-ish small space for tests
            v = np.zeros(8, dtype=np.float32)
            h = abs(hash(t.split("\n")[0])) % 8
            v[h] = 1.0
            # slight boost for "widget" token
            if "widget" in t.lower() or "synthwidget" in t.lower():
                v[0] = 1.0
            if "depot" in t.lower() or "synthdepot" in t.lower():
                v[1] = 1.0
            out.append(v.tolist())
        return out

    monkeypatch.setattr(svc, "_embed_texts", fake_embed)
    # Also patch module-level embed used elsewhere if needed
    yield svc
    pipeline_mod.reset_embedding_service_for_tests()


async def _seed_catalog(store: MemoryGraphStore) -> None:
    await upsert_type(
        store=store,
        name="SynthWidget",
        description="A synthetic widget part for embedding tests",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_type(
        store=store,
        name="SynthDepot",
        description="A storage depot location",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store,
        type_name="SynthWidget",
        attr_name="unit_cost",
        datatype="float",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store,
        type_name="SynthWidget",
        attr_name="stored_in",
        datatype="SynthDepot",
        layer="tenant",
        tenant_id=TENANT,
    )


def test_resolve_openrouter_key_accepts_bare_env(monkeypatch):
    pipeline_mod.reset_embedding_service_for_tests()
    monkeypatch.delenv("INFONA_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-bare")
    # settings may still be empty for INFONA_ prefix
    key = pipeline_mod._resolve_openrouter_api_key()
    assert key == "sk-or-test-bare"
    # get_embedding_service should construct
    monkeypatch.setattr(
        "infona_client.config.settings.openrouter_api_key",
        "",
        raising=False,
    )
    svc = pipeline_mod.get_embedding_service()
    assert svc is not None
    pipeline_mod.reset_embedding_service_for_tests()


def test_build_from_catalog_embeds_types(store, emb_svc):
    async def run():
        await _seed_catalog(store)
        n = await emb_svc.build_from_catalog(TENANT, store=store, graph_uri=GRAPH)
        assert n == 2
        assert "SynthWidget" in emb_svc._stores[GRAPH].chunks
        assert "SynthDepot" in emb_svc._stores[GRAPH].chunks
        # local disk written
        path = emb_svc._local_path(GRAPH)
        assert path.is_file()

    asyncio.run(run())


def test_ensure_index_cold_builds_from_catalog(store, emb_svc):
    async def run():
        await _seed_catalog(store)
        # empty memory
        emb_svc._stores.clear()
        n = await emb_svc.ensure_index(GRAPH, tenant_id=TENANT, store=store)
        assert n == 2
        text = await emb_svc.retrieve(GRAPH, "how many widgets", top_k=5)
        assert text is not None
        assert "SynthWidget" in text

    asyncio.run(run())


def test_embed_types_prefers_catalog_without_neptune(store, emb_svc):
    async def run():
        await _seed_catalog(store)
        await emb_svc.embed_types(
            GRAPH,
            ["SynthWidget"],
            None,  # no Neptune — must use catalog
            store=store,
            tenant_id=TENANT,
        )
        assert "SynthWidget" in emb_svc._stores[GRAPH].chunks

    asyncio.run(run())


def test_incremental_merge_preserves_other_types(store, emb_svc):
    async def run():
        await _seed_catalog(store)
        await emb_svc.build_from_catalog(TENANT, store=store, graph_uri=GRAPH)
        await emb_svc.embed_types_from_catalog(
            TENANT, ["SynthWidget"], store=store, graph_uri=GRAPH
        )
        assert set(emb_svc._stores[GRAPH].chunks) == {"SynthWidget", "SynthDepot"}

    asyncio.run(run())
