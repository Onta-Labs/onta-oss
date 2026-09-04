"""Bind@type then extract. One skill. One corpus dump directory."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from vrdu_binder.bind import Binder, TypeCatalog, bind_one, skill_for_bind
from vrdu_binder.constants import TYPE_FOR_CORPUS, split_filename
from vrdu_binder.dump import write_predictions
from vrdu_binder.extract import Extractor, extract_one
from vrdu_binder.gate import assert_may_dump
from vrdu_binder.headline import bind_accuracy
from vrdu_binder.ocr import bind_prompt
from vrdu_binder.protocol import OpaqueDoc, ProtocolError, opaque_id
from vrdu_binder.skills import Skill
from vrdu_binder.splits import RunSplit


@dataclass
class DocOutcome:
    filename: str
    opaque_id: str
    bound_type: str
    gold_type: str
    items: list[Any]


@dataclass
class CorpusRun:
    corpus: str
    split_name: str
    dump_path: Path
    outcomes: list[DocOutcome] = field(default_factory=list)

    @property
    def pred_types(self) -> dict[str, str]:
        return {o.filename: o.bound_type for o in self.outcomes}

    @property
    def gold_types(self) -> dict[str, str]:
        return {o.filename: o.gold_type for o in self.outcomes}

    @property
    def bind_accuracy(self) -> float:
        return bind_accuracy(self.pred_types, self.gold_types)


def gold_type_for_corpus(corpus: str) -> str:
    if corpus not in TYPE_FOR_CORPUS:
        raise ProtocolError(f"unknown corpus {corpus!r}")
    return TYPE_FOR_CORPUS[corpus]


def run_corpus(
    *,
    corpus: str,
    seed: int,
    split: RunSplit,
    documents: Mapping[str, Mapping[str, Any]],
    skills: Mapping[str, Skill],
    catalog: TypeCatalog,
    binder: Binder,
    extractor: Extractor,
    out_dir: Path | str,
    dump_split_name: str | None = None,
    concurrency: int = 1,
) -> CorpusRun:
    """Unmodified test list. Misbind writes ``results[filename] = []``."""
    gold = gold_type_for_corpus(corpus)
    split_name = dump_split_name or split_filename(corpus, seed).removesuffix(".json")
    assert_may_dump(split_name=split_name, binder=binder, extractor=extractor)
    filled: dict[str, list[Any]] = {}
    misbound: set[str] = set()
    outcomes: list[DocOutcome] = []

    def _one(i: int, filename: str) -> DocOutcome:
        oid = opaque_id(i)
        if filename not in documents:
            raise ProtocolError(f"test filename {filename!r} has no document")
        doc = documents[filename]
        prompt = bind_prompt(doc)
        _ = OpaqueDoc(opaque_id=oid, ocr_tokens=prompt)
        try:
            bound = bind_one(binder, prompt, catalog)
        except ProtocolError:
            # Unparseable bind is not a catalog id. Count as misbind.
            return DocOutcome(
                filename=filename,
                opaque_id=oid,
                bound_type="__unparsed__",
                gold_type=gold,
                items=[],
            )
        if bound != gold:
            return DocOutcome(
                filename=filename,
                opaque_id=oid,
                bound_type=bound,
                gold_type=gold,
                items=[],
            )
        skill = skill_for_bind(bound, skills)
        try:
            items = extract_one(extractor, prompt, skill)
        except ProtocolError:
            # Bind counted; empty extract. Do not abort the corpus.
            items = []
        return DocOutcome(
            filename=filename,
            opaque_id=oid,
            bound_type=bound,
            gold_type=gold,
            items=items,
        )

    workers = max(1, int(concurrency))
    if workers == 1:
        ordered = [_one(i, filename) for i, filename in enumerate(split.test)]
    else:
        by_name: dict[str, DocOutcome] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_one, i, filename): filename
                for i, filename in enumerate(split.test)
            }
            for fut in as_completed(futs):
                by_name[futs[fut]] = fut.result()
        ordered = [by_name[name] for name in split.test]

    for outcome in ordered:
        if outcome.bound_type != gold:
            misbound.add(outcome.filename)
            filled[outcome.filename] = []
        else:
            filled[outcome.filename] = outcome.items
        outcomes.append(outcome)

    dump_path = write_predictions(
        out_dir=out_dir,
        split_name=split_name,
        split=split,
        filled=filled,
        misbound=misbound,
        extra_meta={"adapter": type(binder).__name__},
    )
    return CorpusRun(
        corpus=corpus, split_name=split_name, dump_path=dump_path, outcomes=outcomes
    )
