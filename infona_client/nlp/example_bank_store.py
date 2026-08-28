"""ExampleBank persist / add / populate (the store).

Looks up ``MAX_BANK_SIZE`` / ``json`` / holdout and benchmark gates on
:mod:`infona_client.nlp.example_bank` at call time via ``_host()`` so existing
monkeypatches keep working.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from infona_client.nlp.example_bank_models import Example, _host
from infona_client.nlp.example_bank_retrieve import ExampleBankRetrieveMixin

logger = logging.getLogger("infona_client.nlp.example_bank")


class ExampleBank(ExampleBankRetrieveMixin):
    """Persistent bank of (question, SPARQL) examples with semantic retrieval.

    Stores examples as JSONL on disk. Embeddings are generated via OpenRouter
    text-embedding-3-small (1536 dims). Retrieval uses cosine similarity with
    anti-cheat exclusion, cross-dataset preference, and pattern diversity.

    Usage:
        bank = ExampleBank(openrouter_api_key="sk-...")
        bank.load()
        await bank.add("How many events?", "SELECT ...", "events-kg", "Type: Event...")
        examples = await bank.retrieve("Count the events", "Type: Event...", top_k=3)
    """

    def __init__(self, openrouter_api_key: str, bank_path: str | Path | None = None):
        self._api_key = openrouter_api_key
        self._bank_path = Path(bank_path) if bank_path else _host().DEFAULT_BANK_PATH
        self._examples: list[Example] = []
        # How many rows the last load() dropped as benchmark KGs. A writer that
        # is about to save() can use it to persist that read-side filter and
        # actually clean the file (ONTA-449 note in load()).
        self.skipped_benchmark_on_load = 0
        # INF-567: unscoped Blueprint rows (origin=blueprint, no tenant_id)
        # that load() dropped. Same save-to-clean contract as the benchmark
        # counter: the write gates cannot clean a file already on disk.
        self.skipped_unscoped_blueprint_on_load = 0

    @property
    def size(self) -> int:
        """Number of examples in the bank."""
        return len(self._examples)

    # ── Persistence ──────────────────────────────────────────────────────

    def load(self) -> int:
        """Load examples from JSONL file. Returns number loaded."""
        host = _host()
        self._examples = []
        self.skipped_benchmark_on_load = 0
        self.skipped_unscoped_blueprint_on_load = 0
        if not self._bank_path.exists():
            logger.info("Example bank file not found, starting empty: %s", self._bank_path)
            return 0

        skipped_benchmark = 0
        skipped_unscoped_blueprint = 0
        with open(self._bank_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    example = Example.from_dict(host.json.loads(line))
                except (host.json.JSONDecodeError, KeyError) as exc:
                    logger.warning("Skipping malformed example bank line: %s", exc)
                    continue
                # ONTA-449. The write-side gates below keep new benchmark
                # answers OUT, but they cannot clean a bank file that is
                # already on disk -- a machine-local one regenerated before
                # this landed, or a stale copy in a deployed image. Filtering
                # on read is what makes the exclusion hold for those too.
                #
                # Note this filter is DESTRUCTIVE the next time anything saves:
                # `save()` writes `self._examples`, so a load() followed by the
                # KG-delete purge in api/routes/knowledge_graphs.py rewrites the
                # file without these rows even though the delete targeted an
                # unrelated KG. Intended here (the whole point is that they go
                # away), but it means any future addition to
                # BENCHMARK_KG_PREFIXES turns a false positive into permanent,
                # unprompted deletion of a user's examples. Weigh that before
                # widening the tuple.
                if host.is_benchmark_kg(example.kg_name):
                    skipped_benchmark += 1
                    continue
                # INF-567. A Blueprint example without tenant_id would be
                # globally visible — the exact leak. Tenant-scoped Blueprint
                # rows stay loaded; retrieve() is what hides them from other
                # tenants.
                if host.is_unscoped_blueprint_example(
                    example.origin, example.tenant_id
                ):
                    skipped_unscoped_blueprint += 1
                    continue
                self._examples.append(example)

        self.skipped_benchmark_on_load = skipped_benchmark
        self.skipped_unscoped_blueprint_on_load = skipped_unscoped_blueprint
        if skipped_benchmark:
            logger.warning(
                "Example bank at %s contains %d benchmark-KG example(s); "
                "skipped (ONTA-449). The next save() drops them permanently.",
                self._bank_path, skipped_benchmark,
            )
        if skipped_unscoped_blueprint:
            logger.warning(
                "Example bank at %s contains %d unscoped Blueprint example(s); "
                "skipped (INF-567). The next save() drops them permanently.",
                self._bank_path, skipped_unscoped_blueprint,
            )
        logger.info("Loaded %d examples from %s", len(self._examples), self._bank_path)
        return len(self._examples)

    def save(self) -> None:
        """Persist all examples to JSONL file.

        Writes a sibling temp file and ``os.replace``s it, so an interrupted or
        failing save leaves the previous bank intact instead of a truncated one.
        Opening the real path ``"w"`` truncates before the first byte lands --
        an unacceptable failure mode for a file that is committed to git, is
        rewritten routinely now that the eval rebuild merges into it, and whose
        loss is the exact thing that motivated infona-oss#291.
        """
        host = _host()
        self._bank_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._bank_path.with_name(self._bank_path.name + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                for ex in self._examples:
                    f.write(host.json.dumps(ex.to_dict()) + "\n")
            os.replace(tmp_path, self._bank_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        # The benchmark rows load() filtered are now gone from disk, so the
        # debt is paid; leaving the counter set would make a SECOND save-gated
        # cycle on the same instance fire unconditionally. Read it before
        # save() if you want it for a log line.
        self.skipped_benchmark_on_load = 0
        self.skipped_unscoped_blueprint_on_load = 0
        logger.info("Saved %d examples to %s", len(self._examples), self._bank_path)

    # ── Add examples ─────────────────────────────────────────────────────

    async def add(
        self,
        question: str,
        sparql: str,
        kg_name: str,
        ontology_context: str,
        *,
        tenant_id: str = "",
        origin: str = "",
    ) -> bool:
        """Embed and store an example. Returns True if the bank changed.

        Identity is ``(question, kg_name, tenant_id)`` -- see :func:`example_key`.
        An example that is already in the bank is REFRESHED with the new SPARQL /
        ontology context rather than dropped, and refreshing counts as a change
        (a re-add with identical content does not). Enforces MAX_BANK_SIZE for
        NEW examples only; a refresh is always allowed, since it cannot grow the
        bank. Benchmark KGs are refused outright (ONTA-449). Unscoped Blueprint
        examples (origin=blueprint, no tenant_id) are refused (INF-567).
        """
        host = _host()
        origin = host.normalize_example_origin(origin)
        tenant_id = (tenant_id or "").strip()
        if host.is_benchmark_kg(kg_name):
            logger.debug("Refusing benchmark-KG example from %s (ONTA-449)", kg_name)
            return False
        if host.is_unscoped_blueprint_example(origin, tenant_id):
            logger.debug("Refusing unscoped Blueprint example (INF-567)")
            return False

        # Already present for this KG/tenant -> refresh in place instead of
        # dropping the newer answer on the floor (infona-oss#280 follow-up).
        key = host.example_key(question, kg_name, tenant_id)
        for ex in self._examples:
            if ex.key == key:
                if ex.refresh_from(sparql, ontology_context):
                    logger.debug("Refreshed existing example: %s", question[:80])
                    return True
                logger.debug("Skipping unchanged duplicate question: %s", question[:80])
                return False

        if len(self._examples) >= host.MAX_BANK_SIZE:
            logger.warning("Example bank at capacity (%d), skipping add", host.MAX_BANK_SIZE)
            return False

        pattern_tags = host.detect_pattern_tags(sparql)
        embedding = await self._embed_single(question)

        self._examples.append(
            Example(
                question=question,
                sparql=sparql,
                kg_name=kg_name,
                ontology_context=ontology_context,
                pattern_tags=pattern_tags,
                embedding=embedding,
                cypher="",
                tenant_id=tenant_id,
                origin=origin,
            )
        )
        return True

    async def add_cypher(
        self,
        question: str,
        cypher: str,
        kg_name: str,
        ontology_context: str,
        *,
        sparql: str = "",
        tenant_id: str = "",
        origin: str = "",
    ) -> bool:
        """Embed and store a Cypher (or mixed) example. Returns True if changed.

        Same identity rules as :meth:`add`. ``sparql`` may be empty for
        Cypher-only rows; existing SPARQL rows can gain a ``cypher`` field via
        refresh without losing their SPARQL answer.
        """
        host = _host()
        origin = host.normalize_example_origin(origin)
        tenant_id = (tenant_id or "").strip()
        if host.is_benchmark_kg(kg_name):
            logger.debug("Refusing benchmark-KG example from %s (ONTA-449)", kg_name)
            return False
        if host.is_unscoped_blueprint_example(origin, tenant_id):
            logger.debug("Refusing unscoped Blueprint example (INF-567)")
            return False
        if not (cypher or "").strip() and not (sparql or "").strip():
            return False

        key = host.example_key(question, kg_name, tenant_id)
        for ex in self._examples:
            if ex.key == key:
                if ex.refresh_from(sparql, ontology_context, cypher=cypher):
                    logger.debug("Refreshed existing example (cypher): %s", question[:80])
                    return True
                logger.debug("Skipping unchanged duplicate question: %s", question[:80])
                return False

        if len(self._examples) >= host.MAX_BANK_SIZE:
            logger.warning("Example bank at capacity (%d), skipping add", host.MAX_BANK_SIZE)
            return False

        tags = (
            host.detect_pattern_tags_cypher(cypher)
            if cypher
            else host.detect_pattern_tags(sparql)
        )
        embedding = await self._embed_single(question)
        self._examples.append(
            Example(
                question=question,
                sparql=sparql or "",
                kg_name=kg_name,
                ontology_context=ontology_context,
                pattern_tags=tags,
                embedding=embedding,
                cypher=cypher or "",
                tenant_id=tenant_id,
                origin=origin,
            )
        )
        return True

    async def add_batch(
        self,
        items: list[dict],
    ) -> int:
        """Bulk-add examples. Each dict must have: question, sparql, kg_name, ontology_context.

        Embeds in batches and appends. Returns the number of examples ACCEPTED
        -- newly added PLUS refreshed in place -- so a caller can use it as
        "did the bank change?" (``eval.rebuild_example_bank`` gates its
        ``save()`` on exactly that). Benchmark-KG items are dropped (ONTA-449).
        Unscoped Blueprint items are dropped (INF-567).

        Identity is ``(question, kg_name, tenant_id)`` -- see :func:`example_key`. Two
        consequences, both deliberate (the infona-oss#280 follow-up):

        - An item already in the bank REFRESHES it rather than being dropped.
          This is what lets a re-eval land a corrected SPARQL, and what lets the
          post-eval rebuild run repeatedly against an accumulating
          ``finetune_pairs.jsonl`` instead of no-opping forever after the first
          run.
        - Within one batch, LAST write wins. ``finetune_pairs.jsonl`` is keyed
          on ``(question, graph_uri)`` and appended in time order, so when the
          graph IRI changes under a fixed question -- a namespace rename, the
          exact case that stranded this bank on ``graph.infona.ai`` -- the file holds
          both pairs and the fresher one is later. First-wins kept the stale one.

        Refreshes are exempt from MAX_BANK_SIZE: they cannot grow the bank, and
        a full bank must still be correctable.
        """
        host = _host()
        by_key = {ex.key: ex for ex in self._examples}

        # Collapse the batch by key FIRST, then decide refresh-vs-new. Keyed
        # dict, not a list: preserves first-appearance ORDER while letting a
        # later item overwrite an earlier one's CONTENT (last-wins, above).
        #
        # Collapsing before the refresh (rather than refreshing eagerly as each
        # item arrives) is what makes last-wins uniform across both branches and
        # the returned count exact. Eager refresh applied every duplicate in
        # turn, so a batch holding A then B for one key flipped the example
        # A->B->A -- both writes report "changed", and the caller's
        # "don't rewrite the bank for nothing" gate fires on a net no-op. Not
        # hypothetical: a namespace rename leaves TWO pairs for one question in
        # finetune_pairs.jsonl permanently (it keys on graph_uri, which differs;
        # the rebuild truncates both to the same kg_name), so every subsequent
        # eval re-saved a byte-identical bank and logged a bogus accepted count.
        # Found by independent review of infona-oss#291.
        collapsed: dict[tuple[str, str, str], dict] = {}
        for item in items:
            kg_name = item.get("kg_name", "")
            if host.is_benchmark_kg(kg_name):
                continue
            origin = host.normalize_example_origin(
                item.get("origin", "") or "",
                blueprint_id=item.get("blueprint_id") or "",
            )
            tenant_id = (item.get("tenant_id", "") or "").strip()
            if host.is_unscoped_blueprint_example(origin, tenant_id):
                continue
            item = {**item, "origin": origin, "tenant_id": tenant_id}
            collapsed[host.example_key(item["question"], kg_name, tenant_id)] = item

        refreshed: set[tuple[str, str, str]] = set()
        pending: dict[tuple[str, str, str], dict] = {}
        for key, item in collapsed.items():
            existing = by_key.get(key)
            if existing is not None:
                if existing.refresh_from(
                    item.get("sparql", "") or "",
                    item.get("ontology_context", ""),
                    cypher=item.get("cypher", "") or "",
                ):
                    refreshed.add(key)
                continue
            pending[key] = item

        new_items = list(pending.values())

        # Enforce cap on NEW examples only.
        if new_items:
            capacity = host.MAX_BANK_SIZE - len(self._examples)
            if capacity <= 0:
                logger.warning(
                    "Example bank at capacity (%d), skipping %d new example(s); "
                    "%d existing refreshed",
                    host.MAX_BANK_SIZE, len(new_items), len(refreshed),
                )
                new_items = []
            elif len(new_items) > capacity:
                logger.warning(
                    "Example bank near capacity: keeping %d of %d new example(s)",
                    capacity, len(new_items),
                )
                new_items = new_items[:capacity]

        if new_items:
            # Batch embed all questions
            questions = [item["question"] for item in new_items]
            embeddings = await self._embed_texts(questions)

            for item, emb in zip(new_items, embeddings):
                sparql = item.get("sparql", "") or ""
                cypher = item.get("cypher", "") or ""
                if cypher and not sparql:
                    tags = host.detect_pattern_tags_cypher(cypher)
                else:
                    tags = host.detect_pattern_tags(sparql) if sparql else host.detect_pattern_tags_cypher(cypher)
                self._examples.append(
                    Example(
                        question=item["question"],
                        sparql=sparql,
                        kg_name=item.get("kg_name", ""),
                        ontology_context=item.get("ontology_context", ""),
                        pattern_tags=tags,
                        embedding=emb,
                        cypher=cypher,
                        tenant_id=item.get("tenant_id", "") or "",
                        origin=item.get("origin", "") or "",
                    )
                )

        if new_items or refreshed:
            logger.info(
                "Added %d and refreshed %d examples (batch), bank now has %d",
                len(new_items), len(refreshed), len(self._examples),
            )
        return len(new_items) + len(refreshed)

    # ── Populate from eval reports ───────────────────────────────────────

    async def populate_from_eval_reports(self, reports_dir: str | Path | None = None) -> int:
        """Scan eval_reports/*.json for correct answers and bulk-add them.

        Also reads finetune_pairs.jsonl if present. Returns total examples
        accepted (added + refreshed).

        Adds to whatever is already in memory: call :meth:`load` first to MERGE
        into the committed bank, or don't to build one from the reports alone.
        The ``save()`` at the end is skipped when nothing was accepted, so this
        can never replace a bank on disk with an empty one (ONTA-449 and its follow-up).

        Each eval report JSON has structure:
            {
                "kg_name": str,
                "ontology": str,
                "queries": {
                    "results": [{"question", "sparql", "verdict", ...}, ...]
                }
            }
        """
        host = _host()
        reports_path = Path(reports_dir) if reports_dir else host.EVAL_REPORTS_DIR
        items: list[dict] = []
        # Keyed by :func:`example_key`, not by question alone. Question-alone
        # dropped the same wording asked of a DIFFERENT KG before add_batch ever
        # saw it, so the identity fix could not take effect on this path and the
        # winner was whichever KG `sorted(glob(...))` happened to reach first.
        # Found by independent review of infona-oss#291.
        seen_keys: set[tuple[str, str, str]] = set()

        # 1. Scan eval report JSON files
        for json_file in sorted(reports_path.glob("eval-*.json")):
            try:
                with open(json_file) as f:
                    report = host.json.load(f)

                kg_name = report.get("kg_name", "")
                if kg_name in host.HOLDOUT_V2_KGS:
                    logger.debug(
                        "example_bank: skipping holdout-v2 KG %s from eval report %s",
                        kg_name, json_file,
                    )
                    continue
                # ONTA-449 — gate it here as well as in add_batch so the
                # per-KG balancing below divides capacity among REAL KGs only.
                if host.is_benchmark_kg(kg_name):
                    logger.debug(
                        "example_bank: skipping benchmark KG %s from eval report %s",
                        kg_name, json_file,
                    )
                    continue
                origin = host.normalize_example_origin(
                    report.get("origin") or report.get("source") or "",
                    blueprint_id=report.get("blueprint_id") or "",
                )
                tenant_id = (report.get("tenant_id") or "").strip()
                # INF-567 — same four-layer shape as the spider- prefix block.
                # An unscoped Blueprint report would become a global few-shot.
                if host.is_unscoped_blueprint_example(origin, tenant_id):
                    logger.debug(
                        "example_bank: skipping unscoped Blueprint report %s (INF-567)",
                        json_file,
                    )
                    continue
                ontology = report.get("ontology", "")
                results = report.get("queries", {}).get("results", [])

                for result in results:
                    if result.get("verdict") != "correct":
                        continue
                    question = result.get("question", "").strip()
                    sparql = result.get("sparql", "").strip()
                    if not question or not sparql:
                        continue
                    row_origin = host.normalize_example_origin(
                        result.get("origin") or origin,
                        blueprint_id=result.get("blueprint_id") or "",
                    )
                    row_tenant = (result.get("tenant_id") or tenant_id or "").strip()
                    if host.is_unscoped_blueprint_example(row_origin, row_tenant):
                        continue
                    key = host.example_key(question, kg_name, row_tenant)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    items.append({
                        "question": question,
                        "sparql": sparql,
                        "kg_name": kg_name,
                        "ontology_context": ontology,
                        "tenant_id": row_tenant,
                        "origin": row_origin,
                    })
            except (host.json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping eval report %s: %s", json_file, exc)

        # 2. Read finetune_pairs.jsonl
        finetune_path = reports_path / "finetune_pairs.jsonl"
        if finetune_path.exists():
            try:
                with open(finetune_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            pair = host.json.loads(line)
                            question = pair.get("question", "").strip()
                            sparql = pair.get("sparql", "").strip()
                            if not question or not sparql:
                                continue
                            # Derive kg_name BEFORE the dedup check: the key
                            # needs it, so the old question-only check had to
                            # run first and could drop a pair belonging to a
                            # different KG than the one that claimed the
                            # question.
                            graph_uri = pair.get("graph_uri", "")
                            kg_name = graph_uri.split("/kg/")[-1] if "/kg/" in graph_uri else ""
                            origin = host.normalize_example_origin(
                                pair.get("origin") or pair.get("source") or "",
                                blueprint_id=pair.get("blueprint_id") or "",
                            )
                            tenant_id = (pair.get("tenant_id") or "").strip()
                            key = host.example_key(question, kg_name, tenant_id)
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)
                            if kg_name in host.HOLDOUT_V2_KGS:
                                logger.debug(
                                    "example_bank: skipping holdout-v2 KG %s from finetune pair",
                                    kg_name,
                                )
                                continue
                            if host.is_benchmark_kg(kg_name):  # ONTA-449
                                logger.debug(
                                    "example_bank: skipping benchmark KG %s from finetune pair",
                                    kg_name,
                                )
                                continue
                            if host.is_unscoped_blueprint_example(origin, tenant_id):
                                logger.debug(
                                    "example_bank: skipping unscoped Blueprint finetune pair (INF-567)",
                                )
                                continue
                            items.append({
                                "question": question,
                                "sparql": sparql,
                                "kg_name": kg_name,
                                "ontology_context": pair.get("ontology", ""),
                                "tenant_id": tenant_id,
                                "origin": origin,
                            })
                        except host.json.JSONDecodeError:
                            continue
            except OSError as exc:
                logger.warning("Skipping finetune pairs: %s", exc)

        if not items:
            logger.info("No correct examples found in eval reports")
            return 0

        # Balance across KGs: cap per-KG to ensure representation from all datasets
        from collections import defaultdict
        by_kg: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            by_kg[item.get("kg_name", "")].append(item)

        num_kgs = max(len(by_kg), 1)
        per_kg_cap = host.MAX_BANK_SIZE // num_kgs
        balanced: list[dict] = []
        for kg, kg_items in by_kg.items():
            balanced.extend(kg_items[:per_kg_cap])

        # Fill remaining capacity with extras from any KG
        remaining = host.MAX_BANK_SIZE - len(balanced)
        if remaining > 0:
            extras = [item for item in items if item not in balanced]
            balanced.extend(extras[:remaining])

        logger.info("Found %d correct examples, balanced to %d across %d KGs", len(items), len(balanced), num_kgs)
        added = await self.add_batch(balanced)
        if added or self.skipped_benchmark_on_load or self.skipped_unscoped_blueprint_on_load:
            self.save()
        else:
            logger.info("Example bank unchanged by populate; not rewriting %s", self._bank_path)
        return added

    # ── Embedding API ────────────────────────────────────────────────────

    async def _embed_single(self, text: str) -> list[float]:
        """Embed a single text string."""
        results = await self._embed_texts([text])
        return results[0]

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Delegate to the shared embed client (kept as a method: test seam).

        Raises :class:`~infona_client.nlp.embed_client.EmbeddingError` — a
        ``RuntimeError`` subclass, so historical ``except RuntimeError``
        callers are unaffected.
        """
        return await _host().embed_texts(texts, api_key=self._api_key)
