"""ONTA-259 + ONTA-380: discovery/extraction must NOT fabricate attributes.

ONTA-259 (values): the persona eval surfaced a UCI-health run where the NPI
``1234567890`` appeared on 92 distinct physicians — a hallucinated placeholder
that silently breaks every ID-keyed join. Discovery's extraction path was the
only rail missing an anti-fabrication contract (``research/extract.py`` and
``enrichment/llm_extractor.py`` already had one). Defenses:

  1. a prompt clause forbidding invented VALUES (``EXTRACTION_SYSTEM`` and the
     SOFT-mode ``EXTRACTION_TARGET_SYSTEM``); and
  2. a deterministic, model-agnostic backstop (``_is_fabricated_placeholder``)
     that drops obvious placeholders BEFORE they are written.

ONTA-380 (names AND values): the same rail also invents whole attribute
*families* the page never states (e.g. ``affordability_ranking``). Extended:

  3. the prompt clause covers attribute NAMES as well as VALUES
     (unknown → omit, never invent); and
  4. a source-grounding backstop (``_attribute_grounded_in_source`` /
     ``_drop_ungrounded_attributes``) drops attributes whose name AND value
     both lack support in the source text.

These tests assert the MECHANISM with INVENTED tokens (a ``Gadget`` /
``serial_number`` / ``Widget``, never the literal NPI / BC examples) so they
hold for ANY domain: an unstated field is OMITTED (never a fabricated
attribute), two value-less records never share a hallucinated value, the
backstops drop junk but KEEP legitimate grounded data, and the prompts carry
the clauses.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cograph_client.graph.ontology_queries import entity_uri
from cograph_client.resolver.attribute_resolver import AttributeSchema
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import (
    EXTRACTION_SYSTEM,
    EXTRACTION_TARGET_SYSTEM,
    SchemaResolver,
    _attribute_grounded_in_source,
    _drop_ungrounded_attributes,
    _is_fabricated_placeholder,
    _name_grounded_in_source,
    _value_grounded_in_source,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache

# The fabricated placeholder the eval caught. Used ONLY as ONE of several
# invented tokens — never the sole assertion (the mechanism, not the literal
# example, is what must hold).
_FABRICATED = "1234567890"


# ---------------------------------------------------------------------------
# 1. The deterministic predicate — drops junk, KEEPS legitimate values.
# ---------------------------------------------------------------------------

# Obvious fabricated placeholders — every one must be dropped. Only UNAMBIGUOUS
# non-values live here (the ambiguous "None"/"NA"/"nil"/"nan" are in _KEEP).
_DROP = [
    "1234567890",          # classic sequential (phone-keypad order, wraps 9->0)
    "0123456789",          # ascending run
    "9876543210",          # descending run
    "0000000000",          # all-same digit
    "1111111111",          # all-same digit
    "000-00-0000",         # separators stripped -> all zeros
    "(000) 000-0000",      # phone-shaped all zeros
    "123-45-6789",         # SSN-shaped sequential
    "N/A",
    "n/a",
    "null",
    "NULL",
    "unknown",
    "UNKNOWN",
    "unspecified",
    "tbd",
    "TBD",
    "not applicable",
    "no data",
    "placeholder",
    "xxx",
    "XXXXX",
    "----",
    "????",
]

# Legitimate values — none may be dropped (conservative by design). Includes the
# ambiguous tokens the review flagged (a clinical CONFIRMED-none, a country code)
# that MUST survive because they carry a real reading.
_KEEP = [
    "1000",                # a real price
    "2024",                # a real year
    "42",                  # a small count
    "3.14",                # a real float
    "AAA",                 # a real short code
    "XYZ",                 # a real short code
    "1023011178",          # a real 10-digit NPI (not a contiguous run)
    "4155550123",          # a real phone number
    "SN-1234567890",       # alphanumeric — never touched (has letters)
    "Acme Corporation",    # a real name
    "San Francisco",       # a real place
    "Cardiology",          # a real category
    "id 007",              # a short real code with a space
    "100",                 # small round number
    "None",                # clinical confirmed-none (allergies="None")
    "none",                # same, lower-cased
    "NA",                  # Namibia ISO code / North-America region code
    "na",                  # same, lower-cased
    "nil",                 # a stated zero / nothing, not a placeholder
    "nan",                 # a real code / name ("Nan"), not "not-a-number" filler
]


@pytest.mark.parametrize("value", _DROP)
def test_backstop_drops_obvious_placeholders(value):
    assert _is_fabricated_placeholder(value) is True, f"should DROP {value!r}"


@pytest.mark.parametrize("value", _KEEP)
def test_backstop_keeps_legitimate_values(value):
    assert _is_fabricated_placeholder(value) is False, f"should KEEP {value!r}"


def test_backstop_ignores_empty_and_blank():
    for value in (None, "", "   ", "\t"):
        assert _is_fabricated_placeholder(value) is False


def test_backstop_task_spec_examples():
    """The exact examples named in the task: drop the placeholders, keep the
    price and the year (the MECHANISM must hold for these regardless of domain)."""
    assert _is_fabricated_placeholder("1234567890") is True
    assert _is_fabricated_placeholder("0000000000") is True
    assert _is_fabricated_placeholder("N/A") is True
    assert _is_fabricated_placeholder("1000") is False   # a price
    assert _is_fabricated_placeholder("2024") is False   # a year


def test_backstop_keeps_ambiguous_tokens_but_drops_unambiguous_ones():
    """Review nit (health-domain safety): "None"/"nil" is a clinical
    CONFIRMED-none and "NA"/"nan" is a real code — dropping them would lose
    information, so they are KEPT. The unambiguous non-values ("n/a", "null",
    "unknown") are still dropped, and the digit-pattern rules are unchanged."""
    # KEPT — carry a real reading in some domain.
    for kept in ("None", "none", "NONE", "NA", "na", "nil", "NIL", "nan", "NaN"):
        assert _is_fabricated_placeholder(kept) is False, f"should KEEP {kept!r}"
    # STILL DROPPED — no valid reading as a value.
    for dropped in ("n/a", "N/A", "null", "NULL", "unknown", "tbd", "no data"):
        assert _is_fabricated_placeholder(dropped) is True, f"should DROP {dropped!r}"
    # Digit-pattern rules are untouched by the token-set change.
    assert _is_fabricated_placeholder("1234567890") is True   # sequential
    assert _is_fabricated_placeholder("0000000000") is True   # all-same, len 10
    assert _is_fabricated_placeholder("777777") is True       # all-same, len 6 (>=6)
    assert _is_fabricated_placeholder("12345") is False       # sequential but len 5 (<6)
    assert _is_fabricated_placeholder("55555") is False       # all-same but len 5 (<6)


# ---------------------------------------------------------------------------
# 2. End-to-end through the writer: an unstated value is OMITTED, never a
#    placeholder — and two value-less records never share a fabricated key.
# ---------------------------------------------------------------------------

RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

_GADGET_TYPES = {"Gadget": ""}
_GADGET_ATTRS = {
    "Gadget": {
        "name": AttributeSchema("name", "string"),
        "serial_number": AttributeSchema("serial_number", "string"),
    }
}


def _resolver(tmp_path):
    return SchemaResolver(AsyncMock(), "fake-key", JsonVerdictCache(tmp_path / "c.json"))


async def _drive(resolver, entity, entity_id, *, drop):
    """Drive ``_resolve_and_insert_entity`` for one Gadget, capturing the batch.

    ``drop`` toggles the ONTA-259 backstop the way the real call sites do: True
    on the model-proposed extraction path, False (default) on the authoritative
    CSV path.
    """
    result = IngestResult()
    collected: list[tuple[str, str, str]] = []
    await resolver._resolve_and_insert_entity(
        entity=entity,
        resolved_type="Gadget",
        entity_uri=entity_uri("Gadget", entity_id),
        is_duplicate=False,
        graph_uri="https://omnix.dev/graphs/test",
        existing_types=dict(_GADGET_TYPES),
        existing_attrs={k: dict(v) for k, v in _GADGET_ATTRS.items()},
        source="test",
        result=result,
        _collect_triples=collected,
        drop_placeholder_values=drop,
    )
    return collected


def _objects(triples):
    return [o for _s, _p, o in triples]


async def test_unstated_value_is_omitted_not_a_placeholder(tmp_path):
    """A Gadget whose serial_number the source never stated (the model filled it
    with a placeholder) → the serial attribute is OMITTED, not written."""
    resolver = _resolver(tmp_path)
    gadget = ExtractedEntity(
        type_name="Gadget",
        id="g1",
        attributes=[
            ExtractedAttribute(name="name", value="Widget One", datatype="string"),
            ExtractedAttribute(name="serial_number", value=_FABRICATED, datatype="string"),
        ],
    )
    collected = await _drive(resolver, gadget, "g1", drop=True)

    # The fabricated serial never reaches the graph in ANY form.
    assert _FABRICATED not in _objects(collected)
    assert not any("serial_number" in p for _s, p, _o in collected)
    # The real attribute IS still written — the drop is surgical, not a nuke.
    assert "Widget One" in _objects(collected)


async def test_two_serialless_gadgets_do_not_share_a_fabricated_serial(tmp_path):
    """Two DISTINCT Gadgets the model both stamped with the SAME placeholder
    serial must NOT end up sharing that serial — else an ID-keyed join collapses
    two real things into one (the 92-physicians bug). Neither writes a serial."""
    resolver = _resolver(tmp_path)
    triples_all: list[tuple[str, str, str]] = []
    for gid, label in (("g1", "Widget One"), ("g2", "Widget Two")):
        gadget = ExtractedEntity(
            type_name="Gadget",
            id=gid,
            attributes=[
                ExtractedAttribute(name="name", value=label, datatype="string"),
                ExtractedAttribute(name="serial_number", value=_FABRICATED, datatype="string"),
            ],
        )
        triples_all += await _drive(resolver, gadget, gid, drop=True)

    # No fabricated serial on EITHER record → no false shared key to join on.
    assert _FABRICATED not in _objects(triples_all)
    assert not any("serial_number" in p for _s, p, _o in triples_all)
    # The two records stayed distinct nodes (keyed by id, not by a fake serial).
    assert entity_uri("Gadget", "g1") != entity_uri("Gadget", "g2")
    labels = {o for _s, p, o in triples_all if p == RDFS_LABEL}
    assert {"g1", "g2"} <= labels


async def test_legitimate_serial_passes_through_with_backstop_on(tmp_path):
    """Conservatism end-to-end: a REAL serial survives the backstop untouched —
    the guard drops fabrications, not data."""
    resolver = _resolver(tmp_path)
    gadget = ExtractedEntity(
        type_name="Gadget",
        id="g3",
        attributes=[
            ExtractedAttribute(name="serial_number", value="SN-9F2A-1023011178", datatype="string"),
        ],
    )
    collected = await _drive(resolver, gadget, "g3", drop=True)
    assert "SN-9F2A-1023011178" in _objects(collected)


async def test_backstop_is_off_for_the_verbatim_path(tmp_path):
    """The flag SCOPES the drop: with ``drop_placeholder_values=False`` (the
    default, used by the authoritative CSV path) the same value is written
    verbatim — the backstop only guards the model-proposed extraction rail."""
    resolver = _resolver(tmp_path)
    gadget = ExtractedEntity(
        type_name="Gadget",
        id="g4",
        attributes=[
            ExtractedAttribute(name="serial_number", value=_FABRICATED, datatype="string"),
        ],
    )
    collected = await _drive(resolver, gadget, "g4", drop=False)
    # Verbatim path is untouched: the value is written (proving the drop is gated).
    assert _FABRICATED in _objects(collected)


# ---------------------------------------------------------------------------
# 3. The prompt carries the anti-fabrication clause (both extraction prompts).
# ---------------------------------------------------------------------------


def test_open_extraction_prompt_forbids_invented_values():
    assert "Never fabricate attributes" in EXTRACTION_SYSTEM
    assert "NAMES" in EXTRACTION_SYSTEM
    assert "VALUES" in EXTRACTION_SYSTEM
    assert "NEVER invent an identifier" in EXTRACTION_SYSTEM
    # Names the concrete filler tokens so the model recognizes them.
    assert "1234567890" in EXTRACTION_SYSTEM
    assert "0000000000" in EXTRACTION_SYSTEM
    assert "N/A" in EXTRACTION_SYSTEM


def test_soft_target_prompt_forbids_invented_values():
    assert "NEVER FABRICATE ATTRIBUTES" in EXTRACTION_TARGET_SYSTEM
    assert "1234567890" in EXTRACTION_TARGET_SYSTEM
    assert "0000000000" in EXTRACTION_TARGET_SYSTEM


def test_open_extraction_prompt_forbids_invented_attribute_names():
    """ONTA-380: the open prompt forbids inventing attribute NAMES, not just values."""
    assert "attribute NAMES" in EXTRACTION_SYSTEM or "NAMES" in EXTRACTION_SYSTEM
    assert "Unknown → omit" in EXTRACTION_SYSTEM or "Unknown" in EXTRACTION_SYSTEM
    # Concrete fabricated-family examples so the model recognizes the failure mode.
    assert "affordability_ranking" in EXTRACTION_SYSTEM
    assert "online_activity_percentage_of_summer_instruction" in EXTRACTION_SYSTEM
    assert "hallucinated" in EXTRACTION_SYSTEM.casefold() or "share a hallucinated" in EXTRACTION_SYSTEM


def test_soft_target_prompt_forbids_invented_attribute_names():
    """ONTA-380: the SOFT target prompt carries the same name+value contract."""
    assert "NAMES OR VALUES" in EXTRACTION_TARGET_SYSTEM
    assert "affordability_ranking" in EXTRACTION_TARGET_SYSTEM
    assert "online_activity_percentage_of_summer_instruction" in EXTRACTION_TARGET_SYSTEM


# ---------------------------------------------------------------------------
# 4. ONTA-380: source-grounding predicate — fabricated attr families drop;
#    grounded facts KEEP. Anti-overfit: invented tokens, not domain fixtures.
# ---------------------------------------------------------------------------

# A BC-universities-style page (shape only — invented institution tokens so the
# test is not locked to a live BC crawl). States name / city / year / enrollment;
# says NOTHING about affordability rankings or online-activity percentages.
_WIDGET_SOURCE = """
North Cascadia Polytechnic (NCP)
Located in Harbourview, Cascadia Territory.
Founded in 1908. Approximately 70,000 students enrolled.
Public research polytechnic. Main campus on the harbour.
Website: https://example.edu/ncp
"""


def test_value_grounded_when_stated_in_source():
    src = _WIDGET_SOURCE.casefold()
    assert _value_grounded_in_source("Harbourview", src) is True
    assert _value_grounded_in_source("1908", src) is True
    assert _value_grounded_in_source("70,000", src) is True  # digit-normalized
    assert _value_grounded_in_source("70000", src) is True
    assert _value_grounded_in_source("North Cascadia Polytechnic", src) is True


def test_value_not_grounded_when_absent_or_placeholder():
    src = _WIDGET_SOURCE.casefold()
    assert _value_grounded_in_source("99", src) is False           # short + absent
    assert _value_grounded_in_source("42", src) is False
    assert _value_grounded_in_source("affordable-tier-A", src) is False
    assert _value_grounded_in_source("1234567890", src) is False   # placeholder
    assert _value_grounded_in_source("N/A", src) is False
    assert _value_grounded_in_source("", src) is False
    assert _value_grounded_in_source(None, src) is False


def test_name_grounded_on_distinctive_tokens_only():
    src = _WIDGET_SOURCE.casefold()
    # "harbour" / "cascadia" / "student" appear; pure-generic names do not count.
    assert _name_grounded_in_source("harbour_campus", src) is True
    assert _name_grounded_in_source("student_enrollment", src) is True
    assert _name_grounded_in_source("cascadia_region", src) is True
    assert _name_grounded_in_source("affordability_ranking", src) is False
    assert _name_grounded_in_source(
        "online_activity_percentage_of_summer_instruction", src
    ) is False
    assert _name_grounded_in_source("year", src) is False          # stopword-only
    assert _name_grounded_in_source("ranking", src) is False
    assert _name_grounded_in_source("percentage_score", src) is False


def test_attribute_keep_when_value_or_name_grounded():
    src = _WIDGET_SOURCE
    # Value grounded, name paraphrased → KEEP.
    assert _attribute_grounded_in_source("city", "Harbourview", src) is True
    assert _attribute_grounded_in_source("founded_year", "1908", src) is True
    # Name grounded, value short/weak → KEEP (name alone is enough evidence the
    # concept is on the page; value filtering for placeholders is ONTA-259).
    assert _attribute_grounded_in_source("harbour_campus", "main", src) is True
    # BOTH ungrounded → DROP (pure fabricated attribute family).
    assert _attribute_grounded_in_source(
        "affordability_ranking", "7", src
    ) is False
    assert _attribute_grounded_in_source(
        "online_activity_percentage_of_summer_instruction", "42", src
    ) is False
    # No source text → KEEP (cannot verify; conservative).
    assert _attribute_grounded_in_source(
        "affordability_ranking", "7", ""
    ) is True


def test_drop_ungrounded_attributes_on_bc_style_fixture():
    """BC-style page: grounded facts survive; fabricated attr families vanish.

    Anti-overfit: institution tokens are invented (NCP / Harbourview), not a
    live BC university string. Mechanism under test is source grounding.
    """
    extraction = ExtractionResult(
        source_text=_WIDGET_SOURCE,
        entities=[
            ExtractedEntity(
                type_name="Widget",
                id="ncp",
                attributes=[
                    ExtractedAttribute(
                        name="name",
                        value="North Cascadia Polytechnic",
                        datatype="string",
                    ),
                    ExtractedAttribute(
                        name="city", value="Harbourview", datatype="string",
                    ),
                    ExtractedAttribute(
                        name="founded_year", value="1908", datatype="integer",
                    ),
                    ExtractedAttribute(
                        name="student_count", value="70000", datatype="integer",
                    ),
                    # Fabricated families the page never states:
                    ExtractedAttribute(
                        name="affordability_ranking",
                        value="7",
                        datatype="integer",
                    ),
                    ExtractedAttribute(
                        name="online_activity_percentage_of_summer_instruction",
                        value="42",
                        datatype="float",
                    ),
                ],
            )
        ],
    )
    filtered = _drop_ungrounded_attributes(extraction)
    names = {a.name for a in filtered.entities[0].attributes}
    assert "name" in names
    assert "city" in names
    assert "founded_year" in names
    assert "student_count" in names
    assert "affordability_ranking" not in names
    assert "online_activity_percentage_of_summer_instruction" not in names
    values = {a.value for a in filtered.entities[0].attributes}
    assert "7" not in values
    assert "42" not in values


def test_unstated_field_gets_no_fabricated_attribute():
    """Acceptance: an unstated field produces no attribute after the backstop."""
    extraction = ExtractionResult(
        source_text="Gadget Alpha ships with a titanium shell. SKU GA-100.",
        entities=[
            ExtractedEntity(
                type_name="Gadget",
                id="ga",
                attributes=[
                    ExtractedAttribute(
                        name="name", value="Gadget Alpha", datatype="string",
                    ),
                    ExtractedAttribute(
                        name="sku", value="GA-100", datatype="string",
                    ),
                    # Unstated field the model invented:
                    ExtractedAttribute(
                        name="warp_flux_coefficient",
                        value="0.91",
                        datatype="float",
                    ),
                ],
            )
        ],
    )
    filtered = _drop_ungrounded_attributes(extraction)
    names = {a.name for a in filtered.entities[0].attributes}
    assert names == {"name", "sku"}
    assert "warp_flux_coefficient" not in names


def test_two_entities_do_not_share_a_hallucinated_value():
    """Acceptance: two records that both lack a field never share a stand-in.

    Both Widgets get the same invented ``affordability_ranking=99`` the page
    never stated — after the backstop neither carries it, so no false shared key.
    """
    source = (
        "Widget One is a red handheld tool.\n"
        "Widget Two is a blue handheld tool.\n"
    )
    extraction = ExtractionResult(
        source_text=source,
        entities=[
            ExtractedEntity(
                type_name="Widget",
                id="w1",
                attributes=[
                    ExtractedAttribute(
                        name="name", value="Widget One", datatype="string",
                    ),
                    ExtractedAttribute(
                        name="color", value="red", datatype="string",
                    ),
                    ExtractedAttribute(
                        name="affordability_ranking",
                        value="99",
                        datatype="integer",
                    ),
                ],
            ),
            ExtractedEntity(
                type_name="Widget",
                id="w2",
                attributes=[
                    ExtractedAttribute(
                        name="name", value="Widget Two", datatype="string",
                    ),
                    ExtractedAttribute(
                        name="color", value="blue", datatype="string",
                    ),
                    ExtractedAttribute(
                        name="affordability_ranking",
                        value="99",
                        datatype="integer",
                    ),
                ],
            ),
        ],
    )
    filtered = _drop_ungrounded_attributes(extraction)
    for ent in filtered.entities:
        names = {a.name for a in ent.attributes}
        assert "affordability_ranking" not in names
        values = {a.value for a in ent.attributes}
        assert "99" not in values
    # Real grounded attrs survived on both records.
    by_id = {e.id: e for e in filtered.entities}
    assert {a.value for a in by_id["w1"].attributes} >= {"Widget One", "red"}
    assert {a.value for a in by_id["w2"].attributes} >= {"Widget Two", "blue"}


def test_drop_ungrounded_is_noop_without_source_text():
    """Conservative: empty source_text leaves attributes untouched."""
    extraction = ExtractionResult(
        source_text="",
        entities=[
            ExtractedEntity(
                type_name="Widget",
                id="w",
                attributes=[
                    ExtractedAttribute(
                        name="affordability_ranking",
                        value="7",
                        datatype="integer",
                    ),
                ],
            )
        ],
    )
    filtered = _drop_ungrounded_attributes(extraction)
    assert filtered.entities[0].attributes[0].name == "affordability_ranking"
    assert filtered is extraction  # same object when nothing changes


async def test_extract_applies_grounding_backstop(tmp_path):
    """End-to-end through ``_extract``: ungrounded attr families never leave
    the extraction step (mocked LLM returns a mix of real + fabricated attrs).
    """
    import json

    resolver = _resolver(tmp_path)
    payload = {
        "entities": [
            {
                "type_name": "Widget",
                "id": "ncp",
                "attributes": [
                    {
                        "name": "name",
                        "value": "North Cascadia Polytechnic",
                        "datatype": "string",
                    },
                    {
                        "name": "city",
                        "value": "Harbourview",
                        "datatype": "string",
                    },
                    {
                        "name": "affordability_ranking",
                        "value": "7",
                        "datatype": "integer",
                    },
                    {
                        "name": "online_activity_percentage_of_summer_instruction",
                        "value": "42",
                        "datatype": "float",
                    },
                ],
            }
        ],
        "relationships": [],
    }
    resolver._extract_via_openrouter = AsyncMock(
        return_value=(json.dumps(payload), "stop", None)
    )
    resolver.EXTRACT_PROVIDER = "openrouter"
    resolver._openrouter_key = "fake-key"

    result = await resolver._extract(_WIDGET_SOURCE, "text")
    assert len(result.entities) == 1
    names = {a.name for a in result.entities[0].attributes}
    assert "name" in names
    assert "city" in names
    assert "affordability_ranking" not in names
    assert "online_activity_percentage_of_summer_instruction" not in names
