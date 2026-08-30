"""OCR-token prompts. Filename, path, and annotations never enter the string."""

from __future__ import annotations

from typing import Any, Mapping

from vrdu_binder.protocol import ProtocolError, assert_ocr_only_prompt, sanitize_bind_prompt


def ocr_tokens_only(doc: Mapping[str, Any]) -> str:
    """Read published ``ocr`` shape only: ``ocr.text`` or page token texts.

    Published dataset.jsonl (Wang et al. / google-research-datasets/vrdu):
    ``ocr['text']`` is reading-order text; ``ocr['pages'][i]['tokens']`` holds
    per-token dicts with a text field and bbox. We accept ``text``, ``word``,
    or ``value`` on a token dict. No other document fields are read.
    """
    ocr = doc.get("ocr")
    if not isinstance(ocr, Mapping):
        raise ProtocolError("document has no ocr object")
    text = ocr.get("text")
    if isinstance(text, str) and text.strip():
        return text
    pages = ocr.get("pages")
    if not isinstance(pages, list):
        raise ProtocolError("ocr has neither text nor pages")
    pieces: list[str] = []
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        tokens = page.get("tokens") or page.get("words") or []
        if not isinstance(tokens, list):
            continue
        for tok in tokens:
            piece = _token_text(tok)
            if piece:
                pieces.append(piece)
    if not pieces:
        raise ProtocolError("ocr pages produced no tokens")
    return " ".join(pieces)


def _token_text(tok: Any) -> str:
    if isinstance(tok, str):
        return tok.strip()
    if isinstance(tok, Mapping):
        for key in ("text", "word", "value"):
            val = tok.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def bind_prompt(doc: Mapping[str, Any]) -> str:
    """Prompt for the bind model: OCR tokens, then a leak check."""
    prompt = sanitize_bind_prompt(ocr_tokens_only(doc), doc)
    assert_ocr_only_prompt(prompt, doc)
    return prompt
