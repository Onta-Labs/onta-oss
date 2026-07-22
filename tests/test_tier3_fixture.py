"""Tier-3 QC fixture schema + loader tests (ONTA-283-A).

Covers: the committed fixtures under ``tests/fixtures/tier3/*.json`` load and pass
their shape + conservation checks and their bundled source seeds exist; and the
``from_dict`` validators reject every class of malformed fixture LOUDLY (a typo in a
committed gold file must fail CI, never silently mis-grade).

Fully offline / deterministic — pure schema, no network, no store.
"""

from __future__ import annotations

import copy
import glob
import json
import os

import pytest

from cograph_client.qc.tier3_fixture import (
    TIER_LABELS,
    FixtureValidationError,
    SourceSeed,
    Tier3Fixture,
    Tier3GoldQuestion,
    load_fixture,
    load_fixtures,
    tier_index,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "tier3")
FIXTURE_PATHS = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json")))
# The seed corpus ships at least 2-3 fixtures so the schema is exercised for real.
assert len(FIXTURE_PATHS) >= 2, "Tier-3 fixture corpus needs at least 2 fixtures"


def _valid_fixture_dict() -> dict:
    """A minimal, structurally valid fixture dict used as a base for mutation."""
    return {
        "id": "unit-fixture",
        "goal": "A raw natural-language goal for the whole-product run.",
        "source_seed": {"kind": "url_list", "urls": ["https://seed.example/data"]},
        "questions": [
            {
                "id": "q-1",
                "question": "How many widgets are there?",
                "tier": "T1",
                "gold_sparql": "SELECT (COUNT(?w) AS ?c) WHERE { ?w a :Widget }",
                "full_expected_items": ["3"],
                "full_result_count": 1,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# The committed corpus loads + validates.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture_path", FIXTURE_PATHS, ids=[os.path.basename(p) for p in FIXTURE_PATHS]
)
def test_committed_fixture_loads_and_conserves(fixture_path):
    fx = load_fixture(fixture_path)
    assert fx.id and fx.goal
    assert len(fx.questions) >= 1
    for q in fx.questions:
        assert q.tier in TIER_LABELS
        assert q.gold_sparql
        # Conservation is enforced at load; re-assert the invariant here.
        assert q.full_result_count == len(q.full_expected_items)
    # A bundled_file seed's source must actually be present on disk.
    assert fx.source_exists(), f"bundled source missing for {fixture_path}"


def test_load_fixtures_directory_skips_sources_subdir():
    fixtures = load_fixtures(FIXTURE_DIR)
    assert len(fixtures) == len(FIXTURE_PATHS)
    ids = {f.id for f in fixtures}
    assert len(ids) == len(fixtures), "fixture ids must be unique across the corpus"


def test_bundled_source_paths_resolve_under_fixture_dir():
    for path in FIXTURE_PATHS:
        fx = load_fixture(path)
        if fx.source_seed.kind == "bundled_file":
            resolved = fx.resolve_source_path()
            assert resolved is not None and os.path.isfile(resolved)


# --------------------------------------------------------------------------- #
# tier_index + gold_is_empty helpers.
# --------------------------------------------------------------------------- #
def test_tier_index_maps_labels_to_eval_integers():
    assert tier_index("T1") == 1
    assert tier_index("t3") == 3
    assert tier_index("T4") == 4
    with pytest.raises(FixtureValidationError):
        tier_index("T9")


def test_gold_is_empty_flag():
    q = Tier3GoldQuestion.from_dict(
        {
            "id": "q-empty",
            "question": "Which widgets are flagged?",
            "tier": "T2",
            "gold_sparql": "SELECT ?w WHERE { ?w :flagged true }",
            "full_expected_items": [],
            "full_result_count": 0,
        }
    )
    assert q.gold_is_empty
    assert not Tier3GoldQuestion.from_dict(_valid_fixture_dict()["questions"][0]).gold_is_empty


def test_round_trip_to_dict_reparses():
    fx = Tier3Fixture.from_dict(_valid_fixture_dict())
    again = Tier3Fixture.from_dict(fx.to_dict())
    assert again.id == fx.id
    assert again.question_ids == fx.question_ids


# --------------------------------------------------------------------------- #
# Validation rejects malformed fixtures LOUDLY.
# --------------------------------------------------------------------------- #
def test_conservation_mismatch_is_rejected():
    d = _valid_fixture_dict()
    d["questions"][0]["full_result_count"] = 2  # but only 1 expected item
    with pytest.raises(FixtureValidationError, match="conservation failed"):
        Tier3Fixture.from_dict(d)


def test_missing_goal_is_rejected():
    d = _valid_fixture_dict()
    d["goal"] = "   "
    with pytest.raises(FixtureValidationError, match="goal"):
        Tier3Fixture.from_dict(d)


def test_bad_tier_is_rejected():
    d = _valid_fixture_dict()
    d["questions"][0]["tier"] = "T5"
    with pytest.raises(FixtureValidationError, match="tier"):
        Tier3Fixture.from_dict(d)


def test_missing_gold_sparql_is_rejected():
    d = _valid_fixture_dict()
    d["questions"][0]["gold_sparql"] = ""
    with pytest.raises(FixtureValidationError, match="gold_sparql"):
        Tier3Fixture.from_dict(d)


def test_non_integer_result_count_is_rejected():
    d = _valid_fixture_dict()
    d["questions"][0]["full_result_count"] = "three"
    with pytest.raises(FixtureValidationError, match="full_result_count"):
        Tier3Fixture.from_dict(d)


def test_duplicate_question_id_is_rejected():
    d = _valid_fixture_dict()
    dup = copy.deepcopy(d["questions"][0])
    d["questions"].append(dup)
    with pytest.raises(FixtureValidationError, match="duplicate question id"):
        Tier3Fixture.from_dict(d)


def test_empty_questions_list_is_rejected():
    d = _valid_fixture_dict()
    d["questions"] = []
    with pytest.raises(FixtureValidationError, match="at least one question"):
        Tier3Fixture.from_dict(d)


def test_bad_source_seed_kind_is_rejected():
    d = _valid_fixture_dict()
    d["source_seed"] = {"kind": "carrier_pigeon"}
    with pytest.raises(FixtureValidationError, match="source_seed.kind"):
        Tier3Fixture.from_dict(d)


def test_bundled_file_seed_requires_path():
    d = _valid_fixture_dict()
    d["source_seed"] = {"kind": "bundled_file"}
    with pytest.raises(FixtureValidationError, match="requires a non-empty 'path'"):
        Tier3Fixture.from_dict(d)


def test_url_list_seed_requires_a_url():
    d = _valid_fixture_dict()
    d["source_seed"] = {"kind": "url_list", "urls": []}
    with pytest.raises(FixtureValidationError, match="at least one non-empty URL"):
        Tier3Fixture.from_dict(d)


def test_missing_source_seed_is_rejected():
    d = _valid_fixture_dict()
    del d["source_seed"]
    with pytest.raises(FixtureValidationError, match="source_seed"):
        Tier3Fixture.from_dict(d)


def test_invalid_json_file_fails_loud(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(FixtureValidationError, match="invalid JSON"):
        load_fixture(str(bad))


def test_source_seed_resolves_absolute_path_unchanged():
    seed = SourceSeed.from_dict({"kind": "bundled_file", "path": "/abs/data.csv"})
    assert seed.resolve_path("/anything") == "/abs/data.csv"


def test_committed_json_is_wellformed():
    """Every committed fixture is syntactically valid JSON (guards against a hand-edit
    landing a trailing comma / dangling brace)."""
    for path in FIXTURE_PATHS:
        with open(path, "r", encoding="utf-8") as fh:
            json.load(fh)
