"""Scoring contract entrypoints. Primary metrics are exact / task-specific.

There is no LLM-as-judge function here. A diagnostic judge, if ever added,
must not populate ``metrics.success``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .align import prepare_for_score
from .dataset import Task, load_tasks
from .graph_delta import (
    GraphDelta,
    LiteralSet,
    Merge,
    Triple,
    TypeAssertion,
    graph_delta_prf,
    pairwise_er_prf,
    task_success,
)

SUPPLIES_TO = "https://graph.infona.ai/bench/onto/SUPPLIES_TO"
EMPLOYS = "https://graph.infona.ai/bench/onto/EMPLOYS"
QTY_ATTRS = frozenset({"qty", "quantity"})

METRIC_KEYS = (
    "success",
    "graph_delta_precision",
    "graph_delta_recall",
    "graph_delta_f1",
    "constraint_valid",
    "er_precision",
    "er_recall",
    "er_f1",
)


def empty_metrics() -> dict[str, None]:
    return {key: None for key in METRIC_KEYS}


def score_prediction(
    predicted: GraphDelta,
    gold: GraphDelta,
    *,
    family: str,
    constraints: tuple[str, ...] = (),
    base_graph: GraphDelta | None = None,
    task_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill the locked metric keys for one predicted-vs-gold pair.

    ``graph_delta_prf`` / ``task_success`` stay exact-set. This function first
    aligns minted entities and drops adds that restate structured facts.
    """
    prepared = prepare_for_score(predicted, gold, task_input)
    prf = graph_delta_prf(prepared, gold)
    metrics: dict[str, Any] = {
        "success": task_success(prepared, gold),
        "graph_delta_precision": prf["precision"],
        "graph_delta_recall": prf["recall"],
        "graph_delta_f1": prf["f1"],
        "constraint_valid": None,
        "er_precision": None,
        "er_recall": None,
        "er_f1": None,
    }
    if family == "constraint_violation_repair":
        metrics["constraint_valid"] = constraints_hold(
            apply_delta(base_graph or GraphDelta(), prepared),
            constraints,
        )
    if family == "entity_resolution":
        er = pairwise_er_prf(prepared.merges, gold.merges)
        metrics["er_precision"] = er["precision"]
        metrics["er_recall"] = er["recall"]
        metrics["er_f1"] = er["f1"]
    return metrics


def score_task(predicted: GraphDelta, task: Task) -> dict[str, Any]:
    constraints = tuple(task.input.get("constraints") or ())
    return score_prediction(
        predicted,
        task.gold,
        family=task.family,
        constraints=constraints,
        base_graph=base_graph_from_input(task.input),
        task_input=task.input,
    )


def apply_delta(base: GraphDelta, delta: GraphDelta) -> GraphDelta:
    """Apply predicted ops to the input fragment (set-union, deletes win)."""
    dropped = {
        (t.subject, t.predicate, t.object) for t in delta.deletes
    }
    adds = [
        t
        for t in (*base.adds, *delta.adds)
        if (t.subject, t.predicate, t.object) not in dropped
    ]
    types = list(base.type_assertions) + list(delta.type_assertions)
    lit_map = {(item.entity, item.attr): item for item in base.literals}
    for item in delta.literals:
        if item.value == "":
            lit_map.pop((item.entity, item.attr), None)
            continue
        lit_map[(item.entity, item.attr)] = item
    mapping = _merge_map((*base.merges, *delta.merges))
    return GraphDelta(
        adds=_rewrite_triples(adds, mapping),
        deletes=(),
        type_assertions=_rewrite_types(types, mapping),
        literals=_rewrite_literals(lit_map.values(), mapping),
        merges=(),
        type_extensions=base.type_extensions + delta.type_extensions,
        constraint_repairs=base.constraint_repairs + delta.constraint_repairs,
    )


def base_graph_from_input(inp: Mapping[str, Any]) -> GraphDelta:
    if "graph" in inp:
        return GraphDelta.from_dict(inp.get("graph") or {})
    types: list[TypeAssertion] = []
    adds: list[Triple] = []
    lits: list[LiteralSet] = []
    existing = inp.get("existing")
    if isinstance(existing, dict) and existing.get("entity"):
        types.append(
            TypeAssertion(str(existing["entity"]), str(existing["type_id"]))
        )
    illegal = inp.get("illegal")
    if isinstance(illegal, dict) and illegal.get("subject"):
        adds.append(
            Triple(
                str(illegal["subject"]),
                str(illegal["predicate"]),
                str(illegal["object"]),
            )
        )
    for lit in inp.get("literals") or ():
        if isinstance(lit, dict):
            lits.append(
                LiteralSet(str(lit["entity"]), str(lit["attr"]), str(lit["value"]))
            )
    return GraphDelta(
        adds=tuple(adds),
        type_assertions=tuple(types),
        literals=tuple(lits),
    )


def constraints_hold(graph: GraphDelta, constraint_ids: Iterable[str]) -> bool:
    ids = tuple(constraint_ids)
    if not ids:
        return True
    checkers = {
        "person_not_source_supplies_to": _person_not_source_supplies_to,
        "qty_non_negative": _qty_non_negative,
        "registration_id_not_on_person": _registration_id_not_on_person,
        "supplies_to_source_is_supplier": _supplies_to_source_is_supplier,
        "employs_target_is_person": _employs_target_is_person,
    }
    for cid in ids:
        fn = checkers.get(cid)
        if fn is None:
            raise ValueError(f"unknown constraint {cid!r}")
        if not fn(graph):
            return False
    return True


def _typed(graph: GraphDelta, type_id: str) -> set[str]:
    return {t.entity for t in graph.type_assertions if t.type_id == type_id}


def _person_not_source_supplies_to(graph: GraphDelta) -> bool:
    people = _typed(graph, "Person")
    for triple in graph.adds:
        if triple.predicate == SUPPLIES_TO and triple.subject in people:
            return False
    return True


def _supplies_to_source_is_supplier(graph: GraphDelta) -> bool:
    suppliers = _typed(graph, "Supplier")
    for triple in graph.adds:
        if triple.predicate == SUPPLIES_TO and triple.subject not in suppliers:
            return False
    return True


def _employs_target_is_person(graph: GraphDelta) -> bool:
    people = _typed(graph, "Person")
    for triple in graph.adds:
        if triple.predicate == EMPLOYS and triple.object not in people:
            return False
    return True


def _qty_non_negative(graph: GraphDelta) -> bool:
    for lit in graph.literals:
        if lit.attr not in QTY_ATTRS:
            continue
        try:
            if float(lit.value) < 0:
                return False
        except ValueError:
            return False
    return True


def _registration_id_not_on_person(graph: GraphDelta) -> bool:
    people = _typed(graph, "Person")
    for lit in graph.literals:
        if lit.attr == "registrationId" and lit.entity in people and lit.value != "":
            return False
    return True


def _merge_map(merges: Iterable[Merge]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for merge in merges:
        mapping[merge.absorbed] = merge.survivor
    changed = True
    while changed:
        changed = False
        for src, dst in list(mapping.items()):
            if dst in mapping and mapping[dst] != dst:
                mapping[src] = mapping[dst]
                changed = True
    return mapping


def _rewrite_triples(
    triples: Iterable[Triple], mapping: dict[str, str]
) -> tuple[Triple, ...]:
    return tuple(
        Triple(
            mapping.get(t.subject, t.subject),
            t.predicate,
            mapping.get(t.object, t.object),
        )
        for t in triples
    )


def _rewrite_types(
    types: Iterable[TypeAssertion], mapping: dict[str, str]
) -> tuple[TypeAssertion, ...]:
    return tuple(
        TypeAssertion(mapping.get(t.entity, t.entity), t.type_id) for t in types
    )


def _rewrite_literals(
    literals: Iterable[LiteralSet], mapping: dict[str, str]
) -> tuple[LiteralSet, ...]:
    return tuple(
        LiteralSet(mapping.get(item.entity, item.entity), item.attr, item.value)
        for item in literals
    )


def score_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a predicted GraphDelta against gold (no model)."
    )
    parser.add_argument("--predicted", type=Path, required=True)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--gold", type=Path, default=None)
    parser.add_argument("--family", default="entity_typing")
    args = parser.parse_args(argv)
    predicted = GraphDelta.from_dict(json.loads(args.predicted.read_text()))
    if args.task_id:
        tasks = {t.task_id: t for t in load_tasks()}
        if args.task_id not in tasks:
            raise SystemExit(f"unknown task_id {args.task_id!r}")
        metrics = score_task(predicted, tasks[args.task_id])
        print(json.dumps({"task_id": args.task_id, "metrics": metrics}, indent=2))
        return 0
    if args.gold is None:
        raise SystemExit("pass --task-id or --gold")
    gold = GraphDelta.from_dict(json.loads(args.gold.read_text()))
    metrics = score_prediction(predicted, gold, family=args.family)
    print(json.dumps({"metrics": metrics}, indent=2))
    return 0


__all__ = [
    "GraphDelta",
    "METRIC_KEYS",
    "apply_delta",
    "empty_metrics",
    "graph_delta_prf",
    "pairwise_er_prf",
    "score_main",
    "score_prediction",
    "score_task",
    "task_success",
]
