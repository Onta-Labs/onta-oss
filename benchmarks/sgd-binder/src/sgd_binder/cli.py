"""CLI: fetch SGD, dry four-arm, live experiment-run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sgd_binder.arms import adapters_for_arm, get_arm
from sgd_binder.constants import ARM_IDS, SPEC_VERSION
from sgd_binder.fetch import default_data_root, fetch_all, fetch_split
from sgd_binder.fixtures import StubClient, fixture_catalog, fixture_instances
from sgd_binder.instances import instances_from_dialogues, load_dialogues
from sgd_binder.lora_data import write_infona_together_jsonl, write_vanilla_together_jsonl
from sgd_binder.llm import UrllibChatClient
from sgd_binder.protocol import ProtocolError
from sgd_binder.run import run_instances
from sgd_binder.schema import (
    build_catalog,
    leak_needles,
    load_schema_list,
    redact_needles,
)
from sgd_binder.skills import write_skills


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sgd-binder", description=SPEC_VERSION)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--dest", type=Path, default=None)
    p_fetch.add_argument("--split", choices=("train", "dev", "test"), default=None)
    p_dry = sub.add_parser("experiment-dry")
    p_dry.add_argument("--out", type=Path, required=True)
    p_run = sub.add_parser("experiment-run")
    p_run.add_argument("--arm", required=True, choices=ARM_IDS)
    p_run.add_argument("--data", type=Path, default=None)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--concurrency", type=int, default=1)
    p_run.add_argument("--limit", type=int, default=0, help="cap test instances (0=all)")
    p_lora = sub.add_parser("write-lora-data")
    p_lora.add_argument("--recipe", required=True, choices=("infona", "vanilla"))
    p_lora.add_argument("--data", type=Path, default=None)
    p_lora.add_argument("--out", type=Path, required=True)
    p_lora.add_argument("--max-per-service", type=int, default=250)
    p_lora.add_argument("--fixtures", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "fetch":
            root = args.dest or default_data_root()
            paths = fetch_split(args.split, root) if args.split else fetch_all(root)
            print("\n".join(str(p) for p in paths))
            return 0
        if args.cmd == "experiment-dry":
            return _dry(args.out)
        if args.cmd == "experiment-run":
            return _live(args)
        if args.cmd == "write-lora-data":
            return _lora(args)
    except ProtocolError as exc:
        print(exc)
        return 2
    raise AssertionError(args.cmd)


def _dry(out: Path) -> int:
    catalog = fixture_catalog()
    needles = leak_needles(catalog)
    skills = write_skills(catalog, needles)
    instances = fixture_instances(catalog)
    # utterances already fixture-clean; still assert catalog names stay out of skills
    for arm_id in ARM_IDS:
        arm = get_arm(arm_id)
        binder, extractor = adapters_for_arm(
            arm,
            client=StubClient(),
            catalog=catalog,
            skills=skills,
            needles=needles,
        )
        dest = out / arm_id / "predictions.json"
        score = run_instances(
            instances,
            binder=binder,
            extractor=extractor,
            out_path=dest,
            extra_meta={"fixture": True, "not_a_score": True},
        )
        print(
            f"FIXTURE {arm_id} bind={score.bind_accuracy:.4f} "
            f"unseen_n={score.unseen_n} f1={score.slot_micro_f1:.4f} "
            f"(stub; not an SGD score)"
        )
    print("fixture stub only. not an SGD score.")
    return 0


def _live(args: argparse.Namespace) -> int:
    arm = get_arm(args.arm)
    served = (args.model or "").strip() or None
    if arm.lora_recipe and not served:
        raise ProtocolError(f"{arm.arm_id} needs --model")
    root = args.data or default_data_root()
    train_sch = load_schema_list(root / "train" / "schema.json")
    test_sch = load_schema_list(root / "test" / "schema.json")
    catalog = build_catalog(train_schemas=train_sch, test_schemas=test_sch)
    needles = leak_needles(catalog)
    skills = write_skills(catalog, needles)
    dialogues = []
    for path in sorted((root / "test").glob("dialogues_*.json")):
        dialogues.extend(load_dialogues(path))
    instances = instances_from_dialogues(
        dialogues, catalog, needles=redact_needles(catalog)
    )
    if args.limit:
        instances = instances[: args.limit]
    client = UrllibChatClient(model=served or arm.model_id)
    binder, extractor = adapters_for_arm(
        arm, client=client, catalog=catalog, skills=skills, needles=needles
    )
    dest = args.out / arm.arm_id / "predictions.json"
    score = run_instances(
        instances,
        binder=binder,
        extractor=extractor,
        out_path=dest,
        concurrency=args.concurrency,
    )
    print(dest)
    print(json.dumps(score.__dict__, indent=2))
    n_types = len(catalog.type_ids())
    print(
        f"bind={score.bind_accuracy:.4f} n={score.n} "
        f"seen={score.seen_bind_hits}/{score.seen_n} "
        f"unseen={score.unseen_bind_hits}/{score.unseen_n} "
        f"slot_micro_f1={score.slot_micro_f1:.4f} "
        f"n_catalog={n_types} chance_bind=1/{n_types}"
    )
    print("constructed Infona task on SGD. not official DST Joint Goal Accuracy.")
    return 0


def _lora(args: argparse.Namespace) -> int:
    if args.recipe not in ("infona", "vanilla"):
        raise ProtocolError(f"unknown recipe {args.recipe}")
    if args.fixtures:
        catalog = fixture_catalog()
        needles = leak_needles(catalog)
        skills = write_skills(catalog, needles)
        instances = [i for i in fixture_instances(catalog) if i.seen_in_train]
        path = _write_lora(args.recipe, instances, catalog, skills, args)
        print(path)
        print("fixture LoRA jsonl only. not a train set.")
        return 0
    root = args.data or default_data_root()
    train_sch = load_schema_list(root / "train" / "schema.json")
    test_sch = load_schema_list(root / "test" / "schema.json")
    catalog = build_catalog(train_schemas=train_sch, test_schemas=test_sch)
    needles = leak_needles(catalog)
    skills = write_skills(catalog, needles)
    dialogues = []
    for path in sorted((root / "train").glob("dialogues_*.json")):
        dialogues.extend(load_dialogues(path))
    instances = instances_from_dialogues(
        dialogues, catalog, needles=redact_needles(catalog)
    )
    instances = [i for i in instances if i.seen_in_train]
    out = _write_lora(args.recipe, instances, catalog, skills, args)
    print(out)
    return 0


def _write_lora(recipe, instances, catalog, skills, args):
    if recipe == "vanilla":
        return write_vanilla_together_jsonl(
            instances, catalog, args.out, max_per_service=args.max_per_service
        )
    return write_infona_together_jsonl(
        instances, catalog, skills, args.out, max_per_service=args.max_per_service
    )
