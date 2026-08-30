"""Freeze 3: skills and few-shots come from train filenames only."""

from __future__ import annotations

import pytest

from vrdu_binder.constants import TYPE_0, TYPE_1
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.skills import write_skill, write_skills_for_seed


def test_few_shots_use_train_values_only():
    mem = build_memory_fixtures()
    skill = write_skill(
        type_id=TYPE_0,
        split=mem["splits"][TYPE_0],
        train_docs=mem["docs_by_type"][TYPE_0][:1],
        seed=0,
        keys=FIXTURE_KEYS[TYPE_0],
    )
    assert "W-100" in skill.body
    assert "sprocket" in skill.body
    assert "LEAK_VALID_A" not in skill.body
    assert "W-200" not in skill.body
    assert "W-MISS" not in skill.body
    assert "train_a_1.pdf" not in skill.body


def test_valid_doc_rejected_as_train():
    mem = build_memory_fixtures()
    valid = [d for d in mem["docs_by_type"][TYPE_0] if d["filename"] == "valid_a_1.pdf"]
    with pytest.raises(ProtocolError, match="train filenames only"):
        write_skill(
            type_id=TYPE_0,
            split=mem["splits"][TYPE_0],
            train_docs=valid,
            seed=0,
            keys=FIXTURE_KEYS[TYPE_0],
        )


def test_test_doc_rejected_as_train():
    mem = build_memory_fixtures()
    held = [d for d in mem["docs_by_type"][TYPE_0] if d["filename"] == "test_a_1.pdf"]
    with pytest.raises(ProtocolError, match="train filenames only"):
        write_skill(
            type_id=TYPE_0,
            split=mem["splits"][TYPE_0],
            train_docs=held,
            seed=0,
            keys=FIXTURE_KEYS[TYPE_0],
        )


def test_skill_body_has_keys_and_no_nicknames():
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    body = skills[TYPE_0].body
    assert "widget_id" in body
    assert "widget_name" in body
    for banned in (
        "registration-form",
        "ad-buy-form",
        "FARA",
        "DeepForm",
        "sk_reg",
        "sk_adbuy",
        "type_0",
        "type_1",
        "Registration",
        "Ad-buy",
    ):
        assert banned not in body
    assert "invoice_id" not in skills[TYPE_0].body
    assert "widget_id" not in skills[TYPE_1].body
