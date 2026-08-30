"""Compile path that applies neighborhood policy, then the condition's picker.

``executor.execute_task`` calls :func:`select_for_execute` (compiled skills plus
the RAG retrieval log). :func:`compile_for_execute` is the drop-in for
``compile_for_condition(ontology, task.neighborhood, condition)``.
"""

from __future__ import annotations

from .conditions import Condition
from .dataset import Task
from .models import CompiledSkillSet, Ontology
from .neighborhood_policy import compile_for_task, select_for_task


def compile_for_execute(
    ontology: Ontology,
    task: Task,
    condition: Condition,
    *,
    embedder: object | None = None,
) -> CompiledSkillSet:
    """Drop-in for ``compile_for_condition(ontology, task.neighborhood, condition)``."""
    return compile_for_task(ontology, task, condition, embedder=embedder)


def select_for_execute(
    ontology: Ontology,
    task: Task,
    condition: Condition,
    *,
    embedder: object | None = None,
) -> tuple[CompiledSkillSet, dict[str, object]]:
    """Executor entry: family neighborhood, then routed/flat/none/rag."""
    return select_for_task(ontology, task, condition, embedder=embedder)
