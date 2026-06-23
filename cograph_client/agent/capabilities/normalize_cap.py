"""Cleanup capability — propose + apply normalization rules through the agent.

Reuses the existing normalization engine end-to-end (no reimplementation):

* ``plan`` → :func:`cograph_client.normalization.inference.suggest_rules_for_predicates`
  (the TARGETED variant: only the predicate(s) the instruction names, never a
  whole-type scan) and produces one :class:`PlanStep` per inferred rule with a
  **dry-run preview** computed in-memory from sampled values (no writes).
* ``execute`` → persists the (confirmed) rule via
  :class:`cograph_client.normalization.rules.NormalizationRuleStore` and applies
  it via :func:`cograph_client.normalization.execute.apply_rule`, as a background
  job using the same strong-ref ``_spawn`` pattern as ``enrich.py`` /
  ``normalize.py`` (so the task can't be GC'd after the request returns).

The agent never calls the ``/normalize/*`` HTTP routes — it drives the same
engine functions directly.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

import structlog

from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.normalization.execute import apply_rule
from cograph_client.normalization.inference import (
    list_type_schema,
    sample_predicate_values,
    suggest_rules_for_predicates,
)
from cograph_client.normalization.rules import (
    NormalizationRule,
    NormalizationRuleStore,
    make_rule_id,
)
from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat

logger = structlog.stdlib.get_logger("cograph.agent.normalize")

# Rule types the execute engine + dry-run preview actually support today
# (cograph_client.normalization.inference._SUPPORTED_RULE_TYPES). The planner
# only ever emits one of these; anything else falls through to clarify.
_SUPPORTED_RULE_TYPES = ("list_explode", "strip_emoji")

# Strong refs to background apply tasks (mirrors enrich.py / normalize.py): a
# bare create_task() is only weakly held by CPython and can be GC'd at its first
# await once the request returns.
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# Same fallback delimiters inference defaults to — used only to build an
# in-memory dry-run preview of what the rule WOULD produce.
_PREVIEW_DELIMITERS = [", ", "; ", " / ", " | ", " - ", "__"]
_EMOJI_PATTERN = re.compile(
    "["
    "\U0000200d\U0000fe0e\U0000fe0f"
    "\U0001f3fb-\U0001f3ff\U0001f1e6-\U0001f1ff"
    "\U00002190-\U000021ff\U00002300-\U000023ff"
    "\U00002600-\U000027bf\U00002b00-\U00002bff"
    "\U0001f000-\U0001faff"
    "]+"
)
_WS = re.compile(r"\s+")


class NormalizeCapability:
    name = "normalize"

    def describe(self) -> str:
        return (
            "Clean up messy values on a named attribute/relationship: split "
            "composite multi-value cells into atomic ones (list_explode) and "
            "strip emoji/junk from text (strip_emoji). Use for 'clean', "
            "'normalize', 'split', 'tidy', 'fix the values of <field>' requests."
        )

    async def plan(
        self,
        ctx: AgentContext,
        instruction: str,
        predicate_leaves: list[str] | None = None,
    ) -> list[PlanStep]:
        """Map the instruction to a concrete normalization rule (a PLAN).

        Two callers, two modes:

        * **Prerequisite mode** — the enrich capability passes
          ``predicate_leaves`` explicitly (clean-before-enrich). We INFER the
          rule from the predicate's sampled values via
          :func:`suggest_rules_for_predicates`, exactly as before, so the
          composition logic is unchanged.

        * **User-instruction mode** — no ``predicate_leaves``. We ground the
          extraction in the type's REAL schema and ask the LLM which supported
          rule (``strip_emoji`` | ``list_explode``) applies to which real
          attribute/relationship. "remove emojis from the title field" →
          ``strip_emoji`` on ``title`` (a PLAN, not a clarify); "split the
          languages" → ``list_explode`` on ``speaks``. We only return [] (→
          clarify) when the field genuinely can't be identified.

        We never scan every predicate of a type (COG-118 perf).
        """
        type_name = ctx.type_name or ""
        if not type_name:
            return []

        # Prerequisite mode: keep the inference-from-samples behavior intact.
        if predicate_leaves:
            return await self._plan_from_inference(ctx, type_name, predicate_leaves)

        # User-instruction mode: schema-grounded direct extraction.
        schema = await list_type_schema(ctx.neptune, ctx.tenant_id, type_name)
        directive = await _extract_normalize_directive(
            ctx, instruction, type_name, schema
        )
        if directive and directive.get("rule_type") and directive.get("predicate"):
            step = await self._build_rule_step(ctx, type_name, directive)
            if step is not None:
                return [step]

        # Fallback: no explicit rule_type extracted (or no key) — try the
        # inference-from-samples path on any predicate names we can spot, so a
        # vague-but-targeted "clean the speaks values" still works.
        leaves = _extract_predicate_leaves(instruction)
        if leaves:
            inferred = await self._plan_from_inference(ctx, type_name, leaves)
            if inferred:
                return inferred
        return []

    async def _plan_from_inference(
        self, ctx: AgentContext, type_name: str, leaves: list[str]
    ) -> list[PlanStep]:
        """Infer rule(s) for named predicate(s) from their sampled values."""
        rules = await suggest_rules_for_predicates(
            ctx.neptune, ctx.tenant_id, ctx.kg_name, type_name, leaves
        )
        return [self._step_for_rule(rule, type_name) for rule in rules]

    async def _build_rule_step(
        self, ctx: AgentContext, type_name: str, directive: dict
    ) -> PlanStep | None:
        """Build one PlanStep from an extracted {rule_type, predicate, params}.

        Samples the predicate's current values so the dry-run preview is real,
        and resolves the predicate's ``target_kind`` for a correct list_explode
        default. Returns None for an unsupported rule type (→ caller clarifies).
        """
        rule_type = directive["rule_type"]
        if rule_type not in _SUPPORTED_RULE_TYPES:
            return None
        predicate = directive["predicate"]
        samples, target_kind = await sample_predicate_values(
            ctx.neptune, ctx.tenant_id, ctx.kg_name, type_name, predicate
        )
        params = dict(directive.get("params") or {})
        if rule_type == "list_explode":
            params.setdefault(
                "target", "entity" if target_kind == "relationship" else "literal"
            )
            if not params.get("delimiters"):
                params["delimiters"] = _PREVIEW_DELIMITERS
        elif rule_type == "strip_emoji":
            if not params.get("targets"):
                params["targets"] = ["attribute"]
        rule = NormalizationRule(
            id=make_rule_id(ctx.kg_name, type_name, predicate, rule_type),
            kg_name=ctx.kg_name,
            type_name=type_name,
            predicate=predicate,
            target_kind=target_kind,
            rule_type=rule_type,
            params=params,
            confidence=float(directive.get("confidence", 0.8) or 0.8),
            rationale=directive.get("rationale", "")
            or f"{rule_type} on '{predicate}' per the instruction.",
            sample_values=samples[:25],
            status="suggested",
        )
        return self._step_for_rule(rule, type_name)

    def _step_for_rule(self, rule: NormalizationRule, type_name: str) -> PlanStep:
        return PlanStep(
            capability=self.name,
            action="apply_rule",
            params={"rule": rule.model_dump()},
            rationale=rule.rationale or f"Normalize '{rule.predicate}' on {type_name}.",
            confidence=rule.confidence,
            preview=_dry_run_preview(rule),
            cost={},  # normalization is free (no paid calls)
        )

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        """Persist the rule as confirmed + apply it in the background; ack now."""
        rule = NormalizationRule(**step.params["rule"])
        # The user confirmed the plan, so the rule is confirmed.
        rule.status = "confirmed"
        store = NormalizationRuleStore(ctx.neptune)
        await store.save(ctx.tenant_id, rule)
        _spawn(_apply_and_mark(ctx.neptune, ctx.tenant_id, rule))
        return {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            "rule_id": rule.id,
            "predicate": rule.predicate,
            "rule_type": rule.rule_type,
            "rule_status": "accepted",
            "message": (
                f"Normalizing '{rule.predicate}' ({rule.rule_type}) on "
                f"{rule.type_name} in the background."
            ),
        }


async def _apply_and_mark(neptune, tenant_id: str, rule: NormalizationRule) -> None:
    """Run apply_rule, then mark applied. Detached — errors logged, not raised."""
    try:
        summary = await apply_rule(neptune, tenant_id, rule)
        await NormalizationRuleStore(neptune).update_status(
            tenant_id,
            rule.id,
            "applied",
            applied_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("agent_normalize_done", rule_id=rule.id, **summary)
    except Exception:
        logger.error("agent_normalize_failed", rule_id=rule.id, exc_info=True)


def _dry_run_preview(rule: NormalizationRule) -> dict:
    """Build a before/after preview from the rule's sampled values — IN MEMORY.

    No writes, no Neptune round-trip: we apply the same split/strip logic the
    executor uses to a few sampled values so the user sees what WOULD change.
    """
    samples = rule.sample_values[:5]
    changes: list[dict] = []
    for v in samples:
        after = _apply_in_memory(rule, v)
        if after != [v]:
            changes.append({"before": v, "after": after})
    return {
        "rule_type": rule.rule_type,
        "predicate": rule.predicate,
        "samples": changes,
        "summary": _summary_line(rule, changes),
    }


def _apply_in_memory(rule: NormalizationRule, value: str) -> list[str]:
    """Apply the rule's transform to a single value, returning the result list."""
    if rule.rule_type == "strip_emoji":
        cleaned = _WS.sub(" ", _EMOJI_PATTERN.sub("", value)).strip()
        return [cleaned] if cleaned else []
    # list_explode
    delims = (rule.params or {}).get("delimiters") or _PREVIEW_DELIMITERS
    parts = [value]
    for d in delims:
        nxt: list[str] = []
        for p in parts:
            nxt.extend(p.split(d))
        parts = nxt
    out = [p.strip() for p in parts if p.strip()]
    return out or [value]


def _summary_line(rule: NormalizationRule, changes: list[dict]) -> str:
    if not changes:
        return f"No sampled '{rule.predicate}' values need {rule.rule_type}."
    ex = changes[0]
    if rule.rule_type == "list_explode":
        return (
            f"Split composite '{rule.predicate}' values, e.g. "
            f"{ex['before']!r} → {ex['after']}."
        )
    return f"Strip junk from '{rule.predicate}', e.g. {ex['before']!r} → {ex['after']}."


# --- LLM directive extraction grounded in the type's real schema ------------- #

_DIRECTIVE_SYSTEM = """\
You translate a data-cleaning instruction into ONE normalization rule, GROUNDED \
in the active type's real schema. You are given the type's actual ATTRIBUTE and \
RELATIONSHIP names; map the field the user names onto one of them.

Only two rule types are supported — choose the one the instruction asks for:
- "strip_emoji": remove emoji / pictographic junk characters from text values \
("remove emojis from title", "strip the icons out of the name", "clean up the \
junk symbols in description").
- "list_explode": split a composite multi-value cell into atomic values \
("split the languages into separate ones", "these are packed together, separate \
the skills", "explode the comma-separated tags"). For a relationship like \
"languages" / "what they speak", target the matching relationship (e.g. \
"speaks").

Return STRICT JSON only (no markdown):
{
  "rule_type": "strip_emoji" | "list_explode" | null,
  "predicate": "<an attribute or relationship NAME from the schema>" | null,
  "params": { ...optional rule params... },
  "confidence": 0.0,
  "rationale": "one short sentence"
}

RULES:
- "predicate" MUST be one of the given attribute or relationship names. Map \
phrases: "the title field"/"titles" -> "title"; "languages"/"what they speak" \
-> the "speaks" relationship; "names" -> "name". If the field the user means is \
NOT in the schema and you cannot confidently map it, set predicate to null.
- If the instruction is NOT a strip_emoji or list_explode request (e.g. it asks \
to lowercase, trim, dedupe, or rename), set rule_type to null.
- Set confidence in [0,1] for how sure you are."""

_DIRECTIVE_USER_TEMPLATE = """\
Type: {type_name}
Attributes: {attributes}
Relationships: {relationships}

Instruction: {instruction}

Which normalization rule does this ask for? Respond with strict JSON."""


async def _extract_normalize_directive(
    ctx: AgentContext,
    instruction: str,
    type_name: str,
    schema: dict,
) -> dict | None:
    """LLM-extract {rule_type, predicate, params}, validated against the schema.

    Returns None when there is no key, the LLM errors, the request isn't a
    supported rule type, or the predicate can't be mapped to a real
    attribute/relationship — the caller then falls back / clarifies.
    """
    if not ctx.openrouter_key:
        return None
    attr_names = [a for a in schema.get("attributes", []) if a]
    rel_names = [r.get("name") for r in schema.get("relationships", []) if r.get("name")]
    rels_block = ", ".join(
        f"{r['name']} (-> {r.get('target_type') or '?'})"
        for r in schema.get("relationships", [])
        if r.get("name")
    ) or "(none)"
    user = _DIRECTIVE_USER_TEMPLATE.format(
        type_name=type_name,
        attributes=", ".join(attr_names) or "(none)",
        relationships=rels_block,
        instruction=instruction,
    )
    try:
        text = await openrouter_chat(
            ctx.openrouter_key,
            _DIRECTIVE_SYSTEM,
            user,
            model=PRIMARY_MODEL,
            temperature=0,
            max_tokens=400,
            timeout=30,
        )
        data = _parse_json_object(text)
    except Exception:
        logger.warning("agent_normalize_extract_failed", exc_info=True)
        return None
    if not data or data.get("rule_type") not in _SUPPORTED_RULE_TYPES:
        return None
    pred = data.get("predicate")
    if not isinstance(pred, str) or not pred.strip():
        return None
    # Resolve the predicate against the real schema (case-insensitively). When
    # the schema is empty we can't validate, so accept the extracted name.
    known = {n.lower(): n for n in (*attr_names, *rel_names)}
    resolved = known.get(pred.strip().lower(), pred.strip() if not known else None)
    if not resolved:
        return None
    data["predicate"] = resolved
    return data


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


# Quoted-token | word-after-trigger extraction for predicate names in NL.
_QUOTED = re.compile(r"['\"`]([A-Za-z_][\w-]*)['\"`]")
_TRIGGER = re.compile(
    r"\b(?:field|attribute|predicate|column|property|values? of|the)\s+"
    r"([A-Za-z_][\w-]*)",
    re.IGNORECASE,
)


def _extract_predicate_leaves(instruction: str) -> list[str]:
    """Best-effort pull of predicate leaf names from an NL instruction.

    Prefers quoted tokens (``clean the 'speaks' field``); falls back to a word
    after a trigger phrase (``clean the speaks values``). Returns a de-duped
    list. The planner usually passes predicates explicitly; this is the fallback
    when a user types a free-form clean request straight at the normalize intent.
    """
    found: list[str] = []
    for m in _QUOTED.finditer(instruction):
        found.append(m.group(1))
    if not found:
        for m in _TRIGGER.finditer(instruction):
            tok = m.group(1)
            if tok.lower() not in {"the", "a", "an", "of", "values", "value"}:
                found.append(tok)
    # De-dupe preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for f in found:
        if f.lower() not in seen:
            seen.add(f.lower())
            out.append(f)
    return out
