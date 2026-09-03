"""LLM Binder/Extractor: stubbed HTTP, OCR+keys only, no live call."""

from __future__ import annotations

from vrdu_binder.bind import TypeCatalog, bind_one
from vrdu_binder.constants import KEYS_FOR_TYPE, TYPE_0, TYPE_1
from vrdu_binder.extract import extract_one
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.llm import (
    LlmBinder,
    LlmExtractor,
    UrllibChatClient,
    bind_system_prompt,
    extract_system_prompt,
    llm_base_url,
    parse_extract_json,
    parse_type_id,
    resolve_api_key,
)
from vrdu_binder.constants import TOGETHER_BASE_URL
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
    return write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    ), mem


def test_resolve_api_key_refuses_when_missing(monkeypatch):
    monkeypatch.delenv("INFONA_BINDER_API_KEY", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    try:
        resolve_api_key()
    except ProtocolError as exc:
        assert "INFONA_BINDER_API_KEY" in str(exc)
        assert "TOGETHER_API_KEY" in str(exc)
        assert "KeywordBinder" in str(exc)
    else:
        raise AssertionError("missing key must refuse")


def test_together_key_is_fallback(monkeypatch):
    monkeypatch.delenv("INFONA_BINDER_API_KEY", raising=False)
    monkeypatch.setenv("TOGETHER_API_KEY", "together-not-a-real-key")
    assert resolve_api_key() == "together-not-a-real-key"


def test_default_base_url_is_together(monkeypatch):
    monkeypatch.delenv("INFONA_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("INFONA_BINDER_BASE_URL", raising=False)
    assert llm_base_url() == TOGETHER_BASE_URL


def test_llm_binder_does_not_construct_keyword_fallback(monkeypatch):
    monkeypatch.delenv("INFONA_BINDER_API_KEY", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    try:
        LlmBinder()
    except ProtocolError as exc:
        assert "INFONA_BINDER_API_KEY" in str(exc)
    else:
        raise AssertionError("LlmBinder() without a key must not fall back")


def test_bind_catalog_is_keys_only():
    system = bind_system_prompt(TypeCatalog(keys_by_type=KEYS_FOR_TYPE))
    assert "type_0" in system and "type_1" in system
    assert "file_date" in system and "advertiser" in system
    for banned in ("FARA", "DeepForm", "registration-form", "ad-buy-form", "Ad-buy"):
        assert banned not in system


def test_llm_bind_prompt_is_ocr_and_keys(monkeypatch):
    client = RecordingClient("type_0")
    binder = LlmBinder(client=client)
    mem = build_memory_fixtures()
    doc = dict(mem["index_by_type"][TYPE_0]["test_a_1.pdf"])
    prompt = bind_prompt(doc)
    catalog = TypeCatalog(keys_by_type=FIXTURE_KEYS)
    assert bind_one(binder, prompt, catalog) == TYPE_0
    system, user = client.calls[0]
    assert user == prompt
    assert "widget_id" in system
    assert doc["filename"] not in system and doc["filename"] not in user
    assert "registration-form/" not in system and "registration-form/" not in user
    assert "LEAK_VALID_A" not in system
    assert "FARA" not in system and "DeepForm" not in system
    assert "annotations" not in user


def test_llm_extract_uses_one_skill_body_and_ocr():
    skills, mem = _skills()
    skill = skills[TYPE_0]
    client = RecordingClient('{"widget_id": "W-200", "widget_name": "gasket"}')
    extractor = LlmExtractor(client=client)
    doc = mem["index_by_type"][TYPE_0]["test_a_1.pdf"]
    prompt = bind_prompt(doc)
    items = extract_one(extractor, prompt, skill)
    assert {i[0] for i in items} <= set(skill.keys)
    system, user = client.calls[0]
    assert skill.body in system
    assert user == prompt
    assert skills[TYPE_1].body not in system
    assert "invoice_id" not in system
    assert doc["filename"] not in system


def test_extract_rejects_other_type_keys():
    skills, _ = _skills()
    try:
        parse_extract_json('{"invoice_id": "INV-1"}', skills[TYPE_0])
    except ProtocolError as exc:
        assert "invoice_id" in str(exc)
    else:
        raise AssertionError("other-type key must be refused")


def test_parse_type_id_json_wrapper():
    catalog = TypeCatalog(keys_by_type=FIXTURE_KEYS)
    assert parse_type_id('{"id": "type_1"}', catalog) == TYPE_1


def test_urllib_client_uses_injected_post(monkeypatch):
    monkeypatch.setenv("INFONA_BINDER_API_KEY", "not-a-real-key")
    seen: dict[str, object] = {}

    def post(url, headers, body):
        seen["url"] = url
        seen["body"] = body
        return {"choices": [{"message": {"content": "type_1"}}]}

    client = UrllibChatClient(post=post)
    text = client.complete(system="keys only", user="ocr tokens")
    assert text == "type_1"
    assert str(seen["url"]).endswith("/chat/completions")
    messages = seen["body"]["messages"]  # type: ignore[index]
    assert messages[0]["content"] == "keys only"
    assert messages[1]["content"] == "ocr tokens"
    assert seen["body"]["chat_template_kwargs"]["enable_thinking"] is False
