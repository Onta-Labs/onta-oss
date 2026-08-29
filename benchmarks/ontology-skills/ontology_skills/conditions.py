"""Comparison matrix. Order is the research contract; do not reorder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SkillMode = Literal["none", "ontology_context", "flat", "routed", "rag"]
ModelBucket = Literal["4b", "9b", "27b_or_frontier"]
SkillSource = Literal["none", "executor", "teacher"]


@dataclass(frozen=True, slots=True)
class Condition:
    index: int
    condition_id: str
    name: str
    model_bucket: ModelBucket
    skill_mode: SkillMode
    fine_tuned: bool
    skill_source: SkillSource
    runnable: bool
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "condition_id": self.condition_id,
            "name": self.name,
            "model_bucket": self.model_bucket,
            "skill_mode": self.skill_mode,
            "fine_tuned": self.fine_tuned,
            "skill_source": self.skill_source,
            "runnable": self.runnable,
            "blocked_reason": self.blocked_reason,
        }


# Locked order. Condition 5 is present so later slices cannot invent a
# different numbering; it is not runnable. Condition 9 is RAG over the
# same fixture skill bodies (not compile_routed / compile_flat).
CONDITION_MATRIX: tuple[Condition, ...] = (
    Condition(1, "4b_vanilla", "4B vanilla", "4b", "none", False, "none", True),
    Condition(
        2,
        "4b_ontology_context",
        "4B + ontology context only",
        "4b",
        "ontology_context",
        False,
        "none",
        True,
    ),
    Condition(
        3,
        "4b_flat_skills",
        "4B + flat/full skill set",
        "4b",
        "flat",
        False,
        "executor",
        True,
    ),
    Condition(
        4,
        "4b_ontology_routed",
        "4B + ontology-routed skills",
        "4b",
        "routed",
        False,
        "executor",
        True,
    ),
    Condition(
        5,
        "4b_ft_ontology_routed",
        "4B fine-tuned + ontology-routed skills",
        "4b",
        "routed",
        True,
        "executor",
        False,
        "Fine-tuning is blocked until conditions 1–4, 6–9 have been run. "
        "Do not start with FT.",
    ),
    Condition(6, "9b_vanilla", "9B vanilla", "9b", "none", False, "none", True),
    Condition(
        7,
        "27b_or_frontier_vanilla",
        "27B or frontier vanilla",
        "27b_or_frontier",
        "none",
        False,
        "none",
        True,
    ),
    Condition(
        8,
        "teacher_skills_4b",
        "Strong-teacher-generated skills → 4B executor",
        "4b",
        "routed",
        False,
        "teacher",
        True,
    ),
    Condition(
        9,
        "4b_rag_skills",
        "4B + skills retrieved by embedding similarity",
        "4b",
        "rag",
        False,
        "executor",
        True,
    ),
)

PRIMARY_CONDITION_ID = "4b_ontology_routed"


def condition_by_id(condition_id: str) -> Condition:
    for cond in CONDITION_MATRIX:
        if cond.condition_id == condition_id:
            return cond
    known = ", ".join(c.condition_id for c in CONDITION_MATRIX)
    raise KeyError(f"unknown condition {condition_id!r}; known: {known}")


def condition_by_index(index: int) -> Condition:
    for cond in CONDITION_MATRIX:
        if cond.index == index:
            return cond
    raise KeyError(f"unknown condition index {index}")
