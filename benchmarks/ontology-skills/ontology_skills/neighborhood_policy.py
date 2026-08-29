"""Family-aware neighborhood before compile.

Fixture rows often set ``include_incident_relations=True``. That is right for
``relation_inference``: the compiler should pull SUPPLIES_TO skills. On
``entity_typing`` it is a precision leak. Live et-001 routed had recall 1.0 and
missed exact match because the compiler stapled ``temporal-window`` /
``quantity-validation`` (attached to incident SUPPLIES_TO) onto a typing task,
and the model emitted an extra edge.

This module rewrites the neighborhood, then calls ``compile_routed`` /
``compile_flat`` / ``compile_none``. It does not change ``compiler.py``.
"""

from __future__ import annotations

from dataclasses import replace

from .compiler import compile_flat, compile_none, compile_routed
from .conditions import PRIMARY_CONDITION_ID, Condition, condition_by_id
from .dataset import Task
from .models import CompiledSkillSet, Neighborhood, Ontology

# Typing and column-mapping do not need relation skills. Incident edges and
# leftover fixture relation_ids are dropped so the compiler cannot attach
# SUPPLIES_TO (or any other relation) skills.
_DROP_RELATIONS: frozenset[str] = frozenset(
    {
        "entity_typing",
        "property_schema_mapping",
    }
)

# These families write or compose edges. Keep incident relations as authored
# (and force the flag on so a False in the fixture cannot strip them).
_KEEP_INCIDENT: frozenset[str] = frozenset(
    {
        "relation_inference",
        "multi_step_ingest",
    }
)

# Remaining families: entity_resolution, constraint_violation_repair,
# conflict_resolution, ontology_extension.
# Drop automatic incident relations so unused relation skills stay off the
# prompt. Keep author-seeded relation_ids. CVR already seeds the relations it
# repairs; ER / conflict / extension do not need a stapled SUPPLIES_TO skill.


def neighborhood_for_task(task: Task) -> Neighborhood:
    """Return a compile neighborhood for ``task.family``. Does not mutate ``task``."""
    nb = task.neighborhood
    if task.family in _DROP_RELATIONS:
        return replace(
            nb,
            relation_ids=(),
            include_incident_relations=False,
        )
    if task.family in _KEEP_INCIDENT:
        return replace(nb, include_incident_relations=True)
    return replace(nb, include_incident_relations=False)


def compile_for_task(
    ontology: Ontology,
    task: Task,
    condition: Condition | None = None,
) -> CompiledSkillSet:
    """Apply :func:`neighborhood_for_task`, then compile as ``condition`` requests.

    ``condition`` defaults to the primary routed condition so
    ``compile_for_task(ontology, task)`` is the leak-fix path.
    """
    cond = condition if condition is not None else condition_by_id(
        PRIMARY_CONDITION_ID
    )
    if not cond.runnable:
        raise RuntimeError(
            f"condition {cond.condition_id} is blocked: {cond.blocked_reason}"
        )
    neighborhood = neighborhood_for_task(task)
    if cond.skill_mode in ("none", "ontology_context"):
        return compile_none(ontology, neighborhood)
    if cond.skill_mode == "flat":
        return compile_flat(ontology)
    if cond.skill_mode == "routed":
        return compile_routed(ontology, neighborhood)
    raise RuntimeError(f"unhandled skill_mode {cond.skill_mode!r}")
