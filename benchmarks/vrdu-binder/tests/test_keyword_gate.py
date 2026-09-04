"""KeywordBinder cannot write published-split dumps."""

from __future__ import annotations

from vrdu_binder.bind import ForcedBinder, KeywordBinder, TypeCatalog
from vrdu_binder.cli import main
from vrdu_binder.dump import write_predictions
from vrdu_binder.extract import KeywordExtractor, entity_item
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.gate import is_published_split_name
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.run import run_corpus
from vrdu_binder.skills import write_skills_for_seed

PUBLISHED = "FARA-lv2-mixed_template-train_200-test_300-valid_100-SD_0"


def test_published_split_name_detector():
    assert is_published_split_name(PUBLISHED)
    assert is_published_split_name(PUBLISHED + "-test_predictions.json")
    assert is_published_split_name(
        "DeepForm-mixed_template-train_200-test_300-valid_100-SD_2.json"
    )
    assert not is_published_split_name(
        "SYNTH-mixed_template-train_1-test_2-valid_1-SD_0"
    )


def test_run_corpus_refuses_keyword_on_published_name(tmp_path):
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    try:
        run_corpus(
            corpus="registration",
            seed=0,
            split=mem["splits"]["type_0"],
            documents=mem["index_by_type"]["type_0"],
            skills=skills,
            catalog=TypeCatalog(keys_by_type=FIXTURE_KEYS),
            binder=KeywordBinder(),
            extractor=KeywordExtractor(),
            out_dir=tmp_path,
            dump_split_name=PUBLISHED,
        )
    except ProtocolError as exc:
        assert "KeywordBinder" in str(exc)
        assert "published-split" in str(exc)
    else:
        raise AssertionError("keyword dump on published split must refuse")
    assert list(tmp_path.glob("*-test_predictions.json")) == []


def test_forced_binder_plus_keyword_extractor_also_refused(tmp_path):
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    try:
        run_corpus(
            corpus="registration",
            seed=0,
            split=mem["splits"]["type_0"],
            documents=mem["index_by_type"]["type_0"],
            skills=skills,
            catalog=TypeCatalog(keys_by_type=FIXTURE_KEYS),
            binder=ForcedBinder("type_0"),
            extractor=KeywordExtractor(),
            out_dir=tmp_path,
            dump_split_name=PUBLISHED,
        )
    except ProtocolError as exc:
        assert "KeywordExtractor" in str(exc) or "KeywordBinder" in str(exc)
    else:
        raise AssertionError("keyword extract on published split must refuse")


def test_write_predictions_refuses_keyword_adapter_meta(tmp_path):
    mem = build_memory_fixtures()
    try:
        write_predictions(
            out_dir=tmp_path,
            split_name=PUBLISHED,
            split=mem["splits"]["type_0"],
            filled={"test_a_1.pdf": [entity_item("widget_id", "W-200")]},
            extra_meta={"adapter": "KeywordBinder"},
        )
    except ProtocolError as exc:
        assert "published-split" in str(exc)
    else:
        raise AssertionError("expected refuse")


def test_cli_run_keyword_refuses_before_touching_data(tmp_path, capsys):
    rc = main(
        [
            "run",
            "--binder",
            "keyword",
            "--corpus",
            "registration",
            "--data",
            str(tmp_path / "missing-vrdu"),
            "--out",
            str(tmp_path / "dumps"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "KeywordBinder" in err
    assert not (tmp_path / "dumps").exists()


def test_cli_run_llm_without_key_refuses(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("INFONA_BINDER_API_KEY", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    rc = main(
        [
            "run",
            "--binder",
            "llm",
            "--corpus",
            "registration",
            "--data",
            str(tmp_path / "missing-vrdu"),
            "--out",
            str(tmp_path / "dumps"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "INFONA_BINDER_API_KEY" in err
    assert "KeywordBinder" in err
    assert not (tmp_path / "dumps").exists()
