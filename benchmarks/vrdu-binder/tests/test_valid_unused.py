"""Freeze 6: valid is unused for bind, prompt, redaction, skills, selection."""

from __future__ import annotations

import json

from vrdu_binder.bind import KeywordBinder, TypeCatalog
from vrdu_binder.extract import KeywordExtractor
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.run import run_corpus
from vrdu_binder.skills import write_skills_for_seed
from vrdu_binder.splits import RunSplit, load_run_split


def test_run_split_has_no_valid_attribute():
    split = RunSplit(train=("a.pdf",), test=("b.pdf",))
    assert "valid" not in split.__dataclass_fields__


def test_loader_does_not_store_valid(tmp_path):
    path = tmp_path / "split.json"
    path.write_text(
        json.dumps(
            {
                "train": ["tr.pdf"],
                "valid": ["SHOULD_NEVER_BE_READ.pdf"],
                "test": ["te.pdf"],
            }
        ),
        encoding="utf-8",
    )
    split = load_run_split(path)
    dumped = json.dumps(split.train + split.test)
    assert "SHOULD_NEVER_BE_READ.pdf" not in dumped
    assert "SHOULD_NEVER_BE_READ.pdf" not in split.train
    assert "SHOULD_NEVER_BE_READ.pdf" not in split.test


def test_bind_loop_only_walks_test_filenames(tmp_path):
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    result = run_corpus(
        corpus="registration",
        seed=0,
        split=mem["splits"]["type_0"],
        documents=mem["index_by_type"]["type_0"],
        skills=skills,
        catalog=TypeCatalog(keys_by_type=FIXTURE_KEYS),
        binder=KeywordBinder(),
        extractor=KeywordExtractor(),
        out_dir=tmp_path,
        dump_split_name="SYNTH-mixed_template-train_1-test_2-valid_1-SD_0",
    )
    seen = [o.filename for o in result.outcomes]
    assert seen == list(mem["splits"]["type_0"].test)
    assert "valid_a_1.pdf" not in seen
    assert "LEAK_VALID_A" not in skills["type_0"].body
    valid = mem["index_by_type"]["type_0"]["valid_a_1.pdf"]
    assert "LEAK_VALID_A" in valid["ocr"]["text"]


def test_write_skills_ignores_valid_and_test_docs_sitting_in_the_pile():
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    assert "LEAK_VALID_A" not in skills["type_0"].body
    assert "LEAK_VALID_B" not in skills["type_1"].body
    assert "W-200" not in skills["type_0"].body
    assert "INV-20" not in skills["type_1"].body
