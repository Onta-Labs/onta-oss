"""Resolve the discovery spec: entity type, attributes, search subject.

LLM path via ``openrouter_chat`` (looked up on the host module so tests
that patch ``web_ingest_cap.openrouter_chat`` keep working). Degrades to
a deterministic fallback — never 500s.
"""
from __future__ import annotations

from typing import Optional

from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.resolver.llm_router import PRIMARY_MODEL
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_text import (
    _as_list,
    _clean_query,
    _current_request,
    _dedupe,
    _explicit_user_fields,
    _explicit_user_type,
    _parse_json_object,
    _pascal,
    _slug,
)
from infona_client.agent.capabilities.web_ingest_plan_enum import _norm_subqueries

_SPEC_SYSTEM = """\
You plan a web-discovery ingest: the user wants to pull a NEW set of records from \
the web and add them to a knowledge graph. Read the whole conversation for context, \
but treat the user's CURRENT (latest) request as the PRIMARY intent — earlier turns \
only fill gaps it leaves and must NEVER override the entity type, fields, or search \
subject the current request names. Output STRICT JSON only (no markdown):
{
  "entity_type": "<PascalCase singular type for the records, e.g. Model, Company, Drug>",
  "key_attribute": "<the natural identifier, usually 'name', snake_case>",
  "query": "<a clean, concise SEARCH SUBJECT — the thing to find on the web, with all conversational framing removed>",
  "query_kind": "<'place' when the records are physical places / businesses / real-world locations to find; otherwise null>",
  "subqueries": ["<2-6 SELF-CONTAINED sub-queries that PARTITION an enumeration ask; [] for a single-list ask>"],
  "confirmed_attributes": ["<attributes the user EXPLICITLY named; [] if they only named the entity>"],
  "core_attributes": ["<the 2-4 MOST IMPORTANT attributes for this entity — a strict subset of suggested_attributes, snake_case, excluding the key; these are PRE-SELECTED and shown to the user>"],
  "suggested_attributes": ["<a COMPREHENSIVE set (6-12) of web-discoverable columns for this entity, snake_case, excluding the key>"]
}
RULES:
- query: the SUBJECT to search for, NOT the user's literal sentence. Strip \
questions, meta-framing and filler. "can we ingest open-router's TTS models that \
it currently offers" -> "OpenRouter text-to-speech (TTS) models". "I'm looking \
for a list of models offered by OpenRouter" -> "models offered by OpenRouter". \
Keep it short and specific; do NOT include words like "ingest", "add", "list of", \
"can we", "I'm looking for".
- entity_type: specific but clean — "a list of models offered by OpenRouter" -> \
"Model" (prefer the domain term the user used; singular).
- query_kind: set to "place" ONLY when the records are physical places, \
businesses, venues, or real-world locations you would find on a map — restaurants, \
coffee shops, hotels, stores, clinics, gyms, parks, landmarks, offices \
("coffee shops in SF", "hardware stores near Austin", "urgent care clinics in \
Boston"). Otherwise set it to null. NON-place examples (null): "top LLMs", "S&P \
500 companies", "Nobel laureates", "npm packages", "movies from 2020" — these are \
not physical locations even when they mention an organization. When unsure, use \
null.
- subqueries: for an ENUMERATION / population inventory ask — the user wants \
ALL/every instance of a class across a scope that no single search page covers \
well. Partition into 2-6 SELF-CONTAINED queries, each complete on its own and \
together covering the whole ask with minimal overlap. Prefer AUTHORITATIVE list \
phrasing ("List of …") so directory/wiki/registry pages rank first. Partition by \
(a) region/city when the scope names several places, (b) natural subtypes when the \
class has them (universities vs colleges; coffee shops vs bakeries), and/or (c) \
complementary inventory angles (public vs private; accredited registry vs directory) \
when the scope is a single large region/population. Examples: "all primary care \
physicians in Tustin and Santa Ana" -> ["List of primary care physicians in \
Tustin, CA", "List of primary care physicians in Santa Ana, CA"]. "universities \
in British Columbia" / "BC universities" -> ["List of universities in British \
Columbia", "List of colleges in British Columbia", "List of public universities \
in British Columbia"]. "coffee shops and bakeries in SF" -> ["List of coffee \
shops in San Francisco", "List of bakeries in San Francisco"]. A non-inventory \
ask that one catalogue already returns whole ("OpenRouter models", "S&P 500 \
companies", a single named product line) needs NO partitioning -> []. When \
unsure whether it is a population inventory, PARTITION (prefer recall).
- key_attribute: the human-readable identifier (name/title), snake_case.
- confirmed_attributes: ONLY what the user actually asked for. "models with their \
names and pricing" -> ["name","pricing"]; "a list of models" -> []. When the user \
replies with a list (e.g. "Use these: name, provider, pricing" or "just the name") \
treat THOSE as confirmed. snake_case; exclude nothing they named.
- core_attributes: a SHORT list (aim for 2-4) of the few attributes that matter \
MOST for THIS entity in THIS ask — the ones a user almost always wants and that \
best identify or differentiate a record. MUST be a subset of suggested_attributes. \
These are PRE-SELECTED chips; the rest of suggested_attributes stays a \
behind-the-scenes fetch hint. Choose from the domain the user named (do NOT \
default every "Model" to LLM fields like context_length — a speech/TTS/audio \
model wants modality/pricing/languages; a physician wants specialty/city/phone; \
a package wants version/license/downloads). Keep it minimal — do NOT just repeat \
all of suggested_attributes.
- suggested_attributes: a COMPREHENSIVE set (aim for 6-12) of the columns this \
entity is typically described by ON THE WEB for the user's domain — every \
web-discoverable property a rich source table would carry, snake_case, EXCLUDING \
the key. This is the FETCH hint: the provider projects rows to it, so a thin list \
silently drops the rest of the table before extraction. Be generous and include \
any recurring provider/vendor/organization column and any score/rating/price/ \
ranking/modality column relevant to the ask (those become reified entities \
downstream). Match the modality of the ask (text LLM vs speech vs vision vs \
product vs person) — do not force LLM-only columns onto non-LLM domains."""


# How many leading named fields the DETERMINISTIC fallback pre-selects as the
# "core" recommendation (mirrors the LLM path's short core set). Kept small so a
# long field list doesn't pre-check every column.
_FALLBACK_CORE_MAX = 4

# The user-facing note attached to a GENUINELY degraded spec — the resolver LLM was
# unavailable AND no explicit field list could be recovered, so discovery falls back
# to a bare name/description capture. Surfaced (not swallowed) so the user learns the
# planning degraded and can re-state the fields they want, instead of silently
# receiving a thin dataset.
_DEGRADED_NOTE = (
    "Automated field planning was unavailable, so I set up a basic "
    "name/description capture. Tell me the specific fields you want "
    '(e.g. "with field_a, field_b, field_c") and I\'ll collect those too.'
)


def _fallback_spec(instruction: str) -> dict:
    """Deterministic spec for when the resolver LLM is unavailable / errored / timed
    out / returned nothing usable — NEVER the bare ``[name, description, url]``
    default that silently drops a field list the user explicitly named (the
    persona-eval RCA: a ~15s spec-LLM timeout thinned a fully-specified ask to
    name/description, so the NPI/taxonomy/affiliation fields the user listed never
    landed).

    Recovers the enumerated fields + the named type straight from the message with
    the SAME deterministic parsers the plan-time floor uses, WEIGHTING the current
    request: current-turn fields lead (earlier turns only fill the gaps they leave),
    and the current turn's type / search subject win over a stale earlier turn's.
    When no field list can be parsed at all the spec still degrades to name/
    description, but SURFACES it (``degraded`` + ``degraded_note``) instead of
    thinning silently, so the caller can tell the user rather than quietly hand back
    a thin dataset. When the LLM path succeeds it may ENRICH this set — it must never
    shrink below the fields recovered here."""
    current = _current_request(instruction)
    # Current-turn fields FIRST (weighted), then any additional the earlier turns
    # named — a union, so no explicitly-named field is ever lost, but the current
    # ask leads the ordering. ``_explicit_user_fields`` already scans the whole
    # instruction; the current-first splice is what makes the latest request
    # dominate rather than a stale earlier list.
    fields = _dedupe(
        [*_explicit_user_fields(current), *_explicit_user_fields(instruction)]
    )
    # Type: the current turn wins; the whole instruction only fills a gap.
    etype = _explicit_user_type(current) or _explicit_user_type(instruction)
    # Search SUBJECT: let the CURRENT turn drive it ONLY when that turn actually
    # names a subject/type (a genuine PIVOT like "actually discover Beta records
    # with …"). A bare confirmation ("yes go ahead") or a "Use these: …" chip reply
    # is NOT a subject — but its ``_clean_query`` is still truthy, so a naive
    # ``current or instruction`` would search the web for the confirmation/chip text
    # and return an empty/garbage dataset (reviewer-reproduced regression on the
    # multi-turn confirm path). Gating on ``_explicit_user_type(current)`` falls back
    # to the ORIGINAL first-line ask on any confirm/chip turn while still honoring a
    # real pivot turn.
    query = (
        _clean_query(current) if _explicit_user_type(current) else ""
    ) or _clean_query(instruction)
    key = "name"
    if fields:
        suggested = [a for a in fields if a and a != key]
        return {
            "entity_type": etype or "WebRecord",
            "key_attribute": key,
            "query": query,
            # No LLM ran → no kind classification (general default provider).
            "query_kind": None,
            "subqueries": [],
            "confirmed_attributes": fields,
            "core_attributes": suggested[:_FALLBACK_CORE_MAX],
            "suggested_attributes": suggested,
            "degraded": False,
        }
    # No explicit field list to recover → genuinely degraded. Keep a recovered type
    # if the user named one; SURFACE the thinning so it is not silent.
    return {
        "entity_type": etype or "WebRecord",
        "key_attribute": key,
        "query": query,
        "query_kind": None,
        "subqueries": [],
        "confirmed_attributes": [],
        "core_attributes": ["description"],
        "suggested_attributes": ["name", "description", "url"],
        "degraded": True,
        "degraded_note": _DEGRADED_NOTE,
    }


async def _resolve_spec(ctx: AgentContext, instruction: str) -> dict:
    """LLM-resolve {entity_type, key_attribute, confirmed/suggested attributes}.

    Degrades to a DETERMINISTIC fallback spec (``_fallback_spec``) when there is no
    LLM key or the call errors / times out / returns nothing usable, so the turn
    never 500s AND an explicitly-named field list is never silently dropped by a
    resolver timeout — the fallback recovers the user's fields + type from the
    CURRENT request instead of collapsing to a bare name/description default.
    """
    if ctx.openrouter_key:
        try:
            text = await _wic.openrouter_chat(
                ctx.openrouter_key,
                _SPEC_SYSTEM,
                instruction,
                model=PRIMARY_MODEL,
                temperature=0,
                max_tokens=400,
                # Kept well under the preview budget: this small spec call runs
                # BEFORE the sample fetch, so a slow one eats the sample's time.
                # On timeout _resolve_spec degrades to the fallback spec, never 500s.
                timeout=15,
            )
            parsed = _parse_json_object(text)
            if parsed:
                return _normalize_spec(parsed)
            # Non-empty text that didn't parse as a JSON object — the exception path
            # below never sees this, so surface it instead of a SILENT fall-through
            # to _fallback_spec (a future non-JSON degrade would otherwise vanish).
            _wic.logger.warning("web_ingest_spec_unparsed")
        except Exception:  # noqa: BLE001
            _wic.logger.warning("web_ingest_spec_failed", exc_info=True)
    return _fallback_spec(instruction)


def _normalize_spec(parsed: dict) -> dict:
    et = str(parsed.get("entity_type") or "WebRecord").strip() or "WebRecord"
    key = _slug(parsed.get("key_attribute") or "name") or "name"
    confirmed = [_slug(a) for a in _as_list(parsed.get("confirmed_attributes"))]
    core = [_slug(a) for a in _as_list(parsed.get("core_attributes"))]
    suggested = [_slug(a) for a in _as_list(parsed.get("suggested_attributes"))]
    return {
        "entity_type": _pascal(et),
        "key_attribute": key,
        # Free-text search subject (NOT slugged — it's prose for the provider/card).
        "query": str(parsed.get("query") or "").strip(),
        # Generic query category for kind-routing (ONTA-190). Normalized to a
        # lowercase slug so "Place"/"PLACE" all match a provider's query_kinds; a
        # missing / null / literal-"null" value collapses to None → no routing.
        "query_kind": _norm_query_kind(parsed.get("query_kind")),
        # Enumeration partition (free-text prose like `query`, NOT slugged).
        # Non-empty → execute() fans the discovery out across these instead of
        # the single query. Deduped, capped at the fan-out limit.
        "subqueries": _norm_subqueries(parsed.get("subqueries")),
        "confirmed_attributes": [a for a in confirmed if a],
        "core_attributes": [a for a in core if a],
        "suggested_attributes": [a for a in suggested if a],
    }

def _norm_query_kind(v) -> Optional[str]:
    """Normalize the LLM's ``query_kind`` to a lowercase slug, or ``None``.

    The prompt asks for ``null`` on a non-specialized query, but LLMs sometimes
    emit the string ``"null"``/``"none"`` or an empty value — all collapse to
    ``None`` (no routing). A real kind is lowercased + slugged so it matches a
    provider's generic ``query_kinds`` regardless of casing/punctuation."""
    s = _slug(v)
    if not s or s in {"null", "none"}:
        return None
    return s


# The clarify PRE-SELECTS at most this many attributes — a short, most-important
# recommendation, NOT the comprehensive fetch set (that stays server-side in
# hint_columns). Keeps the chip list lean so the user isn't confronted with a dozen
# pre-checked columns.
_DEFAULT_CORE_CAP = 4


def _core_attrs(key_attr: str, core: list[str], suggested: list[str]) -> list[str]:
    """The SHORT, most-important attribute set to pre-select + show as chips —
    distinct from the comprehensive ``suggested`` FETCH hint. Prefer the LLM's
    ``core_attributes`` (kept to real, non-key members that are also suggested);
    when it gave none (older specs / the no-LLM fallback), degrade to the first few
    suggested extras. Always a small set — never the whole comprehensive list — so
    the UI recommends a minimum, not everything."""
    sugg_extras = [a for a in suggested if a and a != key_attr]
    picked = _dedupe([a for a in core if a and a != key_attr and a in sugg_extras])
    if not picked:
        picked = sugg_extras[:_DEFAULT_CORE_CAP]
    return picked[:_DEFAULT_CORE_CAP]


def _clarify_step(
    type_name: str, key_attr: str, core: list[str], note: str = ""
) -> PlanStep:
    """Ask which attributes to collect. Shows a SHORT recommended set (``core`` —
    the few most-important attributes), pre-selected, as clickable chips; the user
    can drop some, add their own, or keep just the name. The concrete list rides in
    ``options`` so whichever the user picks lands in the accumulated instruction and
    the next turn converges. The question stays terse and does NOT re-list the
    attributes — they're already the chips below it. The comprehensive fetch
    projection is chosen server-side (``hint_columns``), independent of this minimal
    recommendation, so a lean chip list never narrows what actually gets pulled.

    ``note`` is an optional leading advisory (e.g. the degraded-planning note) so a
    resolver-LLM failure is SURFACED in the question the user reads, not swallowed."""
    shown = _dedupe([key_attr, *core])
    question = (
        (f"{note}\n\n" if note else "")
        + f"I'll collect **{type_name}** records and always include **{key_attr}**. "
        "Pick the ones to collect below, add your own, or keep just the name."
    )
    options = [f"Use these: {', '.join(shown)}", f"Just the {key_attr}"]
    return PlanStep(
        capability=_wic.WebIngestCapability.name,
        action="clarify",
        params={"question": question, "options": options},
        rationale="Confirm the entity and attributes before fetching from the web.",
        confidence=1.0,
    )


def _refuse_if_unavailable(urls: list) -> object | None:
    """Availability gate before spec resolve. ``None`` = continue planning.

    URL mode needs a URL-capable provider. Query mode needs a general provider
    or at least one kind-specialized provider. BYOR: OSS registers no default
    fetcher, so a stock checkout refuses here.
    """
    from infona_client.web_sources.base import (
        get_web_source,
        has_kind_specialized_provider,
    )
    from infona_client.agent.capabilities.web_ingest_text import _answer_step

    general = get_web_source(for_urls=bool(urls))
    if urls:
        if general is None:
            return _answer_step(
                "I can see the link(s) you shared, but URL extraction isn't "
                "enabled in this deployment. An admin can configure a "
                "URL-capable web-source provider to parse pages like these "
                "into ingested data."
            )
        return None
    if general is None and not has_kind_specialized_provider():
        return _answer_step(
            "Web discovery isn't enabled in this deployment. An admin can "
            "configure a web-source provider (e.g. Exa or Perplexity) to "
            "turn a request like this into ingested data."
        )
    return None


def _select_plan_ensemble(urls: list, spec: dict, general):
    """Kind-routed provider ensemble, or an answer step / empty list to refuse."""
    from infona_client.web_sources.base import get_web_source_for_kind
    from infona_client.agent.capabilities.web_ingest_text import _answer_step

    if urls:
        ensemble = [general] if general else []
    else:
        query_kind = spec.get("query_kind")
        specialized = get_web_source_for_kind(query_kind) if query_kind else None
        ensemble = []
        for p in (specialized, general):
            if p is not None and all(p is not q for q in ensemble):
                ensemble.append(p)
        if not ensemble:
            return None, _answer_step(
                "Web discovery for this kind of request isn't enabled in "
                "this deployment. The configured web source only handles "
                "certain queries (e.g. finding physical places); an admin "
                "can add a general web-source provider for other requests."
            )
    provider = ensemble[0] if ensemble else None
    if provider is None:
        return None, []
    return ensemble, None
