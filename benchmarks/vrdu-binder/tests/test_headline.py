"""Freeze 7–8: F1_wrong and oracle-type F1 never headline."""

from __future__ import annotations

import pytest

from vrdu_binder.dump import predictions_payload
from vrdu_binder.headline import (
    HEADLINE_KEYS,
    assert_not_headline_path,
    make_headline,
)
from vrdu_binder.protocol import ProtocolError


def test_headline_keys_are_the_slide_set():
    h = make_headline(
        pred_types={"a.pdf": "type_0", "b.pdf": "type_1"},
        gold_types={"a.pdf": "type_0", "b.pdf": "type_1"},
        metric_micro_f1={"registration": 0.1, "adbuy": 0.2},
    )
    d = h.as_dict()
    assert set(d) == set(HEADLINE_KEYS)
    assert "f1_wrong" not in d
    assert "oracle" not in d
    assert d["n_types"] == 2
    assert "50%" in d["n2_tax"]
    assert d["bind_at_type_accuracy"] == 1.0


def test_headline_rejects_f1_wrong_alias():
    with pytest.raises(ProtocolError, match="not headline"):
        make_headline(
            pred_types={"a.pdf": "type_0"},
            gold_types={"a.pdf": "type_0"},
            metric_micro_f1={"f1_wrong": 0.9},
        )


def test_headline_rejects_oracle_alias():
    with pytest.raises(ProtocolError, match="not headline"):
        make_headline(
            pred_types={"a.pdf": "type_0"},
            gold_types={"a.pdf": "type_0"},
            metric_micro_f1={"oracle_type": 0.9},
        )


def test_dump_writer_cannot_mark_oracle():
    with pytest.raises(ProtocolError, match="oracle"):
        predictions_payload(
            split_name="x",
            results={"a.pdf": []},
            extra_meta={"bind": "oracle"},
        )


def test_sanity_paths_are_not_headline_dirs():
    with pytest.raises(ProtocolError):
        assert_not_headline_path("/tmp/sanity/f1_wrong")
    with pytest.raises(ProtocolError):
        assert_not_headline_path("dumps/oracle/registration")
    assert_not_headline_path("/tmp/vrdu-dumps/registration")
