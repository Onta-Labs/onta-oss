"""Shared tier resolution + the web confidence floor (COG-124).

This module is the SINGLE place that decides, for a free-vs-paid enrichment:

* :func:`resolve_auto_tier` — given the requested ``attributes`` on a ``type``,
  decide whether the FREE Wikidata source (``lite``) is good enough or whether to
  reach for PAID live web search (``core``). Reuses the same web-fact judgment the
  agent path already uses (an LLM classify call grounded in the existing web-fact
  guidance) and falls back to a deterministic keyword heuristic when there is no
  LLM key or the call errors/times out. The 3-way outcome is
  ``lite`` / ``core`` / "needs clarification".

* :func:`resolve_chain_cost` / :func:`chain_has_paid` — derive GENERICALLY (from
  adapter-declared metadata, never adapter names — boundary rule COG-123) whether
  a tier's chain contains a paid/web adapter, so callers can apply the web
  confidence floor.

* :data:`WEB_CONFIDENCE_MIN` / :data:`DEFAULT_CONFIDENCE_MIN` — the single source
  of truth for the floor and the "user did not set a confidence" sentinel.

Boundary-clean: this module only ever chooses a tier NAME and reads adapter
*metadata*; it NEVER imports or references a paid adapter, and it never imports
``cograph.*``. It is importable by the ``/enrich`` route WITHOUT pulling in the
heavy agent capability chain (so no circular import).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import structlog

from cograph_client.enrichment.models import EnrichmentTier
from cograph_client.enrichment.sources.base import adapter_cost, get_adapter
from cograph_client.enrichment.tiers import get_chain
from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat

logger = structlog.stdlib.get_logger("cograph.enrichment.tier_router")

# Functional confidence floor for web-sourced enrichments (COG-121). Web adapters
# (Exa/Parallel/…) return verdicts at a low, conservative prior, so the global
# EnrichRequest default of 0.85 silently filters ALL of them → 0 values written.
# This floor is conservative (still rejects junk) but low enough that calibrated
# web verdicts actually land. This is the SINGLE definition — the agent path and
# the route both import it from here so the number never drifts.
WEB_CONFIDENCE_MIN = 0.4

# The global EnrichRequest default; the sentinel for "user did not ask for a
# specific confidence" so callers only override an UNSET (default) confidence.
DEFAULT_CONFIDENCE_MIN = 0.85


def resolve_chain_cost(tier: EnrichmentTier) -> tuple[float, int, bool]:
    """Per-entity paid cost for a tier, derived GENERICALLY from adapter metadata.

    Resolves the tier's adapter chain (:func:`get_chain`), looks up each adapter
    in the global registry, and sums the declared ``cost_per_call`` of the PAID
    adapters (:func:`adapter_cost`). Returns
    ``(per_entity_paid_cost, paid_adapter_count, has_paid)``.

    Boundary-clean (COG-123): "paid" and "how much" come ONLY from what an adapter
    declares about itself — never a hardcoded adapter name. The OSS Wikidata
    adapter declares free, so the OSS-only ``lite`` chain costs 0; a downstream
    deployment that registers a paid adapter (Exa/Parallel/…) with
    ``is_paid``/``cost_per_call`` gets a non-zero estimate with no OSS change. The
    "cache" pseudo-entry in a chain is not an adapter and is skipped, mirroring the
    executor's ``_lookup_chain``. An unregistered adapter name contributes nothing
    (it can't run, so it can't cost) — same as the executor skipping it.
    """
    per_entity_cost = 0.0
    paid_adapters = 0
    for name in get_chain(tier):
        if name == "cache":
            continue
        adapter = get_adapter(name)
        if adapter is None:
            continue
        is_paid, cost = adapter_cost(adapter)
        if is_paid:
            paid_adapters += 1
            per_entity_cost += cost
    return per_entity_cost, paid_adapters, paid_adapters > 0


def chain_has_paid(tier: EnrichmentTier) -> bool:
    """True when the tier's resolved chain contains a paid/web adapter."""
    return resolve_chain_cost(tier)[2]


@dataclass
class TierDecision:
    """Outcome of :func:`resolve_auto_tier`.

    ``resolved_tier`` is ``"lite"`` or ``"core"`` when a tier was chosen; it is
    ``None`` only when ``needs_clarification`` is True (genuinely ambiguous —
    the caller should ask the user rather than create a job).
    """

    resolved_tier: str | None
    needs_clarification: bool = False
    candidates: list[str] = field(default_factory=list)
    routing_note: str = ""


# Open-web / person / company / product facts the FREE Wikidata tier usually can't
# answer well — these should default to the paid web ``core`` tier. Mirrors the
# agent's ``_WEB_FACT_HINTS`` (kept here so the heuristic has zero agent imports).
_WEB_FACT_HINTS = {
    "company", "employer", "organization", "organisation", "website", "url",
    "homepage", "description", "bio", "summary", "reviews", "rating", "founder",
    "headquarters", "hq", "location", "address", "email", "phone", "title",
    "role", "position", "industry", "revenue", "funding", "ceo", "linkedin",
}

# Structured, catalogued identifiers Wikidata reliably holds. A clear match here
# is the ONLY signal that flips the deterministic heuristic to ``lite``; anything
# else leans ``core`` (paid web) so a person/company lookup isn't silently
# downgraded to a Wikidata miss.
_STRUCTURED_ID_HINTS = {
    "isbn", "iso", "iso_code", "isocode", "country_code", "currency",
    "founded", "founding_date", "inception", "release_year", "release_date",
    "population", "capital", "continent", "latitude", "longitude", "coordinates",
    "wikidata_id", "wikidata", "imdb_id", "doi", "gtin", "barcode",
}

_CLASSIFY_SYSTEM = """\
You route an enrichment request to the cheapest source that can actually answer \
it. Given a type and the attribute(s) a user wants to fill in, decide whether \
those facts are best served by:

- "lite": the FREE Wikidata source. Use ONLY for structured, catalogued \
identifiers Wikidata reliably holds for WELL-KNOWN entities — e.g. a country's \
ISO code, a film's release year, a public company's founding date, a city's \
population. These are the kinds of facts a curated public knowledge base keeps.

- "core": PAID live web search. Use for OPEN-WEB / fresh / niche facts and for \
facts about people, private companies, products, or roles that Wikidata does NOT \
reliably hold — employer, current company, website, description, bio, reviews, \
rating, founder, headquarters, email, phone, title, role, industry, revenue, etc.

Return STRICT JSON only (no markdown), exactly:
{"tier": "lite" | "core", "confident": true | false, "reason": "<short>"}

RULES:
- Only set "confident": false when you genuinely cannot tell whether Wikidata \
would hold this. If Wikidata is LIKELY weak / thin / missing for these \
attributes, choose "core" with "confident": true (LEAN PAID).
- Default toward "core" for anything that is not a clearly catalogued structured \
fact about a well-known entity. Wikidata-only "lite" is opt-in, not the silent \
default for a web lookup.
- Do NOT set "confident": false just because a fact is hard to find — that is a \
reason to pick "core", not to ask for clarification."""

_CLASSIFY_USER_TEMPLATE = """\
Type: {type_name}
Attributes to enrich: {attributes}

Classify the best source as strict JSON."""


async def resolve_auto_tier(
    attributes: list[str],
    type_name: str,
    openrouter_key: str | None,
    timeout_s: float = 8.0,
) -> TierDecision:
    """Decide ``lite`` vs ``core`` for an ``auto``-tier enrichment request.

    Strategy (3-way):

    * With an ``openrouter_key``: one focused, bounded LLM classify call adapted
      from the existing web-fact guidance. It returns
      ``{"tier","confident","reason"}``:

      - ``confident:true`` + ``lite`` → resolved ``lite``.
      - ``confident:true`` + ``core`` → resolved ``core``.
      - ``confident:false``           → ``needs_clarification`` (ambiguous; the
        caller should ask rather than guess), ``candidates=["lite","core"]``.

      The prompt LEANS PAID: ambiguity about whether Wikidata holds a fact is a
      reason to pick ``core`` confidently, not to ask for clarification.

    * Without a key OR on any LLM error/timeout: a deterministic keyword heuristic
      picks a tier (structured-ID-ish attribute names → ``lite``; otherwise →
      ``core``, leaning paid). The heuristic NEVER returns ``needs_clarification``
      — it always lands on a concrete tier.

    Always returns a ``routing_note`` explaining the choice. Never raises.
    """
    attrs = [a for a in (attributes or []) if a]
    if openrouter_key:
        try:
            user = _CLASSIFY_USER_TEMPLATE.format(
                type_name=type_name or "(unknown type)",
                attributes=", ".join(attrs) or "(none)",
            )
            text = await openrouter_chat(
                openrouter_key,
                _CLASSIFY_SYSTEM,
                user,
                model=PRIMARY_MODEL,
                temperature=0,
                max_tokens=200,
                timeout=timeout_s,
            )
            parsed = _parse_json_object(text)
            decision = _decision_from_llm(parsed)
            if decision is not None:
                return decision
            logger.warning("auto_tier_llm_unparseable", text=(text or "")[:200])
        except Exception:  # noqa: BLE001 — any LLM/timeout error → heuristic.
            logger.warning("auto_tier_llm_failed", exc_info=True)
    # Fallback: deterministic heuristic. Never needs_clarification.
    return _heuristic_decision(attrs, type_name, had_key=bool(openrouter_key))


def _decision_from_llm(parsed: dict | None) -> TierDecision | None:
    """Build a :class:`TierDecision` from a parsed LLM reply, or None if invalid."""
    if not isinstance(parsed, dict):
        return None
    tier = str(parsed.get("tier", "")).strip().lower()
    confident = parsed.get("confident")
    reason = str(parsed.get("reason", "")).strip()
    if not isinstance(confident, bool):
        return None
    if not confident:
        return TierDecision(
            resolved_tier=None,
            needs_clarification=True,
            candidates=["lite", "core"],
            routing_note=(
                "Ambiguous whether the free Wikidata source covers these "
                "attributes — choose 'lite' (free) or 'core' (paid web search)."
                + (f" {reason}" if reason else "")
            ),
        )
    if tier not in ("lite", "core"):
        return None
    label = "free Wikidata" if tier == "lite" else "paid web search"
    return TierDecision(
        resolved_tier=tier,
        needs_clarification=False,
        routing_note=(
            f"Auto-routed to '{tier}' ({label})."
            + (f" {reason}" if reason else "")
        ),
    )


def _heuristic_decision(
    attributes: list[str], type_name: str, *, had_key: bool
) -> TierDecision:
    """Deterministic tier pick used when the LLM is unavailable/errored.

    Leans paid: only a CLEAR structured-identifier signal flips to ``lite``. Never
    returns ``needs_clarification``.
    """
    lowered = [a.lower() for a in attributes]
    structured = [a for a in lowered if _is_structured_id(a)]
    web = [a for a in lowered if a in _WEB_FACT_HINTS]

    prefix = (
        "LLM unavailable; "
        if not had_key
        else "LLM classify failed; "
    )

    # Any open-web fact present → core (a single web fact dominates the cost).
    if web:
        return TierDecision(
            resolved_tier="core",
            routing_note=(
                f"{prefix}heuristic chose 'core' (paid web search): "
                f"{', '.join(web)} are open-web facts Wikidata rarely holds."
            ),
        )
    # No web fact and we have a clear structured-identifier signal → lite (free).
    if structured and not lowered_has_unknown(lowered, structured):
        return TierDecision(
            resolved_tier="lite",
            routing_note=(
                f"{prefix}heuristic chose 'lite' (free Wikidata): "
                f"{', '.join(structured)} are structured identifiers Wikidata holds."
            ),
        )
    # Mixed (some structured, some unknown) but no explicit web fact: still lean
    # paid for the unknowns rather than risk a Wikidata miss.
    if not attributes:
        # Nothing to go on → free is the safe, no-cost default.
        return TierDecision(
            resolved_tier="lite",
            routing_note=f"{prefix}no attributes given; defaulting to 'lite' (free).",
        )
    return TierDecision(
        resolved_tier="core",
        routing_note=(
            f"{prefix}heuristic chose 'core' (paid web search): no clear "
            f"structured-identifier signal, leaning paid to avoid a Wikidata miss."
        ),
    )


def _is_structured_id(attr: str) -> bool:
    """True when an attribute name looks like a catalogued structured identifier."""
    if attr in _STRUCTURED_ID_HINTS:
        return True
    # token-level match so e.g. "iso_code" / "release year" register.
    tokens = {t for t in attr.replace("-", "_").split("_") if t}
    return bool(tokens & _STRUCTURED_ID_HINTS)


def lowered_has_unknown(lowered: list[str], structured: list[str]) -> bool:
    """True when some attribute is neither a structured-ID nor a known web fact —
    i.e. an unknown we'd rather route to paid web search than gamble on Wikidata."""
    known = set(structured) | _WEB_FACT_HINTS
    return any(a not in known and not _is_structured_id(a) for a in lowered)


def _parse_json_object(text: str) -> dict | None:
    """Best-effort parse of an LLM JSON object reply (tolerant of code fences)."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            l for l in stripped.split("\n") if not l.strip().startswith("```")
        )
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        stripped = stripped[start : end + 1]
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None
