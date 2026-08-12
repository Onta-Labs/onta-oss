"""FIX 3 + FIX 4 — subtype_description belongs ONLY on subtype branches, and is
written idempotently.

``ExtractedEntity.subtype_description`` defines a NEW SUBTYPE (models.py): it must
become the new type's description ONLY when the type is minted as a subtype. The
bug passed it into the type declaration on the DIFFERENT / FLAGGED /
same_as-rejected (top-level) branches too. FIX 3 restricts it to the subtype
branches; FIX 4 writes it as a single-valued REPLACE so re-minting a type across
ingests can't accumulate duplicate descriptions.

Ported by ONTA-527. These used to read the ``rdfs:comment`` SPARQL handed to
``neptune.update``; Neo4j is the only backend now, so the description is read
back from the tenant ``ontology_catalog`` (``OntoTypeRecord.description``) — the
same fact, at the surface that actually serves it.

**The positive half is a PRODUCT GAP and is strict-xfailed here.** The resolver
writes a subtype description with ``OntologyOpKind.SET_COMMENT``
(``_mint_subtype`` / ``_link_parent``), and
``ontology_commit._commit_ontology_graph_store`` has no branch for that op — it
falls through to ``logger.warning("ontology_store_op_skipped")``. So on the
shipped Neo4j path EVERY subtype description is silently dropped. The negative
tests (top-level branches must NOT write one) still pass, and are kept because
they must keep holding once SET_COMMENT is ported.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from infona_client.graph import ontology_catalog as oc
from infona_client.resolver.schema_resolver import SchemaResolver
from infona_client.resolver.models import (
    ExtractedEntity,
    ExtractionResult,
    MatchVerdict,
    TypeMatch,
)
from infona_client.resolver.verdict_cache import JsonVerdictCache


DESC = "a score measuring how human a generated voice sounds"
TENANT = "test-tenant"
KG = "subtypes"
#: Instance data needs a per-KG graph URI (the tenant graph has no write scope);
#: the ontology still goes to the tenant catalog either way.
KG_GRAPH = f"https://graph.infona.ai/graphs/{TENANT}/kg/{KG}"

#: The reason string every strict xfail in this module shares.
_SET_COMMENT_GAP = (
    "PRODUCT BUG (Neo4j port): a subtype's description never reaches the "
    "ontology. schema_resolver._mint_subtype / _link_parent emit "
    "OntologyOpKind.SET_COMMENT, and ontology_commit._commit_ontology_graph_store "
    "handles only UPSERT_TYPE / UPSERT_ATTRIBUTE / UPSERT_RELATIONSHIP / "
    "SET_SUBCLASS — SET_COMMENT hits the else branch and is logged as "
    "'ontology_store_op_skipped', so OntoType.description stays ''."
)


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    client.batch_exists.return_value = set()
    return client


@pytest.fixture
def mock_cache(tmp_path):
    return JsonVerdictCache(tmp_path / "cache.json")


class FakeTypeMatcher:
    """Returns one canned TypeMatch for every proposed type."""

    def __init__(self, verdict: MatchVerdict, parent_type: str | None = None):
        self._verdict = verdict
        self._parent_type = parent_type
        # `_resolve_type` points the embedding pre-filter at the ingest's graph.
        self._graph_uri: str | None = None

    async def match(self, proposed_type, proposed_description, existing_types):
        return TypeMatch(
            proposed=proposed_type,
            resolved=proposed_type,
            verdict=self._verdict,
            confidence=0.9,
            is_new=self._verdict != MatchVerdict.SAME,
            parent_type=self._parent_type,
        )


async def _types() -> dict[str, object]:
    """The tenant ontology's types, by name."""
    return {t.name: t for t in await oc.list_types(tenant_id=TENANT)}


async def _description_of(type_name: str) -> str | None:
    """The stored description of ``type_name``, or None when it has no type row."""
    record = (await _types()).get(type_name)
    return None if record is None else record.description


async def _ingest_one(resolver, entity: ExtractedEntity, existing_types=None):
    existing_types = existing_types or {}
    existing_attrs = {t: {} for t in existing_types}
    extraction = ExtractionResult(entities=[entity], relationships=[])
    with patch.object(resolver, "_extract", return_value=extraction):
        with patch.object(
            resolver, "_fetch_ontology", return_value=(dict(existing_types), existing_attrs)
        ):
            return await resolver.ingest("data", TENANT, instance_graph=KG_GRAPH)


# ---------------------------------------------------------------------------
# Subtype branches WRITE the description (via upsert)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=_SET_COMMENT_GAP)
@pytest.mark.asyncio
async def test_subtype_branch_writes_description(mock_neptune, mock_cache):
    """match.verdict == SUBTYPE → the description IS written on the new type."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    resolver._type_matcher = FakeTypeMatcher(MatchVerdict.SUBTYPE, parent_type="Score")

    entity = ExtractedEntity(
        type_name="HumannessIndex", id="hi-1", subtype_description=DESC,
    )
    result = await _ingest_one(resolver, entity, existing_types={"Score": ""})

    assert "HumannessIndex" in result.types_created
    assert await _description_of("HumannessIndex") == DESC, (
        "subtype branch must write the subtype_description onto the type"
    )
    mock_neptune.update.assert_not_called()


@pytest.mark.xfail(strict=True, reason=_SET_COMMENT_GAP)
@pytest.mark.asyncio
async def test_subtype_description_is_single_valued_across_reingest(
    mock_neptune, mock_cache
):
    """FIX 4 ported: the description is an UPSERT, so re-minting the same subtype
    across ingests leaves ONE description, not an accumulated pile.

    The SPARQL shape this used to assert (a DELETE/INSERT/WHERE rather than a
    blind ``INSERT DATA``) was only ever a proxy for that property; on the store
    path the property itself is directly observable.
    """
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    resolver._type_matcher = FakeTypeMatcher(MatchVerdict.SUBTYPE, parent_type="Score")

    for _ in range(2):
        entity = ExtractedEntity(
            type_name="HumannessIndex", id="hi-1", subtype_description=DESC,
        )
        await _ingest_one(resolver, entity, existing_types={"Score": ""})

    assert await _description_of("HumannessIndex") == DESC
    mock_neptune.update.assert_not_called()


@pytest.mark.xfail(strict=True, reason=_SET_COMMENT_GAP)
@pytest.mark.asyncio
async def test_brand_new_lineage_via_parent_chain_writes_description(mock_neptune, mock_cache):
    """A DIFFERENT verdict but the entity carries a parent_chain → _link_parent
    mints it as a subtype, so the description IS written (there, via upsert)."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    resolver._type_matcher = FakeTypeMatcher(MatchVerdict.DIFFERENT)

    desc = "a privately owned unit in a multi-unit building"
    entity = ExtractedEntity(
        type_name="Condo", id="c-1",
        parent_chain=["Property", "Asset"],
        subtype_description=desc,
    )
    result = await _ingest_one(resolver, entity)

    assert "Condo" in result.types_created
    assert await _description_of("Condo") == desc, (
        "a type linked into a lineage via parent_chain must carry its description"
    )
    mock_neptune.update.assert_not_called()


@pytest.mark.asyncio
async def test_brand_new_parent_subclassof_edge_survives_description_write(mock_neptune, mock_cache):
    """REGRESSION (live): minting a subtype under a BRAND-NEW parent creates the
    subclass edge (via _synthesize_ancestors), then writes the description. The
    description write must NOT wipe that edge.

    The original bug: the description was written with a full ``upsert_type``,
    which DELETEs the subclass edge when given no parent_type — so it silently
    dropped the edge a moment after _synthesize_ancestors created it (the exact
    `HumannessIndexScore` got a description but no `⊂ Score` edge symptom).

    This is a live trap on the store path too, not a historical one:
    ``ontology_catalog.upsert_type`` still clears the parent edge by default
    (``clear_parent=True``, ``onto_subclass_clear``), so a SET_COMMENT port that
    reaches for plain ``upsert_type(description=…)`` reintroduces exactly this
    bug. The lineage half is asserted here and must keep holding; the
    description half is the strict-xfailed gap above.
    """
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    resolver._type_matcher = FakeTypeMatcher(MatchVerdict.DIFFERENT)

    desc = "a score measuring how human-like a TTS voice sounds"
    entity = ExtractedEntity(
        type_name="HumannessIndexScore", id="his-1",
        parent_chain=["Score"],  # Score is BRAND NEW (not in existing_types)
        subtype_description=desc,
    )
    result = await _ingest_one(resolver, entity)

    types = await _types()
    # 1. Both types exist and the subclass edge to the brand-new parent is there.
    assert {"HumannessIndexScore", "Score"} <= set(types)
    assert "Score" in result.types_created
    assert types["HumannessIndexScore"].parent_type == "Score", (
        "the HumannessIndexScore ⊂ Score edge must survive the description write"
    )
    # 2. The synthesized parent is a root — the child's write did not re-parent it.
    assert types["Score"].parent_type is None
    mock_neptune.update.assert_not_called()


# ---------------------------------------------------------------------------
# Top-level branches must NOT write the description
#
# These pass today for a degenerate reason (nothing writes a description at all
# — see the module docstring), and are kept because they are the invariant that
# must still hold the moment SET_COMMENT is ported. Each also pins the branch's
# real observable: the type is minted top-level, with no parent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_level_different_does_not_write_description(mock_neptune, mock_cache):
    """A genuinely-new TOP-LEVEL type (DIFFERENT, no parent_chain) must NOT write
    subtype_description — the field only describes a subtype (FIX 3)."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    resolver._type_matcher = FakeTypeMatcher(MatchVerdict.DIFFERENT)

    # subtype_description present but this is a top-level type → must be ignored.
    entity = ExtractedEntity(type_name="Spaceship", id="ss-1", subtype_description=DESC)
    result = await _ingest_one(resolver, entity)

    assert "Spaceship" in result.types_created
    spaceship = (await _types())["Spaceship"]
    assert spaceship.parent_type is None
    assert spaceship.description == "", (
        "top-level DIFFERENT branch must not write subtype_description"
    )
    mock_neptune.update.assert_not_called()


@pytest.mark.asyncio
async def test_flagged_top_level_does_not_write_description(mock_neptune, mock_cache):
    """A FLAGGED type with no parent linkage must NOT write subtype_description."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    resolver._type_matcher = FakeTypeMatcher(MatchVerdict.FLAGGED)

    entity = ExtractedEntity(type_name="Widget", id="w-1", subtype_description=DESC)
    result = await _ingest_one(resolver, entity)

    assert "Widget" in result.flagged_types
    widget = (await _types())["Widget"]
    assert widget.parent_type is None
    assert widget.description == "", (
        "FLAGGED top-level branch must not write subtype_description"
    )
    mock_neptune.update.assert_not_called()


@pytest.mark.asyncio
async def test_same_as_rejected_does_not_write_description(mock_neptune, mock_cache):
    """same_as claimed but REJECTED (verdict DIFFERENT) → a genuine top-level
    type; subtype_description must not be written (FIX 3)."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    # entity.same_as names an existing type, but the matcher rejects the claim.
    resolver._type_matcher = FakeTypeMatcher(MatchVerdict.DIFFERENT)

    entity = ExtractedEntity(
        type_name="Gadget", id="g-1", same_as="Widget", subtype_description=DESC,
    )
    result = await _ingest_one(resolver, entity, existing_types={"Widget": ""})

    assert "Gadget" in result.types_created
    gadget = (await _types())["Gadget"]
    assert gadget.parent_type is None
    assert gadget.description == "", (
        "same_as-rejected (top-level) branch must not write subtype_description"
    )
    mock_neptune.update.assert_not_called()
