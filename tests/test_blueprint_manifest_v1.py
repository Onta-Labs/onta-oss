"""INF-563 / INF-587 — v1-frozen Blueprint manifest.

Done-when (INF-563): fixtures and round-trip, unknown-key rejection, each
excluded category unrepresentable, version marked v1-frozen.

Done-when (INF-587): a sample that exceeds the cap, lacks a timestamp, or
is not marked as sample fails. Sample-derived rows cannot present as
current (surface contract; Explorer/public-page UI is stubbed).
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from infona_client.blueprints import (
    SCHEMA_VERSION,
    UNREPRESENTABLE_FIELD_NAMES,
    BlueprintValidationError,
    VersionBump,
    classify_change,
    dump_manifest,
    feeds_freshness_panel,
    frozen_json_schema,
    surface_label,
    validate_manifest,
    validate_sample,
)
from infona_client.blueprints.schema import (
    ALLOWED_TOP_LEVEL,
    MAX_SAMPLE_BYTES,
    MAX_SAMPLE_ENTITIES,
    iter_schema_property_names,
)

_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "infona_client"
    / "blueprints"
    / "fixtures"
)
FIXTURE = _FIXTURES / "clinical_trials_v1.json"
PACKAGE_DIR = _FIXTURES / "clinical-trials"
SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "infona_client"
    / "blueprints"
    / "v1_frozen.schema.json"
)


def _load_raw() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_schema_version_is_v1_frozen():
    assert SCHEMA_VERSION == "v1-frozen"
    manifest = validate_manifest(FIXTURE)
    assert manifest.schema_version == "v1-frozen"


def test_fixture_validates():
    manifest = validate_manifest(FIXTURE)
    assert manifest.id == "clinical-trials"
    assert manifest.sample is not None
    assert manifest.sample.kind == "sample"
    assert manifest.sample.captured_at == date(2026, 6, 1)
    assert all(e.is_sample for e in manifest.sample.entities)


def test_round_trip_dict():
    first = validate_manifest(FIXTURE)
    dumped = dump_manifest(first)
    second = validate_manifest(dumped)
    assert dump_manifest(second) == dumped


def test_round_trip_json_string():
    raw = FIXTURE.read_text(encoding="utf-8")
    first = validate_manifest(raw)
    dumped = json.dumps(dump_manifest(first))
    second = validate_manifest(dumped)
    assert dump_manifest(second) == dump_manifest(first)


def test_round_trip_path():
    assert dump_manifest(validate_manifest(FIXTURE))["id"] == "clinical-trials"


def test_unknown_top_level_key_rejected():
    raw = _load_raw()
    raw["not_a_section"] = {"hello": True}
    with pytest.raises(BlueprintValidationError, match="unknown top-level key"):
        validate_manifest(raw)


def test_latest_is_not_a_version():
    raw = _load_raw()
    raw["version"] = "latest"
    with pytest.raises(BlueprintValidationError):
        validate_manifest(raw)


def test_wrong_schema_version_rejected():
    raw = _load_raw()
    raw["schema_version"] = "v1"
    with pytest.raises(BlueprintValidationError):
        validate_manifest(raw)


@pytest.mark.parametrize("key", sorted(UNREPRESENTABLE_FIELD_NAMES))
def test_excluded_category_rejected_as_top_level(key: str):
    raw = _load_raw()
    raw[key] = {"value": "should not be representable"}
    with pytest.raises(BlueprintValidationError, match="unrepresentable"):
        validate_manifest(raw)


def test_excluded_categories_have_no_schema_field():
    """INF-563: no field at all — not optional, not nullable."""

    names = iter_schema_property_names()
    leaked = UNREPRESENTABLE_FIELD_NAMES & names
    assert leaked == set(), f"excluded categories leaked into schema: {leaked}"
    # The one legal instance-shaped section is `sample`, not `records`.
    assert "sample" in names
    assert "records" not in names
    assert "credentials" not in names
    assert "freshness_status" not in names


def test_allowed_top_level_matches_schema():
    schema = frozen_json_schema()
    props = set(schema["properties"])
    assert props == ALLOWED_TOP_LEVEL
    assert schema.get("additionalProperties") is False


def test_frozen_schema_file_matches_model():
    assert SCHEMA_FILE.is_file(), "commit the generated v1_frozen.schema.json"
    on_disk = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert on_disk == frozen_json_schema()
    assert on_disk.get("additionalProperties") is False


def test_type_ranged_attribute_requires_relationship():
    raw = _load_raw()
    raw["relationships"] = [
        rel for rel in raw["relationships"] if rel["name"] != "lead_sponsor"
    ]
    with pytest.raises(BlueprintValidationError, match="type_ranged"):
        validate_manifest(raw)


def test_literal_attribute_cannot_be_a_relationship():
    raw = _load_raw()
    raw["relationships"].append(
        {
            "name": "nct_id",
            "source": "ClinicalTrial",
            "target": "Organization",
            "cardinality": "1",
            "kind": "type_ranged",
        }
    )
    with pytest.raises(BlueprintValidationError, match="literal"):
        validate_manifest(raw)


def test_byok_requires_key_env_name():
    raw = _load_raw()
    raw["sources"][1]["definition"]["credential"] = "byok"
    raw["sources"][1]["definition"]["key_env"] = None
    with pytest.raises(BlueprintValidationError, match="key_env"):
        validate_manifest(raw)


def test_key_env_rejects_secret_shaped_value():
    raw = _load_raw()
    raw["sources"][1]["definition"]["credential"] = "byok"
    raw["sources"][1]["definition"]["key_env"] = "sk-this-is-a-value-not-a-name"
    with pytest.raises(BlueprintValidationError, match="UPPER_SNAKE"):
        validate_manifest(raw)


def test_byok_never_holds_a_secret_field():
    names = iter_schema_property_names()
    assert "api_key" not in names
    assert "secret" not in names
    assert "credential_value" not in names
    assert "key_env" in names


def test_sample_independently_validates_and_is_droppable():
    raw = _load_raw()
    sample = validate_sample(raw["sample"])
    assert sample.kind == "sample"
    again = validate_sample(sample)
    assert again.captured_at == sample.captured_at
    raw_without = copy.deepcopy(raw)
    del raw_without["sample"]
    manifest = validate_manifest(raw_without)
    assert manifest.sample is None


def test_sample_exceeds_entity_cap_fails():
    raw = _load_raw()
    template = raw["sample"]["entities"][0]
    raw["sample"]["entities"] = [
        {**template, "id": f"SAMPLE-{i:03d}", "attributes": {**template["attributes"], "nct_id": f"SAMPLE-{i:03d}"}}
        for i in range(MAX_SAMPLE_ENTITIES + 1)
    ]
    with pytest.raises(BlueprintValidationError, match="hard cap|max_length|at most"):
        validate_manifest(raw)
    with pytest.raises(BlueprintValidationError):
        validate_sample(raw["sample"])


def test_sample_exceeds_byte_cap_fails():
    raw = _load_raw()
    # Keep entity count under 25; blow the 64 KiB serialized cap instead.
    pad = "x" * (MAX_SAMPLE_BYTES // 3)
    raw["sample"]["entities"] = [
        {
            "type": "ClinicalTrial",
            "id": f"SAMPLE-{i}",
            "is_sample": True,
            "attributes": {"nct_id": f"SAMPLE-{i}", "official_title": pad},
        }
        for i in range(3)
    ]
    with pytest.raises(BlueprintValidationError, match="64 KiB|bytes"):
        validate_sample(raw["sample"])


def test_sample_missing_timestamp_fails():
    raw = _load_raw()
    del raw["sample"]["captured_at"]
    with pytest.raises(BlueprintValidationError, match="captured_at"):
        validate_manifest(raw)
    with pytest.raises(BlueprintValidationError, match="captured_at"):
        validate_sample(raw["sample"])


def test_sample_not_marked_as_sample_fails():
    raw = _load_raw()
    raw["sample"]["kind"] = "current"
    with pytest.raises(BlueprintValidationError):
        validate_manifest(raw)

    raw = _load_raw()
    raw["sample"]["entities"][0]["is_sample"] = False
    with pytest.raises(BlueprintValidationError):
        validate_manifest(raw)


def test_sample_cannot_present_as_current():
    """INF-587: never presented as current, on any surface.

    Explorer / public-page marking is stubbed: those surfaces do not exist
    yet. The contract they must call is ``surface_label`` +
    ``feeds_freshness_panel``.
    """

    manifest = validate_manifest(FIXTURE)
    assert manifest.sample is not None
    label = surface_label(manifest.sample.captured_at)
    assert label.startswith("sample")
    assert "2026-06-01" in label
    assert feeds_freshness_panel() is False
    schema_names = iter_schema_property_names()
    assert "is_current" not in schema_names
    assert "freshness_status" not in schema_names


def test_examples_and_evals_require_workspace_only_leak_policy():
    raw = _load_raw()
    raw["examples"]["leak_policy"] = "global_bank"
    with pytest.raises(BlueprintValidationError, match="leak_policy"):
        validate_manifest(raw)


def test_classify_remove_concept_is_major():
    old = validate_manifest(FIXTURE)
    raw = _load_raw()
    raw["concepts"] = [c for c in raw["concepts"] if c["name"] != "Facility"]
    raw["relationships"] = [
        r for r in raw["relationships"] if r["target"] != "Facility" and r["source"] != "Facility"
    ]
    raw["validation"]["entity_resolution"] = [
        e for e in raw["validation"]["entity_resolution"] if e["type"] != "Facility"
    ]
    # Drop the type-ranged attribute that pointed at Facility.
    for concept in raw["concepts"]:
        concept["attributes"] = [
            a for a in concept["attributes"] if a.get("range") != "Facility"
        ]
    new = validate_manifest(raw)
    report = classify_change(old, new)
    assert report.version_bump is VersionBump.major
    assert any("removed concept" in r for r in report.reasons)


def test_classify_add_optional_attribute_is_minor():
    old = validate_manifest(FIXTURE)
    raw = _load_raw()
    for concept in raw["concepts"]:
        if concept["name"] == "Organization":
            concept["attributes"].append(
                {
                    "name": "city",
                    "kind": "literal",
                    "range": "string",
                    "required": False,
                }
            )
    new = validate_manifest(raw)
    report = classify_change(old, new)
    assert report.version_bump is VersionBump.minor
    assert report.acquisition_revision_bump is False


def test_classify_replaced_enum_value_is_major():
    old = validate_manifest(FIXTURE)
    raw = _load_raw()
    for concept in raw["concepts"]:
        if concept["name"] == "Organization":
            for attr in concept["attributes"]:
                if attr["name"] == "org_class":
                    attr["allowed_values"] = ["industry", "nih", "other", "academic"]
    new = validate_manifest(raw)
    report = classify_change(old, new)
    assert report.version_bump is VersionBump.major
    assert any("narrowed" in r for r in report.reasons)


def test_stale_after_days_and_never_are_exclusive():
    raw = _load_raw()
    raw["freshness"]["stale_after"][0]["never"] = True
    with pytest.raises(BlueprintValidationError, match="never"):
        validate_manifest(raw)


def test_classify_narrow_range_is_major():
    old = validate_manifest(FIXTURE)
    raw = _load_raw()
    for concept in raw["concepts"]:
        if concept["name"] == "Organization":
            for attr in concept["attributes"]:
                if attr["name"] == "org_class":
                    attr["allowed_values"] = ["industry"]
    new = validate_manifest(raw)
    report = classify_change(old, new)
    assert report.version_bump is VersionBump.major
    assert any("narrowed" in r for r in report.reasons)


def test_classify_acquisition_change_is_revision_not_silent_minor():
    old = validate_manifest(FIXTURE)
    raw = _load_raw()
    raw["acquisition"]["steps"][0]["instruction"] = "query.cond=diabetes only"
    new = validate_manifest(raw)
    report = classify_change(old, new)
    assert report.acquisition_revision_bump is True
    assert report.version_bump is VersionBump.patch
    assert any("acquisition" in r for r in report.reasons)


def test_module_cli_accepts_fixture(capsys):
    from infona_client.blueprints.__main__ import main

    assert main([str(PACKAGE_DIR)]) == 0
    out = capsys.readouterr().out
    assert '"schema_version": "v1-frozen"' in out


def test_directory_package_validates():
    """ADR 0014: canonical package is a directory with blueprint.yaml."""

    manifest = validate_manifest(PACKAGE_DIR)
    assert manifest.id == "clinical-trials"
    assert manifest.sample is not None
    assert manifest.sample.kind == "sample"


def test_sample_directory_is_independently_droppable(tmp_path):
    import shutil

    copied = tmp_path / "pkg"
    shutil.copytree(PACKAGE_DIR, copied)
    validate_sample(copied / "sample")
    shutil.rmtree(copied / "sample")
    assert validate_manifest(copied).sample is None


def test_package_rejects_yaml_and_json_together(tmp_path):
    import shutil

    copied = tmp_path / "pkg"
    shutil.copytree(PACKAGE_DIR, copied)
    (copied / "blueprint.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BlueprintValidationError, match="must not ship both"):
        validate_manifest(copied)


def test_package_rejects_author_supplied_code(tmp_path):
    import shutil

    copied = tmp_path / "pkg"
    shutil.copytree(PACKAGE_DIR, copied)
    (copied / "hook.py").write_text("print('no')\n", encoding="utf-8")
    with pytest.raises(BlueprintValidationError, match="author-supplied code"):
        validate_manifest(copied)


def test_archive_is_not_the_package(tmp_path):
    blob = tmp_path / "blueprint.tar.gz"
    blob.write_bytes(b"not a real archive")
    with pytest.raises(BlueprintValidationError, match="transport"):
        validate_manifest(blob)
