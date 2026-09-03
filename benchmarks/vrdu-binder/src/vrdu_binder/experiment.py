"""CLI helpers for the four-arm SD_0 experiment. No live sweep in this module."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from vrdu_binder.arms import ARM_IDS, adapters_for_arm, get_arm
from vrdu_binder.bind import TypeCatalog
from vrdu_binder.constants import (
    CORPUS_ADBUY,
    CORPUS_DIR,
    CORPUS_REGISTRATION,
    KEYS_FOR_TYPE,
    SEEDS,
    TYPE_0,
    TYPE_1,
    TYPE_FOR_CORPUS,
)
from vrdu_binder.documents import docs_for_filenames, load_documents
from vrdu_binder.fetch import default_data_root
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.llm import UrllibChatClient
from vrdu_binder.lora_data import write_lora_jsonl
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.run import run_corpus
from vrdu_binder.skills import write_skill, write_skills_for_seed
from vrdu_binder.splits import load_run_split, published_split_path


def add_experiment_parsers(sub) -> None:
    p_exp = sub.add_parser(
        "experiment-run",
        help="One arm, one corpus, SD_0 by default. Needs a served model.",
    )
    p_exp.add_argument("--arm", required=True, choices=ARM_IDS)
    p_exp.add_argument("--seed", type=int, default=0, choices=SEEDS)
    p_exp.add_argument("--corpus", required=True, choices=(CORPUS_REGISTRATION, CORPUS_ADBUY))
    p_exp.add_argument("--data", type=Path, default=None)
    p_exp.add_argument("--out", type=Path, required=True)
    p_exp.add_argument(
        "--fixtures",
        action="store_true",
        help="Synthetic mix + stub client. No VRDU download, no live HTTP.",
    )
    p_exp.add_argument(
        "--model",
        default=None,
        help=(
            "Served model id. Required for FT arms (mlx-lm default_model / "
            "local path, or a Together output name). Bare arms default to "
            "the locked Qwen id."
        ),
    )
    p_exp.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Parallel test docs. Default 1. Dedicated GPU-hours drop if >1.",
    )

    p_lora = sub.add_parser("write-lora-data", help="Train-only LoRA JSONL for one recipe")
    p_lora.add_argument("--recipe", required=True, choices=("vanilla", "infona"))
    p_lora.add_argument("--seed", type=int, default=0, choices=SEEDS)
    p_lora.add_argument("--data", type=Path, default=None)
    p_lora.add_argument("--out", type=Path, required=True)
    p_lora.add_argument("--fixtures", action="store_true")

    p_edry = sub.add_parser(
        "experiment-dry",
        help="Stub all four arms on fixtures. Not a score.",
    )
    p_edry.add_argument("--out", type=Path, required=True)


def dispatch_experiment(args: Namespace) -> int:
    if args.cmd == "experiment-run":
        return cmd_experiment_run(args)
    if args.cmd == "write-lora-data":
        return cmd_write_lora_data(args)
    if args.cmd == "experiment-dry":
        return cmd_experiment_dry(args)
    raise AssertionError(args.cmd)


class _StubClient:
    """Fixture-only chat stub. Not KeywordBinder. Not a published score."""

    def complete(self, *, system: str, user: str) -> str:
        text = user.lower()
        if "Reply with exactly one id" in system or "Pick exactly one type id" in system:
            return "type_1" if "invoice" in text else "type_0"
        if "invoice_id" in text:
            return json.dumps({"invoice_id": "INV-20", "invoice_total": "40.00"})
        return json.dumps({"widget_id": "W-200", "widget_name": "gasket"})


def cmd_experiment_run(args: Namespace) -> int:
    arm = get_arm(args.arm)
    if args.fixtures:
        _run_fixtures(arm, args.out / arm.arm_id / args.corpus, args.corpus)
        return 0
    served = (args.model or "").strip() or None
    if arm.lora_recipe and not served:
        raise ProtocolError(
            f"{arm.arm_id} needs --model (local mlx path / default_model, "
            "or Together output id). Refusing to score the base 0.8B as an "
            "FT arm."
        )
    client = UrllibChatClient(model=served or arm.model_id)
    binder, extractor = adapters_for_arm(arm, client=client)
    root = args.data or default_data_root()
    split = load_run_split(
        published_split_path(root, args.corpus, args.seed),
        corpus=args.corpus,
        seed=args.seed,
    )
    documents, skills, catalog = _load_published(root, args.corpus, args.seed)
    result = run_corpus(
        corpus=args.corpus,
        seed=args.seed,
        split=split,
        documents=documents,
        skills=skills,
        catalog=catalog,
        binder=binder,
        extractor=extractor,
        out_dir=args.out,
        concurrency=args.concurrency,
    )
    print(result.dump_path)
    print(f"arm={arm.arm_id} model={served or arm.model_id} infona_router={arm.uses_infona_router}")
    print(
        f"bind_at_type_accuracy (this corpus only)={result.bind_accuracy:.4f} "
        f"n={len(result.outcomes)}"
    )
    print(
        "Score with stock: python -m vrdu.evaluate --base_dirpath "
        f"{root / CORPUS_DIR[args.corpus]} --extraction_path {args.out}"
    )
    return 0


def cmd_write_lora_data(args: Namespace) -> int:
    if args.fixtures:
        mem = build_memory_fixtures()
        skills = write_skills_for_seed(
            split_by_type=mem["splits"],
            docs_by_type=mem["docs_by_type"],
            seed=args.seed,
            keys_by_type=FIXTURE_KEYS,
        )
        path = write_lora_jsonl(
            recipe=args.recipe,
            split_by_type=mem["splits"],
            docs_by_type=mem["docs_by_type"],
            skills=skills,
            catalog=TypeCatalog(keys_by_type=FIXTURE_KEYS),
            seed=args.seed,
            out_path=args.out,
        )
        print(path)
        return 0
    root = args.data or default_data_root()
    split_by_type, docs_by_type, skills, catalog = _load_both_published(root, args.seed)
    path = write_lora_jsonl(
        recipe=args.recipe,
        split_by_type=split_by_type,
        docs_by_type=docs_by_type,
        skills=skills,
        catalog=catalog,
        seed=args.seed,
        out_path=args.out,
    )
    print(path)
    return 0


def cmd_experiment_dry(args: Namespace) -> int:
    for arm_id in ARM_IDS:
        arm = get_arm(arm_id)
        for corpus in (CORPUS_REGISTRATION, CORPUS_ADBUY):
            _run_fixtures(arm, args.out / arm.arm_id / corpus, corpus)
    print("arms=" + ",".join(ARM_IDS))
    print("fixture stub only. not a published score.")
    return 0


def _run_fixtures(arm, out: Path, corpus: str) -> None:
    mem = build_memory_fixtures()
    type_id = TYPE_FOR_CORPUS[corpus]
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    binder, extractor = adapters_for_arm(arm, client=_StubClient())
    seed_name = {
        CORPUS_REGISTRATION: "SYNTH-mixed_template-train_1-test_2-valid_1-SD_0",
        CORPUS_ADBUY: "SYNTH-mixed_template-train_1-test_1-valid_1-SD_0",
    }[corpus]
    result = run_corpus(
        corpus=corpus,
        seed=0,
        split=mem["splits"][type_id],
        documents=mem["index_by_type"][type_id],
        skills=skills,
        catalog=TypeCatalog(keys_by_type=FIXTURE_KEYS),
        binder=binder,
        extractor=extractor,
        out_dir=out,
        dump_split_name=seed_name,
    )
    print(result.dump_path)


def _docs_path(root: Path, corpus: str) -> Path:
    docs_path = root / CORPUS_DIR[corpus] / "main" / "dataset.jsonl"
    gz = docs_path.with_suffix(".jsonl.gz")
    if not docs_path.is_file() and gz.is_file():
        return gz
    return docs_path


def _load_published(root: Path, corpus: str, seed: int):
    split = load_run_split(published_split_path(root, corpus, seed), corpus=corpus, seed=seed)
    documents = load_documents(_docs_path(root, corpus))
    other = CORPUS_ADBUY if corpus == CORPUS_REGISTRATION else CORPUS_REGISTRATION
    other_split = load_run_split(
        published_split_path(root, other, seed), corpus=other, seed=seed
    )
    other_docs = load_documents(_docs_path(root, other))
    type_id = TYPE_FOR_CORPUS[corpus]
    other_id = TYPE_1 if type_id == TYPE_0 else TYPE_0
    skills = {
        type_id: write_skill(
            type_id=type_id,
            split=split,
            train_docs=docs_for_filenames(documents, split.train),
            seed=seed,
        ),
        other_id: write_skill(
            type_id=other_id,
            split=other_split,
            train_docs=docs_for_filenames(other_docs, other_split.train),
            seed=seed,
        ),
    }
    return documents, skills, TypeCatalog(keys_by_type=KEYS_FOR_TYPE)


def _load_both_published(root: Path, seed: int):
    split_by_type = {}
    docs_by_type = {}
    for corpus, type_id in ((CORPUS_REGISTRATION, TYPE_0), (CORPUS_ADBUY, TYPE_1)):
        split = load_run_split(
            published_split_path(root, corpus, seed), corpus=corpus, seed=seed
        )
        index = load_documents(_docs_path(root, corpus))
        split_by_type[type_id] = split
        docs_by_type[type_id] = docs_for_filenames(index, split.train)
    skills = write_skills_for_seed(
        split_by_type=split_by_type, docs_by_type=docs_by_type, seed=seed
    )
    return split_by_type, docs_by_type, skills, TypeCatalog(keys_by_type=KEYS_FOR_TYPE)
