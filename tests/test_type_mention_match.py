"""General NL type-mention matching (anti-overfit: synthetic types only).

Defect class: COUNT / list fixtures invent non-existent type leaves when the
user says a domain synonym or CamelCase head-noun plural, producing silent 0.
"""

from __future__ import annotations

from infona_client.nlp.cypher_generate import (
    TEMPLATE_COUNT_BY_TYPE,
    match_type_name,
    resolve_type_name,
    try_deterministic_cypher,
    try_related_name_filter_query,
    try_stub_count_query,
)


SYNTH_TYPES = ["Widget", "SensorHub", "TrialRun", "InventorySKU"]


def test_match_exact_and_simple_plural():
    assert match_type_name("widgets", SYNTH_TYPES) == "Widget"
    assert match_type_name("Widget", SYNTH_TYPES) == "Widget"


def test_match_camelcase_head_noun_plural():
    """Compound type leaves match their final CamelCase segment plural."""
    assert match_type_name("runs", SYNTH_TYPES) == "TrialRun"
    assert match_type_name("hubs", SYNTH_TYPES) == "SensorHub"
    assert match_type_name("skus", SYNTH_TYPES) == "InventorySKU"


def test_match_multiword_phrase():
    assert match_type_name("sensor hubs", SYNTH_TYPES) == "SensorHub"
    assert match_type_name("trial runs", SYNTH_TYPES) == "TrialRun"


def test_match_or_alternatives_picks_known_leaf():
    # Only InventorySKU is present among the alternatives' hits.
    assert (
        match_type_name("inventory items or SKUs", ["InventorySKU", "Widget"])
        == "InventorySKU"
    )


def test_no_invent_when_ontology_known():
    assert resolve_type_name("gadgets", SYNTH_TYPES) is None
    assert try_stub_count_query(
        "How many gadgets?", type_names=SYNTH_TYPES
    ) is None


def test_guess_only_when_ontology_empty():
    # type_names is None AND no summary types → bootstrap invent
    assert resolve_type_name("widgets", None, "") == "Widget"
    # Explicit empty candidate list still falls through to invent only when
    # no ontology summary is available (same as None for hermetic tests).
    assert resolve_type_name("widgets", None, "Type: SensorHub") is None


def test_count_fixture_head_noun():
    payload = try_stub_count_query(
        "How many runs?", type_names=SYNTH_TYPES
    )
    assert payload is not None
    assert payload["template"] == TEMPLATE_COUNT_BY_TYPE
    assert payload["params"]["type_names"] == ["TrialRun"]


def test_count_fixture_simple_plural():
    payload = try_stub_count_query(
        "How many widgets are there?", type_names=SYNTH_TYPES
    )
    assert payload is not None
    assert payload["params"]["type_names"] == ["Widget"]


def test_count_does_not_silent_zero_on_unknown():
    """Miss must fall through (None), not invent Trial/SKU and count 0."""
    assert (
        try_stub_count_query(
            "How many inventory items or SKUs are there?",
            type_names=["Widget", "SensorHub"],
        )
        is None
    )


def test_containment_false_positives_short_types():
    """Short accidental substrings must not bind (ONTA-450 class)."""
    types = ["Age", "Cat", "Ion", "Category"]
    # "categories" → Category via plural, not Age/Cat/Ion
    assert match_type_name("categories", types) == "Category"
    assert match_type_name("medication", types) is None


def test_related_filter_resolves_phase_from_ontology():
    onto = (
        "Type: TrialRun\n"
        "  - enrollment: integer (literal)\n"
        "  - has_phase: relationship → Phase\n"
        "Type: Phase\n"
    )
    payload = try_related_name_filter_query(
        "which trial runs have phase 2",
        onto,
        type_names=["TrialRun", "Phase"],
    )
    assert payload is not None
    assert payload["template"] == "related_entity_name_filter"
    assert payload["params"]["rel_attr"] == "has_phase"
    assert payload["params"]["target_name"] == "2"
    assert "TrialRun" in payload["params"]["type_names"]


def test_related_filter_resolves_supplier_leaf():
    onto = (
        "Type: Widget\n"
        "  - unit_cost: float (literal)\n"
        "  - supplied_by: relationship → Vendor\n"
        "Type: Vendor\n"
    )
    payload = try_deterministic_cypher(
        "widgets supplied by Northwind",
        onto,
        type_names=["Widget", "Vendor"],
    )
    assert payload is not None
    assert payload["params"].get("rel_attr") == "supplied_by"
    assert "Northwind" in str(payload["params"].get("target_name", ""))


def test_equality_routes_relationship_to_related_filter():
    onto = (
        "Type: Widget\n"
        "  - has_status: relationship → Status\n"
        "Type: Status\n"
    )
    payload = try_deterministic_cypher(
        "widgets where has_status is backorder",
        onto,
        type_names=["Widget", "Status"],
    )
    assert payload is not None
    assert payload["template"] == "related_entity_name_filter"
    assert payload["params"]["rel_attr"] == "has_status"
    assert payload["params"]["target_name"] == "backorder"
