"""Hermetic token-hygiene tests (English-general, no eval-set strings)."""

from __future__ import annotations

from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.query_intent import (
    collapse_filter_tokens,
    extract_filter_tokens,
    sketch_query_intent,
)

# Same unfiltered COUNT shape as test_query_constraint_coverage.py
_UNFILTERED_COUNT = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
RETURN count(e) AS n
""".strip()

_FILTERED_COMPARE = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND toFloat(e.start_year) >= $threshold
RETURN count(e) AS n
""".strip()

_DISTINCT_COUNT = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
RETURN count(DISTINCT e.name) AS n
""".strip()

_STATUS_ONLY_COUNT = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.status_label = $prop_value
RETURN count(e) AS n
""".strip()

_PHASE_ONLY_AVG = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.phase = $prop_value
RETURN avg(toFloat(e.price)) AS value
""".strip()

_PHASE_AND_TARGET_AVG = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.phase = $prop_value AND e.target = $target
RETURN avg(toFloat(e.price)) AS value
""".strip()


def _lows(question: str) -> set[str]:
    return {t.lower() for t in extract_filter_tokens(question)}


def test_involved_in_type_is_not_a_filter():
    """Join verb + in <NP> is not two dim values; type-scan COUNT still fails."""
    q = "How many unique authors are involved in books?"
    sk = sketch_query_intent(q)
    lows = {t.lower() for t in sk.filter_tokens}
    assert "involved" not in lows
    assert "books" not in lows
    assert sk.has_unique_count_intent
    assert sk.has_filter_intent
    r = check_constraint_coverage(
        q, _UNFILTERED_COUNT, params={"type_names": ["Book"]}
    )
    assert not r.ok
    assert r.fail_closed
    r2 = check_constraint_coverage(
        q, _DISTINCT_COUNT, params={"type_names": ["Author"]}
    )
    assert r2.ok
    assert not r2.fail_closed


def test_in_type_phrase_dropped_for_in_only():
    """Bare inventory trailer after ``in`` is type-scope, not a dim."""
    assert "products" not in _lows("count titles in products")
    assert "items" not in _lows("how many rows in items")
    lows = _lows("sum unit_qty in West")
    assert any(t == "west" for t in lows)


def test_in_code_and_status_type_kept():
    assert any(t.lower() == "docka" for t in extract_filter_tokens("sum qty in DockA"))
    lows = _lows("sum assay_cost in ready tests")
    assert "ready" in lows


def test_copula_status_without_prep_still_extracts():
    lows = _lows("how many gadgets are completed")
    assert "completed" in lows


def test_status_participle_plus_place_keeps_both():
    """``are completed in West`` is status + place, not a join verb."""
    lows = _lows("how many gadgets are completed in West")
    assert "completed" in lows
    assert "west" in lows


def test_nonstatus_verb_still_keeps_capital_place():
    """``manufactured in West`` is a place dim, not type chatter."""
    lows = _lows("how many gadgets are manufactured in West")
    assert "west" in lows
    assert "involved" not in lows


def test_bare_in_multiword_type_is_not_dropped():
    """Without a join participle, ``in oncology trials`` stays a token."""
    lows = _lows("sum unit_qty in oncology trials")
    assert any("oncology" in t for t in lows)


def test_started_in_or_after_year_is_one_constraint():
    q = "How many books started in or after 2014?"
    raw = extract_filter_tokens(q)
    collapsed = collapse_filter_tokens(raw)
    blob = " ".join(t.lower() for t in collapsed)
    assert "or after 2014" not in blob
    # At most one year-ish token after collapse (year or a single compare).
    yearish = [t for t in collapsed if "2014" in t]
    assert yearish == ["2014"]
    sk = sketch_query_intent(q)
    assert sk.has_filter_intent
    # Unfiltered COUNT still fail-closes (year is a real constraint).
    r = check_constraint_coverage(q, _UNFILTERED_COUNT, params={"type_names": ["Book"]})
    assert not r.ok
    assert r.fail_closed
    # A plan that actually compares the year passes.
    r2 = check_constraint_coverage(
        q,
        _FILTERED_COMPARE,
        params={"type_names": ["Book"], "threshold": 2014, "op": "ge"},
    )
    assert r2.ok
    assert not r2.fail_closed


def test_phase_n_type_trailer_collapses():
    toks = collapse_filter_tokens(["Phase 3 tests", "Phase 3"])
    assert toks == ["Phase 3"]
    sk = sketch_query_intent("What is the average price of Phase 3 gadgets?")
    lows = {t.lower() for t in sk.filter_tokens}
    assert "phase 3" in lows


def test_north_and_quoted_status_still_extract():
    toks = extract_filter_tokens("sum unit_qty for North")
    assert any(t.lower() == "north" for t in toks)
    toks2 = extract_filter_tokens('count gadgets with status_label "open" in West')
    lows = {t.lower() for t in toks2}
    assert "open" in lows
    assert "west" in lows


def test_collapse_ready_tests_unchanged():
    assert collapse_filter_tokens(["ready tests", "ready"]) == ["ready"]


def test_collapse_does_not_merge_unrelated_dims():
    toks = collapse_filter_tokens(["ready", "DockA"])
    assert set(t.lower() for t in toks) == {"ready", "docka"}


def test_collapse_does_not_use_character_subset():
    """``active`` must not vanish inside ``inactive``."""
    toks = collapse_filter_tokens(["active", "inactive"])
    lows = {t.lower() for t in toks}
    assert "active" in lows
    assert "inactive" in lows


def test_word_sequence_collapse_keeps_shorter():
    toks = collapse_filter_tokens(["or after 2014", "after 2014"])
    assert toks == ["after 2014"]


def test_year_and_status_stay_two_constraints():
    """A year compare next to a status is multi-filter, not a single year."""
    q = "total cost of ready gadgets after 2014"
    collapsed = collapse_filter_tokens(extract_filter_tokens(q))
    lows = {t.lower() for t in collapsed}
    assert "ready" in lows
    assert "2014" in lows
    assert len(lows) >= 2
    r = check_constraint_coverage(
        q, _UNFILTERED_COUNT, params={"type_names": ["Gadget"]}
    )
    assert not r.ok
    assert r.fail_closed


def test_allcaps_acronym_is_a_second_constraint():
    """Phase-N + all-caps target: both tokens survive; phase-only plan fails.

    General acronym shape. A Phase-only aggregate would otherwise look
    covered after word-sequence collapse of the Phase-N phrase.
    """
    q = "What is the average price of Phase 3 gadgets targeting QEDX?"
    collapsed = collapse_filter_tokens(extract_filter_tokens(q))
    lows = {t.lower() for t in collapsed}
    assert "phase 3" in lows
    assert "qedx" in lows
    r = check_constraint_coverage(
        q,
        _PHASE_ONLY_AVG,
        params={"type_names": ["Gadget"], "prop_value": "Phase 3"},
    )
    assert not r.ok
    assert r.fail_closed
    r2 = check_constraint_coverage(
        q,
        _PHASE_AND_TARGET_AVG,
        params={
            "type_names": ["Gadget"],
            "prop_value": "Phase 3",
            "target": "QEDX",
        },
    )
    assert r2.ok
    assert not r2.fail_closed


def test_targeting_lowercase_is_a_constraint():
    q = "What is the average price of Phase 3 gadgets targeting qedx?"
    lows = {t.lower() for t in collapse_filter_tokens(extract_filter_tokens(q))}
    assert "phase 3" in lows
    assert "qedx" in lows


def test_year_wrong_leaf_plan_fail_closes():
    """A status filter must not cover an unbound year on a count."""
    q = "How many gadgets started in or after 2014?"
    r = check_constraint_coverage(
        q,
        _STATUS_ONLY_COUNT,
        params={"type_names": ["Gadget"], "prop_value": "ready"},
    )
    assert not r.ok
    assert r.fail_closed
