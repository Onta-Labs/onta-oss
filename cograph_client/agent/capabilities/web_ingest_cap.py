"""Web-discovery capability — find a NEW set of records on the web and ingest them.

This is the discovery counterpart to enrichment. Enrichment fills a missing
``(entity, attribute)`` cell on entities that ALREADY exist; discovery CREATES a
whole set of new entities from a natural-language query ("a list of models
offered by OpenRouter"). So it reuses the **ingest** engine, not the enrichment
engine.

The flow deliberately confirms the SHAPE before fetching, so the ontology expands
accurately and the user doesn't have to run a separate enrichment afterward:

1. ``plan`` resolves the target ENTITY type and the ATTRIBUTES to collect. If the
   user only named the entity ("a list of models"), it proposes a sensible
   attribute set and returns a CLARIFY turn ("I'll collect Model with name —
   also want provider, open_source, context_length, pricing?"). The user's reply
   (a clicked option carrying the list, or free text) enters the accumulated
   instruction so the next turn converges.
2. Once attributes are confirmed, ``plan`` fetches a cheap SAMPLE constrained to
   those attributes, builds a deterministic :class:`CSVSchemaMapping` from the
   CONFIRMED (type, attributes) — not just whatever the web returned — and
   returns a plan card (sample rows + sources + cost). The mapping is PERSISTED
   so the schema previewed is exactly the schema committed (preview == commit).
3. ``execute`` fetches the FULL set (targeting the same attributes) and ingests
   it through :meth:`SchemaResolver.ingest_mapped_records` — the identical
   resolve→dedup→batch-insert path CSV ingest commits through — as a background
   job. Returns an ack.

OSS ships with NO web-source provider registered, so the capability degrades
gracefully: ``plan`` returns a plain "not enabled" answer until a downstream
deployment registers a provider (the dev stub, or a paid Exa/Perplexity fan-out).
"""

from __future__ import annotations

import asyncio
import json
import re

import structlog

from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat
from cograph_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
)
from cograph_client.web_sources.base import (
    WebSourceProvider,
    get_web_source,
    provider_cost,
)

logger = structlog.stdlib.get_logger("cograph.agent.web_ingest")

_bg_tasks: set[asyncio.Task] = set()

# Rows requested for the cheap plan-time sample (preview + datatype inference).
_SAMPLE_ROWS = 8
_PREVIEW_SAMPLE = 5
_PREVIEW_SOURCES = 5
# Conservative default cap so a first (paid) discovery is BOUNDED and cheap to
# inspect. Mirrors the enrich plan's _DEFAULT_PLAN_LIMIT. User-overridable.
_DEFAULT_PLAN_CAP = 200


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


class WebIngestCapability:
    name = "web_ingest"

    def describe(self) -> str:
        return (
            "Discover a NEW set of records from the web and ingest them as a new "
            "dataset/type. Use for 'find a list of X from the web', 'pull all Y', "
            "'add data about Z from the web', 'get me <records> and add them'. Use "
            "when the user wants to CREATE entities that don't exist in the graph "
            "yet — NOT to fill attributes on existing entities (that is enrich)."
        )

    async def plan(
        self,
        ctx: AgentContext,
        instruction: str,
        parsed: dict | None = None,
    ) -> list[PlanStep]:
        provider = get_web_source()
        if provider is None:
            return [
                _answer_step(
                    "Web discovery isn't enabled in this deployment. An admin can "
                    "configure a web-source provider (e.g. Exa or Perplexity) to "
                    "turn a request like this into ingested data."
                )
            ]

        query = _clean_query(instruction)
        if not query:
            return []

        # 1. Resolve the entity type + the attributes to collect. If the user only
        #    named the entity, propose a set and confirm before spending anything.
        spec = parsed or await _resolve_spec(ctx, instruction)
        type_name = spec.get("entity_type") or "WebRecord"
        key_attr = spec.get("key_attribute") or "name"
        confirmed = _dedupe([key_attr, *spec.get("confirmed_attributes", [])])
        suggested = _dedupe([key_attr, *spec.get("suggested_attributes", [])])

        already_asked = int(ctx.extras.get("prior_clarify_count", 0)) >= 1
        if len(confirmed) <= 1 and not already_asked:
            # Only the key is "confirmed" (i.e. the user just named the entity).
            # Ask which attributes to collect — clickable options carry the list
            # so the next turn converges without new UI.
            return [_clarify_step(type_name, key_attr, suggested)]

        # Commit: use the confirmed set, or fall back to the suggested set if we
        # already asked once (don't loop).
        attributes = confirmed if len(confirmed) > 1 else suggested

        # 2. Cheap sample, constrained to the chosen attributes.
        try:
            sample = await provider.discover(
                query,
                sample=True,
                max_rows=_SAMPLE_ROWS,
                hint_columns=attributes,
                context=_provider_context(ctx),
            )
        except Exception:  # noqa: BLE001 — a sample failure must never 500 the turn
            logger.warning("web_ingest_sample_failed", exc_info=True)
            return [
                _answer_step(
                    "I couldn't reach the web source to preview that just now. "
                    "Try again in a moment or rephrase the request."
                )
            ]
        if not sample.rows:
            return [
                _answer_step(
                    f"I couldn't find anything on the web for “{query}”. "
                    "Try rephrasing or narrowing it."
                )
            ]

        # 3. Build a DETERMINISTIC mapping from the confirmed (type, attributes) so
        #    the ontology expands exactly as confirmed (not whatever the web
        #    returned). Datatypes are inferred from the sample values only.
        mapping = _build_mapping(type_name, key_attr, attributes, sample.rows)
        est_total = sample.estimated_total or len(sample.rows)
        cap = _DEFAULT_PLAN_CAP
        cost = _estimate_cost(provider, est_total, cap)

        step = PlanStep(
            capability=self.name,
            action="discover_ingest",
            params={
                "query": query,
                "proposed_type": type_name,
                "attributes": attributes,
                "mapping": mapping.model_dump(mode="json"),
                "max_rows": cap,
                "kg_name": ctx.kg_name,
                "provider": provider.name,
            },
            rationale=(
                f"Search the web for “{query}” and ingest the results as "
                f"{type_name} with {', '.join(attributes)} (capped at {cap})."
            ),
            confidence=0.7,
            preview={
                "summary": (
                    f"Found ~{est_total} record(s) on the web for “{query}”. "
                    f"Ingest them as a new “{type_name}” dataset with "
                    f"{len(attributes)} attribute(s), capped at {cap}, and review "
                    f"before they go live."
                ),
                "proposed_type": type_name,
                "attributes": attributes,
                "columns": _column_preview(mapping),
                "sample_rows": sample.rows[:_PREVIEW_SAMPLE],
                "sources": sample.sources[:_PREVIEW_SOURCES],
                "estimated_total": est_total,
                "cost_estimate": cost.get("note", ""),
            },
            cost=cost,
        )
        return [step]

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        p = step.params
        provider = get_web_source(p.get("provider"))
        if provider is None:
            raise RuntimeError("web-source provider not available at execute time")

        mapping = CSVSchemaMapping.model_validate(p["mapping"])
        query = p["query"]
        attributes = p.get("attributes") or []
        proposed_type = p.get("proposed_type") or mapping.entity_type
        cap = int(p.get("max_rows") or _DEFAULT_PLAN_CAP)
        kg_name = p.get("kg_name") or ctx.kg_name
        instance_graph = kg_graph_uri(ctx.tenant_id, kg_name) if kg_name else None
        resolver = _build_resolver(ctx)
        source = f"web:{provider.name}:{query}"
        pctx = _provider_context(ctx)

        async def _run() -> None:
            try:
                full = await provider.discover(
                    query,
                    sample=False,
                    max_rows=cap,
                    hint_columns=attributes,
                    context=pctx,
                )
                rows = full.rows[:cap]
                if not rows:
                    logger.info("web_ingest_no_rows", query=query)
                    return
                result = await resolver.ingest_mapped_records(
                    rows, mapping, ctx.tenant_id, source=source,
                    instance_graph=instance_graph,
                )
                logger.info(
                    "web_ingest_complete",
                    query=query,
                    rows=len(rows),
                    entities=result.entities_resolved,
                    types=result.types_created,
                )
            except Exception:  # noqa: BLE001 — background job must self-contain errors
                logger.error("web_ingest_failed", query=query, exc_info=True)

        _spawn(_run())
        return {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            "message": (
                f"Searching the web for “{query}” and ingesting the results "
                f"as {proposed_type} ({', '.join(attributes)}) in the background."
            ),
        }


# --- entity + attribute resolution ------------------------------------------- #

_SPEC_SYSTEM = """\
You plan a web-discovery ingest: the user wants to pull a NEW set of records from \
the web and add them to a knowledge graph. From the WHOLE conversation, output \
STRICT JSON only (no markdown):
{
  "entity_type": "<PascalCase singular type for the records, e.g. Model, Company, Drug>",
  "key_attribute": "<the natural identifier, usually 'name', snake_case>",
  "confirmed_attributes": ["<attributes the user EXPLICITLY named; [] if they only named the entity>"],
  "suggested_attributes": ["<3-6 useful, web-discoverable attributes for this entity, snake_case, excluding the key>"]
}
RULES:
- entity_type: specific but clean — "a list of models offered by OpenRouter" -> \
"Model" (prefer the domain term the user used; singular).
- key_attribute: the human-readable identifier (name/title), snake_case.
- confirmed_attributes: ONLY what the user actually asked for. "models with their \
names and pricing" -> ["name","pricing"]; "a list of models" -> []. When the user \
replies with a list (e.g. "Use these: name, provider, pricing" or "just the name") \
treat THOSE as confirmed. snake_case; exclude nothing they named.
- suggested_attributes: a sensible default set the user is likely to want, \
snake_case, EXCLUDING the key. For Model: ["provider","open_source","context_length","input_price","modality"]."""


async def _resolve_spec(ctx: AgentContext, instruction: str) -> dict:
    """LLM-resolve {entity_type, key_attribute, confirmed/suggested attributes}.

    Degrades to a minimal deterministic spec when there is no key or the LLM
    errors, so the turn never 500s — that minimal spec triggers the clarify path.
    """
    if ctx.openrouter_key:
        try:
            text = await openrouter_chat(
                ctx.openrouter_key,
                _SPEC_SYSTEM,
                instruction,
                model=PRIMARY_MODEL,
                temperature=0,
                max_tokens=400,
                timeout=30,
            )
            parsed = _parse_json_object(text)
            if parsed:
                return _normalize_spec(parsed)
        except Exception:  # noqa: BLE001
            logger.warning("web_ingest_spec_failed", exc_info=True)
    # No-LLM fallback: name the records generically and ask.
    return {
        "entity_type": "WebRecord",
        "key_attribute": "name",
        "confirmed_attributes": [],
        "suggested_attributes": ["name", "description", "url"],
    }


def _normalize_spec(parsed: dict) -> dict:
    et = str(parsed.get("entity_type") or "WebRecord").strip() or "WebRecord"
    key = _slug(parsed.get("key_attribute") or "name") or "name"
    confirmed = [_slug(a) for a in _as_list(parsed.get("confirmed_attributes"))]
    suggested = [_slug(a) for a in _as_list(parsed.get("suggested_attributes"))]
    return {
        "entity_type": _pascal(et),
        "key_attribute": key,
        "confirmed_attributes": [a for a in confirmed if a],
        "suggested_attributes": [a for a in suggested if a],
    }


def _clarify_step(type_name: str, key_attr: str, suggested: list[str]) -> PlanStep:
    """Ask which attributes to collect. Both clickable options carry the concrete
    attribute list, so whichever the user clicks lands in the accumulated
    instruction and the next turn converges. The user can also type their own."""
    full = _dedupe([key_attr, *suggested])
    extras = [a for a in full if a != key_attr]
    question = (
        f"I'll collect **{type_name}** records and always include **{key_attr}**. "
        + (
            f"Want these attributes too: {', '.join(extras)}? "
            if extras
            else ""
        )
        + "Pick a set below, or type the attributes you want."
    )
    options = [f"Use these: {', '.join(full)}", f"Just the {key_attr}"]
    return PlanStep(
        capability=WebIngestCapability.name,
        action="clarify",
        params={"question": question, "options": options},
        rationale="Confirm the entity and attributes before fetching from the web.",
        confidence=1.0,
    )


def _build_mapping(
    type_name: str, key_attr: str, attributes: list[str], sample_rows: list[dict]
) -> CSVSchemaMapping:
    """Deterministic mapping from the CONFIRMED shape: the key column is the
    type id, every other confirmed attribute is an attribute column with a
    datatype inferred from the sample values."""
    cols: list[ColumnMapping] = [
        ColumnMapping(
            column_name=key_attr, role=ColumnRole.TYPE_ID,
            datatype="string", attribute_name=key_attr,
        )
    ]
    for a in attributes:
        if a == key_attr:
            continue
        cols.append(
            ColumnMapping(
                column_name=a, role=ColumnRole.ATTRIBUTE,
                datatype=_infer_datatype(a, sample_rows), attribute_name=a,
            )
        )
    return CSVSchemaMapping(entity_type=type_name, columns=cols)


def _infer_datatype(attr: str, rows: list[dict]) -> str:
    """Cheap datatype guess from the sample values for one column."""
    vals = [str(r.get(attr, "")).strip() for r in rows if r.get(attr) not in (None, "")]
    if not vals:
        return "string"
    if all(_is_int(v) for v in vals):
        return "integer"
    if all(_is_float(v) for v in vals):
        return "float"
    return "string"


# --- helpers ----------------------------------------------------------------- #


def _provider_context(ctx: AgentContext) -> dict:
    return {
        "tenant_id": ctx.tenant_id,
        "kg_name": ctx.kg_name,
        "type_name": ctx.type_name,
    }


def _build_resolver(ctx: AgentContext):
    """Build a SchemaResolver from the agent context (same wiring the ingest
    route uses). Constructed per call — cheap, and keeps no cross-request state."""
    import tempfile
    from pathlib import Path

    from cograph_client.resolver.schema_resolver import SchemaResolver
    from cograph_client.resolver.verdict_cache import JsonVerdictCache

    cache = JsonVerdictCache(Path(tempfile.gettempdir()) / "omnix-verdict-cache.json")
    return SchemaResolver(
        neptune=ctx.neptune,
        anthropic_key=ctx.anthropic_key,
        verdict_cache=cache,
    )


# Leading filler we can safely drop so the provider sees a cleaner query. We also
# strip a leading "Use these:" / "just the …" confirmation prefix so the cleaned
# query is the discovery subject, not the attribute reply.
_LEAD_FILLER = re.compile(
    r"^\s*(?:i['’]?m\s+looking\s+for|i\s+want|i\s+need|please\s+|can\s+you\s+|"
    r"could\s+you\s+|find\s+me|find|get\s+me|get|pull|fetch|add|search\s+for)\s+"
    r"(?:a\s+|an\s+|the\s+|me\s+)?",
    re.IGNORECASE,
)


def _clean_query(instruction: str) -> str:
    """Best-effort tidy of the instruction into a discovery query. Uses the FIRST
    line (the original ask), dropping later attribute-confirmation replies, then
    strips one leading filler phrase."""
    if not instruction:
        return ""
    first = next(
        (ln.strip() for ln in instruction.splitlines() if ln.strip()),
        instruction.strip(),
    )
    q = _LEAD_FILLER.sub("", first, count=1).strip()
    return q or first


def _column_preview(mapping: CSVSchemaMapping) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for c in mapping.columns:
        name = (c.attribute_name or c.column_name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "datatype": c.datatype})
    return out


def _estimate_cost(
    provider: WebSourceProvider, estimated_total: int, cap: int
) -> dict:
    """Plan-time cost estimate, using the SAME contract keys the plan card reads
    (``estimated_usd`` / ``paid_calls`` / ``note``)."""
    is_paid, cost_per_call = provider_cost(provider)
    rows = min(estimated_total or 0, cap) if cap else (estimated_total or 0)
    if not is_paid:
        return {
            "paid_calls": 0,
            "estimated_usd": 0.0,
            "note": "No paid calls (the configured web source is free).",
        }
    estimated_usd = round(cost_per_call, 4)
    return {
        "paid_calls": 1,
        "paid_calls_estimated": True,
        "estimated_usd": estimated_usd,
        "per_call_cost_usd": round(cost_per_call, 4),
        "note": (
            f"Paid web discovery via '{provider.name}': ≈ ${estimated_usd:.2f} "
            f"to fetch up to {rows} record(s) (estimate; provider may fan out "
            f"across sub-queries)."
        ),
    }


def _answer_step(text: str) -> PlanStep:
    """A single no-write 'answer' step (planner short-circuits it to kind:answer)."""
    return PlanStep(
        capability=WebIngestCapability.name,
        action="answer",
        params={"answer_payload": {"answer": text, "narrative": text}},
        rationale=text,
        confidence=1.0,
    )


def _parse_json_object(text: str) -> dict | None:
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


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


def _slug(v) -> str:
    """snake_case a single attribute name; drop surrounding junk."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(v or "").strip().lower()).strip("_")
    return s


def _pascal(v: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", str(v or "").strip())
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "WebRecord"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        s = (x or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _is_int(v: str) -> bool:
    try:
        int(v.replace(",", ""))
        return True
    except (ValueError, AttributeError):
        return False


def _is_float(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except (ValueError, AttributeError):
        return False
