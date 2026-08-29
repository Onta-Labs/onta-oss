"""Scoring contract entrypoints. Primary metrics are exact / task-specific.

There is no LLM-as-judge function here. Later slices must not add one as a
primary metric; a diagnostic judge, if ever added, lives behind a flag and
cannot populate ``metrics.success``.
"""

from .graph_delta import GraphDelta, graph_delta_prf, pairwise_er_prf, task_success

__all__ = [
    "GraphDelta",
    "graph_delta_prf",
    "pairwise_er_prf",
    "task_success",
]
