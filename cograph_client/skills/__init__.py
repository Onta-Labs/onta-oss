"""Type-attached SKILLS — markdown instruction attached to an entity type,
whose consumer is an LM agent.

    Skills teach. Functions compute.

A skill is prose (``cograph_client/skills``); a function is an async endpoint
invoked against a node (``cograph_client/functions``). They are separate
concepts with separate storage, CRUD, and consumers — do not merge them.

Public surface:

* :class:`TypeSkill` / :func:`validate_skill` — the model.
* :func:`make_type_skill_store` — the durable per-tenant store (Postgres when a
  DSN is configured, in-memory otherwise).
* :func:`make_global_type_skill_store` — durable **Enhanced** global skill store
  that survives restart/redeploy (ONTA-399); Public remains reserved empty.
* :func:`register_skill_layer` — process-registry seam (premium file overlay).
* :func:`global_skills_for_type` — the operator Global Ontology assembler's read
  function (merges process registry + durable Enhanced mirror).
* :func:`resolve_skills` — Tenant > Enhanced > Public resolution for one tenant.
* :func:`skills_prompt_block` — **the agent-injection seam**: resolved skills
  rendered for a prompt, never raising, empty when there is nothing to say.
  Wiring into production prompts is a separate founder decision (out of scope
  for ONTA-399).
"""

from .global_store import (
    GlobalTypeSkillStore,
    InMemoryGlobalTypeSkillStore,
    PostgresGlobalTypeSkillStore,
    hydrate_global_skills_from_store,
    make_global_type_skill_store,
    reset_global_type_skill_store,
)
from .models import (
    MAX_BODY_CHARS,
    TypeSkill,
    validate_skill,
)
from .registry import (
    global_skills_by_layer,
    global_skills_for_type,
    invalidate_skill_cache,
    register_skill_layer,
    reset_skill_layers,
)
from .resolve import (
    DEFAULT_PROMPT_BUDGET,
    merge_layers,
    render_skills_block,
    resolve_skills,
    skills_prompt_block,
)
from .store import (
    InMemoryTypeSkillStore,
    PostgresTypeSkillStore,
    TypeSkillStore,
    make_type_skill_store,
    reset_type_skill_store,
)

__all__ = [
    "TypeSkill",
    "validate_skill",
    "MAX_BODY_CHARS",
    "TypeSkillStore",
    "InMemoryTypeSkillStore",
    "PostgresTypeSkillStore",
    "make_type_skill_store",
    "reset_type_skill_store",
    "GlobalTypeSkillStore",
    "InMemoryGlobalTypeSkillStore",
    "PostgresGlobalTypeSkillStore",
    "make_global_type_skill_store",
    "reset_global_type_skill_store",
    "hydrate_global_skills_from_store",
    "register_skill_layer",
    "reset_skill_layers",
    "invalidate_skill_cache",
    "global_skills_by_layer",
    "global_skills_for_type",
    "resolve_skills",
    "merge_layers",
    "render_skills_block",
    "skills_prompt_block",
    "DEFAULT_PROMPT_BUDGET",
]
