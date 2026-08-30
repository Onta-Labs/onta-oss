"""Freeze 4: misbind writes empty results[filename], not a dropped row."""

from __future__ import annotations

from vrdu_binder.bind import ForcedBinder, KeywordBinder, TypeCatalog
from vrdu_binder.dump import load_predictions
from vrdu_binder.extract import KeywordExtractor
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.run import run_corpus
from vrdu_binder.skills import write_skills_for_seed


def _run(tmp_path, binder):
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    return run_corpus(
        corpus="registration",
        seed=0,
        split=mem["splits"]["type_0"],
        documents=mem["index_by_type"]["type_0"],
        skills=skills,
        catalog=TypeCatalog(keys_by_type=FIXTURE_KEYS),
        binder=binder,
        extractor=KeywordExtractor(),
        out_dir=tmp_path,
        dump_split_name="SYNTH-mixed_template-train_1-test_2-valid_1-SD_0",
    )


def test_keyword_binder_misbind_is_empty_list_not_omitted(tmp_path):
    result = _run(tmp_path, KeywordBinder())
    payload = load_predictions(result.dump_path)
    results = payload["results"]
    assert "test_a_misbind.pdf" in results
    assert results["test_a_misbind.pdf"] == []
    assert results["test_a_1.pdf"]  # correct bind still extracts
    assert list(results) == ["test_a_1.pdf", "test_a_misbind.pdf"]


def test_forced_other_type_empties_every_row(tmp_path):
    result = _run(tmp_path, ForcedBinder("type_1"))
    payload = load_predictions(result.dump_path)
    assert payload["results"]["test_a_1.pdf"] == []
    assert payload["results"]["test_a_misbind.pdf"] == []
    assert set(payload["results"]) == {"test_a_1.pdf", "test_a_misbind.pdf"}
