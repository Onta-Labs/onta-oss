"""Precomputed capability-card coverage index (ONTA-390).

Verifies the index retrieves the right entry, embeds each card only ONCE (a second
query does not re-embed the whole catalog), re-embeds on card mutation, and falls
back to lexical ranking when embeddings are unavailable. Fully offline — a fake
``embed_fn`` maps token presence to fixed unit vectors; no OpenRouter.
"""

from __future__ import annotations

import pytest

from cograph_client.api_registry.coverage_index import (
    CoverageIndex,
    rank_lexical,
)
from cograph_client.api_registry.ranking import coverage_text
from cograph_client.api_registry.spec import (
    ApiSourceSpec,
    Coverage,
    EndpointSpec,
    ParamSpec,
)


# --------------------------------------------------------------------------- #
# Fixtures: a catalog of N >= 12 specs (1 healthcare, 1 geocoding, rest noise)
# --------------------------------------------------------------------------- #
def _spec(slug, *, description="", kinds=None, attributes=None, keywords=None) -> ApiSourceSpec:
    return ApiSourceSpec(
        slug=slug,
        title=slug,
        description=description,
        base_url="https://example.test",
        coverage=Coverage(
            entity_kinds=list(kinds or []),
            attributes=list(attributes or []),
            keywords=list(keywords or []),
        ),
        endpoints=[EndpointSpec(name="s", path="/s", result_path="r",
                                params=[ParamSpec(name="q")], field_mappings={"id": "id"})],
    )


def _specs() -> list[ApiSourceSpec]:
    healthcare = _spec(
        "nppes",
        description="US national provider identifier registry for physicians and clinicians",
        kinds=["healthcare_provider"], attributes=["npi", "taxonomy"],
        keywords=["npi", "physician", "doctor", "clinician"],
    )
    geocoding = _spec(
        "geonames",
        description="Geocoding and place lookup: latitude longitude for a place name",
        kinds=["place"], attributes=["latitude", "longitude"],
        keywords=["geocode", "geocoding", "coordinates", "place"],
    )
    noise = [
        _spec(f"noise_{i}", description=f"Unrelated widget catalog number {i}",
              kinds=["widget"], attributes=["sku"], keywords=["widget", f"gadget{i}"])
        for i in range(12)
    ]
    return [healthcare, geocoding, *noise]


class _FakeEmbedder:
    """Records every batch of texts it is asked to embed and scores each text by a
    couple of domain tokens so the healthcare query lands on the healthcare card."""

    def __init__(self):
        self.batches: list[list[str]] = []

    async def __call__(self, texts):
        self.batches.append(list(texts))
        out = []
        for t in texts:
            tl = t.lower()
            # 3-dim unit-ish vector: [healthcare, geo, other]
            health = 1.0 if any(k in tl for k in ("npi", "physician", "clinician", "cardiolog")) else 0.0
            geo = 1.0 if any(k in tl for k in ("geocod", "coordinates", "latitude", "place")) else 0.0
            out.append([health, geo, 0.1])
        return out

    @property
    def total_texts(self) -> int:
        return sum(len(b) for b in self.batches)


# --------------------------------------------------------------------------- #
# 1. Relevance
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rank_surfaces_healthcare_entry_not_noise():
    specs = _specs()
    idx = CoverageIndex()
    embed = _FakeEmbedder()
    ranked = await idx.rank("cardiologists in San Francisco", specs, top_k=5, embed_fn=embed)
    slugs = [s.slug for s in ranked]
    assert "nppes" in slugs                    # healthcare entry is in the top-k
    assert len(slugs) == 5
    assert not slugs[0].startswith("noise_")   # a pure-noise slug is not ranked #1


# --------------------------------------------------------------------------- #
# 2. Precompute: a second query does not re-embed every card
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_second_query_does_not_reembed_catalog():
    specs = _specs()
    idx = CoverageIndex()
    embed = _FakeEmbedder()
    await idx.rank("cardiologists near me", specs, top_k=5, embed_fn=embed)
    first_batches = len(embed.batches)
    first_texts = embed.total_texts
    # First query: one batch embeds all N cards + one batch embeds the query.
    assert first_texts == len(specs) + 1

    await idx.rank("geocode this address", specs, top_k=5, embed_fn=embed)
    # Second query embeds ONLY the query — the N cards are served from the cache.
    new_batches = embed.batches[first_batches:]
    new_texts = [t for b in new_batches for t in b]
    assert new_texts == ["geocode this address"]
    assert embed.total_texts == first_texts + 1   # +1 for the query, not +N


# --------------------------------------------------------------------------- #
# 3. Invalidation on card mutation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mutating_a_card_reembeds_it():
    specs = _specs()
    idx = CoverageIndex()
    embed = _FakeEmbedder()
    await idx.rank("anything", specs, top_k=3, embed_fn=embed)
    baseline_texts = embed.total_texts

    # Mutate one entry's coverage text (add keywords) → its card hashes anew.
    specs[5].coverage.keywords.append("brand-new-term")
    await idx.rank("anything", specs, top_k=3, embed_fn=embed)
    # Exactly the mutated card is re-embedded (+ the query); the other N-1 are cached.
    delta = embed.total_texts - baseline_texts
    assert delta == 2                            # 1 changed card + 1 query
    assert coverage_text(specs[5]) in embed.batches[-2]   # the changed card was re-embedded


# --------------------------------------------------------------------------- #
# 4. Fallback when embeddings unavailable
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rank_returns_none_without_embedder():
    idx = CoverageIndex()
    # No embed_fn and no key → None (caller falls back to lexical).
    assert await idx.rank("q", _specs(), top_k=5) is None


@pytest.mark.asyncio
async def test_rank_returns_none_when_embedder_raises():
    idx = CoverageIndex()

    async def boom(texts):
        raise RuntimeError("embed backend down")

    assert await idx.rank("q", _specs(), top_k=5, embed_fn=boom) is None


def test_lexical_fallback_returns_capped_list():
    specs = _specs()
    ranked = rank_lexical("npi physician provider", specs, top_k=5)
    assert len(ranked) == 5
    assert isinstance(ranked[0], ApiSourceSpec)
    # The healthcare card (rich npi/physician overlap) beats a pure-noise widget.
    assert "nppes" in [s.slug for s in ranked]


@pytest.mark.asyncio
async def test_rebuild_warms_then_query_only_embeds_query():
    specs = _specs()
    idx = CoverageIndex()
    embed = _FakeEmbedder()
    embedded = await idx.rebuild(specs, embed_fn=embed)
    assert embedded == len(specs)                # all cards embedded once
    assert embed.total_texts == len(specs)       # no query yet
    await idx.rank("cardiologists", specs, top_k=3, embed_fn=embed)
    # Warmed cache → the query is the only new text.
    assert embed.total_texts == len(specs) + 1


def test_keywords_included_in_coverage_text():
    s = _spec("x", description="desc", keywords=["alpha_kw", "beta_kw"])
    text = coverage_text(s)
    assert "alpha_kw" in text and "beta_kw" in text
