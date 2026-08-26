"""Layer-aware skill resolution + the AGENT-INJECTION SEAM.

Resolution reuses the ontology's own precedence machinery
(``graph/layers.py::LayerStack``) rather than minting a second layer vocabulary:
``stack.layers`` yields the VISIBLE layers in precedence order and already drops
``ENHANCED`` for a non-entitled tenant. Skills therefore inherit the ontology's
entitlement behaviour for free — a non-entitled workspace simply never sees an
Enhanced skill, and resolution degrades to ``Tenant > Public`` without erroring.

**Union-with-shadowing, not pure shadowing.** ``LayerStack.resolve_type`` picks
exactly ONE definition because a type has one definition. A type can have MANY
skills, and they compose: a Public skill ("a Person's name may be in any
script") and a Tenant skill ("in this workspace a Person is always a
clinician") are both true and an agent wants both. So skills from every visible
layer ACCUMULATE, and a higher layer shadows a lower one only when they collide
on the same ``(type, slug)`` — that collision is the override mechanism (a
tenant re-declares ``naming-conventions`` to replace the curated one). Same
semantics as the API-source catalog's layer merge, which shadows by slug.

Ordering is precedence order (Tenant, then Enhanced, then Public). That matters
for the prompt: the most specific guidance appears first, and if the character
budget truncates anything it truncates the most general guidance last-first.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

import structlog

from infona_client.graph.layers import Layer, LayerStack
from infona_client.graph.queries import tenant_graph_uri

from .models import TypeSkill
from .registry import global_skills_by_layer
from .store import TypeSkillStore, make_type_skill_store

logger = structlog.stdlib.get_logger("infona.skills.resolve")

#: Default character budget for one injected skills block. Sized to be
#: meaningful guidance without crowding out the schema/question in a planner
#: prompt; callers with a tighter budget pass their own.
DEFAULT_PROMPT_BUDGET = 6_000


def merge_layers(
    skills_by_layer: dict[Layer, Sequence[TypeSkill]],
    stack: LayerStack,
    *,
    type_name: Optional[str] = None,
    include_disabled: bool = False,
) -> list[TypeSkill]:
    """Merge per-layer skills into one resolved list (union-with-shadowing).

    Pure and synchronous — all the resolution SEMANTICS live here so they can be
    tested without a store, a graph, or an event loop.
    """
    want = type_name.casefold() if type_name else None
    seen: set[tuple[str, str]] = set()
    out: list[TypeSkill] = []
    for layer in stack.layers:  # precedence order, entitlement already applied
        for skill in skills_by_layer.get(layer, []) or []:
            if want is not None and skill.type_name.casefold() != want:
                continue
            if not include_disabled and not skill.enabled:
                # Still claim the key: a DISABLED higher-layer skill is an
                # explicit "this guidance does not apply here", so it must
                # suppress the lower layer's version rather than fall through
                # to it.
                seen.add(skill.key)
                continue
            if skill.key in seen:
                continue
            seen.add(skill.key)
            out.append(skill)
    return out


async def resolve_skills(
    type_name: str,
    *,
    tenant_id: str,
    entitled: bool = False,
    store: Optional[TypeSkillStore] = None,
    include_disabled: bool = False,
) -> list[TypeSkill]:
    """All skills visible to ``tenant_id`` for ``type_name``, in precedence order.

    Tenant skills come from the durable store; the two global layers come from
    the curated registry. Never raises on a store failure — an unreachable
    tenant store degrades to the global layers only, mirroring
    ``fetch_types_by_layer``'s "a broken layer is an empty layer" contract.
    """
    stack = LayerStack(tenant_graph_uri=tenant_graph_uri(tenant_id), entitled=entitled)

    by_layer: dict[Layer, Sequence[TypeSkill]] = dict(global_skills_by_layer())
    try:
        tenant_store = store if store is not None else make_type_skill_store()
        by_layer[Layer.TENANT] = await tenant_store.list_for_tenant(
            tenant_id, type_name
        )
    except Exception:
        logger.warning(
            "tenant_skill_layer_unavailable", tenant_id=tenant_id, exc_info=True
        )
        by_layer[Layer.TENANT] = []

    return merge_layers(
        by_layer, stack, type_name=type_name, include_disabled=include_disabled
    )


# --------------------------------------------------------------------------- #
# The agent-injection seam
# --------------------------------------------------------------------------- #
def render_skills_block(
    skills: Iterable[TypeSkill],
    *,
    max_chars: int = DEFAULT_PROMPT_BUDGET,
    heading: str = "TYPE SKILLS",
) -> str:
    """Render resolved skills as ONE deterministic markdown block for a prompt.

    Returns ``""`` for no skills — callers concatenate unconditionally and a
    workspace with no skills authored gets a byte-identical prompt to today's.
    That "empty means invisible" property is what makes this safe to call from
    every prompt assembler.

    Truncation is per-skill and announced (``[truncated]``) so a model is never
    silently handed half a sentence as if it were the whole instruction, and
    skills that do not fit at all are reported as an omitted count rather than
    vanishing.
    """
    skills = list(skills)
    if not skills:
        return ""

    parts: list[str] = [
        f"## {heading}",
        "Curated guidance about these entity types. Treat it as authoritative "
        "instruction about what the types mean in this workspace.",
    ]
    used = sum(len(p) for p in parts)
    omitted = 0

    for skill in skills:
        label = skill.title or skill.slug
        header = f"\n### {skill.type_name} — {label} ({skill.layer.value})"
        body = skill.body.strip()
        remaining = max_chars - used - len(header)
        if remaining <= 0:
            omitted += 1
            continue
        if len(body) > remaining:
            body = body[:remaining].rstrip() + "\n[truncated]"
        parts.append(header)
        parts.append(body)
        used += len(header) + len(body)

    if omitted:
        parts.append(f"\n[{omitted} further skill(s) omitted — prompt budget]")
    return "\n".join(parts)


async def skills_prompt_block(
    type_names: Iterable[str],
    *,
    tenant_id: str,
    entitled: bool = False,
    store: Optional[TypeSkillStore] = None,
    max_chars: int = DEFAULT_PROMPT_BUDGET,
) -> str:
    """**THE SEAM.** Resolved skills for ``type_names``, ready to concatenate
    into any LM prompt.

    This is the single function every agent-context assembler should call. One
    function so the injected text cannot drift per surface (the
    interface-convergence rule applied to prompt context), and so a change to
    budgeting or ordering lands everywhere at once.

    Contract, in order of importance:

    * **Never raises.** Any failure resolving skills degrades to ``""``. A
      broken skills store must never take down a query.
    * **Empty means invisible.** No skills → ``""`` → the caller's prompt is
      byte-identical to today's. This is what makes the seam safe to call
      unconditionally from a hot prompt path.
    * Deduped across the requested types, precedence-ordered, budget-capped.

    THE INJECTION POINTS (surveyed and exact). Today no per-type prose reached
    any prompt: the ontology read used for prompts
    (``ontology_queries.get_full_ontology_query``) does not even project
    ``rdfs:comment``. These four are net-new context, not a swap. Empty block
    == noop (prompts stay byte-identical).

    1. **NL→Cypher /ask** (production) — ``nlp/pipeline_cypher_exec._try_llm_cypher``
       appends this block to the ontology string handed to
       ``build_cypher_generation_prompt``. Residual SPARQL
       ``_generate_sparql`` / ``build_generation_prompt`` does the same.
    2. **Unified agent capabilities** — all three NL→params extractors
       (``enrich_extract._extract_enrich_request``,
       ``normalize_cap._extract_normalize_directive``,
       ``ontology_cap._extract_directive``) format
       ``Type / Attributes / Relationships`` from
       ``normalization/inference.list_type_schema``. That primitive now carries
       a ``skills`` key; extractors append it when non-empty.
    3. **Agent planner** — ``agent/planner_classify._classify`` appends this
       block when ``ctx.type_name`` is set.
    4. **MCP ``view_ontology``** — still unwired (clients must call
       ``GET /graphs/{tenant}/skills/prompt-block`` rather than render).

    **Wiring status:** 1–3 are always-on. MCP (4) is out of this surface.
    """
    # dict.fromkeys is the dedup: it collapses a repeated type (an agent that
    # names the same type twice) into ONE resolution. No second dedup pass over
    # the resolved skills is needed — TypeSkill.key is type-scoped, so two
    # DISTINCT types can never produce colliding keys, and within one type
    # merge_layers has already applied shadowing.
    names = [n for n in dict.fromkeys(type_names) if n]
    if not names:
        return ""
    try:
        resolved: list[TypeSkill] = []
        for name in names:
            resolved.extend(
                await resolve_skills(
                    name, tenant_id=tenant_id, entitled=entitled, store=store
                )
            )
        return render_skills_block(resolved, max_chars=max_chars)
    except Exception:
        logger.warning("skills_prompt_block_failed", tenant_id=tenant_id, exc_info=True)
        return ""


__all__ = [
    "resolve_skills",
    "merge_layers",
    "render_skills_block",
    "skills_prompt_block",
    "DEFAULT_PROMPT_BUDGET",
]
