"""Bare bind/extract: OCR only. No type catalog keys and no skill body."""

from __future__ import annotations

from typing import Any

from vrdu_binder.bind import TypeCatalog
from vrdu_binder.extract import entity_item
from vrdu_binder.llm import ChatClient, UrllibChatClient, assert_llm_messages_clean, parse_type_id
from vrdu_binder.protocol import ProtocolError, require_one_skill
from vrdu_binder.skills import Skill

BARE_BIND_SYSTEM = (
    "Reply with exactly one id: type_0 or type_1.\n"
    "Reply with the id only."
)
BARE_EXTRACT_SYSTEM = (
    "Extract a JSON object mapping field names to string values "
    "from the document tokens. Omit missing keys. Do not invent values."
)


def assert_bare_system(system: str, *, catalog: TypeCatalog | None = None) -> None:
    """Fail if a catalog key list or a skill procedure leaked into bare text."""
    lowered = system.lower()
    if "keys:" in lowered or "worked examples" in lowered:
        raise ProtocolError("bare prompt leaked a type catalog or skill procedure")
    if catalog is not None:
        for keys in catalog.keys_by_type.values():
            # A single official key can appear in OCR; this checks the system
            # string, which must not list the schema.
            listed = sum(1 for key in keys if f"- {key}" in system or f"{key}," in system)
            if listed >= 3:
                raise ProtocolError("bare prompt listed type catalog keys")


class BareBinder:
    """Bind without injecting the Infona key catalog."""

    def __init__(self, client: ChatClient | None = None) -> None:
        self.client = client or UrllibChatClient()

    def bind(self, prompt: str, catalog: TypeCatalog) -> str:
        system = BARE_BIND_SYSTEM
        assert_llm_messages_clean(system, prompt)
        assert_bare_system(system, catalog=catalog)
        reply = self.client.complete(system=system, user=prompt)
        return parse_type_id(reply, catalog)


class BareExtractor:
    """Extract without injecting a skill body. Skill keys are a post-filter."""

    def __init__(self, client: ChatClient | None = None) -> None:
        self.client = client or UrllibChatClient()

    def extract(self, prompt: str, skill: Skill) -> list[Any]:
        require_one_skill(skill)
        system = BARE_EXTRACT_SYSTEM
        if skill.body and skill.body.strip() and skill.body.strip() in system:
            raise ProtocolError("bare extract must not include the skill body")
        assert_llm_messages_clean(system, prompt)
        assert_bare_system(system)
        reply = self.client.complete(system=system, user=prompt)
        return _parse_bare_extract(reply, skill)


def _parse_bare_extract(text: str, skill: Skill) -> list[Any]:
    """Keep keys that belong to the bound type. Drop the rest (no dump-both)."""
    import json

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
        raise ProtocolError("bare extractor reply is not a JSON object")
    allowed = set(skill.keys)
    items: list[Any] = []
    for key, val in obj.items():
        name = str(key)
        if name not in allowed or val is None:
            continue
        items.append(entity_item(name, str(val)))
    return items
