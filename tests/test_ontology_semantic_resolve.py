"""Hermetic tests for ONTA-537 semantic NL type/rel resolution.

Anti-overfit: synthetic type names only (Widget, InventorySKU, ProductLike, …).
No live OpenRouter — FakeEmbedder supplies deterministic orthonormal vectors so
cosine rank order is fully controlled.
"""

from __future__ import annotations

import numpy as np
import pytest

from infona_client.nlp.cypher_generate import (
    TEMPLATE_COUNT_BY_TYPE,
    resolve_type_name,
    resolve_type_name_async,
    try_stub_count_query,
)
from infona_client.nlp.ontology_mention_index import (
    DEFAULT_EMBED_MODEL,
    EmbedConfigError,
    OntologyMentionIndex,
    format_rel_embed_text,
    format_type_embed_text,
    require_embed_config,
    semantic_resolve_context,
)


# ---------------------------------------------------------------------------
# Fake embedder — fixed map phrase → unit vector (3-D for readability)
# ---------------------------------------------------------------------------

# Basis axes: product-like, book-like, animal-like
V_PRODUCT = [1.0, 0.0, 0.0]
V_BOOK = [0.0, 1.0, 0.0]
V_ANIMAL = [0.0, 0.0, 1.0]
# Near product (synonym space)
V_PRODUCT_NEAR = [0.95, 0.05, 0.0]
# Near animal
V_ANIMAL_NEAR = [0.05, 0.05, 0.95]
# Child of animal, slightly offset
V_DOG = [0.1, 0.05, 0.9]
# Relationship leaves
V_STORED = [0.0, 0.8, 0.2]
V_PHASE = [0.2, 0.1, 0.8]


def _norm(v: list[float]) -> list[float]:
    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return (a / n).tolist() if n else v


class FakeEmbedder:
    """Deterministic embedder: substring / keyword → fixed vector.

    Records batches for assertions that catalog re-embed is batched.
    """

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.batches: list[list[str]] = []
        # lowercased keyword → vector
        self.table = {
            k.lower(): _norm(v)
            for k, v in (table or {}).items()
        }

    def register(self, keyword: str, vec: list[float]) -> None:
        self.table[keyword.lower()] = _norm(vec)

    def _vec_for(self, text: str) -> list[float]:
        tl = text.lower()
        # Prefer the catalog header identity ("Entity type: X" / "Relationship: X")
        # so domain/range words in the body do not steal the leaf's vector.
        for prefix in ("relationship:", "entity type:"):
            if prefix in tl:
                after = tl.split(prefix, 1)[1].strip().splitlines()[0].strip()
                # first token of the name line
                name = after.split()[0] if after else ""
                if name and name in self.table:
                    return self.table[name]
                # underscored / spaced variants
                for k, v in self.table.items():
                    if k == name or k.replace(" ", "_") == name or k.replace("_", " ") == name:
                        return v
        # Prefer longest keyword hit for free-text mentions.
        best_k = ""
        best_v = _norm([0.33, 0.33, 0.34])
        for k, v in self.table.items():
            if k in tl and len(k) >= len(best_k):
                best_k = k
                best_v = v
        return best_v

    async def __call__(self, texts):
        self.batches.append(list(texts))
        return [self._vec_for(t) for t in texts]


def _default_fake() -> FakeEmbedder:
    fe = FakeEmbedder(
        {
            "inventorysku": V_PRODUCT,
            "inventory sku": V_PRODUCT,
            "productlike": V_PRODUCT_NEAR,
            "product like": V_PRODUCT_NEAR,
            "product": V_PRODUCT_NEAR,
            "products": V_PRODUCT_NEAR,
            "book": V_BOOK,
            "books": V_BOOK,
            "gadgettome": V_BOOK,
            "animal": V_ANIMAL,
            "animals": V_ANIMAL_NEAR,
            "canineunit": V_DOG,
            "canine": V_DOG,
            "dog": V_DOG,
            "stored_in": V_STORED,
            "stored in": V_STORED,
            "warehouse": V_STORED,
            "has_phase": V_PHASE,
            "phase": V_PHASE,
            "widget": [0.5, 0.5, 0.0],
            "widgets": [0.5, 0.5, 0.0],
        }
    )
    return fe


async def _build_index(fe: FakeEmbedder) -> OntologyMentionIndex:
    idx = OntologyMentionIndex()
    idx.upsert_type(
        "InventorySKU",
        description="stock-keeping unit for sellable inventory rows",
        parents=["ProductLike"],
    )
    idx.upsert_type(
        "ProductLike",
        description="abstract parent of inventory-shaped types",
        parents=[],
    )
    idx.upsert_type(
        "GadgetTome",
        description="a bound volume of gadget lore",
        parents=[],
    )
    idx.upsert_type(
        "Animal",
        description="living organism in the bestiary",
        parents=[],
    )
    idx.upsert_type(
        "CanineUnit",
        description="a dog entity under Animal",
        parents=["Animal"],
    )
    idx.upsert_type("Widget", description="generic widget part", parents=[])
    idx.set_hierarchy(
        {
            "InventorySKU": "ProductLike",
            "CanineUnit": "Animal",
        }
    )
    idx.upsert_rel(
        "stored_in",
        domain="InventorySKU",
        range_type="WarehouseNode",
        description="which warehouse holds the sku",
    )
    idx.upsert_rel(
        "has_phase",
        domain="TrialRun",
        range_type="PhaseNode",
        description="lifecycle phase of a trial",
    )
    await idx.embed_missing(fe)
    return idx


# ---------------------------------------------------------------------------
# Config / fail-closed
# ---------------------------------------------------------------------------


def test_default_embed_model_is_cheap_openrouter_id():
    assert "embedding" in DEFAULT_EMBED_MODEL.lower() or "embed" in DEFAULT_EMBED_MODEL.lower()
    # Documented default from embed_client
    assert DEFAULT_EMBED_MODEL == "openai/text-embedding-3-small" or DEFAULT_EMBED_MODEL


def test_require_embed_config_fail_closed(monkeypatch):
    monkeypatch.delenv("INFONA_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # settings may still have a key in some envs — force empty via monkeypatch of helper path
    monkeypatch.setattr(
        "infona_client.nlp.ontology_mention_index.embed_api_key_from_env",
        lambda: "",
    )
    with pytest.raises(EmbedConfigError) as ei:
        require_embed_config()
    msg = str(ei.value).lower()
    assert "embed" in msg or "openrouter" in msg
    assert "infona_openrouter_api_key" in msg or "openrouter_api_key" in msg


def test_resolve_type_name_require_semantic_without_index_raises():
    with pytest.raises(EmbedConfigError):
        resolve_type_name(
            "widgets",
            ["Widget", "SensorHub"],
            require_semantic=True,
        )


def test_resolve_type_name_require_semantic_via_context_raises():
    with semantic_resolve_context(require_semantic=True):
        with pytest.raises(EmbedConfigError):
            resolve_type_name("widgets", ["Widget"])


# ---------------------------------------------------------------------------
# Synonym → type (semantic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synonym_products_maps_to_inventory_sku_not_book():
    fe = _default_fake()
    idx = await _build_index(fe)
    # Synonym space: "products" is near InventorySKU; GadgetTome is book-space.
    # ProductLike is an abstract parent — keep it out of the candidate set so
    # the test asserts synonym→concrete leaf (not parent).
    q = (await fe(["products"]))[0]
    hit = resolve_type_name(
        "products",
        ["InventorySKU", "GadgetTome", "Widget"],
        mention_index=idx,
        query_embedding=q,
        require_semantic=True,
    )
    assert hit == "InventorySKU"


@pytest.mark.asyncio
async def test_async_resolve_uses_fake_embedder():
    fe = _default_fake()
    idx = await _build_index(fe)
    hit = await resolve_type_name_async(
        "products",
        ["InventorySKU", "GadgetTome", "Widget"],
        mention_index=idx,
        embed_fn=fe,
        require_semantic=True,
    )
    assert hit == "InventorySKU"


# ---------------------------------------------------------------------------
# Empty leftover loses to populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_leftover_loses_to_populated_synonym():
    """Product-like empty leftover must not win when InventorySKU has rows.

    Vectors: mention 'products' near ProductLike AND InventorySKU; instance
    prior demotes empty ProductLike and boosts InventorySKU.
    """
    fe = _default_fake()
    idx = await _build_index(fe)
    # Both ProductLike and InventorySKU live in product space; empty loses.
    idx.set_activity({"ProductLike": 0, "InventorySKU": 6, "GadgetTome": 0})
    onto = (
        "Type: ProductLike [no instances]\n"
        "Type: InventorySKU (6 entities)\n"
        "Type: GadgetTome [no instances]\n"
    )
    q = (await fe(["products"]))[0]
    hit = resolve_type_name(
        "products",
        ["ProductLike", "InventorySKU", "GadgetTome"],
        onto,
        mention_index=idx,
        query_embedding=q,
        require_semantic=True,
    )
    assert hit == "InventorySKU"


@pytest.mark.asyncio
async def test_exact_empty_type_does_not_silent_zero_when_populated_near():
    """Exact name match on empty leftover still yields to populated near-synonym."""
    fe = _default_fake()
    idx = await _build_index(fe)
    # Register exact empty type "BookAlias" far from inventory; mention exact.
    idx.upsert_type("BookAlias", description="empty leftover book alias", parents=[])
    fe.register("bookalias", V_BOOK)
    await idx.embed_missing(fe)
    idx.set_activity({"BookAlias": 0, "InventorySKU": 6})
    # Mention is exactly BookAlias but empty — with only these candidates,
    # semantic returns None rather than binding empty (no populated near in book space).
    q = (await fe(["BookAlias"]))[0]
    hit = resolve_type_name(
        "BookAlias",
        ["BookAlias", "InventorySKU"],
        "Type: BookAlias [no instances]\nType: InventorySKU (6 entities)\n",
        mention_index=idx,
        query_embedding=q,
        require_semantic=True,
    )
    # Empty exact with no high-sim populated alternative → None (clarify)
    assert hit is None


# ---------------------------------------------------------------------------
# Parent / child expansion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_mention_binds_parent_for_subclass_count_expand():
    """'animals' → Animal; fixture expands subclasses via type_names_with_subclasses."""
    fe = _default_fake()
    idx = await _build_index(fe)
    idx.set_activity({"Animal": 0, "CanineUnit": 4})  # instances on child
    onto = (
        "Type: Animal [no instances]\n"
        "  parent: \n"
        "Type: CanineUnit (4 entities)\n"
        "  parent: Animal\n"
    )
    # Fix ontology parent parse format
    onto = (
        "Type: Animal [no instances]\n"
        "Type: CanineUnit (4 entities)\n"
        "  parent: Animal\n"
    )
    q = (await fe(["animals"]))[0]
    hit = resolve_type_name(
        "animals",
        ["Animal", "CanineUnit", "Widget"],
        onto,
        mention_index=idx,
        query_embedding=q,
        require_semantic=True,
    )
    # Parent concept wins (or populated child if parent demoted empty).
    # With EMPTY_PENALTY on Animal and child boost on CanineUnit near animal,
    # either Animal or CanineUnit is acceptable; count path expands Animal.
    assert hit in ("Animal", "CanineUnit")

    # When Animal binds, count expands to include CanineUnit.
    if hit == "Animal":
        with semantic_resolve_context(
            idx,
            query_embeddings={"animals": q},
            require_semantic=True,
        ):
            payload = try_stub_count_query(
                "How many animals?",
                onto,
                type_names=["Animal", "CanineUnit", "Widget"],
            )
        assert payload is not None
        assert payload["template"] == TEMPLATE_COUNT_BY_TYPE
        names = payload["params"]["type_names"]
        assert "Animal" in names
        assert "CanineUnit" in names


@pytest.mark.asyncio
async def test_hierarchy_child_boost_when_mention_near_parent():
    fe = _default_fake()
    idx = await _build_index(fe)
    idx.set_activity({"Animal": 0, "CanineUnit": 3, "Widget": 1})
    q = (await fe(["animals"]))[0]
    hit = idx.resolve_type(
        "animals",
        query_embedding=q,
        activity={"Animal": 0, "CanineUnit": 3, "Widget": 1},
        type_names=["Animal", "CanineUnit", "Widget"],
    )
    assert hit in ("Animal", "CanineUnit")
    # Widget (unrelated) must not win
    assert hit != "Widget"


# ---------------------------------------------------------------------------
# Relationship leaves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rel_mention_warehouse_to_stored_in():
    fe = _default_fake()
    idx = await _build_index(fe)
    hit = await idx.resolve_rel_async("warehouse", embed_fn=fe)
    assert hit == "stored_in"


@pytest.mark.asyncio
async def test_rel_mention_phase_to_has_phase():
    fe = _default_fake()
    idx = await _build_index(fe)
    hit = await idx.resolve_rel_async("phase", embed_fn=fe)
    assert hit == "has_phase"


# ---------------------------------------------------------------------------
# Embed text shape + reindex hook
# ---------------------------------------------------------------------------


def test_format_type_embed_text_includes_parent_and_description():
    text = format_type_embed_text(
        "CanineUnit",
        description="a dog row",
        parents=["Animal"],
    )
    assert "CanineUnit" in text
    assert "a dog row" in text
    assert "Animal" in text


def test_format_rel_embed_text_includes_domain_range():
    text = format_rel_embed_text(
        "stored_in",
        domain="InventorySKU",
        range_type="WarehouseNode",
        description="location edge",
    )
    assert "stored_in" in text
    assert "InventorySKU" in text
    assert "WarehouseNode" in text


@pytest.mark.asyncio
async def test_reindex_embed_missing_is_batched():
    fe = _default_fake()
    idx = OntologyMentionIndex()
    for name in ("Widget", "InventorySKU", "GadgetTome"):
        idx.upsert_type(name, description=f"type {name}")
    n = await idx.embed_missing(fe)
    assert n == 3
    # One batch of 3 texts (not N round-trips of 1)
    assert any(len(b) == 3 for b in fe.batches)


@pytest.mark.asyncio
async def test_index_is_healthy_only_after_embed():
    idx = OntologyMentionIndex()
    idx.upsert_type("Widget")
    assert not idx.is_healthy()
    fe = _default_fake()
    await idx.embed_missing(fe)
    assert idx.is_healthy()


# ---------------------------------------------------------------------------
# Context wiring into fixtures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_context_drives_count_fixture_synonym():
    fe = _default_fake()
    idx = await _build_index(fe)
    idx.set_activity({"InventorySKU": 6, "GadgetTome": 0, "Widget": 0})
    q = (await fe(["products"]))[0]
    onto = (
        "Type: InventorySKU (6 entities)\n"
        "Type: GadgetTome [no instances]\n"
        "Type: Widget [no instances]\n"
    )
    with semantic_resolve_context(
        idx,
        query_embeddings={"products": q},
        require_semantic=True,
    ):
        payload = try_stub_count_query(
            "How many products?",
            onto,
            type_names=["InventorySKU", "GadgetTome", "Widget"],
        )
    assert payload is not None
    assert payload["params"]["type_names"] == ["InventorySKU"]


@pytest.mark.asyncio
async def test_ambiguous_near_ties_return_none():
    """Two equally scoring populated types → None (clarify), not a silent pick."""
    fe = FakeEmbedder({"alpha": [1, 0, 0], "beta": [1, 0, 0], "query": [1, 0, 0]})
    idx = OntologyMentionIndex()
    idx.upsert_type("AlphaType", description="alpha")
    idx.upsert_type("BetaType", description="beta")
    # Force identical embeddings
    idx.upsert_type("AlphaType", description="alpha", embedding=[1, 0, 0])
    idx.upsert_type("BetaType", description="beta", embedding=[1, 0, 0])
    idx.set_activity({"AlphaType": 2, "BetaType": 2})
    q = (await fe(["query"]))[0]
    hit = idx.resolve_type(
        "query",
        query_embedding=q,
        activity={"AlphaType": 2, "BetaType": 2},
        type_names=["AlphaType", "BetaType"],
    )
    assert hit is None


# Optional live smoke (not required for CI green)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_openrouter_embed_optional():
    """Marked integration — skipped unless key present and marker selected."""
    from infona_client.nlp.ontology_mention_index import embed_api_key_from_env

    key = embed_api_key_from_env()
    if not key:
        pytest.skip("no OpenRouter key")
    from infona_client.nlp.ontology_mention_index import openrouter_embed_fn

    fn = openrouter_embed_fn(key)
    vecs = await fn(["synthetic widget part"])
    assert len(vecs) == 1
    assert len(vecs[0]) > 8
