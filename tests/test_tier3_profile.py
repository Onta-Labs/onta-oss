"""Tier-3 enumeration + scoped-schema profile bar (ONTA-384).

Regression guard for the BC-universities failure mode that compounded:

  * thin discovery (~5 of ~40 institutions)          → coverage (P1)
  * attribute explosion / invention (~49 attrs)      → scope-adherence (P2)
  * type fragmentation + junk types (~17 types)      → fragmentation (P5)

Asserts:
  1. The committed ``bc_universities`` fixture loads with a valid
     ``enumeration_scope`` and a bundled source seed that exists.
  2. Today's **broken profile** (``BROKEN_BC_PROFILE`` ≈ 5/40, 49 attrs,
     17 types) FAILS coverage, scope-adherence, and fragmentation under the
     post-P1/P2/P5 pass thresholds.
  3. A **clean profile** (full roster, attrs ⊆ requested, types ⊆ allowed)
     PASSES the same thresholds.
  4. Each anti-gaming counter FIRES on a planted bad case and stays clean on
     the good one.

Fully offline / deterministic — pure scorer, no network, no LLM, no store.

Pass-threshold contract (``POST_FIX_PROFILE_THRESHOLDS`` / ``ProfileThresholds``
defaults) — designed so the broken profile fails and a post-fix profile passes:

  coverage_floor                  ≥ 0.50   (broken ≈ 0.125)
  off_roster_ceiling              ≤ 0.30
  near_dup_ceiling                ≤ 0.15
  scope_adherence_floor           ≥ 0.80   (broken ≈ 4/49 ≈ 0.08)
  requested_attr_coverage_floor   ≥ 0.67
  max_out_of_scope_attrs          ≤ 5      (broken ≈ 45)
  max_types                       ≤ 6      (broken = 17)
  max_forbidden_types             ≤ 0      (broken has Colour/Asset/Online/InstructionMode)
  min_allowed_types_present       ≥ 1
"""

from __future__ import annotations

import os

import pytest

from cograph_client.qc.tier3_fixture import (
    FixtureValidationError,
    Tier3Fixture,
    load_fixture,
)
from cograph_client.qc.tier3_grade import (
    BROKEN_BC_PROFILE,
    POST_FIX_PROFILE_THRESHOLDS,
    EnumerationProfileScore,
    GraphProfileSnapshot,
    ProfileThresholds,
    grade_enumeration_profile,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "tier3")
BC_FIXTURE_PATH = os.path.join(FIXTURE_DIR, "bc_universities.json")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _bc_fixture() -> Tier3Fixture:
    return load_fixture(BC_FIXTURE_PATH)


def _clean_profile(fx: Tier3Fixture) -> GraphProfileSnapshot:
    """A post-P1/P2/P5 clean profile that must PASS the bar."""
    scope = fx.enumeration_scope
    assert scope is not None
    return GraphProfileSnapshot(
        entity_keys=scope.expected_entities,
        attributes=scope.requested_attributes + ("label",),
        types=("University", "College"),
    )


def _score(
    profile: GraphProfileSnapshot,
    *,
    fx: Tier3Fixture | None = None,
    thresholds: ProfileThresholds | None = None,
) -> EnumerationProfileScore:
    return grade_enumeration_profile(
        fixture=fx or _bc_fixture(),
        profile=profile,
        thresholds=thresholds or POST_FIX_PROFILE_THRESHOLDS,
    )


# --------------------------------------------------------------------------- #
# Fixture loads + enumeration_scope shape.
# --------------------------------------------------------------------------- #
def test_bc_universities_fixture_loads_with_enumeration_scope():
    fx = _bc_fixture()
    assert fx.id == "tier3-bc-universities"
    assert "British Columbia" in fx.goal
    assert fx.enumeration_scope is not None
    scope = fx.enumeration_scope
    # ~40 expected institutions (the brief's broken profile is ~5/40).
    assert len(scope.expected_entities) == 40
    assert set(scope.requested_attributes) == {"name", "website", "type"}
    assert "University" in scope.allowed_types
    assert "College" in scope.allowed_types
    assert "Colour" in scope.forbidden_types
    assert "InstructionMode" in scope.forbidden_types
    assert fx.source_exists()
    # Bundled CSV has one row per expected entity.
    csv_path = fx.resolve_source_path()
    assert csv_path and os.path.isfile(csv_path)
    with open(csv_path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    # header + 40 rows
    assert len(lines) == 41


def test_enumeration_scope_round_trip():
    fx = _bc_fixture()
    again = Tier3Fixture.from_dict(fx.to_dict())
    assert again.enumeration_scope is not None
    assert again.enumeration_scope.expected_entities == fx.enumeration_scope.expected_entities
    assert again.enumeration_scope.requested_attributes == fx.enumeration_scope.requested_attributes
    assert again.enumeration_scope.forbidden_types == fx.enumeration_scope.forbidden_types


def test_enumeration_scope_rejects_empty_entities():
    d = _bc_fixture().to_dict()
    d["enumeration_scope"]["expected_entities"] = []
    with pytest.raises(FixtureValidationError, match="expected_entities"):
        Tier3Fixture.from_dict(d)


def test_enumeration_scope_rejects_empty_requested_attributes():
    d = _bc_fixture().to_dict()
    d["enumeration_scope"]["requested_attributes"] = []
    with pytest.raises(FixtureValidationError, match="requested_attributes"):
        Tier3Fixture.from_dict(d)


def test_grade_requires_enumeration_scope():
    # A fixture without enumeration_scope cannot be profile-graded.
    bare = Tier3Fixture.from_dict(
        {
            "id": "no-scope",
            "goal": "anything",
            "source_seed": {"kind": "url_list", "urls": ["https://example.org/x"]},
            "questions": [
                {
                    "id": "q1",
                    "question": "q",
                    "tier": "T1",
                    "gold_sparql": "SELECT ?x WHERE { ?x a :X }",
                    "full_expected_items": ["1"],
                    "full_result_count": 1,
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="enumeration_scope"):
        grade_enumeration_profile(
            fixture=bare,
            profile=GraphProfileSnapshot(entity_keys=(), attributes=(), types=()),
        )


# --------------------------------------------------------------------------- #
# Broken profile FAILS (the regression control).
# --------------------------------------------------------------------------- #
def test_broken_bc_profile_shape_matches_documented_numbers():
    """Pin the control snapshot to the brief's ~5/40, ~49 attrs, ~17 types."""
    p = BROKEN_BC_PROFILE
    assert len(p.entity_keys) == 5
    assert len(p.attributes) == 49
    assert len(p.types) == 17
    # Junk types that motivated ONTA-383 must be in the control snapshot.
    for junk in ("Colour", "Asset", "Online", "InstructionMode"):
        assert junk in p.types


def test_broken_bc_profile_fails_coverage_scope_and_fragmentation():
    """The load-bearing acceptance: today's broken numbers fail the bar.

    After P1/P2/P5 land, a clean profile (see test_clean_profile_passes) passes
    the same thresholds — authoring is independent of those fixes.
    """
    score = _score(BROKEN_BC_PROFILE)

    # Headline metrics all fail.
    assert score.coverage == pytest.approx(5 / 40)
    assert score.coverage < POST_FIX_PROFILE_THRESHOLDS.coverage_floor
    assert not score.coverage_ok

    assert score.attribute_total == 49
    assert score.out_of_scope_attributes >= 40
    assert score.scope_adherence < POST_FIX_PROFILE_THRESHOLDS.scope_adherence_floor
    assert not score.scope_adherence_ok
    assert not score.out_of_scope_count_ok

    assert score.type_count == 17
    assert score.type_count > POST_FIX_PROFILE_THRESHOLDS.max_types
    assert not score.type_count_ok
    assert score.forbidden_types_present >= 4
    assert not score.forbidden_types_ok
    for junk in ("colour", "asset", "online", "instructionmode"):
        assert junk in score.forbidden_type_names

    assert not score.passed
    failures = score.failures()
    assert "coverage" in failures
    assert "scope_adherence" in failures or "out_of_scope_count" in failures
    assert "type_count" in failures or "forbidden_types" in failures


# --------------------------------------------------------------------------- #
# Clean profile PASSES (the post-P1/P2/P5 control).
# --------------------------------------------------------------------------- #
def test_clean_profile_passes_all_gates():
    fx = _bc_fixture()
    score = _score(_clean_profile(fx), fx=fx)

    assert score.coverage == pytest.approx(1.0)
    assert score.coverage_ok
    assert score.off_roster_ok
    assert score.near_dup_ok

    assert score.scope_adherence == pytest.approx(1.0)
    assert score.scope_adherence_ok
    assert score.requested_attr_coverage == pytest.approx(1.0)
    assert score.requested_attr_coverage_ok
    assert score.out_of_scope_count_ok
    assert score.out_of_scope_attributes == 0

    assert score.type_count == 2
    assert score.type_count_ok
    assert score.forbidden_types_present == 0
    assert score.forbidden_types_ok
    assert score.allowed_types_present == 2
    assert score.allowed_presence_ok

    assert score.passed
    assert score.failures() == []


def test_alias_table_counts_toward_coverage():
    """'UBC' via the fixture alias_table is the same entity as the full name."""
    fx = _bc_fixture()
    # Produce only the four major research unis, two of them via alias.
    profile = GraphProfileSnapshot(
        entity_keys=(
            "UBC",
            "SFU",
            "University of Victoria",
            "University of Northern British Columbia",
        ),
        attributes=("name", "website", "type"),
        types=("University",),
    )
    score = _score(profile, fx=fx)
    assert score.found_gold_entities == 4
    assert score.coverage == pytest.approx(4 / 40)


# --------------------------------------------------------------------------- #
# Anti-gaming counters — each FIRES on a planted bad case.
# --------------------------------------------------------------------------- #
def test_off_roster_counter_fires_on_noise_padding():
    """Padding with off-roster entities to look 'busy' without covering gold."""
    fx = _bc_fixture()
    # 2 true + 8 noise → off_roster_rate = 8/10 = 0.80 > 0.30 ceiling.
    profile = GraphProfileSnapshot(
        entity_keys=(
            "University of British Columbia",
            "Simon Fraser University",
            "Totally Fake U",
            "Invented College of Nowhere",
            "Phantom Polytechnic",
            "Ghost Institute",
            "Mirage University",
            "Hologram College",
            "Spectral Academy",
            "Ethereal School",
        ),
        attributes=("name", "website", "type"),
        types=("University", "College"),
    )
    score = _score(profile, fx=fx)
    assert score.off_roster_entities == 8
    assert score.off_roster_rate == pytest.approx(0.8)
    assert not score.off_roster_ok
    assert "off_roster" in score.failures()


def test_near_dup_counter_fires_on_restatement_padding():
    """Restating the same entity under key-normalization pads raw counts."""
    fx = _bc_fixture()
    profile = GraphProfileSnapshot(
        entity_keys=(
            "University of British Columbia",
            "university of british columbia",  # near-dup
            "UNIVERSITY OF BRITISH COLUMBIA",  # near-dup
            "Simon Fraser University",
            "simon fraser university",  # near-dup
        ),
        attributes=("name", "website", "type"),
        types=("University",),
    )
    score = _score(profile, fx=fx)
    # 5 rows → 2 distinct → 3 collapsed; rate = 3/5 = 0.60 > 0.15.
    assert score.near_dup_collapsed_rows == 3
    assert score.near_dup_collapse_rate == pytest.approx(0.6)
    assert not score.near_dup_ok
    # Coverage itself is set-based so the two unis still count once each.
    assert score.found_gold_entities == 2


def test_requested_attr_coverage_blocks_empty_field_set_gaming():
    """Emitting only structural attrs (or nothing useful) cannot coast on scope.

    Scope-adherence alone is gameable: emit only ``label`` (structural) →
    scope_adherence = 1.0 with zero of the user's requested fields. The
    requested_attr_coverage counter catches that.
    """
    fx = _bc_fixture()
    profile = GraphProfileSnapshot(
        entity_keys=fx.enumeration_scope.expected_entities,
        attributes=("label",),  # structural only — none of name/website/type
        types=("University", "College"),
    )
    score = _score(profile, fx=fx)
    # label is structural → fully in-scope, but requested coverage is 0.
    assert score.scope_adherence == pytest.approx(1.0)
    assert score.requested_attr_coverage == pytest.approx(0.0)
    assert not score.requested_attr_coverage_ok
    assert "requested_attr_coverage" in score.failures()


def test_out_of_scope_count_ceiling_fires_on_attr_explosion():
    fx = _bc_fixture()
    # 3 requested + 10 junk = 13 attrs; out_of_scope = 10 > max 5.
    junk = tuple(f"fabricated_attr_{i}" for i in range(10))
    profile = GraphProfileSnapshot(
        entity_keys=fx.enumeration_scope.expected_entities,
        attributes=("name", "website", "type") + junk,
        types=("University", "College"),
    )
    score = _score(profile, fx=fx)
    assert score.out_of_scope_attributes == 10
    assert not score.out_of_scope_count_ok
    assert "out_of_scope_count" in score.failures()


def test_forbidden_types_counter_fires_on_junk_types():
    fx = _bc_fixture()
    profile = GraphProfileSnapshot(
        entity_keys=fx.enumeration_scope.expected_entities,
        attributes=("name", "website", "type"),
        types=("University", "College", "Colour", "InstructionMode"),
    )
    score = _score(profile, fx=fx)
    assert score.forbidden_types_present == 2
    assert not score.forbidden_types_ok
    assert "forbidden_types" in score.failures()


def test_allowed_type_presence_blocks_empty_type_set_gaming():
    """type_count=0 would pass max_types; the presence floor blocks that."""
    fx = _bc_fixture()
    profile = GraphProfileSnapshot(
        entity_keys=fx.enumeration_scope.expected_entities,
        attributes=("name", "website", "type"),
        types=(),  # no types at all
    )
    score = _score(profile, fx=fx)
    assert score.type_count == 0
    assert score.type_count_ok  # 0 ≤ 6
    assert not score.allowed_presence_ok
    assert "allowed_type_presence" in score.failures()


def test_type_count_ceiling_fires_on_fragmentation():
    fx = _bc_fixture()
    # 8 types, all "allowed-looking" but over the ceiling of 6. Only University
    # and College are in the allow-list; the rest are extras (not forbidden).
    profile = GraphProfileSnapshot(
        entity_keys=fx.enumeration_scope.expected_entities,
        attributes=("name", "website", "type"),
        types=(
            "University",
            "College",
            "PublicInstitution",
            "PrivateInstitution",
            "Polytechnic",
            "Institute",
            "CommunityCollege",
            "ArtSchool",
        ),
    )
    score = _score(profile, fx=fx)
    assert score.type_count == 8
    assert not score.type_count_ok
    assert "type_count" in score.failures()
    # Allowed presence still holds (University + College present).
    assert score.allowed_presence_ok


# --------------------------------------------------------------------------- #
# Threshold documentation pin — keep the contract honest.
# --------------------------------------------------------------------------- #
def test_post_fix_thresholds_match_documented_contract():
    th = POST_FIX_PROFILE_THRESHOLDS
    assert th.coverage_floor == pytest.approx(0.50)
    assert th.off_roster_ceiling == pytest.approx(0.30)
    assert th.near_dup_ceiling == pytest.approx(0.15)
    assert th.scope_adherence_floor == pytest.approx(0.80)
    assert th.requested_attr_coverage_floor == pytest.approx(0.67)
    assert th.max_out_of_scope_attrs == 5
    assert th.max_types == 6
    assert th.max_forbidden_types == 0
    assert th.min_allowed_types_present == 1


def test_profile_thresholds_from_dict_partial():
    th = ProfileThresholds.from_dict({"coverage_floor": 0.9, "max_types": 3})
    assert th.coverage_floor == pytest.approx(0.9)
    assert th.max_types == 3
    # Unspecified fields keep defaults.
    assert th.scope_adherence_floor == pytest.approx(0.80)
