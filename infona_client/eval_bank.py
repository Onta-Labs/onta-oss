"""Merge eval finetune pairs into the committed example bank.

Implementation sibling of :mod:`infona_client.eval`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # the bank is imported lazily; this is for the signature only
    from infona_client.nlp.example_bank import ExampleBank

logger = structlog.stdlib.get_logger("infona.eval")


async def rebuild_example_bank(
    finetune_pairs_path: Path,
    bank: ExampleBank | None = None,
) -> int:
    """MERGE this run's finetune pairs into the committed example bank.

    Returns the number of examples accepted (added + refreshed). ``bank`` is a
    test seam; the eval always passes None and gets the default bank path.

    **This is a merge, not a regenerate.** It used to be neither, by accident:
    the bank object was built and ``save()``d without ever being
    ``load()``ed, and ``save()`` writes ``self._examples`` wholesale -- so the
    committed ``infona_client/nlp/data/example_bank.jsonl``, shared state in git, was
    REPLACED by whatever ``eval_reports/finetune_pairs.jsonl`` happened to hold
    on the machine that ran the eval. That file is gitignored and machine-local,
    so a dev evaluating one KG and committing the result silently shrank the
    shared bank to their own subset. That is the most likely way the 148
    Spider4SPARQL entries reached the OSS bank (infona-oss#280 stopped the bank
    being truncated to ZERO, by skipping ``save()`` when nothing was accepted;
    it did not stop 114 entries being replaced by 12).

    Merge is the right semantics rather than regenerate because the bank is the
    durable artifact -- it is the file in git, and the file the per-KG purge in
    ``api/routes/knowledge_graphs.py`` already treats as
    load-mutate-save. (The bank the parent repo's Dockerfile bakes into the
    image is the PARENT's own 507-entry copy, not this one -- keep the two
    straight; this rebuild writes package-relative and never reaches that one.)
    ``finetune_pairs.jsonl`` is a per-machine
    scratch log of one dev's eval history; making IT the source of truth would
    mean committing a multi-megabyte append-only file carrying a full ontology
    dump per pair, and would still lose every example from a KG that machine
    never evaluated.

    Two things make the merge actually converge, both in ``add_batch``:
    identity is ``(question, kg_name)`` so an example is refreshed rather than
    dropped when a re-eval improves its SPARQL, and last-wins within the batch
    so an appended pair beats the stale one it supersedes.
    """
    from infona_client.nlp.example_bank import ExampleBank

    if bank is None:
        bank = ExampleBank(openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""))
    # The load() this whole docstring is about.
    loaded = bank.load()

    items = []
    lines = finetune_pairs_path.read_text().splitlines() if finetune_pairs_path.exists() else []
    for line in lines:
        if not line.strip():
            continue
        try:
            p = json.loads(line)
            kg = p.get("graph_uri", "").split("/kg/")[-1] if "/kg/" in p.get("graph_uri", "") else ""
            items.append({
                "question": p["question"],
                "sparql": p["sparql"],
                "kg_name": kg,
                "ontology_context": p.get("ontology", ""),
                "tenant_id": p.get("tenant_id", "") or "",
                "origin": p.get("origin") or "",
                "blueprint_id": p.get("blueprint_id") or "",
            })
        except (json.JSONDecodeError, KeyError):
            continue

    rebuilt = await bank.add_batch(items) if items else 0
    # Never rewrite the committed bank for nothing (ONTA-449). With the load()
    # above a save() is no longer destructive, but a run whose pairs were all
    # dropped (e.g. all from a benchmark tenant) changed nothing, and rewriting
    # 114 identical lines only invites a confusing no-op diff. The one exception
    # is a bank whose file still carries benchmark rows that load() filtered:
    # saving is what finally removes them from disk.
    purged = (
        bank.skipped_benchmark_on_load
        or getattr(bank, "skipped_unscoped_blueprint_on_load", 0)
    )  # save() clears both
    if rebuilt or purged:
        bank.save()
        logger.info(
            "example_bank_rebuilt",
            loaded=loaded,
            accepted=rebuilt,
            purged=purged,
            total=bank.size,
        )
    else:
        logger.warning(
            "example_bank_rebuild_empty",
            candidates=len(items),
            loaded=loaded,
            detail="no eligible pairs; existing bank left untouched",
        )
    return rebuilt
