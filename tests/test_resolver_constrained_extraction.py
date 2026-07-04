"""ONTA-199 — DISCOVERY-ONLY constrained extraction.

Web discovery reuses ``SchemaResolver.ingest`` but has already CONFIRMED a
single target type + attribute set with the user. Re-running the OPEN-ENDED
multi-type reifier over a rich source payload mints ~20 unwanted sub-types
(Address/Taxonomy/Organization/…) and ~3x output tokens, which blew the
extraction watchdog. ``ingest(constrain_types=..., constrain_attributes=...)``
pins extraction to the confirmed type + attributes.

These tests prove three things:

  1. **Constraint works** — an active constraint (a) reaches ``_extract`` /
     the extraction prompt, and (b) drives a post-extraction guard that drops
     off-type entities and unrequested attributes.
  2. **REGRESSION GUARD (the safety argument)** — ``constrain_types=None`` (the
     default, and every document/CSV/text caller) is a byte-for-byte no-op: the
     system + user prompt handed to the LLM is IDENTICAL to the pre-ONTA-199
     open-ended prompt, and the extraction result is returned untouched.
  3. **Prompt-builder + guard unit behavior** in isolation.

Harness mirrors tests/test_resolver_calibration_concurrency.py: a bare AsyncMock
Neptune with ``_extract`` / ``_fetch_ontology`` patched, plus (for the prompt
tests) a patched module-level ``openrouter_chat`` to capture the exact prompt.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from cograph_client.resolver import schema_resolver as sr
from cograph_client.resolver.schema_resolver import (
    EXTRACTION_SYSTEM,
    EXTRACTION_CONSTRAINT_SYSTEM,
    SchemaResolver,
    _apply_extraction_constraint,
    _build_constraint_user_block,
)
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionConstraint,
    ExtractionResult,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache


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


PHYSICIAN_CONSTRAINT_TYPES = ["Physician"]
PHYSICIAN_CONSTRAINT_ATTRS = {"Physician": ["name", "specialty", "city", "phone"]}


# --- (3) prompt-builder + guard unit behavior --------------------------------


def test_build_constraint_user_block_lists_type_and_attributes():
    c = ExtractionConstraint(
        types=PHYSICIAN_CONSTRAINT_TYPES, attributes=PHYSICIAN_CONSTRAINT_ATTRS
    )
    block = _build_constraint_user_block(c)
    assert "CONSTRAINT" in block
    assert "Physician: name, specialty, city, phone" in block
    # No other type mentioned.
    assert "Address" not in block and "Taxonomy" not in block


def test_build_constraint_user_block_is_empty_for_none_or_inactive():
    assert _build_constraint_user_block(None) == ""
    assert _build_constraint_user_block(ExtractionConstraint()) == ""  # no types


def test_apply_constraint_drops_off_type_entities_and_unrequested_attrs():
    c = ExtractionConstraint(
        types=PHYSICIAN_CONSTRAINT_TYPES, attributes=PHYSICIAN_CONSTRAINT_ATTRS
    )
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Physician",
                id="dr-jane-roe",
                attributes=[
                    ExtractedAttribute(name="name", value="Jane Roe"),
                    ExtractedAttribute(name="specialty", value="Cardiology"),
                    # Unrequested attributes the open-ended prompt would mint:
                    ExtractedAttribute(name="taxonomy_code", value="207RC0000X"),
                    ExtractedAttribute(name="npi", value="1234567890"),
                ],
            ),
            # Off-type sub-entity the reifier lifts out — must be dropped.
            ExtractedEntity(type_name="Address", id="addr-1"),
            ExtractedEntity(type_name="Taxonomy", id="tax-1"),
        ],
        relationships=[],
    )
    guarded = _apply_extraction_constraint(result, c)
    assert [e.type_name for e in guarded.entities] == ["Physician"]
    attr_names = {a.name for a in guarded.entities[0].attributes}
    assert attr_names == {"name", "specialty"}  # taxonomy_code / npi dropped


def test_apply_constraint_keeps_identifying_attribute_even_if_not_listed():
    # 'name' is always retained so a trimmed record stays identifiable, even when
    # the confirmed attribute set happened to omit it.
    c = ExtractionConstraint(types=["Physician"], attributes={"Physician": ["specialty"]})
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Physician",
                id="p1",
                attributes=[
                    ExtractedAttribute(name="name", value="Jane"),
                    ExtractedAttribute(name="specialty", value="Cardiology"),
                    ExtractedAttribute(name="junk", value="x"),
                ],
            )
        ]
    )
    guarded = _apply_extraction_constraint(result, c)
    kept = {a.name for a in guarded.entities[0].attributes}
    assert kept == {"name", "specialty"}


def test_apply_constraint_strips_lineage_that_would_mint_extra_types():
    # A surviving on-type entity that STILL carries also_types / parent_chain /
    # parent_type / subtype_description would mint exactly the extra types
    # ONTA-199 prevents during the resolve step — the guard clears them.
    c = ExtractionConstraint(types=["Physician"], attributes={"Physician": ["name"]})
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Physician",
                id="p1",
                also_types=["HealthcareOrganization"],
                parent_type="Provider",
                parent_chain=["Provider", "Agent"],
                subtype_description="a doctor who...",
                attributes=[ExtractedAttribute(name="name", value="Jane")],
            )
        ]
    )
    guarded = _apply_extraction_constraint(result, c)
    e = guarded.entities[0]
    assert e.type_name == "Physician"  # own type preserved
    assert e.also_types == []
    assert e.parent_chain == []
    assert e.parent_type is None
    assert e.subtype_description is None


def test_apply_constraint_none_or_inactive_is_identity():
    result = ExtractionResult(
        entities=[ExtractedEntity(type_name="Anything", id="x")],
        relationships=[],
    )
    assert _apply_extraction_constraint(result, None) is result
    assert _apply_extraction_constraint(result, ExtractionConstraint()) is result


def test_apply_constraint_keeps_relationships_between_survivors_drops_dangling():
    # A relationship whose endpoints both survive is KEPT; one pointing at a
    # dropped off-type entity is removed (no dangling edges).
    c = ExtractionConstraint(types=["Physician"], attributes={"Physician": ["name"]})
    result = ExtractionResult(
        entities=[
            ExtractedEntity(type_name="Physician", id="p1"),
            ExtractedEntity(type_name="Physician", id="p2"),
            ExtractedEntity(type_name="Address", id="addr-1"),  # dropped
        ],
        relationships=[
            ExtractedRelationship(source_id="p1", predicate="colleague_of", target_id="p2"),
            ExtractedRelationship(source_id="p1", predicate="located_at", target_id="addr-1"),
        ],
    )
    guarded = _apply_extraction_constraint(result, c)
    assert {e.id for e in guarded.entities} == {"p1", "p2"}
    # Only the p1->p2 edge survives; the p1->addr-1 edge (dangling) is dropped.
    assert len(guarded.relationships) == 1
    r = guarded.relationships[0]
    assert (r.source_id, r.predicate, r.target_id) == ("p1", "colleague_of", "p2")


# --- (1) constraint works end-to-end through ingest(...) ---------------------


@pytest.mark.asyncio
async def test_ingest_threads_constraint_into_extract(mock_neptune, mock_cache):
    """An active constraint reaches ``_extract`` as the ``constraint`` kwarg."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    seen: dict = {}

    async def fake_extract(content, content_type, existing_types=None, constraint=None):
        seen["constraint"] = constraint
        return ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Physician",
                    id="p0",
                    attributes=[ExtractedAttribute(name="name", value="Jane")],
                )
            ]
        )

    records = json.dumps([{"name": "Jane", "specialty": "Cardiology"}])
    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            await resolver.ingest(
                records,
                "test-tenant",
                content_type="json",
                constrain_types=PHYSICIAN_CONSTRAINT_TYPES,
                constrain_attributes=PHYSICIAN_CONSTRAINT_ATTRS,
            )

    c = seen["constraint"]
    assert isinstance(c, ExtractionConstraint)
    assert c.types == ["Physician"]
    assert c.allowed_attributes("Physician") == {"name", "specialty", "city", "phone"}


@pytest.mark.asyncio
async def test_ingest_threads_constraint_through_multi_chunk_json(
    mock_neptune, mock_cache, monkeypatch
):
    """The MULTI-CHUNK calibrated JSON path forwards the active constraint to
    EVERY ``_extract`` call (first-batch + concurrent remainder), not just the
    single-chunk path — so a large discovery pull stays constrained throughout.
    """
    from cograph_client.resolver import chunker

    # Force the multi-chunk path: small conservative batches over 60 records.
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 700)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    monkeypatch.setattr(SchemaResolver, "EXTRACT_CONCURRENCY", 8)

    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    seen_constraints: list = []

    async def fake_extract(content, content_type, existing_types=None, constraint=None):
        seen_constraints.append(constraint)
        rows = json.loads(content)
        return ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Physician",
                    id=str(r["id"]),
                    attributes=[ExtractedAttribute(name="name", value=r["name"])],
                )
                for r in rows
            ]
        )

    records = json.dumps([{"id": i, "name": f"dr_{i}"} for i in range(60)])
    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(
                records,
                "test-tenant",
                content_type="json",
                constrain_types=PHYSICIAN_CONSTRAINT_TYPES,
                constrain_attributes=PHYSICIAN_CONSTRAINT_ATTRS,
            )

    # More than one chunk actually ran (multi-chunk path exercised)...
    assert len(seen_constraints) > 1, seen_constraints
    # ...and EVERY chunk received the active constraint (none silently dropped).
    assert all(isinstance(c, ExtractionConstraint) and c.is_active for c in seen_constraints)
    assert result.entities_resolved == 60


@pytest.mark.asyncio
async def test_ingest_constraint_prompt_pins_type_and_drops_off_type(
    mock_neptune, mock_cache, monkeypatch
):
    """Drive ingest through the REAL ``_extract`` with a captured ``openrouter_chat``.

    Asserts: (a) the extraction prompt carries the CONSTRAINED system + the
    per-type user block, and (b) the post-guard drops the off-type Address the
    model still emitted, keeping only the Physician with confirmed attributes.
    """
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")

    captured: dict = {}

    async def fake_or(api_key, system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        # Model still emits an off-type Address + an unrequested attribute.
        body = {
            "entities": [
                {
                    "type_name": "Physician",
                    "id": "dr-jane",
                    "attributes": [
                        {"name": "name", "value": "Jane", "datatype": "string"},
                        {"name": "specialty", "value": "Cardiology", "datatype": "string"},
                        {"name": "taxonomy_code", "value": "207R", "datatype": "string"},
                    ],
                },
                {"type_name": "Address", "id": "addr-1", "attributes": []},
            ],
            "relationships": [],
        }
        return json.dumps(body), "stop", None

    monkeypatch.setattr(sr, "openrouter_chat", fake_or)

    captured_extraction: dict = {}
    orig = resolver._resolve_and_insert

    async def spy(extraction, *a, **k):
        captured_extraction["ex"] = extraction
        return await orig(extraction, *a, **k)

    records = json.dumps([{"name": "Jane", "specialty": "Cardiology"}])
    with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
        with patch.object(resolver, "_resolve_and_insert", side_effect=spy):
            await resolver.ingest(
                records,
                "test-tenant",
                content_type="json",
                constrain_types=PHYSICIAN_CONSTRAINT_TYPES,
                constrain_attributes=PHYSICIAN_CONSTRAINT_ATTRS,
            )

    # (a) prompt carries the constraint.
    assert EXTRACTION_CONSTRAINT_SYSTEM in captured["system"]
    assert "Physician: name, specialty, city, phone" in captured["user"]

    # (b) post-guard dropped the off-type Address and the unrequested attribute.
    ex = captured_extraction["ex"]
    assert [e.type_name for e in ex.entities] == ["Physician"]
    assert {a.name for a in ex.entities[0].attributes} == {"name", "specialty"}


# --- (2) REGRESSION GUARD: constrain_types=None is a byte-for-byte no-op ------


async def _capture_prompt_no_constraint(resolver, monkeypatch, content):
    """Run one document-path ingest with a captured openrouter_chat, no constraint."""
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")
    captured: dict = {}

    async def fake_or(api_key, system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        captured["kwargs"] = kwargs
        return json.dumps({"entities": [], "relationships": []}), "stop", None

    monkeypatch.setattr(sr, "openrouter_chat", fake_or)
    with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
        await resolver.ingest(content, "test-tenant", content_type="json")
    return captured


@pytest.mark.asyncio
async def test_no_constraint_prompt_is_unchanged_open_ended(
    mock_neptune, mock_cache, monkeypatch
):
    """The document path (no constraint) hands the LLM the EXACT open-ended
    system prompt — no constraint block appended — and does NOT pass a
    ``system_prompt`` kwarg to ``openrouter_chat`` (byte-for-byte pre-ONTA-199).
    """
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    content = json.dumps([{"name": "Jane", "role": "doctor"}])
    captured = await _capture_prompt_no_constraint(resolver, monkeypatch, content)

    # System prompt is EXACTLY the open-ended one — no constraint suffix.
    assert captured["system"] == EXTRACTION_SYSTEM
    assert EXTRACTION_CONSTRAINT_SYSTEM not in captured["system"]
    # User prompt carries no constraint block.
    assert "CONSTRAINT — extract ONLY these type(s)" not in captured["user"]
    # And the no-op path never forwards the system_prompt kwarg.
    assert "system_prompt" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_no_constraint_extraction_result_is_untouched(mock_neptune, mock_cache):
    """With no constraint, a multi-type extraction is returned verbatim — the
    open-ended behavior (Physician + Address + Taxonomy all survive)."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

    multi_type = ExtractionResult(
        entities=[
            ExtractedEntity(type_name="Physician", id="p0"),
            ExtractedEntity(type_name="Address", id="a0"),
            ExtractedEntity(type_name="Taxonomy", id="t0"),
        ],
        relationships=[],
    )

    async def fake_extract(content, content_type, existing_types=None):
        # NOTE: no ``constraint`` param — the no-op path must not pass the kwarg,
        # exactly like the pre-existing mocks in the other resolver suites.
        return multi_type

    captured: dict = {}
    orig = resolver._resolve_and_insert

    async def spy(extraction, *a, **k):
        captured["ex"] = extraction
        return await orig(extraction, *a, **k)

    records = json.dumps([{"name": "Jane"}])
    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            with patch.object(resolver, "_resolve_and_insert", side_effect=spy):
                await resolver.ingest(records, "test-tenant", content_type="json")

    # All three types survived — open-ended behavior preserved.
    assert [e.type_name for e in captured["ex"].entities] == [
        "Physician",
        "Address",
        "Taxonomy",
    ]
