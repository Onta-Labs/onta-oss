"""Harness stub: write one machine-readable result row per task.

No model is called. Metrics stay null. The row still records the condition,
compiler output, and the run-log fields later slices must fill in.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

from .compiler import compile_flat, compile_none, compile_routed
from .conditions import CONDITION_MATRIX, Condition, condition_by_id
from .dataset import TASK_FAMILIES, Task, load_fixture_bundle
from .models import CompiledSkillSet, Neighborhood, Ontology

SCHEMA_VERSION = "1.0.0"


@dataclass
class ModelSpec:
    name: str = "unspecified"
    quantization: str = "unspecified"
    param_count: str = "unspecified"
    backend: str = "unspecified"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "quantization": self.quantization,
            "param_count": self.param_count,
            "backend": self.backend,
        }


@dataclass
class DecodingSpec:
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int | None = None
    seed: int | None = 0
    max_new_tokens: int = 512

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "seed": self.seed,
            "max_new_tokens": self.max_new_tokens,
        }


@dataclass
class ContextBudget:
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    compiled_skill_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "compiled_skill_chars": self.compiled_skill_chars,
        }


@dataclass
class ResourceUse:
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ram_mb: float | None = None
    vram_mb: float | None = None
    hosted_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "ram_mb": self.ram_mb,
            "vram_mb": self.vram_mb,
            "hosted_cost_usd": self.hosted_cost_usd,
        }


def empty_metrics() -> dict[str, None]:
    """Primary metric keys. Values stay null until a real executor runs."""
    return {
        "success": None,
        "graph_delta_precision": None,
        "graph_delta_recall": None,
        "graph_delta_f1": None,
        "constraint_valid": None,
        "er_precision": None,
        "er_recall": None,
        "er_f1": None,
    }


@dataclass
class RunResult:
    condition: Condition
    task: Task
    compiled: CompiledSkillSet
    model: ModelSpec = field(default_factory=ModelSpec)
    decoding: DecodingSpec = field(default_factory=DecodingSpec)
    context_budget: ContextBudget = field(default_factory=ContextBudget)
    resources: ResourceUse = field(default_factory=ResourceUse)
    tools: tuple[str, ...] = ()
    prompt_template_id: str = "stub.v1"
    prompt_sha256: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=empty_metrics)
    status: str = "stub"
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    notes: str = "harness stub; no model executed"

    def to_dict(self) -> dict[str, Any]:
        skill_chars = sum(len(s.body) for s in self.compiled.skills)
        budget = ContextBudget(
            max_context_tokens=self.context_budget.max_context_tokens,
            max_output_tokens=self.context_budget.max_output_tokens,
            compiled_skill_chars=skill_chars,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "status": self.status,
            "condition": self.condition.to_dict(),
            "task_id": self.task.task_id,
            "task_family": self.task.family,
            "split": self.task.split,
            "model": self.model.to_dict(),
            "prompt": {
                "template_id": self.prompt_template_id,
                "sha256": self.prompt_sha256,
                "skill_injection": self.condition.skill_mode,
            },
            "context_budget": budget.to_dict(),
            "tools": list(self.tools),
            "decoding": self.decoding.to_dict(),
            "resources": self.resources.to_dict(),
            "compiler": self.compiled.to_dict(),
            "metrics": dict(self.metrics),
            "notes": self.notes,
        }


def compile_for_condition(
    ontology: Ontology, neighborhood: Neighborhood, condition: Condition
) -> CompiledSkillSet:
    if not condition.runnable:
        raise RuntimeError(
            f"condition {condition.condition_id} is blocked: "
            f"{condition.blocked_reason}"
        )
    if condition.skill_mode in ("none", "ontology_context"):
        return compile_none(ontology, neighborhood)
    if condition.skill_mode == "flat":
        return compile_flat(ontology)
    if condition.skill_mode == "routed":
        return compile_routed(ontology, neighborhood)
    raise RuntimeError(f"unhandled skill_mode {condition.skill_mode!r}")


def write_result_row(result: RunResult, dest: Path | TextIO) -> None:
    line = json.dumps(result.to_dict(), sort_keys=True) + "\n"
    if isinstance(dest, Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return
    dest.write(line)


def run_stub(
    *,
    condition_id: str = "4b_ontology_routed",
    dest: Path | TextIO | None = None,
) -> list[dict[str, Any]]:
    """Compile every fixture task for one condition and write stub rows."""
    condition = condition_by_id(condition_id)
    bundle = load_fixture_bundle()
    rows: list[dict[str, Any]] = []
    target: Path | TextIO = dest if dest is not None else sys.stdout
    for task in bundle.tasks:
        compiled = compile_for_condition(
            bundle.ontology, task.neighborhood, condition
        )
        result = RunResult(condition=condition, task=task, compiled=compiled)
        write_result_row(result, target)
        rows.append(result.to_dict())
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Ontology-skills benchmark harness stub (no model)."
    )
    parser.add_argument(
        "--condition",
        default="4b_ontology_routed",
        help="condition_id from the locked matrix",
    )
    parser.add_argument(
        "--list-conditions",
        action="store_true",
        help="print the comparison matrix and exit",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSONL path (default: stdout)",
    )
    args = parser.parse_args(argv)
    if args.list_conditions:
        for cond in CONDITION_MATRIX:
            state = "runnable" if cond.runnable else "BLOCKED"
            print(f"{cond.index}. {cond.condition_id}\t{state}\t{cond.name}")
        print("families: " + ", ".join(TASK_FAMILIES))
        return 0
    run_stub(condition_id=args.condition, dest=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
