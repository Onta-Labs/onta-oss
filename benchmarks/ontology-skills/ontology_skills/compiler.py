"""Deterministic neighborhood → compiled skill set.

This is a capability router, not retrieval. Given an ontology and a neighborhood,
the compiler walks type/relation inheritance and emits a unique ordered skill
set. Same inputs always yield the same ``CompiledSkillSet`` (including order).

Normative algorithm is locked in SPEC.md §Compilation. Do not add scoring,
embedding, or LLM choice points here.
"""

from __future__ import annotations

from .models import (
    CompiledSkillSet,
    CompileMode,
    Neighborhood,
    Ontology,
    OntologyError,
    Skill,
)


def compile_none(ontology: Ontology, neighborhood: Neighborhood) -> CompiledSkillSet:
    """Condition 1 / 6 / 7 vanilla, and the skill half of condition 2.

    Lineage is still computed so the harness can inject ontology context without
    skills (condition 2) from the same neighborhood object.
    """
    lineage, rels = _expand(ontology, neighborhood)
    return CompiledSkillSet(
        mode="none",
        skills=(),
        type_lineage=lineage,
        relation_ids=rels,
    )


def compile_flat(ontology: Ontology) -> CompiledSkillSet:
    """WikiSkill-style dump: every enabled skill, no neighborhood routing.

    Order is ``(kind, attached_to, skill_id)`` so a shuffled skill tuple in the
    ontology cannot change the emitted set.
    """
    enabled = [s for s in ontology.skills if s.enabled]
    enabled.sort(key=lambda s: (s.kind, s.attached_to, s.skill_id))
    suppressed = tuple(
        sorted(s.skill_id for s in ontology.skills if not s.enabled)
    )
    return CompiledSkillSet(
        mode="flat",
        skills=tuple(enabled),
        type_lineage=tuple(sorted(ontology.types)),
        relation_ids=tuple(sorted(ontology.relations)),
        suppressed_skill_ids=suppressed,
    )


def compile_routed(
    ontology: Ontology, neighborhood: Neighborhood
) -> CompiledSkillSet:
    """Primary Infona condition: skills from the local neighborhood only."""
    return _compile(ontology, neighborhood, mode="routed")


def _compile(
    ontology: Ontology,
    neighborhood: Neighborhood,
    *,
    mode: CompileMode,
) -> CompiledSkillSet:
    lineage, rels = _expand(ontology, neighborhood)
    skills: list[Skill] = []
    suppressed: list[str] = []
    seen: set[str] = set()

    def take(attached_to: str) -> None:
        for skill in ontology.skills_for(attached_to):
            if skill.skill_id in seen:
                continue
            seen.add(skill.skill_id)
            if not skill.enabled:
                suppressed.append(skill.skill_id)
                continue
            skills.append(skill)

    for type_id in lineage:
        take(type_id)
    for rel_id in rels:
        take(rel_id)

    return CompiledSkillSet(
        mode=mode,
        skills=tuple(skills),
        type_lineage=lineage,
        relation_ids=rels,
        suppressed_skill_ids=tuple(suppressed),
    )


def _expand(
    ontology: Ontology, neighborhood: Neighborhood
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    lineage = _lineage(
        {k: v.parent_ids for k, v in ontology.types.items()},
        neighborhood.type_ids,
        include_ancestors=neighborhood.include_ancestors,
        kind="type",
    )
    specified_rels = _lineage(
        {k: v.parent_ids for k, v in ontology.relations.items()},
        neighborhood.relation_ids,
        include_ancestors=neighborhood.include_ancestors,
        kind="relation",
    )
    incident: list[str] = []
    if neighborhood.include_incident_relations:
        type_set = set(lineage)
        for rel_id in sorted(ontology.relations):
            rel = ontology.relations[rel_id]
            if rel.source_type in type_set or rel.target_type in type_set:
                incident.append(rel_id)
    # Specified relations first (seed order, ancestors folded), then incident
    # relations not already present, alphabetical — then fold relation parents.
    merged_seeds = _unique(specified_rels + tuple(incident))
    rels = _lineage(
        {k: v.parent_ids for k, v in ontology.relations.items()},
        merged_seeds,
        include_ancestors=neighborhood.include_ancestors,
        kind="relation",
    )
    return lineage, rels


def _lineage(
    parents_of: dict[str, tuple[str, ...]],
    seeds: tuple[str, ...],
    *,
    include_ancestors: bool,
    kind: str,
) -> tuple[str, ...]:
    for seed in seeds:
        if seed not in parents_of:
            raise OntologyError(f"unknown {kind} id {seed!r}")
    if not include_ancestors:
        return _unique(seeds)

    # Specific-first DFS: emit the node, then walk parent_ids in listed order.
    # Seed order is preserved; a later seed that was already visited as an
    # ancestor is skipped. Cycles raise. No hash-randomized iteration.
    out: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def dfs(nid: str) -> None:
        if nid in done:
            return
        if nid in visiting:
            raise OntologyError(f"{kind} inheritance cycle at {nid!r}")
        visiting.add(nid)
        out.append(nid)
        for parent in parents_of[nid]:
            if parent not in parents_of:
                raise OntologyError(f"unknown {kind} parent {parent!r}")
            dfs(parent)
        visiting.remove(nid)
        done.add(nid)

    for seed in seeds:
        dfs(seed)
    return tuple(out)


def _unique(ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for nid in ids:
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    return tuple(out)
