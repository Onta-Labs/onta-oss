"""One-line AI summary of a knowledge graph, derived from its type breakdown.

The dashboard and Explorer want a human "what is this graph about?" line for
each KG. We synthesize it from the KG's entity types + counts (the same
``type_breakdown`` the stats store already holds) with one small LLM call, and
persist it on the :class:`~cograph_client.graph.kg_stats_store.KgStats` row so
listing a tenant's KGs stays a tiny relational read — the description is computed
once (at stats-recompute time, or lazily on first list) and reused until the
graph's type set changes.

Everything here is **best-effort**: no OpenRouter key, an empty breakdown, or any
LLM/parse error yields ``""`` — a missing description must never fail a
recompute or a KG listing. The generation funnels through the shared
:mod:`cograph_client.resolver.llm_router` seam like every other OSS LLM call, so
the provider/model/fallback knobs (``OMNIX_LLM_MODEL`` etc.) apply uniformly.
"""

from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

# How many entity types (highest count first) to show the model. A dozen is
# plenty of signal for a one-liner while keeping the prompt tiny/cheap.
_MAX_TYPES = 12
# Hard cap on the returned line so a runaway reply can't bloat a stored row.
_MAX_CHARS = 140

_SYSTEM = (
    "You write a single short label describing what a knowledge graph is about, "
    "given its entity types and their counts. Reply with ONE line, at most about "
    "10 words, in plain text — no quotes, no trailing period, no preamble, and do "
    "not start with 'This graph' or 'A knowledge graph'. Name the real-world "
    "domain the data is about, not the schema."
)


def _openrouter_key() -> str:
    """The OpenRouter key, from settings then a plain env fallback.

    Same source the rest of OSS uses (``normalization.inference._openrouter_key``)
    so one configuration lights up every LLM call site.
    """
    from cograph_client.config import settings

    return settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")


def should_generate_summary(
    existing: str,
    old_breakdown: dict[str, int],
    new_breakdown: dict[str, int],
) -> bool:
    """Whether a KG's one-line summary should be (re)generated.

    Regenerate only when there's something to describe (``new_breakdown``
    non-empty) AND either no summary exists yet OR the *set of entity types*
    changed — the summary describes the graph's domain, which is a function of
    its types, not of per-type counts. This keeps enrichment writes (which fill
    attributes on existing types) from triggering a needless regeneration on
    every ingest, while a genuinely new type set gets a fresh line.
    """
    if not new_breakdown:
        return False
    if not existing.strip():
        return True
    return set(old_breakdown) != set(new_breakdown)


def _clean(text: str) -> str:
    """Normalize a raw model reply into a single tidy line."""
    line = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    # Drop wrapping quotes the model sometimes adds despite instructions.
    if len(line) >= 2 and line[0] in "\"'" and line[-1] == line[0]:
        line = line[1:-1].strip()
    line = line.rstrip(".").strip()
    if len(line) > _MAX_CHARS:
        line = line[:_MAX_CHARS].rstrip()
    return line


def _prompt(kg_name: str, breakdown: dict[str, int]) -> str:
    top = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_TYPES]
    types_block = "\n".join(f"- {name}: {count}" for name, count in top)
    return (
        f"Knowledge graph name: {kg_name}\n"
        f"Entity types (name: count):\n{types_block}\n\n"
        f"One-line description:"
    )


async def generate_kg_summary(
    kg_name: str,
    breakdown: dict[str, int],
    *,
    api_key: str | None = None,
    timeout: float = 20.0,
) -> str:
    """Generate a one-line description of a KG from its type breakdown.

    Best-effort: returns ``""`` when there's no OpenRouter key, an empty
    breakdown, or any LLM/parse error. Never raises.
    """
    if not breakdown:
        return ""
    key = api_key if api_key is not None else _openrouter_key()
    if not key:
        logger.debug("no_openrouter_key_for_kg_summary", kg=kg_name)
        return ""
    # Lazy import keeps module import cheap and avoids any graph→resolver import
    # cycle at load time (mirrors graph/text_markers.py).
    from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat

    try:
        text = await openrouter_chat(
            key,
            _SYSTEM,
            _prompt(kg_name, breakdown),
            model=PRIMARY_MODEL,
            temperature=0.0,
            max_tokens=60,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 — a summary is never worth failing a caller
        logger.warning("kg_summary_llm_failed", kg=kg_name, exc_info=True)
        return ""
    return _clean(text if isinstance(text, str) else "")
