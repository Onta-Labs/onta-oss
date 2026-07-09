"""ONTA-259: discovery/extraction must NOT fabricate placeholder attribute VALUES.

The persona eval surfaced a UCI-health run where the NPI ``1234567890`` appeared
on 92 distinct physicians — a hallucinated placeholder that silently breaks
every ID-keyed join. Discovery's extraction path was the only rail missing an
anti-fabrication contract (``research/extract.py`` and
``enrichment/llm_extractor.py`` already had one). This adds two defenses in
``schema_resolver``:

  1. a prompt clause forbidding invented VALUES (``EXTRACTION_SYSTEM`` and the
     SOFT-mode ``EXTRACTION_TARGET_SYSTEM``); and
  2. a deterministic, model-agnostic backstop (``_is_fabricated_placeholder``)
     that drops obvious placeholders BEFORE they are written, so a hallucinated
     value can't reach the graph even if the prompt fails.

These tests assert the MECHANISM with INVENTED tokens (a ``Gadget`` /
``serial_number``, never the literal NPI example) so they hold for ANY domain:
an unstated value is OMITTED (never a placeholder), two value-less records never
share a fabricated key, the backstop drops junk but KEEPS legitimate values, and
the prompt carries the clause.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cograph_client.graph.ontology_queries import entity_uri
from cograph_client.resolver.attribute_resolver import AttributeSchema
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import (
    EXTRACTION_SYSTEM,
    EXTRACTION_TARGET_SYSTEM,
    SchemaResolver,
    _is_fabricated_placeholder,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache

# The fabricated placeholder the eval caught. Used ONLY as ONE of several
# invented tokens — never the sole assertion (the mechanism, not the literal
# example, is what must hold).
_FABRICATED = "1234567890"


# ---------------------------------------------------------------------------
# 1. The deterministic predicate — drops junk, KEEPS legitimate values.
# ---------------------------------------------------------------------------

# Obvious fabricated placeholders — every one must be dropped.
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
    "NA",
    "None",
    "unknown",
    "UNKNOWN",
    "tbd",
    "TBD",
    "not applicable",
    "xxx",
    "XXXXX",
    "----",
    "????",
]

# Legitimate values — none may be dropped (conservative by design).
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
    assert "Never fabricate values" in EXTRACTION_SYSTEM
    assert "NEVER invent an identifier" in EXTRACTION_SYSTEM
    # Names the concrete filler tokens so the model recognizes them.
    assert "1234567890" in EXTRACTION_SYSTEM
    assert "0000000000" in EXTRACTION_SYSTEM
    assert "N/A" in EXTRACTION_SYSTEM


def test_soft_target_prompt_forbids_invented_values():
    assert "NEVER FABRICATE A VALUE" in EXTRACTION_TARGET_SYSTEM
    assert "1234567890" in EXTRACTION_TARGET_SYSTEM
    assert "0000000000" in EXTRACTION_TARGET_SYSTEM
