"""Type-wide enrich plan copy: all N vs the 200-record cap."""

from infona_client.agent.capabilities.enrich_common import _DEFAULT_PLAN_LIMIT
from infona_client.agent.capabilities.enrich_plan import unscoped_target_phrase


def test_type_wide_exact_under_cap_says_all_n():
    phrase = unscoped_target_phrase(
        "ClinicalTrial",
        matched=25,
        matched_exact=True,
        scope=None,
        entity_uris=None,
        limit=_DEFAULT_PLAN_LIMIT,
        subset_desc=None,
    )
    assert phrase == "all 25 ClinicalTrial records"


def test_type_wide_exact_over_cap_does_not_say_all():
    phrase = unscoped_target_phrase(
        "ClinicalTrial",
        matched=5000,
        matched_exact=True,
        scope=None,
        entity_uris=None,
        limit=_DEFAULT_PLAN_LIMIT,
        subset_desc=None,
    )
    assert "all 5000" not in phrase
    assert "capped at 200" in phrase


def test_subset_uris_stay_selected_not_all():
    phrase = unscoped_target_phrase(
        "ClinicalTrial",
        matched=25,
        matched_exact=True,
        scope=None,
        entity_uris=["u1", "u2", "u3", "u4", "u5"],
        limit=None,
        subset_desc="IMvigor",
    )
    assert phrase == "the 5 ClinicalTrial entities matching “IMvigor”"
    assert "all " not in phrase


def test_predicate_scope_does_not_say_all_records():
    phrase = unscoped_target_phrase(
        "ClinicalTrial",
        matched=3,
        matched_exact=True,
        scope={"predicate": "status", "value": "Recruiting"},
        entity_uris=None,
        limit=_DEFAULT_PLAN_LIMIT,
        subset_desc=None,
    )
    assert "all 3" not in phrase
    assert "capped at 200" in phrase

