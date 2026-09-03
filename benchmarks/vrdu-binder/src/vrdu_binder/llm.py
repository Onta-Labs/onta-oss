"""LLM Binder/Extractor. OCR + keys/skill only. No live call without a key."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Protocol

from vrdu_binder.bind import TypeCatalog
from vrdu_binder.constants import LEAK_LITERALS, LEAK_PATTERNS, TOGETHER_BASE_URL
from vrdu_binder.extract import entity_item
from vrdu_binder.protocol import ProtocolError, require_one_skill
from vrdu_binder.skills import Skill, assert_extract_keys_subset

API_KEY_VARS = ("INFONA_BINDER_API_KEY", "TOGETHER_API_KEY")
DEFAULT_BASE_URL = TOGETHER_BASE_URL
DEFAULT_MODEL = "Qwen/Qwen3.5-0.8B"


class ChatClient(Protocol):
    def complete(self, *, system: str, user: str) -> str:
        """Return assistant text for one chat turn."""


def resolve_api_key() -> str:
    for name in API_KEY_VARS:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    raise ProtocolError(
        "LLM binder/extractor needs INFONA_BINDER_API_KEY or TOGETHER_API_KEY. "
        "Refusing rather than falling back to KeywordBinder on real data."
    )


def llm_base_url() -> str:
    return (
        os.environ.get("INFONA_LLM_BASE_URL")
        or os.environ.get("INFONA_BINDER_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")


def llm_model() -> str:
    return os.environ.get("INFONA_BINDER_MODEL") or os.environ.get(
        "INFONA_LLM_MODEL", DEFAULT_MODEL
    )


PostFn = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


def _default_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            if not isinstance(raw, dict):
                raise ProtocolError("LLM response is not a JSON object")
            return raw
        except urllib.error.HTTPError as exc:
            code = exc.code
            last_exc = exc
            if code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise ProtocolError(f"LLM HTTP call failed: {exc}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise ProtocolError(f"LLM HTTP call failed: {exc}") from exc
    raise ProtocolError(f"LLM HTTP call failed: {last_exc}")


class UrllibChatClient:
    """OpenAI-compatible chat/completions. Inject ``post`` in tests."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        post: PostFn | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else resolve_api_key()
        self.base_url = (base_url or llm_base_url()).rstrip("/")
        self.model = model or llm_model()
        self._post = post or _default_post

    def complete(self, *, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Qwen3.5 thinking is on by default. Bind replies must stay type ids.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        raw = self._post(url, headers, body)
        return _assistant_text(raw)


def _assistant_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProtocolError("LLM response has no choices")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("LLM response content is empty")
    return _strip_thinking(text)


def _strip_thinking(text: str) -> str:
    """Drop a leading think block if a provider ignores enable_thinking=false."""
    stripped = text.strip()
    if "</think>" in stripped:
        stripped = stripped.split("</think>", 1)[-1].strip()
    return stripped


def bind_system_prompt(catalog: TypeCatalog) -> str:
    """Keys-only catalog. Ids are type_0 / type_1. No corpus names."""
    lines = [
        "Pick exactly one type id. Reply with that id only.",
        "Types:",
    ]
    for row in catalog.describe_for_model():
        keys = ", ".join(str(k) for k in row["keys"])
        lines.append(f"- {row['id']} keys: {keys}")
    return "\n".join(lines)


def extract_system_prompt(skill: Skill) -> str:
    require_one_skill(skill)
    return (
        "Extract values using only the procedure below. "
        "Reply with a JSON object mapping key to string. "
        "Omit missing keys. Do not invent values.\n\n"
        f"{skill.body}"
    )


def assert_llm_messages_clean(system: str, user: str) -> None:
    blob = f"{system}\n{user}"
    for lit in LEAK_LITERALS:
        if lit in ("filename", "file_path", "annotations"):
            continue
        if lit in blob:
            raise ProtocolError(f"LLM prompt leaked {lit!r}")
    for pat in LEAK_PATTERNS:
        if pat.search(blob):
            raise ProtocolError(f"LLM prompt leaked pattern {pat.pattern!r}")
    for field in ("filename", "file_path", "annotations"):
        if f'"{field}"' in blob or f"'{field}'" in blob:
            raise ProtocolError(f"LLM prompt leaked field name {field!r}")


def parse_type_id(text: str, catalog: TypeCatalog) -> str:
    allowed = set(catalog.type_ids())
    stripped = text.strip().strip("`").strip()
    if stripped in allowed:
        return stripped
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        cand = obj.get("id") or obj.get("type_id") or obj.get("type")
        if isinstance(cand, str) and cand in allowed:
            return cand
    for type_id in catalog.type_ids():
        if re_word(type_id, stripped):
            return type_id
    raise ProtocolError(f"binder reply {text!r} is not a catalog type id")


def re_word(type_id: str, text: str) -> bool:
    return f" {type_id} " in f" {text} "


def parse_extract_json(text: str, skill: Skill) -> list[Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [ln for ln in stripped.splitlines() if not ln.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    obj = None
    for i, ch in enumerate(stripped):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(stripped, i)
            except json.JSONDecodeError:
                continue
            break
    if not isinstance(obj, dict):
        raise ProtocolError("extractor reply is not a JSON object")
    allowed = set(skill.keys)
    items: list[Any] = []
    for key, val in obj.items():
        name = str(key)
        if name not in allowed:
            raise ProtocolError(f"extract emitted key {name!r} not on the bound skill")
        if val is None:
            continue
        items.append(entity_item(name, str(val)))
    assert_extract_keys_subset(skill, (item[0] for item in items))
    return items


class LlmBinder:
    def __init__(self, client: ChatClient | None = None) -> None:
        self.client = client or UrllibChatClient()

    def bind(self, prompt: str, catalog: TypeCatalog) -> str:
        system = bind_system_prompt(catalog)
        assert_llm_messages_clean(system, prompt)
        reply = self.client.complete(system=system, user=prompt)
        return parse_type_id(reply, catalog)


class LlmExtractor:
    def __init__(self, client: ChatClient | None = None) -> None:
        self.client = client or UrllibChatClient()

    def extract(self, prompt: str, skill: Skill) -> list[Any]:
        require_one_skill(skill)
        system = extract_system_prompt(skill)
        assert_llm_messages_clean(system, prompt)
        reply = self.client.complete(system=system, user=prompt)
        return parse_extract_json(reply, skill)
