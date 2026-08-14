"""Shared constants and call-time host lookup for the enrich capability.

Owns process-wide background-task strong-refs (``_bg_tasks`` / ``_spawn``)
and the small scope-value splitter. Implementation of plan/execute lives in
sibling ``enrich_*.py`` modules.

Invariants other agents must not break:
- Look up monkeypatched names on the public ``enrich_cap`` module at call
  time via :func:`_host` (``openrouter_chat``, ``logger``, ``_spawn``,
  ``list_type_schema``, ``sample_predicate_values``, ``_list_types``,
  ``_extract_enrich_request``, ``refresh_after_write``, env flags). A missed
  lookup is the usual cause of a hanging test after extract.
- This rail does not write the graph. The executor the job drives must stay
  on ``insert_facts`` / ``refresh_after_write``; instance edges on
  ``onto/<leaf>``; entity IRIs via ``entity_uri``.
"""

from __future__ import annotations

import asyncio
import re

import structlog

logger = structlog.stdlib.get_logger("infona.agent.enrich")

_bg_tasks: set[asyncio.Task] = set()

# Conservative default cap so a large/expensive enrich is BOUNDED by default
# (COG-123). It is written into the plan ``params`` (and the EnrichJob.limit at
# execute time) and surfaced in the preview; the user can override it. 200 keeps
# a first paid run small enough to inspect cheaply while still covering most
# scoped subsets in one pass.
_DEFAULT_PLAN_LIMIT = 200

# Outer safety cap on a resolved subset ("top N", "those", an explicit list) so a
# missing/over-broad LIMIT in the generated subset SPARQL can't fan a paid enrich
# out to thousands of entities. The subset's own N (when given) still applies; this
# only bounds the worst case.
_SUBSET_MAX = 500

# Delimiters that signal a composite (un-normalized) target value. "__" is the
# slugified list separator the ingest produces; the rest are raw list delimiters.
_COMPOSITE_DELIMS = ["__", ", ", "; ", " / ", " | "]

# A scope value that names MULTIPLE things — "OpenAI, Google, Deepgram and
# ElevenLabs", "Hoag / Kaiser / MemorialCare". The LLM extractor frequently crams
# such a list into a single ``scope.value``; matched as one literal it hits 0 rows
# (the reported persona-eval refresh gap). We split on commas / semicolons /
# slashes / pipes / the word "and" (or "&") so the scope becomes a value SET and
# each member is matched case-insensitively (value IN {…}). Split is done ONLY
# when a delimiter is present, so an ordinary single value ("titanium", "Manager",
# "Persian") is never fragmented.
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|/|\||\band\b|&)\s*", re.IGNORECASE)


def _host():
    """Call-time lookup of the public enrich_cap module (monkeypatch surface)."""
    from infona_client.agent.capabilities import enrich_cap as _mod

    return _mod


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _split_scope_values(value: str) -> list[str]:
    """Split a delimited scope value into its members, or ``[]`` if it names one.

    "OpenAI, Google, Deepgram and ElevenLabs" -> the four names; "titanium" -> []
    (no delimiter → a single value, left on the normal single-value scope path).
    De-duped case-insensitively, order-preserving, each trimmed of surrounding
    quotes/whitespace. A trailing/serial-comma "and" ("A, B, and C") collapses to
    three, not four (empty fragments are dropped).
    """
    if not isinstance(value, str) or not value.strip():
        return []
    parts = [
        p.strip().strip("\"'").strip()
        for p in _LIST_SPLIT_RE.split(value)
    ]
    members: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            members.append(p)
    # Only treat it as a LIST when it actually decomposed into 2+ members.
    return members if len(members) >= 2 else []
