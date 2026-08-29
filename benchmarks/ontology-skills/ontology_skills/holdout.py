"""Holdout fixture loader and executor entry.

PR 487 ``execute_task`` always calls ``load_fixture_bundle()`` with no path.
This module points that call at ``fixtures/holdout`` for one task, then
restores it. It does not copy the executor loop and it does not add a
holdout-only prompt.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .backends import CannedBackend, LiveBackend, load_canned
from .dataset import FIXTURES_DIR, FixtureBundle, Task, load_fixture_bundle
from .executor import execute_task
from .harness import RunResult

HOLDOUT_DIR = FIXTURES_DIR / "holdout"


def load_holdout_bundle() -> FixtureBundle:
    """Load ``fixtures/holdout/ontology.json`` + ``tasks.jsonl``."""
    return load_fixture_bundle(
        HOLDOUT_DIR / "ontology.json", HOLDOUT_DIR / "tasks.jsonl"
    )


def execute_holdout_task(
    task: Task,
    **kwargs: Any,
) -> RunResult:
    """Run ``execute_task`` against the holdout ontology for ``task``."""
    import ontology_skills.executor as executor_mod

    bundle = load_holdout_bundle()
    original = executor_mod.load_fixture_bundle
    executor_mod.load_fixture_bundle = lambda *args, **kw: bundle
    try:
        return execute_task(task, **kwargs)
    finally:
        executor_mod.load_fixture_bundle = original


def holdout_main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m ontology_skills.holdout --task-id ho-et-01``.

    Always requires ``--task-id``. Does not sweep. Live still needs a key and
    will not POST without one.
    """
    parser = argparse.ArgumentParser(
        description="Run one holdout task through the 487 executor."
    )
    parser.add_argument("--condition", default="4b_ontology_routed")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--canned", type=Path, default=None)
    parser.add_argument(
        "--backend",
        choices=("canned", "live"),
        default="canned",
    )
    args = parser.parse_args(argv)
    bundle = load_holdout_bundle()
    tasks = tuple(t for t in bundle.tasks if t.task_id == args.task_id)
    if not tasks:
        print(f"unknown holdout task_id {args.task_id!r}")
        return 2
    if args.backend == "live":
        from .conditions import condition_by_id

        condition = condition_by_id(args.condition)
        if not condition.runnable:
            print(
                f"condition {condition.condition_id} is blocked: "
                f"{condition.blocked_reason}"
            )
            return 2
        live = LiveBackend.from_env(condition=condition)
        if live is None:
            print("live holdout run needs a Bearer key; no POST without one.")
            return 2
        backend: CannedBackend | LiveBackend = live
    else:
        try:
            backend = load_canned(args.canned)
        except FileNotFoundError:
            print("canned holdout run needs a JSONL of {task_id, text}.")
            return 2
        if args.task_id not in backend.responses:
            print(
                f"no canned text for {args.task_id!r}; "
                "pass --canned PATH. Not a published score."
            )
            return 2
    result = execute_holdout_task(
        tasks[0], condition_id=args.condition, backend=backend
    )
    row = result.to_dict()
    if args.out is not None:
        args.out.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    else:
        print(json.dumps(row, sort_keys=True))
    return 0


def main() -> int:
    return holdout_main()


if __name__ == "__main__":
    raise SystemExit(main())
