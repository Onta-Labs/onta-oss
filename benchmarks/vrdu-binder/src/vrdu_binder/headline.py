"""Headline metrics only. F1_wrong and oracle-type F1 are not on this object."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from vrdu_binder.constants import CORPUS_ADBUY, CORPUS_REGISTRATION
from vrdu_binder.protocol import ProtocolError

HEADLINE_KEYS = (
    "spec",
    "bind_at_type_accuracy",
    "n_types",
    "n_bind_docs",
    "metric_micro_f1",
    "n2_tax",
)


@dataclass
class Headline:
    bind_at_type_accuracy: float
    n_bind_docs: int
    metric_micro_f1: dict[str, float | None] = field(default_factory=dict)
    spec: str = "v11"
    n_types: int = 2
    n2_tax: str = (
        "Two types. Chance bind accuracy is 50%. Do not compare this number "
        "to a many-class router."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "bind_at_type_accuracy": self.bind_at_type_accuracy,
            "n_types": self.n_types,
            "n_bind_docs": self.n_bind_docs,
            "metric_micro_f1": {
                CORPUS_REGISTRATION: self.metric_micro_f1.get(CORPUS_REGISTRATION),
                CORPUS_ADBUY: self.metric_micro_f1.get(CORPUS_ADBUY),
            },
            "n2_tax": self.n2_tax,
        }


def bind_accuracy(pred: Mapping[str, str], gold: Mapping[str, str]) -> float:
    if not gold:
        raise ProtocolError("gold type map is empty")
    extra = set(pred) - set(gold)
    if extra:
        raise ProtocolError(f"predictions for filenames not in gold {sorted(extra)[:5]}")
    hits = sum(1 for name, g in gold.items() if pred.get(name) == g)
    return hits / len(gold)


def make_headline(
    *,
    pred_types: Mapping[str, str],
    gold_types: Mapping[str, str],
    metric_micro_f1: Mapping[str, float] | None = None,
) -> Headline:
    if any(k in (metric_micro_f1 or {}) for k in ("f1_wrong", "oracle", "oracle_type")):
        raise ProtocolError("F1_wrong and oracle-type F1 are not headline metrics")
    acc = bind_accuracy(pred_types, gold_types)
    f1 = {CORPUS_REGISTRATION: None, CORPUS_ADBUY: None}
    if metric_micro_f1:
        for corpus, value in metric_micro_f1.items():
            if corpus not in f1:
                raise ProtocolError(f"{corpus!r} is not a headline corpus")
            f1[corpus] = value
    return Headline(
        bind_at_type_accuracy=acc,
        n_bind_docs=len(gold_types),
        metric_micro_f1=f1,
    )


def assert_not_headline_path(path_or_label: str) -> None:
    lowered = path_or_label.lower()
    for token in ("f1_wrong", "oracle", "gold-routed", "gold_routed"):
        if token in lowered:
            raise ProtocolError(
                f"{path_or_label!r} is a sanity dump, not a headline path"
            )
