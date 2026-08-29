"""INF-591 / INF-587 — sample facts cannot present as current.

Classifier + Explorer row stamp + FactCitation validator. Does not invent a
second sample store: install still writes through ``insert_facts`` and the
lock lists those subjects.
"""

from __future__ import annotations

import pytest

from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.install import inspect_blueprint, install_blueprint
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.blueprint.overlay import reset_blueprint_overlay_store
from infona_client.blueprint.plan import SAMPLE_SOURCE_MARK, facts_for_sample
from infona_client.blueprint.sample_mark import (
    SAMPLE_FLAG,
    SampleIndex,
    is_sample_mark,
    mark_record,
    sample_answer_note,
    sample_index_for_kg,
    sample_status_label,
)
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.models.query import FactCitation
from infona_client.nlp.answer_meta import build_citations, build_coverage_caveat
from infona_client.skills.store import reset_type_skill_store

TENANT = "bp-sample-mark-tenant"
KG = "clinical-trials"


@pytest.fixture(autouse=True)
def _reset_blueprint_state():
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()
    yield
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()


def test_is_sample_mark_accepts_install_stamps():
    assert is_sample_mark(provenance=SAMPLE_SOURCE_MARK)
    assert is_sample_mark(source="blueprint:infona/clinical-trials@0.1.0#sample")
    assert is_sample_mark(source="https://example.org/preview#sample")
    assert not is_sample_mark(source="https://clinicaltrials.gov/study/NCT04660344")
    assert not is_sample_mark(source="", provenance="")


def test_sample_status_label_ignores_sample_is_current_true():
    """A lying payload cannot render as current."""
    assert "current" not in sample_status_label(
        included=True,
        captured_at="2026-06-01",
        sample_is_current=True,
    ).lower().replace("not current", "")
    label = sample_status_label(
        included=True,
        captured_at="2026-06-01",
        sample_is_current=True,
    )
    assert label == "Sample · not current · captured 2026-06-01"
    assert "current" not in sample_answer_note("2026-06-01").lower().replace(
        "not current", ""
    )


def test_fact_citation_sample_cannot_be_current():
    cite = FactCitation(
        subject="https://graph.infona.ai/entities/ClinicalTrial/SAMPLE-1",
        source="blueprint:infona/clinical-trials@0.1.0#sample",
        verdict="current",
        is_current=True,
        is_sample=True,
        sample_captured_at="2026-06-01",
    )
    assert cite.is_sample is True
    assert cite.is_current is False
    assert cite.verdict == "sample"


def test_mark_record_stamps_flags_and_never_current():
    index = SampleIndex(
        subjects=frozenset({"https://graph.infona.ai/entities/ClinicalTrial/S1"}),
        captured_at="2026-06-01",
        captured_by_subject={
            "https://graph.infona.ai/entities/ClinicalTrial/S1": "2026-06-01"
        },
    )
    row = mark_record(
        {"id": "https://graph.infona.ai/entities/ClinicalTrial/S1", "name": "Trial"},
        index,
    )
    assert row["flags"] == [SAMPLE_FLAG]
    assert row["sample_is_current"] is False
    assert row["sample_captured_at"] == "2026-06-01"
    live = mark_record(
        {"id": "https://graph.infona.ai/entities/ClinicalTrial/NCT1", "name": "Live"},
        index,
    )
    assert "flags" not in live
    assert "sample_is_current" not in live


def test_clinical_trials_sample_facts_carry_the_mark():
    from infona_client.blueprint.load import load_blueprint_package

    manifest = load_blueprint_package(CLINICAL_TRIALS)
    facts, subjects = facts_for_sample(manifest)
    assert subjects
    assert all(
        is_sample_mark(f.source, f.provenance) or f.kind == "type" for f in facts
    )
    typed = [f for f in facts if f.kind == "type"]
    assert typed
    assert all(is_sample_mark(f.source) for f in typed)
    literals = [f for f in facts if f.kind != "type"]
    assert literals
    assert all(f.provenance == SAMPLE_SOURCE_MARK for f in literals)


@pytest.mark.asyncio
async def test_clinical_trials_install_index_and_records_are_sample():
    first = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert first.sample_is_current is False
    assert first.sample_included is True
    card = await inspect_blueprint(TENANT, "infona/clinical-trials")
    assert card["sample_is_current"] is False

    index = await sample_index_for_kg(TENANT, KG)
    assert index.subjects == frozenset(first.sample_subjects)
    assert index.captured_at == "2026-06-01"
    assert index.count_for_type("ClinicalTrial") > 0

    from infona_client.api.routes.explore_records import _records_from_explore_store

    page = await _records_from_explore_store(
        tenant_id=TENANT,
        kg_name=KG,
        type_name="ClinicalTrial",
        limit=50,
        cursor=None,
    )
    assert page is not None
    assert page["rows"]
    for row in page["rows"]:
        assert SAMPLE_FLAG in (row.get("flags") or [])
        assert row.get("sample_is_current") is False
        assert row.get("sample_captured_at") == "2026-06-01"


@pytest.mark.asyncio
async def test_build_citations_sample_subject_is_never_current():
    first = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    subject = first.sample_subjects[0]
    from infona_client.graph.queries import kg_graph_uri

    graph = kg_graph_uri(TENANT, KG)
    citations = await build_citations(
        None,
        graph,
        ["s", "p", "o"],
        [
            {
                "s": subject,
                "p": "https://graph.infona.ai/onto/nct_id",
                "o": "SAMPLE-NCT",
                "label": "SAMPLE-NCT",
            }
        ],
    )
    assert citations
    for cite in citations:
        assert cite.is_sample is True
        assert cite.is_current is False
        assert cite.verdict == "sample"
    caveat = build_coverage_caveat(
        None,
        sample_count=len(citations),
        total_cited=len(citations),
        sample_captured_at="2026-06-01",
    )
    assert "sample, not current" in caveat
    assert "stale" not in caveat
    assert "2026-06-01" in caveat
