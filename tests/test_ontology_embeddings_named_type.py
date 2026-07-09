"""ONTA-258 (semantic path): the embedding-retrieval subset must ALWAYS surface a
declared type the question names by name, even when it ranks below top-K or has
no instances. Otherwise the named type is invisible to the SPARQL LLM, which then
claims it "does not exist". Invented tokens only (Widget / Sprocket / Gadget).
"""

from __future__ import annotations

import numpy as np

from cograph_client.nlp.ontology_embeddings import (
    OntologyEmbeddingService,
    TenantEmbeddingStore,
    TypeChunk,
    _singularize,
    _types_named_in_question,
)

GRAPH = "https://cograph.tech/graphs/inv-tenant"


def test_singularize_regular_plurals():
    assert _singularize("Sprockets") == "sprocket"
    assert _singularize("Widgets") == "widget"
    assert _singularize("Gadget") == "gadget"        # already singular
    assert _singularize("Batteries") == "battery"    # -ies -> -y
    assert _singularize("Boxes") == "box"            # -xes -> -x
    assert _singularize("Class") == "class"          # -ss guard: not stripped
    assert _singularize("Address") == "address"      # -ss guard: not stripped


def test_types_named_in_question_matches_plural():
    names = ["Widget", "Sprocket", "Gadget"]
    assert _types_named_in_question("list all Sprockets", names) == {"Sprocket"}
    assert _types_named_in_question("count the widgets please", names) == {"Widget"}
    # A question naming several declared types surfaces all of them.
    assert _types_named_in_question("Sprocket vs Gadget", names) == {"Sprocket", "Gadget"}


def test_types_named_in_question_no_false_positive():
    names = ["Widget", "Sprocket", "Gadget"]
    # No declared type named -> empty (no spurious substring hits).
    assert _types_named_in_question("how many rows are there", names) == set()


def _chunk(name: str, vec: list[float]) -> TypeChunk:
    return TypeChunk(
        type_name=name,
        chunk_text=f"Type: {name} — URI: <https://cograph.tech/types/{name}>",
        embedding=np.array(vec, dtype=np.float32),
        attributes=[],
        relationship_targets=[],
    )


async def test_retrieve_force_includes_named_type_below_top_k(monkeypatch):
    """With top_k=1, only the closest type ranks in — but a question that names
    Sprocket must still surface Sprocket in the retrieved subset (ONTA-258)."""
    svc = OntologyEmbeddingService(openrouter_api_key="fake", s3_bucket="", s3_prefix="test")
    store = TenantEmbeddingStore()
    # Orthogonal unit vectors so ranking is unambiguous.
    store.chunks["Widget"] = _chunk("Widget", [1.0, 0.0, 0.0])
    store.chunks["Sprocket"] = _chunk("Sprocket", [0.0, 1.0, 0.0])
    store.chunks["Gadget"] = _chunk("Gadget", [0.0, 0.0, 1.0])
    svc._stores[GRAPH] = store

    # Question embedding is closest to Widget → top_k=1 selects Widget alone.
    async def fake_embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(svc, "_embed_texts", fake_embed)

    text = await svc.retrieve(GRAPH, "list all Sprockets", top_k=1)
    assert text is not None
    # Sprocket is named in the question → force-included despite ranking last.
    assert "Sprocket" in text
    # And the genuinely-closest type is still there.
    assert "Widget" in text


async def test_retrieve_unnamed_type_still_ranked_only(monkeypatch):
    """Control: when the question names no declared type, retrieval is pure
    top-K (force-include adds nothing spurious)."""
    svc = OntologyEmbeddingService(openrouter_api_key="fake", s3_bucket="", s3_prefix="test")
    store = TenantEmbeddingStore()
    store.chunks["Widget"] = _chunk("Widget", [1.0, 0.0, 0.0])
    store.chunks["Sprocket"] = _chunk("Sprocket", [0.0, 1.0, 0.0])
    store.chunks["Gadget"] = _chunk("Gadget", [0.0, 0.0, 1.0])
    svc._stores[GRAPH] = store

    async def fake_embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(svc, "_embed_texts", fake_embed)

    text = await svc.retrieve(GRAPH, "how many rows total", top_k=1)
    assert text is not None
    assert "Widget" in text
    # No named type → Sprocket/Gadget stay out of the top-1 subset.
    assert "Sprocket" not in text
    assert "Gadget" not in text
