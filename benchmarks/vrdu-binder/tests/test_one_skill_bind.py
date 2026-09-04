"""Freeze 2: exactly one skill reaches extract. No dump-both-then-pick."""

from __future__ import annotations

import pytest

from vrdu_binder.bind import ForcedBinder, TypeCatalog, skill_for_bind
from vrdu_binder.extract import KeywordExtractor, extract_one
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.ocr import bind_prompt
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.run import run_corpus
from vrdu_binder.skills import write_skills_for_seed


class SpyExtractor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.inner = KeywordExtractor()

    def extract(self, prompt, skill):
        self.calls.append(skill.type_id)
        return self.inner.extract(prompt, skill)


def test_extract_rejects_a_skill_collection():
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    with pytest.raises(ProtocolError, match="only the bound skill"):
        extract_one(KeywordExtractor(), "widget_id W-1", [skills["type_0"], skills["type_1"]])


def test_run_calls_extract_with_one_skill_per_correct_bind(tmp_path):
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    spy = SpyExtractor()
    run_corpus(
        corpus="registration",
        seed=0,
        split=mem["splits"]["type_0"],
        documents=mem["index_by_type"]["type_0"],
        skills=skills,
        catalog=TypeCatalog(keys_by_type=FIXTURE_KEYS),
        binder=ForcedBinder("type_0"),
        extractor=spy,
        out_dir=tmp_path,
        dump_split_name="SYNTH-mixed_template-train_1-test_2-valid_1-SD_0",
    )
    assert spy.calls == ["type_0", "type_0"]
    assert "type_1" not in spy.calls


def test_bound_skill_cannot_emit_the_other_types_keys():
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    skill = skill_for_bind("type_0", skills)
    doc = mem["index_by_type"]["type_0"]["test_a_1.pdf"]
    items = extract_one(KeywordExtractor(), bind_prompt(doc), skill)
    assert {i[0] for i in items} <= {"widget_id", "widget_name"}
    assert "invoice_id" not in {i[0] for i in items}
