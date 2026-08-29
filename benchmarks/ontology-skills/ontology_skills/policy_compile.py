"""Optional compile path that applies neighborhood policy.

``executor.execute_task`` still calls
``compile_for_condition(ontology, task.neighborhood, condition)``.
Swap that call to :func:`compile_for_execute` when merging this fix. This
module does not import or patch ``executor``.
"""

from __future__ import annotations

from .conditions import Condition
from .dataset import Task
from .models import CompiledSkillSet, Ontology
from .neighborhood_policy import compile_for_task


def compile_for_execute(
    ontology: Ontology, task: Task, condition: Condition
) -> CompiledSkillSet:
    """Drop-in for ``compile_for_condition(ontology, task.neighborhood, condition)``."""
    return compile_for_task(ontology, task, condition)
