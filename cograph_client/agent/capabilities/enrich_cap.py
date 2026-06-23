"""Enrichment capability — with clean-before-enrich composition.

Reuses the existing enrichment engine (no reimplementation):

* ``plan`` parses the NL instruction into the existing :class:`EnrichRequest`
  shape (attributes + optional scope ``predicate=value`` + tier + confidence).
  THEN it detects a prerequisite: if the **scope predicate's target values are
  composite** (un-normalized — a delimiter shows up in the sampled target
  labels), scoping by ``value`` would MISS the rows packed inside a composite
  cell (e.g. scope ``speaks=Persian`` misses an entity whose ``speaks`` points
  at ``English__Persian``). In that case it emits a NORMALIZE step FIRST (reusing
  :class:`NormalizeCapability.plan` so the cleanup logic isn't duplicated) and
  sets the enrich step's ``depends_on`` to it. Returns ``[normalize_step?,
  enrich_step]``. No writes.

* ``execute`` runs the enrichment as a background job, building the EXACT same
  :class:`EnrichJob` + ``EnrichmentExecutor.run`` the ``/enrich/jobs`` route
  builds (strong-ref ``_spawn`` so the task can't be GC'd). Returns an ack.

The agent never calls the ``/enrich`` HTTP route — it drives the executor + job
store directly via the same primitives.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

import structlog

from cograph_client.agent.capabilities.normalize_cap import NormalizeCapability
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.enrichment.models import (
    EnrichJob,
    EnrichScope,
    EnrichmentTier,
    JobStatus,
)
from cograph_client.normalization.inference import (
    list_type_schema,
    sample_predicate_values,
)
from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat

logger = structlog.stdlib.get_logger("cograph.agent.enrich")

_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# Delimiters that signal a composite (un-normalized) target value. "__" is the
# slugified list separator the ingest produces; the rest are raw list delimiters.
_COMPOSITE_DELIMS = ["__", ", ", "; ", " / ", " | "]


class EnrichCapability:
    name = "enrich"

    def __init__(self, normalize: NormalizeCapability | None = None) -> None:
        # Reuse the normalize capability to BUILD the prerequisite step so the
        # clean-before-enrich logic lives in exactly one place.
        self._normalize = normalize or NormalizeCapability()

    def describe(self) -> str:
        return (
            "Fill in or verify missing attributes on a type by looking them up "
            "from external sources (enrichment). Use for 'enrich', 'fill in', "
            "'look up', 'find the <attribute> for <type>' requests, optionally "
            "scoped (e.g. 'for managers', 'who speak Persian')."
        )

    async def plan(
        self,
        ctx: AgentContext,
        instruction: str,
        parsed: dict | None = None,
    ) -> list[PlanStep]:
        """Build [normalize_step?, enrich_step] from the instruction.

        ``parsed`` (optional) lets the planner pass an already-parsed request
        (attributes/scope/tier/confidence). When absent we ground the extraction
        in the type's REAL schema: we fetch the active type's attribute +
        relationship names from the ontology and feed them to the LLM so an NL
        phrase like "current company" maps to the ``company`` attribute (and the
        tier is chosen with web-fact guidance), instead of the model guessing a
        stray word ("current") and the planner bailing to clarify.
        """
        type_name = ctx.type_name or ""
        if not type_name:
            return []
        schema = await list_type_schema(ctx.neptune, ctx.tenant_id, type_name)
        req = parsed or await _extract_enrich_request(
            ctx, instruction, type_name, schema
        )
        attributes: list[str] = req.get("attributes") or []
        if not attributes:
            return []
        tier = _coerce_tier(req.get("tier"))
        confidence_min = float(req.get("confidence_min", 0.85) or 0.85)
        scope = req.get("scope")  # {"predicate":..., "value":...} | None

        steps: list[PlanStep] = []
        depends_on: list[str] = []

        # clean-before-enrich: if a scope predicate's target is composite,
        # normalize it FIRST so the scope actually matches the packed rows.
        if scope and scope.get("predicate"):
            samples, _kind = await sample_predicate_values(
                ctx.neptune,
                ctx.tenant_id,
                ctx.kg_name,
                type_name,
                scope["predicate"],
            )
            if _looks_composite(samples):
                norm_steps = await self._normalize.plan(
                    ctx, instruction, predicate_leaves=[scope["predicate"]]
                )
                if norm_steps:
                    norm = norm_steps[0]
                    norm.rationale = (
                        f"Clean '{scope['predicate']}' before enrichment: its "
                        f"values are composite, so scoping by "
                        f"{scope.get('value')!r} would miss packed rows."
                    )
                    steps.append(norm)
                    depends_on = [norm.id]

        cost = _estimate_cost(tier)
        enrich_step = PlanStep(
            capability=self.name,
            action="run_enrichment",
            params={
                "type_name": type_name,
                "attributes": attributes,
                "tier": tier.value,
                "confidence_min": confidence_min,
                "scope": scope,
            },
            rationale=(
                f"Enrich {', '.join(attributes)} on {type_name}"
                + (f" scoped to {scope['predicate']}={scope['value']}" if scope else "")
                + f" via the {tier.value} tier."
            ),
            confidence=0.8,
            preview={
                "summary": (
                    f"Look up {', '.join(attributes)} for matched {type_name} "
                    f"entities and stage the results for review."
                ),
                "scope": scope,
                "tier": tier.value,
            },
            cost=cost,
            depends_on=depends_on,
        )
        steps.append(enrich_step)
        return steps

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        """Create + run an EnrichJob in the background (same as /enrich/jobs)."""
        p = step.params
        executor = ctx.extras.get("enrichment_executor")
        job_store = ctx.extras.get("enrichment_job_store")
        if executor is None or job_store is None:
            raise RuntimeError(
                "enrichment executor/job_store not available in agent context"
            )
        scope = None
        if p.get("scope") and p["scope"].get("predicate"):
            scope = EnrichScope(
                predicate=p["scope"]["predicate"], value=p["scope"]["value"]
            )
        job = EnrichJob(
            id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            kg_name=ctx.kg_name,
            type_name=p["type_name"],
            attributes=p["attributes"],
            tier=_coerce_tier(p.get("tier")),
            status=JobStatus.queued,
            created_at=datetime.now(timezone.utc),
            conflict_policy=_default_conflict_policy(),
            confidence_min=float(p.get("confidence_min", 0.85) or 0.85),
            scope=scope,
        )
        await job_store.create(job)
        _spawn(executor.run(job, ctx.tenant_id))
        return {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            "job_id": job.id,
            "job_status": job.status.value,
            "message": (
                f"Enriching {', '.join(job.attributes)} on {job.type_name} "
                "in the background; results will be staged for review."
            ),
        }


def _default_conflict_policy():
    from cograph_client.enrichment.models import ConflictPolicy

    return ConflictPolicy.stage


def _looks_composite(samples: list[str]) -> bool:
    """Cheap composite check: any sampled target value carries a list delimiter."""
    for v in samples:
        for d in _COMPOSITE_DELIMS:
            if d in v:
                return True
    return False


def _coerce_tier(tier) -> EnrichmentTier:
    if isinstance(tier, EnrichmentTier):
        return tier
    try:
        return EnrichmentTier(str(tier))
    except ValueError:
        return EnrichmentTier.lite


def _estimate_cost(tier: EnrichmentTier) -> dict:
    """Cost estimate. The matched count is resolved by the executor at run time
    (not in the request path — COG-112), so at plan time we report the tier and
    a note rather than a blocking COUNT. ``lite`` is free (Wikidata only)."""
    if tier == EnrichmentTier.lite:
        return {"paid_calls": 0, "note": "lite tier (Wikidata) — no paid calls"}
    return {
        "paid_calls": None,
        "note": (
            f"{tier.value} tier may use paid sources; matched-entity count is "
            "resolved when the job runs (no blocking COUNT at plan time)."
        ),
    }


# --- LLM extraction grounded in the type's real schema ----------------------- #

# Open-web / person / company facts the FREE Wikidata tier usually can't answer
# well — these should default to the paid web ``core`` tier (Parallel/Exa). Used
# only as a deterministic backstop when the LLM omits a tier.
_WEB_FACT_HINTS = {
    "company", "employer", "organization", "organisation", "website", "url",
    "homepage", "description", "bio", "summary", "reviews", "rating", "founder",
    "headquarters", "hq", "location", "address", "email", "phone", "title",
    "role", "position", "industry", "revenue", "funding", "ceo", "linkedin",
}

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
- "scope" restricts WHICH entities to enrich ("for managers", "who speak \
Persian"). Its "predicate" MUST be one of the given attribute or relationship \
names. "languages" / "what they speak" -> the "speaks" relationship; "level" / \
"who are managers" -> the level attribute/relationship. If there is no scope, \
return null.
- "tier" selects the data source. Choose "core" (paid web search: \
Parallel/Exa) for OPEN-WEB facts about people or companies — employer, company, \
website, description, bio, reviews, founder, headquarters, email, role, title, \
industry, etc. Wikidata (the free "lite" tier) does NOT have these. Use "lite" \
ONLY for structured, catalogued identifiers Wikidata reliably holds (e.g. a \
country's ISO code, a film's release year, a well-known org's founding date). \
When unsure for a web-lookup attribute, default to "core".
- "confidence_min" defaults to 0.85 unless the user asks for stricter/looser."""

_EXTRACT_USER_TEMPLATE = """\
Type: {type_name}
Attributes: {attributes}
Relationships: {relationships}

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
            instruction=instruction,
        )
        try:
            text = await openrouter_chat(
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
            logger.warning("agent_enrich_extract_failed", exc_info=True)
            parsed = None
    if not parsed:
        parsed = _parse_enrich_instruction(instruction)
    return _validate_enrich_request(parsed, attr_names, rel_names)


def _validate_enrich_request(
    parsed: dict, attr_names: list[str], rel_names: list[str]
) -> dict:
    """Sanitize an extracted request against the type's real schema.

    - attributes: dropped if they are stray modifier words; otherwise normalized
      (matched case-insensitively to an existing attribute, else kept as a
      proposed new attribute name).
    - scope.predicate: kept only if it resolves to a real attribute/relationship
      (case-insensitively); otherwise the scope is dropped (a bad scope would
      match nothing).
    - tier: web-fact backstop applied when missing/invalid.
    """
    known = {n.lower(): n for n in (*attr_names, *rel_names)}
    attr_lookup = {n.lower(): n for n in attr_names}

    raw_attrs = parsed.get("attributes") or []
    if isinstance(raw_attrs, str):
        raw_attrs = [raw_attrs]
    attributes: list[str] = []
    for a in raw_attrs:
        norm = _normalize_attr(a)
        if not norm:
            continue
        attributes.append(attr_lookup.get(norm.lower(), norm))
    # De-dupe preserving order.
    seen: set[str] = set()
    attributes = [a for a in attributes if not (a.lower() in seen or seen.add(a.lower()))]

    scope = parsed.get("scope")
    if isinstance(scope, dict) and scope.get("predicate") and scope.get("value"):
        pred = str(scope["predicate"]).strip()
        # Resolve against the real schema. When the schema is EMPTY (no ontology
        # available — e.g. a brand-new/uningested type) we can't validate, so we
        # keep the extracted predicate rather than silently dropping a valid scope.
        resolved = known.get(pred.lower(), pred if not known else None)
        scope = (
            {"predicate": resolved, "value": str(scope["value"]).strip()}
            if resolved
            else None
        )
    else:
        scope = None

    tier = parsed.get("tier")
    if tier not in {t.value for t in EnrichmentTier}:
        tier = _tier_for_attributes(attributes)

    return {
        "attributes": attributes,
        "scope": scope,
        "tier": tier,
        "confidence_min": parsed.get("confidence_min", 0.85),
    }


# Stray modifier / filler words an extractor must never emit as an attribute.
_STOPWORDS = {
    "current", "the", "a", "an", "their", "its", "his", "her", "missing",
    "this", "that", "these", "those", "all", "each", "every", "some", "new",
    "of", "for", "in", "on", "with",
}


def _normalize_attr(value) -> str:
    """Reduce an extracted attribute phrase to a clean predicate noun, or "".

    Strips a leading modifier ("current company" -> "company"), drops pure
    stopwords ("current" -> ""), and slugs spaces to underscores so the result
    is a usable attribute leaf name.
    """
    if not isinstance(value, str):
        return ""
    words = [w for w in re.split(r"\s+", value.strip()) if w]
    # Drop leading stopwords ("current company" -> "company").
    while words and words[0].lower() in _STOPWORDS:
        words.pop(0)
    # Stop at the first trailing stopword ("company for" -> "company").
    kept: list[str] = []
    for w in words:
        if w.lower() in _STOPWORDS:
            break
        kept.append(w)
    if not kept:
        return ""
    cleaned = "_".join(kept).strip("_-")
    return cleaned if cleaned and cleaned.lower() not in _STOPWORDS else ""


def _tier_for_attributes(attributes: list[str]) -> str:
    """Default tier: ``core`` (paid web) when any attribute is an open-web fact,
    else ``core`` anyway for safety — Wikidata-only ``lite`` is opt-in via the
    LLM (structured identifiers), not the silent default for a web lookup."""
    for a in attributes:
        if a.lower() in _WEB_FACT_HINTS:
            return EnrichmentTier.core.value
    # No clear structured-identifier signal → prefer the paid web tier so a
    # person/company lookup isn't silently downgraded to a Wikidata miss.
    return EnrichmentTier.core.value if attributes else EnrichmentTier.lite.value


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
    """
    attributes: list[str] = []
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
