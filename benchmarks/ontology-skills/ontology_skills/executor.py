"""Executor loop: prompt → backend → parse → score → run-log row.

Dry-run uses canned fixture text. Live OpenAI-compatible POST is implemented
on ``LiveBackend`` and is not invoked unless a later slice opts in.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TextIO

from .backends import CannedBackend, LiveBackend, load_canned
from .conditions import condition_by_id
from .dataset import Task, load_fixture_bundle
from .harness import (
    ContextBudget,
    DecodingSpec,
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
    raw = backend.complete(
        prompt.text, decoding=decoding, task_id=task.task_id
    )
    parsed = parse_graph_delta(raw)
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
        tools=PROMPT_TOOLS,
        prompt_template_id=TEMPLATE_ID,
        prompt_sha256=prompt.sha256,
        metrics=metrics,
        status="ok" if parsed.ok else "error",
        notes=notes,
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
    rows: list[dict[str, Any]] = []
    for task in tasks:
        result = execute_task(
            task, condition_id=condition_id, backend=backend
        )
        if dest is not None:
            write_result_row(result, dest)
        rows.append(result.to_dict())
    return rows


def execute_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ontology-skills executor (dry-run default; no model)."
    )
    parser.add_argument("--condition", default="4b_ontology_routed")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="use canned fixture text (default)",
    )
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
        help="live requires INFONA_BENCH_BASE_URL + INFONA_BENCH_MODEL and POSTs",
    )
    args = parser.parse_args(argv)
    if args.backend == "live":
        live = LiveBackend.from_env()
        if live is None:
            print(_live_env_help(), end="")
            return 2
        # A later slice may call live.complete(). This slice refuses to POST.
        print(
            "live backend is wired but disabled in INF-607; "
            "use --backend canned. Set env vars for a future run:\n"
            f"{_live_env_help()}",
            end="",
        )
        return 2
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


def _live_env_help() -> str:
    return (
        "Future GPU/API run (not invoked now):\n"
        "  INFONA_BENCH_BASE_URL   OpenAI-compatible root "
        "(alias OPENAI_BASE_URL), e.g. http://127.0.0.1:8000/v1\n"
        "  INFONA_BENCH_MODEL      model id (alias OPENAI_MODEL or MODEL)\n"
        "  INFONA_BENCH_API_KEY    optional (alias OPENAI_API_KEY)\n"
        "  INFONA_BENCH_QUANTIZATION  optional, recorded on the run log\n"
    )
