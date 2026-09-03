"""CLI: fetch published splits, write train-only skills, dump one corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vrdu_binder.bind import KeywordBinder, TypeCatalog
from vrdu_binder.constants import (
    CORPUS_ADBUY,
    CORPUS_DIR,
    CORPUS_REGISTRATION,
    KEYS_FOR_TYPE,
    SEEDS,
    SPEC_VERSION,
    TYPE_0,
    TYPE_1,
)
from vrdu_binder.documents import docs_for_filenames, load_documents
from vrdu_binder.extract import KeywordExtractor
from vrdu_binder.fetch import default_data_root, fetch_meta, fetch_ocr, fetch_splits
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.experiment import add_experiment_parsers, dispatch_experiment
from vrdu_binder.headline import Headline, make_headline
from vrdu_binder.llm import LlmBinder, LlmExtractor
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.run import run_corpus
from vrdu_binder.skills import write_skills_for_seed
from vrdu_binder.splits import load_run_split, published_split_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrdu-binder",
        description=f"Infona binder-bench spec {SPEC_VERSION} (constructed VRDU mix).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch-splits", help="Download published split JSON")
    p_fetch.add_argument("--dest", type=Path, default=None)

    p_meta = sub.add_parser("fetch-meta", help="Download official meta.json files")
    p_meta.add_argument("--dest", type=Path, default=None)

    p_ocr = sub.add_parser(
        "fetch-ocr",
        help="Download dataset.jsonl.gz (large). Do not commit it.",
    )
    p_ocr.add_argument("--dest", type=Path, default=None)

    p_skills = sub.add_parser("write-skills", help="Train-only skill writer for one seed")
    p_skills.add_argument("--seed", type=int, default=0, choices=SEEDS)
    p_skills.add_argument("--data", type=Path, default=None)
    p_skills.add_argument("--out", type=Path, required=True)

    p_run = sub.add_parser("run", help="Bind then extract; dump one corpus")
    p_run.add_argument("--seed", type=int, default=0, choices=SEEDS)
    p_run.add_argument(
        "--corpus",
        required=True,
        choices=(CORPUS_REGISTRATION, CORPUS_ADBUY),
    )
    p_run.add_argument("--data", type=Path, default=None)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument(
        "--binder",
        default="llm",
        choices=("llm", "keyword"),
        help="llm writes published dumps. keyword is fixtures/dry-run only.",
    )

    p_dry = sub.add_parser("dry-run", help="Fixture mix. No VRDU download, no LLM.")
    p_dry.add_argument("--out", type=Path, required=True)
    add_experiment_parsers(sub)

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ProtocolError as exc:
        print(exc, file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "fetch-splits":
        paths = fetch_splits(args.dest)
        print("\n".join(str(p) for p in paths))
        return 0
    if args.cmd == "fetch-meta":
        paths = fetch_meta(args.dest)
        print("\n".join(str(p) for p in paths))
        return 0
    if args.cmd == "fetch-ocr":
        paths = fetch_ocr(args.dest)
        print("\n".join(str(p) for p in paths))
        return 0
    if args.cmd == "write-skills":
        return _cmd_write_skills(args)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "dry-run":
        return _cmd_dry_run(args)
    if args.cmd in {"experiment-run", "write-lora-data", "experiment-dry"}:
        return dispatch_experiment(args)
    raise AssertionError(args.cmd)


def _cmd_write_skills(args: argparse.Namespace) -> int:
    root = args.data or default_data_root()
    payload: dict[str, object] = {"spec": SPEC_VERSION, "seed": args.seed, "skills": {}}
    for corpus, type_id in (
        (CORPUS_REGISTRATION, TYPE_0),
        (CORPUS_ADBUY, TYPE_1),
    ):
        split = load_run_split(
            published_split_path(root, corpus, args.seed), corpus=corpus, seed=args.seed
        )
        docs_path = root / CORPUS_DIR[corpus] / "main" / "dataset.jsonl"
        if not docs_path.is_file():
            gz = docs_path.with_suffix(".jsonl.gz")
            docs_path = gz if gz.is_file() else docs_path
        index = load_documents(docs_path)
        train_docs = docs_for_filenames(index, split.train)
        from vrdu_binder.skills import write_skill

        skill = write_skill(
            type_id=type_id, split=split, train_docs=train_docs, seed=args.seed
        )
        payload["skills"][type_id] = {  # type: ignore[index]
            "keys": list(skill.keys),
            "body": skill.body,
            "n_train": len(skill.train_filenames),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(args.out)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.binder == "keyword":
        raise ProtocolError(
            "KeywordBinder cannot dump published VRDU splits. "
            "Use `dry-run` for fixtures, or --binder llm with INFONA_BINDER_API_KEY. "
            "Refusing rather than writing a keyword score."
        )
    binder = LlmBinder()
    extractor = LlmExtractor()
    root = args.data or default_data_root()
    split = load_run_split(
        published_split_path(root, args.corpus, args.seed),
        corpus=args.corpus,
        seed=args.seed,
    )
    docs_path = root / CORPUS_DIR[args.corpus] / "main" / "dataset.jsonl"
    gz = docs_path.with_suffix(".jsonl.gz")
    if not docs_path.is_file() and gz.is_file():
        docs_path = gz
    documents = load_documents(docs_path)
    train_docs = docs_for_filenames(documents, split.train)
    from vrdu_binder.constants import TYPE_FOR_CORPUS
    from vrdu_binder.skills import write_skill

    type_id = TYPE_FOR_CORPUS[args.corpus]
    other = TYPE_1 if type_id == TYPE_0 else TYPE_0
    # The other skill is written from ITS train split, not this corpus's test.
    other_corpus = CORPUS_ADBUY if args.corpus == CORPUS_REGISTRATION else CORPUS_REGISTRATION
    other_split = load_run_split(
        published_split_path(root, other_corpus, args.seed),
        corpus=other_corpus,
        seed=args.seed,
    )
    other_docs_path = root / CORPUS_DIR[other_corpus] / "main" / "dataset.jsonl"
    other_gz = other_docs_path.with_suffix(".jsonl.gz")
    if not other_docs_path.is_file() and other_gz.is_file():
        other_docs_path = other_gz
    other_index = load_documents(other_docs_path)
    skills = {
        type_id: write_skill(
            type_id=type_id, split=split, train_docs=train_docs, seed=args.seed
        ),
        other: write_skill(
            type_id=other,
            split=other_split,
            train_docs=docs_for_filenames(other_index, other_split.train),
            seed=args.seed,
        ),
    }
    catalog = TypeCatalog(keys_by_type=KEYS_FOR_TYPE)
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
    )
    print(result.dump_path)
    print(
        f"bind_at_type_accuracy (this corpus only)={result.bind_accuracy:.4f} "
        f"n={len(result.outcomes)}"
    )
    print(
        "Score with: python -m vrdu.evaluate --base_dirpath "
        f"{root / CORPUS_DIR[args.corpus]} --extraction_path {args.out}"
    )
    return 0


def _cmd_dry_run(args: argparse.Namespace) -> int:
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    catalog = TypeCatalog(keys_by_type=FIXTURE_KEYS)
    binder = KeywordBinder()
    extractor = KeywordExtractor()
    pred: dict[str, str] = {}
    gold: dict[str, str] = {}
    for corpus, type_id, seed_name in (
        (CORPUS_REGISTRATION, TYPE_0, "SYNTH-mixed_template-train_1-test_2-valid_1-SD_0"),
        (CORPUS_ADBUY, TYPE_1, "SYNTH-mixed_template-train_1-test_1-valid_1-SD_0"),
    ):
        out = args.out / corpus
        result = run_corpus(
            corpus=corpus,
            seed=0,
            split=mem["splits"][type_id],
            documents=mem["index_by_type"][type_id],
            skills=skills,
            catalog=catalog,
            binder=binder,
            extractor=extractor,
            out_dir=out,
            dump_split_name=seed_name,
        )
        pred.update(result.pred_types)
        gold.update(result.gold_types)
        print(result.dump_path)
    headline: Headline = make_headline(pred_types=pred, gold_types=gold)
    print(json.dumps(headline.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
