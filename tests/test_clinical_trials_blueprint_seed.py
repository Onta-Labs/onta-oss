"""INF-566 — Clinical Trials Blueprint v0 seed package.

Gate B bar: the package validates against the INF-563 v1 schema.
Install / inspect / uninstall live in ``infona_client.blueprint``
(INF-575 / INF-577). Export (INF-565 reverse) is still open.
"""

from __future__ import annotations

import json
from pathlib import Path

from infona_client.blueprint import (
    FORBIDDEN_TOP_LEVEL_KEYS,
    SAMPLE_MAX_BYTES,
    SAMPLE_MAX_ENTITIES,
    find_manifest,
    load_blueprint_package,
    validate_blueprint,
    validate_blueprint_package,
)
from infona_client.blueprint.seeds import CLINICAL_TRIALS

SEED = CLINICAL_TRIALS
MANIFEST = SEED / "blueprint.yaml"


def test_seed_is_an_adr_0014_directory():
    assert SEED.is_dir()
    assert MANIFEST.is_file()
    assert not (SEED / "blueprint.json").exists()
    assert (SEED / "README.md").is_file()
    assert (SEED / "LICENSE").is_file()
    assert (SEED / "sample" / "README.md").is_file()
    text = MANIFEST.read_text(encoding="utf-8")
    assert text.isascii() or "\ufffd" not in text
    text.encode("utf-8")


def test_seed_validates():
    assert validate_blueprint_package(SEED) == []
    assert validate_blueprint(MANIFEST.read_text(encoding="utf-8")) == []
    manifest = load_blueprint_package(SEED)
    assert manifest.schema_version == "1"
    assert manifest.schema_status == "v1-frozen"
    assert manifest.id == "infona/clinical-trials"
    assert manifest.version == "0.1.0"
    assert manifest.acquisition_revision == 1
    assert find_manifest(SEED) == MANIFEST


def test_cli_validate_exits_zero():
    from infona_client.blueprint.__main__ import main

    assert main(["validate", str(SEED)]) == 0


def test_domain_model_is_the_contract_slice_not_a_demo_dump():
    manifest = load_blueprint_package(SEED)
    names = {c.name for c in manifest.concepts}
    assert names == {
        "ClinicalTrial",
        "Organization",
        "MedicalCondition",
        "Intervention",
        "Investigator",
        "Facility",
    }
    # City / State / ZIP stay on Facility. Promoting them is the demo-tenant accident.
    assert "City" not in names
    assert "State" not in names
    assert "Zip" not in names
    trial = next(c for c in manifest.concepts if c.name == "ClinicalTrial")
    literals = {a.name for a in trial.attributes if a.kind == "literal"}
    rels = {a.name for a in trial.attributes if a.kind == "relationship"}
    assert {
        "nct_id",
        "official_title",
        "brief_title",
        "overall_status",
        "phase",
        "enrollment",
        "start_date",
        "primary_completion_date",
        "study_type",
    } <= literals
    assert {
        "lead_sponsor",
        "collaborator",
        "studies_condition",
        "uses_intervention",
        "has_investigator",
        "conducted_at",
    } <= rels
    lead = next(a for a in trial.attributes if a.name == "lead_sponsor")
    assert lead.kind == "relationship" and lead.cardinality == "N:1"
    assert lead.range_type == "Organization"
    reserved = {"name", "id", "label", "source"}
    for concept in manifest.concepts:
        leaves = {a.name for a in concept.attributes}
        assert leaves.isdisjoint(reserved), concept.name
    facility = next(c for c in manifest.concepts if c.name == "Facility")
    assert {a.name for a in facility.attributes} >= {
        "facility_name",
        "city",
        "state",
        "country",
    }


def test_ctgov_source_and_cadence():
    manifest = load_blueprint_package(SEED)
    source_ids = {s.id for s in manifest.sources}
    assert "ctgov" in source_ids
    ctgov = next(s for s in manifest.sources if s.id == "ctgov")
    assert ctgov.credential == "none"
    assert ctgov.key_env == ""
    assert "clinicaltrials.gov/api/v2/studies" in ctgov.url
    assert ctgov.mappings
    lands = {m.lands_on for m in ctgov.mappings}
    assert "Facility.facility_name" in lands
    nppes = next(s for s in manifest.sources if s.id == "nppes")
    nppes_lands = {m.lands_on for m in nppes.mappings}
    assert nppes_lands == {"Investigator.npi", "Investigator.specialty"}
    assert any("14" in p.cadence or p.stale_after_days == 14 for p in manifest.freshness.policies)
    status = next(
        p
        for p in manifest.freshness.policies
        if p.target == "ClinicalTrial.overall_status"
    )
    assert status.stale_after_days == 14
    enrollment = next(
        p for p in manifest.freshness.policies if p.target == "ClinicalTrial.enrollment"
    )
    assert enrollment.stale_after_days == 45


def test_ten_supported_questions_and_ten_evals():
    manifest = load_blueprint_package(SEED)
    assert len(manifest.examples) == 10
    assert len(manifest.evals) == 10
    questions = [ex.question.lower() for ex in manifest.examples]
    assert any("recruiting" in q and "phase 3" in q for q in questions)
    assert any("lead sponsor" in q for q in questions)
    kinds = {ev.kind for ev in manifest.evals}
    assert kinds == {"structural", "question"}
    question_evals = [ev for ev in manifest.evals if ev.kind == "question"]
    assert all(ev.still_works_when.strip() for ev in question_evals)
    sample_eval = next(ev for ev in manifest.evals if ev.id == "sample-recruiting-labelled")
    assert "2026-06-01" in sample_eval.still_works_when
    assert "SAMPLE-003" in sample_eval.still_works_when


def test_tasks_and_rules_present():
    manifest = load_blueprint_package(SEED)
    task_ids = {t.id for t in manifest.tasks}
    assert {
        "acquire_condition_set",
        "refresh_stale_status",
        "verify_trial",
        "answer_supported_question",
        "watch_status_change",
    } <= task_ids
    assert manifest.rules.tombstones.disappeared_row == "withdrawn"
    assert manifest.rules.tombstones.delete_forbidden is True
    winners = {rule.winner for rule in manifest.rules.conflict}
    assert "ctgov" in winners


def test_no_instance_records_outside_sample():
    raw = MANIFEST.read_text(encoding="utf-8")
    manifest = load_blueprint_package(SEED)
    dumped = json.dumps(manifest.model_dump(mode="json"), allow_nan=False)
    for key in FORBIDDEN_TOP_LEVEL_KEYS:
        assert key not in manifest.model_fields_set
        assert f"\n{key}:" not in raw
    assert "dev-key-001" not in dumped
    assert "NCT01234567" not in dumped


def test_sample_is_inf_587():
    manifest = load_blueprint_package(SEED)
    sample = manifest.sample
    assert sample is not None
    assert sample.kind == "synthetic"
    assert str(sample.captured_at) == "2026-06-01"
    assert len(sample.entities) <= SAMPLE_MAX_ENTITIES
    dumped = json.dumps(sample.model_dump(mode="json"), allow_nan=False)
    assert len(dumped.encode("utf-8")) <= SAMPLE_MAX_BYTES
    for entity in sample.entities:
        for value in entity.attributes.values():
            if isinstance(value, str) and value.startswith("NCT"):
                raise AssertionError(f"synthetic sample used a real NCT form: {value}")
    trial_ids = [
        e.attributes["nct_id"]
        for e in sample.entities
        if e.type == "ClinicalTrial"
    ]
    assert trial_ids[0].startswith("SAMPLE-")
    sample_readme = (SEED / "sample" / "README.md").read_text(encoding="utf-8")
    assert "not current" in sample_readme.lower()
    assert "2026-06-01" in sample_readme


def test_sample_is_independently_droppable():
    manifest = load_blueprint_package(SEED)
    doc = manifest.model_dump(mode="json")
    del doc["sample"]
    assert validate_blueprint(doc) == []


def test_install_and_export_are_both_on_the_package():
    """Install (INF-575) and export (INF-565) share the protocol package."""
    import infona_client.blueprint as pkg

    assert "install_blueprint" in pkg.__all__
    assert "export_blueprint" in pkg.__all__
    assert hasattr(pkg, "install_blueprint")
    assert hasattr(pkg, "export_blueprint")
    readme = (SEED / "README.md").read_text(encoding="utf-8")
    assert "INF-565" in readme


def test_package_must_not_ship_yaml_and_json(tmp_path: Path):
    pkg = tmp_path / "both"
    pkg.mkdir()
    pkg.joinpath("blueprint.yaml").write_text("id: x\n", encoding="utf-8")
    pkg.joinpath("blueprint.json").write_text("{}", encoding="utf-8")
    try:
        find_manifest(pkg)
        raise AssertionError("expected F1 rejection")
    except ValueError as exc:
        assert "must not ship both" in str(exc)


def test_seed_is_not_a_hosted_registry():
    readme = " ".join((SEED / "README.md").read_text(encoding="utf-8").lower().split())
    assert "not a hosted registry" in readme
    # No registry index, listing UI, or entitlement surface under seeds/.
    seed_files = [p.name for p in Path(SEED).rglob("*") if p.is_file()]
    assert "registry.json" not in seed_files
    assert "catalog.json" not in seed_files
