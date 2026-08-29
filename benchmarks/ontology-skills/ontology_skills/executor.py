"""Executor loop: prompt → backend → parse → score → run-log row.

Canned dry-run uses fixture text. Live POSTs to an OpenAI-compatible endpoint
when a Bearer key is present. Live without ``--task-id`` is refused so this
package cannot accidentally sweep 80 tasks.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TextIO

from .backends import (
    CannedBackend,
    CompletionResult,
    LiveBackend,
    default_model_for_condition,
    load_canned,
)
from .conditions import condition_by_id
from .dataset import Task, load_fixture_bundle, load_tasks
from .harness import (
    ContextBudget,
    DecodingSpec,
    ResourceUse,
    RunResult,
    compile_for_condition,
    write_result_row,
)
from .parse import parse_graph_delta
from .prompts import TEMPLATE_ID, build_prompt
from .scoring import score_task

PROMPT_TOOLS: tuple[str, ...] = ()


def execute_task(
    task: Task,
    *,
    condition_id: str,
    backend: CannedBackend | LiveBackend,
    decoding: DecodingSpec | None = None,
) -> RunResult:
    bundle = load_fixture_bundle()
    condition = condition_by_id(condition_id)
    compiled = compile_for_condition(
        bundle.ontology, task.neighborhood, condition
    )
    prompt = build_prompt(task, bundle.ontology, compiled, condition)
    decoding = decoding or DecodingSpec()
    completion = backend.complete(
        prompt.text, decoding=decoding, task_id=task.task_id
    )
    parsed = parse_graph_delta(completion.text)
    metrics = score_task(parsed.predicted, task)
    notes = (
        "canned-fixture; not a model run"
        if backend.model.backend == "fixture"
        else "live executor"
    )
    if not parsed.ok:
        notes = f"parse_failure: {parsed.error}; {notes}"
    return RunResult(
        condition=condition,
        task=task,
        compiled=compiled,
        model=backend.model,
        decoding=decoding,
        context_budget=ContextBudget(
            compiled_skill_chars=sum(len(s.body) for s in compiled.skills),
        ),
        resources=_resources(completion),
        tools=PROMPT_TOOLS,
        prompt_template_id=TEMPLATE_ID,
        prompt_sha256=prompt.sha256,
        metrics=metrics,
        status="ok" if parsed.ok else "error",
        notes=notes,
    )


def _resources(completion: CompletionResult) -> ResourceUse:
    return ResourceUse(
        latency_ms=completion.latency_ms,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        hosted_cost_usd=completion.hosted_cost_usd,
    )


def run_dry(
    *,
    condition_id: str = "4b_ontology_routed",
    task_id: str | None = None,
    canned_path: Path | None = None,
    dest: Path | TextIO | None = None,
) -> list[dict[str, Any]]:
    """Score canned model text. Resources stay null. No HTTP."""
    backend = load_canned(canned_path)
    bundle = load_fixture_bundle()
    tasks = bundle.tasks
    if task_id is not None:
        tasks = tuple(t for t in tasks if t.task_id == task_id)
        if not tasks:
            raise KeyError(f"unknown task_id {task_id!r}")
    else:
        tasks = tuple(t for t in tasks if t.task_id in backend.responses)
    return _run_tasks(tasks, condition_id=condition_id, backend=backend, dest=dest)


def run_live(
    *,
    condition_id: str,
    task_id: str,
    backend: LiveBackend,
    dest: Path | TextIO | None = None,
) -> list[dict[str, Any]]:
    """One live task. Callers that want a sweep must loop explicitly."""
    tasks = tuple(t for t in load_tasks() if t.task_id == task_id)
    if not tasks:
        raise KeyError(f"unknown task_id {task_id!r}")
    return _run_tasks(tasks, condition_id=condition_id, backend=backend, dest=dest)


def _run_tasks(
    tasks: tuple[Task, ...],
    *,
    condition_id: str,
    backend: CannedBackend | LiveBackend,
    dest: Path | TextIO | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task in tasks:
        result = execute_task(task, condition_id=condition_id, backend=backend)
        if dest is not None:
            write_result_row(result, dest)
        rows.append(result.to_dict())
    return rows


def execute_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ontology-skills executor (canned default; live POSTs with a key)."
    )
    parser.add_argument("--condition", default="4b_ontology_routed")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--canned",
        type=Path,
        default=None,
        help="JSONL of {task_id, text} (default: fixtures/canned_responses.jsonl)",
    )
    parser.add_argument(
        "--backend",
        choices=("canned", "live"),
        default="canned",
        help="live POSTs when INFONA_BENCH_API_KEY / OPENAI_API_KEY / "
        "OPENROUTER_API_KEY is set",
    )
    args = parser.parse_args(argv)
    if args.backend == "live":
        return _execute_live(args)
    dest: Path | None = args.out
    rows = run_dry(
        condition_id=args.condition,
        task_id=args.task_id,
        canned_path=args.canned,
        dest=dest,
    )
    if dest is None:
        for row in rows:
            print(json.dumps(row, sort_keys=True))
    return 0


def _execute_live(args: argparse.Namespace) -> int:
    condition = condition_by_id(args.condition)
    if not condition.runnable:
        print(
            f"condition {condition.condition_id} is blocked: "
            f"{condition.blocked_reason}"
        )
        return 2
    if not args.task_id:
        print(
            "live requires --task-id; this CLI will not sweep the dataset.\n"
            f"{_live_env_help()}",
            end="",
        )
        return 2
    live = LiveBackend.from_env(condition=condition)
    if live is None:
        print(_live_env_help(), end="")
        return 2
    dest: Path | None = args.out
    rows = run_live(
        condition_id=args.condition,
        task_id=args.task_id,
        backend=live,
        dest=dest,
    )
    if dest is None:
        for row in rows:
            print(json.dumps(row, sort_keys=True))
    return 0


def _live_env_help() -> str:
    return (
        "Live OpenRouter run needs a Bearer key (no POST without one):\n"
        "  INFONA_BENCH_API_KEY    (alias OPENAI_API_KEY or OPENROUTER_API_KEY)\n"
        "  INFONA_BENCH_BASE_URL   default https://openrouter.ai/api/v1 "
        "(alias OPENAI_BASE_URL)\n"
        "  INFONA_BENCH_MODEL      optional override; else by condition:\n"
        "    4B (1–4, 8): qwen/qwen3-4b\n"
        "    9B (6):      qwen/qwen3.5-9b\n"
        "    27B (7):     qwen/qwen3.5-27b\n"
        "  INFONA_BENCH_QUANTIZATION  optional, recorded on the run log\n"
        "Headers sent: HTTP-Referer https://infona.ai ; "
        "X-Title Infona ontology-skills bench\n"
        "Pass --task-id; this command does not sweep.\n"
    )


__all__ = [
    "execute_main",
    "execute_task",
    "default_model_for_condition",
    "run_dry",
    "run_live",
]
