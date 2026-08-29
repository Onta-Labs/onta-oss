"""Exact-set scoring. No judge model."""

from __future__ import annotations

from ontology_skills.graph_delta import GraphDelta, Merge, Triple, TypeAssertion
from ontology_skills.scoring import graph_delta_prf, pairwise_er_prf, task_success


def test_exact_match_is_success() -> None:
    gold = GraphDelta(
        type_assertions=(
            TypeAssertion("https://graph.infona.ai/bench/ent/acme-components", "Supplier"),
        )
    )
    assert task_success(gold, gold) is True
    prf = graph_delta_prf(gold, gold)
    assert prf == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_extra_op_hurts_precision() -> None:
    gold = GraphDelta(
        adds=(
            Triple("s", "https://graph.infona.ai/bench/onto/SUPPLIES_TO", "o"),
        )
    )
    predicted = GraphDelta(
        adds=gold.adds
        + (Triple("s", "https://graph.infona.ai/bench/onto/EMPLOYS", "x"),)
    )
    prf = graph_delta_prf(predicted, gold)
    assert prf["precision"] == 0.5
    assert prf["recall"] == 1.0
    assert task_success(predicted, gold) is False


def test_er_pairs_are_undirected() -> None:
    gold = (Merge("a", "b"),)
    predicted = (Merge("b", "a"),)
    prf = pairwise_er_prf(predicted, gold)
    assert prf == {"precision": 1.0, "recall": 1.0, "f1": 1.0}
