"""Isolated SLM × Ontology Skills benchmark (INF-606).

This package is a research contract, not product runtime. It must not import
``infona_client`` (including ``qc``, ``eval``, ``skills``, ``research``).
"""

from .compiler import compile_flat, compile_none, compile_routed
from .conditions import CONDITION_MATRIX, condition_by_id
from .dataset import load_fixture_bundle, load_ontology, load_tasks
from .executor import execute_task, run_dry
from .graph_delta import GraphDelta, graph_delta_prf
from .harness import RunResult, write_result_row
from .neighborhood_policy import compile_for_task, neighborhood_for_task
from .parse import parse_graph_delta
from .policy_compile import compile_for_execute
from .prompts import build_prompt
from .scoring import score_prediction, score_task
from .models import (
    CompiledSkillSet,
    EntityType,
    Neighborhood,
    Ontology,
    Relation,
    Skill,
)

__version__ = "0.1.0"

__all__ = [
    "CONDITION_MATRIX",
    "CompiledSkillSet",
    "EntityType",
    "GraphDelta",
    "Neighborhood",
    "Ontology",
    "Relation",
    "RunResult",
    "Skill",
    "compile_flat",
    "compile_for_execute",
    "compile_for_task",
    "compile_none",
    "compile_routed",
    "condition_by_id",
    "execute_task",
    "neighborhood_for_task",
    "graph_delta_prf",
    "load_fixture_bundle",
    "load_ontology",
    "load_tasks",
    "parse_graph_delta",
    "build_prompt",
    "run_dry",
    "score_prediction",
    "score_task",
    "write_result_row",
]
