"""Canonical graph-delta ops and exact-set P/R/F1.

Primary scoring is structured. There is no LLM-as-judge path in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Triple:
    subject: str
    predicate: str
    object: str

    def canonical(self) -> str:
        return f"ADD\t{self.subject}\t{self.predicate}\t{self.object}"


@dataclass(frozen=True, slots=True)
class TypeAssertion:
    entity: str
    type_id: str

    def canonical(self) -> str:
        return f"TYPE\t{self.entity}\t{self.type_id}"


@dataclass(frozen=True, slots=True)
class LiteralSet:
    entity: str
    attr: str
    value: str

    def canonical(self) -> str:
        return f"LIT\t{self.entity}\t{self.attr}\t{self.value}"


@dataclass(frozen=True, slots=True)
class Merge:
    absorbed: str
    survivor: str

    def canonical(self) -> str:
        return f"MERGE\t{self.absorbed}\t{self.survivor}"


@dataclass(frozen=True, slots=True)
class TypeExtension:
    type_id: str
    parent_id: str
    label: str

    def canonical(self) -> str:
        return f"EXTEND_TYPE\t{self.type_id}\t{self.parent_id}\t{self.label}"


@dataclass(frozen=True)
class GraphDelta:
    """Expected or predicted graph mutation. All fields are exact-match sets."""

    adds: tuple[Triple, ...] = ()
    deletes: tuple[Triple, ...] = ()
    type_assertions: tuple[TypeAssertion, ...] = ()
    literals: tuple[LiteralSet, ...] = ()
    merges: tuple[Merge, ...] = ()
    type_extensions: tuple[TypeExtension, ...] = ()
    constraint_repairs: tuple[str, ...] = ()

    def canonical_ops(self) -> frozenset[str]:
        ops: list[str] = []
        for triple in self.adds:
            ops.append(triple.canonical())
        for triple in self.deletes:
            ops.append(
                f"DEL\t{triple.subject}\t{triple.predicate}\t{triple.object}"
            )
        for item in self.type_assertions:
            ops.append(item.canonical())
        for item in self.literals:
            ops.append(item.canonical())
        for item in self.merges:
            ops.append(item.canonical())
        for item in self.type_extensions:
            ops.append(item.canonical())
        for repair in self.constraint_repairs:
            ops.append(f"REPAIR\t{repair}")
        return frozenset(ops)

    def to_dict(self) -> dict[str, object]:
        return {
            "adds": [_triple_dict(t) for t in self.adds],
            "deletes": [_triple_dict(t) for t in self.deletes],
            "type_assertions": [
                {"entity": t.entity, "type_id": t.type_id}
                for t in self.type_assertions
            ],
            "literals": [
                {"entity": t.entity, "attr": t.attr, "value": t.value}
                for t in self.literals
            ],
            "merges": [
                {"absorbed": m.absorbed, "survivor": m.survivor}
                for m in self.merges
            ],
            "type_extensions": [
                {
                    "type_id": t.type_id,
                    "parent_id": t.parent_id,
                    "label": t.label,
                }
                for t in self.type_extensions
            ],
            "constraint_repairs": list(self.constraint_repairs),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object] | None) -> "GraphDelta":
        data = raw or {}
        return cls(
            adds=tuple(Triple(**_triple_kwargs(t)) for t in _list(data, "adds")),
            deletes=tuple(
                Triple(**_triple_kwargs(t)) for t in _list(data, "deletes")
            ),
            type_assertions=tuple(
                TypeAssertion(
                    entity=str(t["entity"]),
                    type_id=str(t.get("type_id") or t.get("type")),
                )
                for t in _list(data, "type_assertions")
            ),
            literals=tuple(
                LiteralSet(
                    entity=str(t["entity"]),
                    attr=str(t["attr"]),
                    value=str(t["value"]),
                )
                for t in _list(data, "literals")
            ),
            merges=tuple(
                Merge(
                    absorbed=str(t["absorbed"]),
                    survivor=str(t["survivor"]),
                )
                for t in _list(data, "merges")
            ),
            type_extensions=tuple(
                TypeExtension(
                    type_id=str(t["type_id"]),
                    parent_id=str(t["parent_id"]),
                    label=str(t["label"]),
                )
                for t in _list(data, "type_extensions")
            ),
            constraint_repairs=tuple(
                str(x) for x in _list(data, "constraint_repairs")
            ),
        )


def graph_delta_prf(
    predicted: GraphDelta, gold: GraphDelta
) -> dict[str, float | None]:
    """Exact-set precision / recall / F1 over canonical ops."""
    pred = predicted.canonical_ops()
    gold_ops = gold.canonical_ops()
    if not pred and not gold_ops:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    if not pred:
        return {"precision": None, "recall": 0.0, "f1": 0.0}
    if not gold_ops:
        return {"precision": 0.0, "recall": None, "f1": 0.0}
    overlap = pred & gold_ops
    precision = len(overlap) / len(pred)
    recall = len(overlap) / len(gold_ops)
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1}


def pairwise_er_prf(
    predicted_merges: Iterable[Merge], gold_merges: Iterable[Merge]
) -> dict[str, float | None]:
    """Pairwise ER P/R/F1. Pairs are undirected: (a,b) == (b,a)."""

    def keys(merges: Iterable[Merge]) -> frozenset[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for merge in merges:
            pair = tuple(sorted((merge.absorbed, merge.survivor)))
            out.add((pair[0], pair[1]))
        return frozenset(out)

    pred = keys(predicted_merges)
    gold = keys(gold_merges)
    if not pred and not gold:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    if not pred:
        return {"precision": None, "recall": 0.0, "f1": 0.0}
    if not gold:
        return {"precision": 0.0, "recall": None, "f1": 0.0}
    overlap = pred & gold
    precision = len(overlap) / len(pred)
    recall = len(overlap) / len(gold)
    f1 = (
        0.0
        if precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def task_success(predicted: GraphDelta, gold: GraphDelta) -> bool:
    """Binary success: canonical op sets match exactly."""
    return predicted.canonical_ops() == gold.canonical_ops()


def _triple_dict(triple: Triple) -> dict[str, str]:
    return {
        "subject": triple.subject,
        "predicate": triple.predicate,
        "object": triple.object,
    }


def _triple_kwargs(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise TypeError("triple must be an object")
    return {
        "subject": str(raw["subject"]),
        "predicate": str(raw["predicate"]),
        "object": str(raw["object"]),
    }


def _list(data: dict[str, object], key: str) -> list[dict[str, object] | str]:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    return value
