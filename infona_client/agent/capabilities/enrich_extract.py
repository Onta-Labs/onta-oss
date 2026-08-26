"""LLM + deterministic extraction of an EnrichRequest from NL.

Owns the schema-grounded extract prompt, the OpenRouter call, JSON
coercion, and the no-key regex fallback parser.

Invariants other agents must not break:
- Look up ``openrouter_chat`` and ``logger`` on the public ``enrich_cap``
  module via :func:`_host` so tests that patch them keep working.
- Never 500 on extraction: fall back to the deterministic parser.
- This module does not write the graph.
"""

from __future__ import annotations

import json
import re

from infona_client.agent.capabilities.enrich_common import _host
from infona_client.agent.capabilities.enrich_validate import (
    _normalize_attr,
    _split_attr_list,
    _validate_enrich_request,
)
from infona_client.agent.registry import AgentContext
from infona_client.resolver.llm_router import PRIMARY_MODEL
from infona_client.skills.inject import schema_skills_suffix

# --- LLM extraction grounded in the type's real schema ----------------------- #

_EXTRACT_SYSTEM = """\
You extract an enrichment request from a user's instruction, GROUNDED in the \
active type's real schema. You are given the type's actual ATTRIBUTE names and \
RELATIONSHIP names (with their target types). Map the natural-language phrases \
in the instruction onto those real predicate names — never invent a stray word.

Return STRICT JSON only (no markdown):
{
  "attributes": ["<attribute name(s) to enrich>"],
  "scope": {"predicate": "<an attribute OR relationship name>", "value": "<v>"} \
or null,
  "subset": {"description": "<self-contained description of WHICH entities>", \
"limit": <int or null>} or null,
  "tier": "lite" | "base" | "core" | "pro",
  "confidence_min": 0.85
}

RULES:
- "attributes" are the field(s) to FILL IN / look up. Map the noun in the \
instruction to the nearest existing ATTRIBUTE name. Examples: "current company" \
/ "employer" -> "company"; "the website" -> "website"; "their bio" -> \
"description". If NO existing attribute fits but the user clearly names a new \
fact to add, propose a clean lowercase singular noun for it (e.g. "company") — \
NEVER emit a modifier word like "current", "their", "the", "missing".
- "scope" restricts WHICH entities to enrich by a simple FIELD=VALUE match ("for \
managers", "who speak Persian"). Its "predicate" MUST be one of the given \
attribute or relationship names. "languages" / "what they speak" -> the "speaks" \
relationship; "level" / "who are managers" -> the level attribute/relationship. \
If there is no such filter, return null.
- "subset" pins enrichment to a RANKED or SPECIFIC set of entities that a simple \
field=value "scope" CANNOT express — "the top 5 <type> by <metric>", "the 10 \
most recent ...", "those"/"them"/"these" (entities referenced earlier in the \
conversation), or an explicit named list. Write "description" as a SELF-CONTAINED \
phrase naming exactly which entities (resolve pronouns using the whole \
conversation, e.g. turn "those" into "the 5 brokers with the most property \
listings"), and "limit" = the count if the user gave one (else null). Use \
"subset" ONLY for ranked/specific sets; for "all <type>" or a plain field=value \
filter leave it null. "scope" and "subset" are mutually exclusive — prefer \
"subset" when the request is ranked or refers to specific earlier entities.
- "tier" selects the data source. Prefer "base" when a free AUTHORITATIVE \
registry API covers the type + attribute (e.g. ClinicalTrial lead_sponsor / \
status / phase → ClinicalTrials.gov; healthcare NPI fields → NPPES). Use "core" \
(paid web search: Parallel/Exa) for OPEN-WEB facts about people or companies — \
employer, company, website, description, bio, reviews, founder, headquarters, \
email, role, title, industry, etc. Wikidata (the free "lite" tier) does NOT have \
those. Use "lite" ONLY for structured, catalogued identifiers Wikidata reliably \
holds (e.g. a country's ISO code, a film's release year, a well-known org's \
founding date). When unsure for a web-lookup attribute with NO registry match, \
default to "core". Prefer the schema's full attribute name (lead_sponsor, not \
sponsor) when the user names a role-qualified field.
- "confidence_min" defaults to 0.85 unless the user asks for stricter/looser."""

_EXTRACT_USER_TEMPLATE = """\
Type: {type_name}
Attributes: {attributes}
Relationships: {relationships}{skills}

Instruction: {instruction}

Extract the enrichment request as strict JSON."""


async def _extract_enrich_request(
    ctx: AgentContext,
    instruction: str,
    type_name: str,
    schema: dict,
) -> dict:
    """LLM-extract {attributes, scope, tier, confidence_min}, schema-grounded.

    Falls back to the deterministic regex parser when there is no key or the LLM
    errors, so the agent never 500s on extraction. The extracted attributes /
    scope predicate are validated against the type's real schema; the tier is
    backstopped from the web-fact heuristic when the model omits it.
    """
    attr_names = [a for a in schema.get("attributes", []) if a]
    rel_names = [r.get("name") for r in schema.get("relationships", []) if r.get("name")]
    parsed: dict | None = None
    if ctx.openrouter_key:
        rels_block = ", ".join(
            f"{r['name']} (-> {r.get('target_type') or '?'})"
            for r in schema.get("relationships", [])
            if r.get("name")
        ) or "(none)"
        user = _EXTRACT_USER_TEMPLATE.format(
            type_name=type_name,
            attributes=", ".join(attr_names) or "(none)",
            relationships=rels_block,
            skills=schema_skills_suffix(schema),
            instruction=instruction,
        )
        try:
            text = await _host().openrouter_chat(
                ctx.openrouter_key,
                _EXTRACT_SYSTEM,
                user,
                model=PRIMARY_MODEL,
                temperature=0,
                max_tokens=400,
                timeout=30,
            )
            parsed = _parse_json_object(text)
        except Exception:
            _host().logger.warning("agent_enrich_extract_failed", exc_info=True)
            parsed = None
    if not parsed:
        parsed = _parse_enrich_instruction(instruction)
    return _validate_enrich_request(parsed, attr_names, rel_names, type_name)


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


# --- Deterministic fallback parser (no LLM key / LLM error) ------------------ #

_ATTR_TRIGGER = re.compile(
    r"\b(?:enrich|fill in|fill|look up|lookup|find|get|add)\s+(?:the\s+)?"
    r"([A-Za-z_][\w-]*(?:\s+[A-Za-z_][\w-]*)?)",
    re.IGNORECASE,
)
# "enrich <type> with their lead sponsor and latest status" — the attributes
# live AFTER "with [their/its/the]", not immediately after the verb (the verb
# is followed by the TYPE). Without this, the fallback parser captured
# "clinical trials" as the attribute and planned a bogus new leaf.
_WITH_ATTRS = re.compile(
    r"\bwith\s+(?:their|its|his|her|the|our)\s+(.+)$",
    re.IGNORECASE,
)
# Relationship scope: "<verb> <Value>" e.g. "speak Persian", "speaks French".
# group(1) = verb, group(2) = value. Verb is lemmatized to its predicate leaf.
_SCOPE_REL = re.compile(
    r"\b(speak|speaks|speaking|knows?|knowing|using|uses?)\s+"
    r"([A-Z][\w-]+)",
)


def _parse_enrich_instruction(instruction: str) -> dict:
    """Deterministic best-effort parse used only when the LLM is unavailable.

    Extracts attribute noun(s) after the enrich verb (dropping a leading
    modifier like "current") and an optional relationship scope. Tier is left
    unset so :func:`_validate_enrich_request` applies the web-fact default.

    Examples:
      "enrich the current company for managers"
        → attributes=["company"]   (the "current" modifier is dropped)
      "enrich company for mentors who speak Persian"
        → attributes=["company"], scope={"predicate":"speaks","value":"Persian"}
      "enrich clinical trials with their lead sponsor and latest status"
        → attributes=["lead_sponsor", "latest_status"]
    """
    attributes: list[str] = []
    with_m = _WITH_ATTRS.search(instruction or "")
    if with_m:
        # Prefer the "with [their] X and Y" clause — that's the attribute list
        # in "enrich <type> with their <attrs>". Split on the same delimiters
        # as multi-attribute extraction so "A and B" / "A, B" both work.
        for frag in _split_attr_list(with_m.group(1)):
            norm = _normalize_attr(frag)
            if norm and norm.lower() not in {a.lower() for a in attributes}:
                attributes.append(norm)
    if not attributes:
        m = _ATTR_TRIGGER.search(instruction)
        if m:
            norm = _normalize_attr(m.group(1))
            if norm:
                attributes = [norm]

    scope = None
    rel = _SCOPE_REL.search(instruction)
    if rel:
        verb = rel.group(1).lower()
        pred = _SCOPE_VERB_LEMMA.get(verb, verb)
        scope = {"predicate": pred, "value": rel.group(2)}
    return {"attributes": attributes, "scope": scope, "tier": None}


# Map inflected scope verbs to their predicate leaf (the ontology stores the
# bare relationship name, e.g. "speaks").
_SCOPE_VERB_LEMMA = {
    "speak": "speaks",
    "speaks": "speaks",
    "speaking": "speaks",
    "know": "knows",
    "knows": "knows",
    "knowing": "knows",
    "use": "uses",
    "uses": "uses",
    "using": "uses",
}
