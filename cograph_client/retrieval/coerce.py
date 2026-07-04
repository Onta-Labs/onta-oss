"""The ONE tolerant JSON-coercion seam every retrieval rail parses through
(ONTA-193 P4).

Discovery and enrichment each grew their own best-effort "pull JSON out of an
LLM/web reply" helper — ``web_sources/_common.extract_json_array`` (array-shaped:
a JSON array of row objects) and ``enrichment/extraction._try_parse_json``
(object-shaped: a single ``{"value", "confidence"}`` object). Same job — coerce a
loose model reply into strict JSON, tolerating code fences and surrounding prose,
never raising into the agent — but two implementations, the exact "two
JSON-coercion helpers" drift ADR 0008 called out. They now delegate here.

Two shapes, deliberately kept as two functions, because each caller's tolerant
parse differs in ways that are load-bearing and must NOT change:

* :func:`parse_json_array` — strips a leading ```` ``` ```` code fence, locates the
  OUTERMOST ``[``…``]`` (first ``[`` .. last ``]``), ``json.loads`` that slice,
  and filters to ``dict`` members. ``[]`` on any failure. This is exactly the
  behaviour every discovery provider's project step relied on.
* :func:`parse_json_object` — when the (stripped) text ALREADY starts with ``{``
  it parses the WHOLE text (so trailing prose after a complete object is a parse
  failure → ``None``, the enrichment extractor's long-standing behaviour); only
  when it does not start with ``{`` does it slice the outermost ``{``…``}``.
  Returns the ``dict`` or ``None``.

The two are a superset seam (one module, both shapes) rather than a single merged
function precisely because those tolerance rules diverge; merging them into one
code path would silently change one caller's outputs. Keeping them side by side
here removes the duplicated implementations without touching behaviour.

Boundary: OSS. Imports only stdlib (``json``). No ``from cograph.*``.
"""

from __future__ import annotations

import json
from typing import Optional


def parse_json_array(text: str) -> list[dict]:
    """Best-effort parse of a JSON array of objects from an LLM/web reply.

    Tolerant of code fences and surrounding prose; returns ``[]`` on any failure
    so a bad reply degrades to "found nothing" rather than raising into the
    agent. Filters the parsed array to its ``dict`` members.
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = "\n".join(ln for ln in s.split("\n") if not ln.strip().startswith("```"))
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def parse_json_object(text: str) -> Optional[dict]:
    """Best-effort parse of the first ``{...}`` object in ``text``.

    When the stripped text already starts with ``{`` the WHOLE text is parsed
    (so trailing prose after a complete object fails → ``None``); otherwise the
    outermost ``{``…``}`` slice is parsed. Returns the ``dict`` or ``None`` on any
    failure / non-object result.
    """
    text = text.strip()
    candidate = text
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["parse_json_array", "parse_json_object"]
