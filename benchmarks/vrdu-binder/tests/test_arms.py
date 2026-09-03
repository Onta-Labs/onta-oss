"""Arm identity: bare prompts have no catalog/skill leak. Keyword gate holds."""

from __future__ import annotations

from vrdu_binder.arms import (
    ARM_08B_BARE,
    ARM_08B_FT_INFONA,
    ARM_08B_VANILLA_FT,
    ARM_32B_BARE,
    ARM_IDS,
    adapters_for_arm,
    get_arm,
)
from vrdu_binder.bare import BareBinder, BareExtractor
from vrdu_binder.bind import TypeCatalog, bind_one
from vrdu_binder.cli import main
from vrdu_binder.constants import KEYS_FOR_TYPE, MODEL_08B, MODEL_32B, TYPE_0, TYPE_1
from vrdu_binder.extract import extract_one
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.llm import LlmBinder, LlmExtractor
from vrdu_binder.ocr import bind_prompt
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.skills import write_skills_for_seed


class RecordingClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


def _skills():
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    return skills, mem


def test_arm_table_is_the_locked_four():
    assert ARM_IDS == (ARM_32B_BARE, ARM_08B_BARE, ARM_08B_VANILLA_FT, ARM_08B_FT_INFONA)
    assert get_arm(ARM_32B_BARE).model_id == MODEL_32B
    assert get_arm(ARM_08B_BARE).model_id == MODEL_08B
    assert "20B" not in get_arm(ARM_32B_BARE).model_id
    assert get_arm(ARM_32B_BARE).uses_infona_router is False
    assert get_arm(ARM_08B_VANILLA_FT).uses_infona_router is False
    assert get_arm(ARM_08B_FT_INFONA).uses_infona_router is True
    assert get_arm(ARM_08B_VANILLA_FT).lora_recipe == "vanilla"
    assert get_arm(ARM_08B_FT_INFONA).lora_recipe == "infona"


def test_bare_arms_use_bare_adapters():
    for arm_id in (ARM_32B_BARE, ARM_08B_BARE, ARM_08B_VANILLA_FT):
        binder, extractor = adapters_for_arm(get_arm(arm_id), client=RecordingClient("type_0"))
        assert type(binder).__name__ == "BareBinder"
        assert type(extractor).__name__ == "BareExtractor"


def test_infona_arm_uses_llm_adapters():
    binder, extractor = adapters_for_arm(
        get_arm(ARM_08B_FT_INFONA), client=RecordingClient("type_0")
    )
    assert isinstance(binder, LlmBinder)
    assert isinstance(extractor, LlmExtractor)
    assert not isinstance(binder, BareBinder)


def test_bare_bind_has_no_catalog_keys_or_skill():
    skills, mem = _skills()
    client = RecordingClient("type_0")
    binder = BareBinder(client=client)
    doc = mem["index_by_type"][TYPE_0]["test_a_1.pdf"]
    prompt = bind_prompt(doc)
    catalog = TypeCatalog(keys_by_type=FIXTURE_KEYS)
    assert bind_one(binder, prompt, catalog) == TYPE_0
    system, user = client.calls[0]
    assert user == prompt
    assert "widget_id" not in system
    assert "invoice_id" not in system
    assert skills[TYPE_0].body not in system
    assert "file_date" not in system
    assert "FARA" not in system and "DeepForm" not in system
    assert doc["filename"] not in system


def test_bare_extract_omits_skill_body():
    skills, mem = _skills()
    client = RecordingClient('{"widget_id": "W-200", "invoice_id": "INV-LEAK"}')
    extractor = BareExtractor(client=client)
    items = extract_one(extractor, bind_prompt(mem["index_by_type"][TYPE_0]["test_a_1.pdf"]), skills[TYPE_0])
    system, _user = client.calls[0]
    assert skills[TYPE_0].body not in system
    assert "Worked examples" not in system
    names = {i[0] for i in items}
    assert "widget_id" in names
    assert "invoice_id" not in names


def test_infona_extract_includes_one_skill_only():
    skills, mem = _skills()
    client = RecordingClient('{"widget_id": "W-200"}')
    extractor = LlmExtractor(client=client)
    extract_one(extractor, bind_prompt(mem["index_by_type"][TYPE_0]["test_a_1.pdf"]), skills[TYPE_0])
    system, _ = client.calls[0]
    assert skills[TYPE_0].body in system
    assert skills[TYPE_1].body not in system


def test_official_catalog_keys_stay_out_of_bare_bind():
    client = RecordingClient("type_0")
    binder = BareBinder(client=client)
    bind_one(binder, "ocr tokens only", TypeCatalog(keys_by_type=KEYS_FOR_TYPE))
    system, _ = client.calls[0]
    for key in ("file_date", "advertiser", "foreign_principle_name"):
        assert key not in system


def test_experiment_run_official_without_key_refuses(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("INFONA_BINDER_API_KEY", raising=False)
    rc = main(
        [
            "experiment-run",
            "--arm",
            "32b_bare",
            "--corpus",
            "registration",
            "--data",
            str(tmp_path / "missing"),
            "--out",
            str(tmp_path / "dumps"),
        ]
    )
    assert rc == 2
    assert "INFONA_BINDER_API_KEY" in capsys.readouterr().err


def test_experiment_run_fixtures_needs_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("INFONA_BINDER_API_KEY", raising=False)
    rc = main(
        [
            "experiment-run",
            "--arm",
            "0.8b_ft_infona",
            "--corpus",
            "registration",
            "--fixtures",
            "--out",
            str(tmp_path),
        ]
    )
    assert rc == 0
    assert list(tmp_path.rglob("*-test_predictions.json"))


def test_experiment_dry_is_not_a_published_dump(tmp_path, capsys):
    assert main(["experiment-dry", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "not a published score" in out
    dumps = list(tmp_path.rglob("*-test_predictions.json"))
    assert len(dumps) == 8
    for path in dumps:
        assert "FARA-lv2" not in path.name
        assert "DeepForm-mixed" not in path.name
