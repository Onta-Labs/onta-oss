"""Bind@service + slot micro-F1. Seen vs unseen. Not SGD Joint Goal Accuracy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sgd_binder.instances import Instance


@dataclass(frozen=True)
class Score:
    n: int
    bind_hits: int
    bind_accuracy: float
    seen_n: int
    seen_bind_hits: int
    unseen_n: int
    unseen_bind_hits: int
    slot_tp: int
    slot_fp: int
    slot_fn: int
    slot_micro_f1: float


def _norm(val: str) -> str:
    return " ".join(val.strip().lower().split())


def score_predictions(
    instances: list[Instance],
    pred_type: Mapping[str, str],
    pred_slots: Mapping[str, Mapping[str, str]],
) -> Score:
    bind_hits = seen_hits = unseen_hits = 0
    seen_n = unseen_n = 0
    tp = fp = fn = 0
    for inst in instances:
        gold_t = inst.type_id
        pred_t = pred_type.get(inst.instance_id, "")
        hit = pred_t == gold_t
        if inst.seen_in_train:
            seen_n += 1
            seen_hits += int(hit)
        else:
            unseen_n += 1
            unseen_hits += int(hit)
        bind_hits += int(hit)
        gold = {_norm(k): _norm(v) for k, v in inst.gold_slots.items()}
        pred = {}
        if hit:
            pred = {
                _norm(k): _norm(v)
                for k, v in (pred_slots.get(inst.instance_id) or {}).items()
            }
        for k, v in gold.items():
            if pred.get(k) == v:
                tp += 1
            else:
                fn += 1
        for k, v in pred.items():
            if k not in gold or gold[k] != v:
                fp += 1
    n = len(instances)
    f1 = 0.0
    if tp + fp + fn:
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec)
    return Score(
        n=n,
        bind_hits=bind_hits,
        bind_accuracy=(bind_hits / n) if n else 0.0,
        seen_n=seen_n,
        seen_bind_hits=seen_hits,
        unseen_n=unseen_n,
        unseen_bind_hits=unseen_hits,
        slot_tp=tp,
        slot_fp=fp,
        slot_fn=fn,
        slot_micro_f1=f1,
    )
