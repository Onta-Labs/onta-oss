"""Isolated SLM × Ontology Skills benchmark (INF-606).

This package is a research contract, not product runtime. It must not import
``infona_client`` (including ``qc``, ``eval``, ``skills``, ``research``).
"""

from .compiler import compile_flat, compile_none, compile_routed
from .conditions import CONDITION_MATRIX, condition_by_id
from .dataset import load_fixture_bundle, load_ontology, load_tasks
from .graph_delta import GraphDelta, graph_delta_prf
from .harness import RunResult, write_result_row
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
    "compile_none",
    "compile_routed",
    "condition_by_id",
    "graph_delta_prf",
    "load_fixture_bundle",
    "load_ontology",
    "load_tasks",
    "write_result_row",
]
