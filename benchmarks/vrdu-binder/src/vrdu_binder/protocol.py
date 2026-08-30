"""Leak-honest protocol helpers: redaction, opaque ids, freeze errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from vrdu_binder.constants import LEAK_LITERALS, LEAK_PATTERNS


class ProtocolError(ValueError):
    """A freeze rule was about to be broken."""


@dataclass(frozen=True)
class OpaqueDoc:
    """Document as the bind/extract models may see it."""

    opaque_id: str
    ocr_tokens: str


def opaque_id(index: int) -> str:
    return f"doc_{index:04d}"


def iter_harness_leaks(doc: Mapping[str, Any]) -> Iterator[str]:
    """Strings the harness must not copy into a bind prompt.

    Document tokens may coincidentally contain some of these. Callers pass a
    filename that is absent from OCR when they want a hard assertion.
    """
    filename = str(doc.get("filename") or "")
    file_path = str(doc.get("file_path") or "")
    if filename:
        yield filename
    if file_path:
        yield file_path
        for part in ("registration-form/", "ad-buy-form/"):
            if part in file_path:
                yield part
    annotations = doc.get("annotations")
    if annotations is not None:
        for planted in _planted_annotation_texts(annotations):
            yield planted


def _planted_annotation_texts(annotations: Any) -> Iterator[str]:
    if not isinstance(annotations, list):
        return
    for item in annotations:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        for cand in item[1]:
            if isinstance(cand, (list, tuple)) and cand:
                text = cand[0]
                if isinstance(text, str) and text.startswith("LEAK_"):
                    yield text


def sanitize_bind_prompt(raw: str, doc: Mapping[str, Any]) -> str:
    """Drop filename, path prefixes, and dataset names from OCR text."""
    text = raw
    for field in ("filename", "file_path"):
        val = doc.get(field)
        if isinstance(val, str) and val:
            text = text.replace(val, " ")
    for lit in ("registration-form/", "ad-buy-form/", "registration-form", "ad-buy-form"):
        text = text.replace(lit, " ")
    for pat in LEAK_PATTERNS:
        text = pat.sub(" ", text)
    return " ".join(text.split())


def assert_ocr_only_prompt(prompt: str, doc: Mapping[str, Any]) -> None:
    """Fail if harness metadata from ``doc`` landed in ``prompt``."""
    if not isinstance(prompt, str):
        raise ProtocolError("bind prompt must be a string of OCR tokens")
    for leak in iter_harness_leaks(doc):
        if leak and leak in prompt:
            raise ProtocolError(f"bind prompt leaked harness field {leak!r}")
    for lit in LEAK_LITERALS:
        if lit in ("filename", "file_path", "annotations"):
            continue
        if lit in prompt:
            raise ProtocolError(f"bind prompt leaked corpus marker {lit!r}")
    for pat in LEAK_PATTERNS:
        if pat.search(prompt):
            raise ProtocolError(f"bind prompt leaked pattern {pat.pattern!r}")


def assert_skill_body_clean(body: str) -> None:
    """Skill text: official keys + procedure. No corpus/type nicknames."""
    lowered = body.lower()
    for lit in (
        "registration-form",
        "ad-buy-form",
        "sk_reg",
        "sk_adbuy",
        "mixed_template",
        "type_0",
        "type_1",
        "skill_0",
        "skill_1",
    ):
        if lit in lowered:
            raise ProtocolError(f"skill body contains forbidden token {lit!r}")
    for pat in LEAK_PATTERNS:
        if pat.search(body):
            raise ProtocolError(f"skill body leaked pattern {pat.pattern!r}")


def require_one_skill(skill: Any) -> None:
    if skill is None:
        raise ProtocolError("extract requires exactly one skill")
    if isinstance(skill, (list, tuple, set, dict)):
        raise ProtocolError("extract may include only the bound skill, not a collection")
