"""Deterministic prompt builders for comparison-matrix skill modes.

Conditions 1/6/7 → vanilla (no ontology, no skills).
Condition 2 → ontology context (types/relations, no skill bodies).
Condition 3 → flat skill dump.
Conditions 4/8 → routed compiled skills.
Condition 5 is refused by the compiler, not this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from .conditions import Condition
from .dataset import Task
from .models import CompiledSkillSet, Ontology

TEMPLATE_ID = "ontology_skills.prompt.v1"

_SCHEMA_HINT = """Emit a single JSON object and nothing else. Keys (omit empty ones):
  adds: [{subject, predicate, object}]
  deletes: [{subject, predicate, object}]
  type_assertions: [{entity, type_id}]
  literals: [{entity, attr, value}]
  merges: [{absorbed, survivor}]
  type_extensions: [{type_id, parent_id, label}]
  constraint_repairs: [string]
Entity URIs: https://graph.infona.ai/bench/ent/{slug}
Relation IRIs: https://graph.infona.ai/bench/onto/{RELATION_ID}
Assert leaf types only. Do not narrate."""


@dataclass(frozen=True, slots=True)
class BuiltPrompt:
    text: str
    template_id: str
    skill_injection: str

    @property
    def sha256(self) -> str:
        return sha256(self.text.encode("utf-8")).hexdigest()


def build_prompt(
    task: Task,
    ontology: Ontology,
    compiled: CompiledSkillSet,
    condition: Condition,
) -> BuiltPrompt:
    """Build the exact prompt bytes a backend will see. Deterministic."""
    parts = [
        _SCHEMA_HINT,
        "",
        f"family: {task.family}",
        f"split: {task.split}",
        "",
        _mode_block(ontology, compiled, condition),
        "",
        "task_input:",
        json.dumps(dict(task.input), sort_keys=True, ensure_ascii=False, indent=2),
    ]
    text = "\n".join(parts).rstrip() + "\n"
    return BuiltPrompt(
        text=text,
        template_id=TEMPLATE_ID,
        skill_injection=condition.skill_mode,
    )


def _mode_block(
    ontology: Ontology, compiled: CompiledSkillSet, condition: Condition
) -> str:
    mode = condition.skill_mode
    if mode == "none":
        return "skills: none\nontology: none"
    if mode == "ontology_context":
        return _ontology_context(ontology, compiled)
    if mode in ("flat", "routed"):
        return _skill_block(compiled, mode)
    raise RuntimeError(f"unhandled skill_mode {mode!r}")


def _ontology_context(ontology: Ontology, compiled: CompiledSkillSet) -> str:
    lines = ["ontology_context:"]
    for type_id in compiled.type_lineage:
        typ = ontology.types[type_id]
        parents = ",".join(typ.parent_ids) if typ.parent_ids else "-"
        lines.append(f"  type {type_id} parents={parents}")
    for rel_id in compiled.relation_ids:
        rel = ontology.relations[rel_id]
        lines.append(
            f"  relation {rel_id} {rel.source_type}->{rel.target_type}"
        )
    lines.append("skills: none")
    return "\n".join(lines)


def _skill_block(compiled: CompiledSkillSet, mode: str) -> str:
    lines = [f"skills ({mode}):"]
    if not compiled.skills:
        lines.append("  (empty)")
        return "\n".join(lines)
    for skill in compiled.skills:
        lines.append(f"### {skill.skill_id} [{skill.kind}:{skill.attached_to}]")
        lines.append(skill.body.strip())
        lines.append("")
    return "\n".join(lines).rstrip()
