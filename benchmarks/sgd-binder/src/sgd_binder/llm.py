"""OpenAI-compatible bind/extract. No keyword fallback on real data."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Protocol

from sgd_binder.constants import MODEL_08B, TOGETHER_BASE_URL, TOGETHER_USER_AGENT
from sgd_binder.protocol import ProtocolError
from sgd_binder.schema import TypeCatalog, assert_no_leaks
from sgd_binder.skills import Skill

API_KEY_VARS = ("INFONA_BINDER_API_KEY", "TOGETHER_API_KEY")


class ChatClient(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...


def resolve_api_key() -> str:
    for name in API_KEY_VARS:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    raise ProtocolError(
        "needs INFONA_BINDER_API_KEY or TOGETHER_API_KEY; no keyword fallback"
    )


def llm_base_url() -> str:
    return (
        os.environ.get("INFONA_LLM_BASE_URL")
        or os.environ.get("INFONA_BINDER_BASE_URL")
        or TOGETHER_BASE_URL
    ).rstrip("/")


PostFn = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


def _default_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            if not isinstance(raw, dict):
                raise ProtocolError("LLM response is not a JSON object")
            return raw
        except urllib.error.HTTPError as exc:
            last = exc
            snippet = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise ProtocolError(f"LLM HTTP {exc.code}: {snippet}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt < 3:
                time.sleep(2**attempt)
                continue
            raise ProtocolError(f"LLM HTTP call failed: {exc}") from exc
    raise ProtocolError(f"LLM HTTP call failed: {last}")


class UrllibChatClient:
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
        self.model = model or os.environ.get("INFONA_BINDER_MODEL") or MODEL_08B
        self._post = post or _default_post

    def complete(self, *, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": TOGETHER_USER_AGENT,
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "chat_template_kwargs": {"enable_thinking": False},
        }
        raw = self._post(url, headers, body)
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProtocolError("LLM response has no choices")
        msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
        text = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("LLM response content is empty")
        if "</think>" in text:
            text = text.split("</think>", 1)[-1]
        return text.strip()


def bind_system(catalog: TypeCatalog) -> str:
    lines = ["Pick exactly one type id. Reply with that id only.", "Types:"]
    for row in catalog.describe_for_model():
        keys = ", ".join(row["keys"])
        lines.append(f"- {row['id']} keys: {keys}")
    return "\n".join(lines)


def parse_type_id(text: str, catalog: TypeCatalog) -> str:
    allowed = set(catalog.type_ids())
    stripped = text.strip().strip("`")
    if stripped in allowed:
        return stripped
    for type_id in catalog.type_ids():
        if f" {type_id} " in f" {stripped} ":
            return type_id
    raise ProtocolError(f"binder reply {text!r} is not a catalog type id")


def parse_extract(text: str, skill: Skill) -> dict[str, str]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            ln for ln in stripped.splitlines() if not ln.strip().startswith("```")
        ).strip()
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
    out: dict[str, str] = {}
    for key, val in obj.items():
        name = str(key)
        if name not in allowed:
            raise ProtocolError(f"extract emitted key {name!r} not on the bound skill")
        if val is None:
            continue
        out[name] = str(val)
    return out


class InfonaBinder:
    def __init__(self, client: ChatClient, catalog: TypeCatalog, needles: tuple[str, ...]):
        self.client = client
        self.catalog = catalog
        self.needles = needles

    def bind(self, prompt: str) -> str:
        system = bind_system(self.catalog)
        assert_no_leaks(system + "\n" + prompt, self.needles)
        return parse_type_id(self.client.complete(system=system, user=prompt), self.catalog)


class InfonaExtractor:
    def __init__(self, client: ChatClient, skills: Mapping[str, Skill], needles: tuple[str, ...]):
        self.client = client
        self.skills = skills
        self.needles = needles

    def extract(self, prompt: str, type_id: str) -> dict[str, str]:
        skill = self.skills[type_id]
        assert_no_leaks(skill.body + "\n" + prompt, self.needles)
        return parse_extract(
            self.client.complete(system=skill.body, user=prompt), skill
        )


class BareBinder:
    def __init__(self, client: ChatClient, catalog: TypeCatalog, needles: tuple[str, ...]):
        self.client = client
        self.catalog = catalog
        self.needles = needles

    def bind(self, prompt: str) -> str:
        ids = ", ".join(self.catalog.type_ids())
        system = f"Reply with exactly one id: {ids}. Reply with the id only."
        assert_no_leaks(system + "\n" + prompt, self.needles)
        if "keys:" in system.lower():
            raise ProtocolError("bare bind leaked catalog keys")
        return parse_type_id(self.client.complete(system=system, user=prompt), self.catalog)


class BareExtractor:
    def __init__(self, client: ChatClient, skills: Mapping[str, Skill], needles: tuple[str, ...]):
        self.client = client
        self.skills = skills
        self.needles = needles

    def extract(self, prompt: str, type_id: str) -> dict[str, str]:
        skill = self.skills[type_id]
        system = (
            "Extract a JSON object mapping field names to string values. "
            "Omit missing keys. Do not invent values."
        )
        assert_no_leaks(system + "\n" + prompt, self.needles)
        try:
            return parse_extract(self.client.complete(system=system, user=prompt), skill)
        except ProtocolError:
            return {}
