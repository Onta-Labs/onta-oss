"""INF-563 — frozen Blueprint manifest schema v1.

The schema is the protocol. This file pins:

* fixtures round-trip
* unknown top-level keys rejected
* each INF-564 excluded category is unrepresentable
* sample bounds (INF-587)
* semver vs ``acquisition_revision`` (INF-560 C4)
* ``schema_status`` is ``v1-frozen``
* range classification reuses ``classify_attr_range`` (no second reader)
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from infona_client.blueprint import (
    ALLOWED_TOP_LEVEL_KEYS,
    FORBIDDEN_TOP_LEVEL_KEYS,
    SAMPLE_MAX_BYTES,
    SAMPLE_MAX_ENTITIES,
    SCHEMA_STATUS,
    SCHEMA_VERSION,
    BlueprintManifest,
    classify_manifest_change,
    dumps_blueprint,
    parse_blueprint,
    validate_blueprint,
)
from infona_client.blueprint.semver import (
    ACQUISITION_REVISION_CHANGES,
    SEMVER_MAJOR,
    SEMVER_MINOR,
    SEMVER_PATCH,
)
from infona_client.graph.ontology_catalog_models import classify_attr_range
from infona_client.graph.predicates import ATTR_META_SUFFIXES

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "infona_client" / "blueprint" / "data"
MIN_FIXTURE = FIXTURE_DIR / "clinical_trials_min.json"
SAMPLE_FIXTURE = FIXTURE_DIR / "clinical_trials_with_sample.json"

# Workspace-only categories INF-563 / INF-564 must make unrepresentable.
EXCLUDED_CATEGORY_KEYS: dict[str, str] = {
    "records": "actual records outside sample",
    "entities": "actual records outside sample",
    "credentials": "credentials",
    "api_key": "credentials",
    "jobs": "scheduled jobs",
    "schedules": "scheduled jobs",
    "cron": "scheduled jobs",
    "last_run": "scheduled jobs",
    "citations": "citations/provenance",
    "provenance": "citations/provenance",
    "freshness_status": "freshness status",
    "last_refresh": "freshness status",
    "source_health": "freshness status",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture
def min_doc() -> dict:
    return _load(MIN_FIXTURE)


@pytest.fixture
def sample_doc() -> dict:
    return _load(SAMPLE_FIXTURE)


# ---------------------------------------------------------------------------
# Freeze markers
# ---------------------------------------------------------------------------
def test_schema_is_marked_v1_frozen():
    assert SCHEMA_VERSION == "1"
    assert SCHEMA_STATUS == "v1-frozen"
    schema = BlueprintManifest.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) <= ALLOWED_TOP_LEVEL_KEYS
    assert FORBIDDEN_TOP_LEVEL_KEYS.isdisjoint(schema["properties"])


# ---------------------------------------------------------------------------
# Fixtures + round-trip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [MIN_FIXTURE, SAMPLE_FIXTURE], ids=["min", "sample"])
def test_fixture_validates_and_round_trips(path: Path):
    raw = path.read_text()
    assert validate_blueprint(raw) == []
    first = parse_blueprint(raw)
    assert first.schema_version == "1"
    assert first.schema_status == "v1-frozen"
    dumped = dumps_blueprint(first)
    second = parse_blueprint(dumped)
    assert first == second
    assert validate_blueprint(dumped) == []


def test_min_fixture_has_no_sample(min_doc):
    assert "sample" not in min_doc
    manifest = parse_blueprint(min_doc)
    assert manifest.sample is None


def test_sample_fixture_is_separated_and_timestamped(sample_doc):
    manifest = parse_blueprint(sample_doc)
    assert manifest.sample is not None
    assert manifest.sample.kind == "synthetic"
    assert str(manifest.sample.captured_at) == "2026-06-01"
    assert len(manifest.sample.entities) <= SAMPLE_MAX_ENTITIES


# ---------------------------------------------------------------------------
# Unknown keys
# ---------------------------------------------------------------------------
def test_unknown_top_level_key_rejected(min_doc):
    min_doc["author_notes"] = "should not be silently kept"
    errors = validate_blueprint(min_doc)
    assert errors
    assert any("author_notes" in err for err in errors)
    with pytest.raises(ValidationError):
        parse_blueprint(min_doc)


def test_unknown_nested_key_rejected(min_doc):
    min_doc["concepts"][0]["aliases"] = ["Trial"]
    errors = validate_blueprint(min_doc)
    assert errors
    assert any("aliases" in err for err in errors)


def test_allowed_top_level_keys_are_exactly_the_freeze():
    schema_keys = set(BlueprintManifest.model_json_schema()["properties"])
    assert schema_keys == ALLOWED_TOP_LEVEL_KEYS


# ---------------------------------------------------------------------------
# Excluded categories are unrepresentable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key,category", sorted(EXCLUDED_CATEGORY_KEYS.items()))
def test_excluded_category_is_unrepresentable(min_doc, key, category):
    min_doc[key] = {"placeholder": True}
    errors = validate_blueprint(min_doc)
    assert errors, f"{category} leaked through top-level {key!r}"
    assert any(key in err or "extra" in err.lower() for err in errors)
    with pytest.raises(ValidationError):
        parse_blueprint(min_doc)


def test_invalid_type_name_is_returned_not_raised(min_doc):
    min_doc["concepts"][0]["name"] = "Clinical Trial"
    errors = validate_blueprint(min_doc)
    assert errors
    assert all(isinstance(err, str) for err in errors)


def test_source_cannot_carry_a_credential_value(min_doc):
    min_doc["sources"][0]["token"] = "sk-live-not-a-real-key"
    errors = validate_blueprint(min_doc)
    assert errors
    assert any("token" in err for err in errors)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:s3cret@clinicaltrials.gov/api/v2/studies",
        "https://clinicaltrials.gov/api/v2/studies?api_key=sk-live-not-real",
    ],
)
def test_source_url_cannot_embed_credentials(min_doc, url):
    min_doc["sources"][0]["url"] = url
    errors = validate_blueprint(min_doc)
    assert any("url" in err and "credential" in err for err in errors)


def test_source_byok_is_env_name_only(min_doc):
    source = min_doc["sources"][0]
    source["credential"] = "byok"
    source["key_env"] = "sk-looks-like-a-secret"
    errors = validate_blueprint(min_doc)
    assert any("key_env" in err for err in errors)

    source["key_env"] = "NPPES_API_KEY"
    assert validate_blueprint(min_doc) == []


def test_freshness_cannot_carry_status(min_doc):
    min_doc["freshness"]["last_refresh"] = "2026-08-01T00:00:00Z"
    errors = validate_blueprint(min_doc)
    assert errors
    assert any("last_refresh" in err for err in errors)


def test_task_cannot_carry_a_cron_row(min_doc):
    min_doc["tasks"][0]["cron"] = "0 6 * * *"
    errors = validate_blueprint(min_doc)
    assert any("cron" in err for err in errors)


@pytest.mark.parametrize("leaf", ATTR_META_SUFFIXES)
def test_sample_cannot_carry_provenance_companions(sample_doc, leaf):
    sample_doc["sample"]["entities"][0]["attributes"][leaf] = "https://example.com"
    errors = validate_blueprint(sample_doc)
    assert any(leaf in err for err in errors)


@pytest.mark.parametrize("leaf", ["credentials", "cron", "last_run", "staleness"])
def test_sample_cannot_carry_workspace_only_leaves(sample_doc, leaf):
    sample_doc["concepts"][0]["attributes"].append(
        {
            "name": leaf,
            "kind": "literal",
            "datatype": "string",
            "optional": True,
            "cardinality": "1:1",
        }
    )
    sample_doc["sample"]["entities"][0]["attributes"][leaf] = "workspace-only"
    errors = validate_blueprint(sample_doc)
    assert any(leaf in err for err in errors)


# ---------------------------------------------------------------------------
# Ontology classifier reuse — not a second reader
# ---------------------------------------------------------------------------
def test_literal_vs_type_ranged_uses_classify_attr_range(min_doc):
    # Sanity: the catalog helper is what the validator consults.
    assert classify_attr_range("string")[0] == "literal"
    assert classify_attr_range("Organization")[0] == "relationship"

    min_doc["concepts"][0]["attributes"][0]["datatype"] = "Organization"
    errors = validate_blueprint(min_doc)
    assert any("classify_attr_range" in err or "relationship" in err for err in errors)


def test_relationship_must_match_type_ranged_attribute(min_doc):
    min_doc["relationships"][0]["target"] = "ClinicalTrial"
    errors = validate_blueprint(min_doc)
    assert any("range_type" in err or "target" in err for err in errors)


def test_range_type_must_be_a_declared_concept(min_doc):
    min_doc["concepts"][0]["attributes"][-1]["range_type"] = "Person"
    errors = validate_blueprint(min_doc)
    assert any("unknown concept" in err for err in errors)


def test_identity_must_be_literal(min_doc):
    min_doc["concepts"][0]["identity"] = ["lead_sponsor"]
    errors = validate_blueprint(min_doc)
    assert any("identity" in err and "literal" in err for err in errors)


def test_reserved_attribute_leaf_rejected_by_catalog_helper(min_doc):
    min_doc["concepts"][1]["attributes"][0]["name"] = "name"
    min_doc["concepts"][1]["identity"] = ["name"]
    min_doc["freshness"]["er"][1]["identity"] = ["name"]
    min_doc["freshness"]["er"][1]["blocking"] = ["name"]
    errors = validate_blueprint(min_doc)
    assert any("reserved" in err.lower() or "name" in err for err in errors)


def test_skill_identifier_rules_match_type_skill_module():
    """Keep blueprint skill caps identical to ``skills.models`` without importing
    the skill store (that package init pulls settings)."""
    from infona_client.blueprint.models import (
        MAX_SKILL_BODY_CHARS,
        MAX_SKILL_SUMMARY_CHARS,
        MAX_SKILL_TITLE_CHARS,
        SLUG_RE,
        TYPE_NAME_RE,
    )

    text = (
        Path(__file__).resolve().parents[1]
        / "infona_client"
        / "skills"
        / "models.py"
    ).read_text()
    assert SLUG_RE.pattern in text
    assert TYPE_NAME_RE.pattern in text
    assert f"MAX_BODY_CHARS = {MAX_SKILL_BODY_CHARS:,}".replace(",", "_") in text or (
        f"MAX_BODY_CHARS = {MAX_SKILL_BODY_CHARS}" in text
    )
    assert f"MAX_TITLE_CHARS = {MAX_SKILL_TITLE_CHARS}" in text
    assert f"MAX_SUMMARY_CHARS = {MAX_SKILL_SUMMARY_CHARS}" in text


def test_module_is_not_an_ontology_reader():
    import infona_client.blueprint as pkg
    import infona_client.blueprint.validate as validate_mod

    source = Path(pkg.__file__).read_text() + Path(validate_mod.__file__).read_text()
    for banned in (
        "schema_types_for_kg(",
        "fetch_ontology(",
        "ontology_from_graph_store(",
        "load_ontology_shape(",
    ):
        assert banned not in source


# ---------------------------------------------------------------------------
# Sample policy
# ---------------------------------------------------------------------------
def test_sample_rejects_real_nct_on_synthetic(sample_doc):
    sample_doc["sample"]["entities"][0]["attributes"]["nct_id"] = "NCT01234567"
    errors = validate_blueprint(sample_doc)
    assert any("NCT" in err for err in errors)


def test_sample_relationship_must_resolve_to_a_sample_entity(sample_doc):
    """INF-576 — a type-ranged sample slot is legal when the target exists."""
    sample_doc["sample"]["entities"][0]["attributes"]["lead_sponsor"] = (
        "Example Pharma A"
    )
    assert validate_blueprint(sample_doc) == []
    sample_doc["sample"]["entities"][0]["attributes"]["lead_sponsor"] = (
        "Missing Org"
    )
    errors = validate_blueprint(sample_doc)
    assert any("does not resolve" in err for err in errors)


def _with_facility(sample_doc: dict) -> dict:
    sample_doc["concepts"].append(
        {
            "name": "Facility",
            "label": "Facility",
            "identity": ["facility_name", "country"],
            "attributes": [
                {
                    "name": "facility_name",
                    "kind": "literal",
                    "datatype": "string",
                    "optional": False,
                    "cardinality": "1:1",
                },
                {
                    "name": "country",
                    "kind": "literal",
                    "datatype": "string",
                    "optional": False,
                    "cardinality": "1:1",
                },
            ],
        }
    )
    sample_doc["concepts"][0]["attributes"].append(
        {
            "name": "conducted_at",
            "kind": "relationship",
            "range_type": "Facility",
            "cardinality": "N:N",
        }
    )
    sample_doc["relationships"].append(
        {
            "name": "conducted_at",
            "source": "ClinicalTrial",
            "target": "Facility",
            "cardinality": "N:N",
        }
    )
    sample_doc["sample"]["entities"].append(
        {
            "type": "Facility",
            "attributes": {"facility_name": "MGH", "country": "USA"},
        }
    )
    sample_doc["freshness"]["er"].append(
        {
            "type_name": "Facility",
            "identity": ["facility_name", "country"],
            "blocking": ["facility_name", "country"],
            "signals": ["facility_name", "country"],
            "weights": [0.5, 0.5],
            "auto_merge_threshold": 0.95,
            "review_threshold": 0.75,
            "decisive_signals": [],
            "reversible": True,
        }
    )
    return sample_doc


def test_sample_composite_identity_rejects_a_partial_key(sample_doc):
    """INF-576 — Facility identity is name+country; ``MGH`` alone is not a target."""
    from infona_client.graph.ontology_queries import entity_uri

    doc = _with_facility(sample_doc)
    trial = doc["sample"]["entities"][0]["attributes"]
    trial["conducted_at"] = "MGH"
    errors = validate_blueprint(doc)
    assert any("does not resolve" in err for err in errors)

    trial["conducted_at"] = "MGH_USA"
    assert validate_blueprint(doc) == []

    trial["conducted_at"] = entity_uri("Facility", "MGH_USA")
    assert validate_blueprint(doc) == []


def test_sample_entity_cap():
    assert SAMPLE_MAX_ENTITIES == 25
    assert SAMPLE_MAX_BYTES == 64 * 1024


def test_sample_over_byte_cap_rejected(sample_doc):
    sample_doc["sample"]["entities"][0]["attributes"]["official_title"] = "x" * (
        SAMPLE_MAX_BYTES + 1
    )
    errors = validate_blueprint(sample_doc)
    assert any("serialized size" in err for err in errors)


def test_sample_over_entity_cap_rejected(sample_doc):
    proto = sample_doc["sample"]["entities"][0]
    sample_doc["sample"]["entities"] = [
        {
            **copy.deepcopy(proto),
            "attributes": {**proto["attributes"], "nct_id": f"SAMPLE-{i:03d}"},
        }
        for i in range(SAMPLE_MAX_ENTITIES + 1)
    ]
    errors = validate_blueprint(sample_doc)
    assert errors


# ---------------------------------------------------------------------------
# Semver / acquisition_revision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "change,expected",
    [
        ("remove_concept", "major"),
        ("narrow_range", "major"),
        ("literal_to_type_ranged", "major"),
        ("add_optional_attribute", "minor"),
        ("add_source_binding", "minor"),
        ("wording", "patch"),
        ("change_acquisition_instruction", "acquisition_revision"),
        ("change_freshness_window", "acquisition_revision"),
        ("change_seed_query", "acquisition_revision"),
    ],
)
def test_semver_table(change, expected):
    assert classify_manifest_change(change) == expected


def test_semver_table_is_a_partition():
    all_kinds = SEMVER_MAJOR | SEMVER_MINOR | SEMVER_PATCH | ACQUISITION_REVISION_CHANGES
    assert len(all_kinds) == (
        len(SEMVER_MAJOR)
        + len(SEMVER_MINOR)
        + len(SEMVER_PATCH)
        + len(ACQUISITION_REVISION_CHANGES)
    )
    for kind in all_kinds:
        classify_manifest_change(kind)


def test_unknown_change_kind_is_not_a_silent_minor():
    with pytest.raises(ValueError, match="v1-frozen"):
        classify_manifest_change("whatever_feels_minor")


def test_acquisition_revision_is_required_and_independent_of_version(min_doc):
    del min_doc["acquisition_revision"]
    errors = validate_blueprint(min_doc)
    assert any("acquisition_revision" in err for err in errors)
    min_doc["acquisition_revision"] = 2
    min_doc["version"] = "1.0.0"
    assert validate_blueprint(min_doc) == []


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------
def test_fork_requires_parent_and_chain(min_doc):
    min_doc["lineage"] = {
        "parent": {"id": "infona/clinical-trials", "version": "1.0.0"},
    }
    errors = validate_blueprint(min_doc)
    assert any("chain" in err for err in errors)

    min_doc["lineage"]["chain"] = [
        {"id": "infona/clinical-trials", "version": "1.0.0"}
    ]
    assert validate_blueprint(min_doc) == []
