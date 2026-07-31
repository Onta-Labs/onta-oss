"""SPARQL example bank with semantic retrieval for few-shot prompting.

Stores (question, SPARQL) pairs from successful evaluations. At query time,
retrieves the most relevant examples via embedding similarity with anti-cheat
filtering, cross-dataset preference, and pattern diversity.

Uses the same OpenRouter text-embedding-3-small embeddings as ontology_embeddings.py.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Shared embed client (ONTA-174) — model/batching/errors live in ONE place.
# Constants are re-exported for backward compatibility with existing importers.
from cograph_client.nlp.embed_client import (  # noqa: F401 — re-exports
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    OPENROUTER_EMBEDDINGS_URL,
    embed_texts,
)
from cograph_client.nlp.embed_client import cosine_similarity as _cosine_similarity  # noqa: F401

logger = logging.getLogger(__name__)

# Bank limits
MAX_BANK_SIZE = 500

# Similarity thresholds
ANTI_CHEAT_THRESHOLD = 0.90  # Exclude examples too similar to excluded questions
SAME_DATASET_MAX_SIM = 0.75  # Same-KG examples must be below this to avoid near-cheating

# Pattern tags detected from SPARQL text
PATTERN_DETECTORS: list[tuple[str, str]] = [
    ("count", r"COUNT\s*\("),
    ("avg", r"AVG\s*\("),
    ("max", r"MAX\s*\("),
    ("sum", r"SUM\s*\("),
    ("filter", r"FILTER\s*\("),
    ("contains", r"CONTAINS\s*\("),
    ("date_filter", r"xsd:dateTime"),
    ("group_by", r"GROUP\s+BY"),
]

# Default file paths
DEFAULT_BANK_PATH = Path(__file__).resolve().parent.parent.parent / "eval_reports" / "example_bank.jsonl"
EVAL_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "eval_reports"

# Holdout v2 KG exclusion list (spec §6.1): holdout-v2 KGs must never be
# indexed into the example bank, regardless of whether they appear in
# eval_reports. The manifest is the source of truth; we fall back to a
# hardcoded list if the manifest isn't reachable (e.g. prod deploys that
# don't ship eval_holdout_v2/). Drift between the fallback and manifest
# is logged at import time so it gets noticed.
_HOLDOUT_V2_KGS_FALLBACK: frozenset[str] = frozenset({
    # healthcare
    "cms-nursing-home-compare",
    "samhsa-n-ssats",
    "medicare-part-d-pricing",
    "hrsa-hpsa",
    "cdc-fluview",
    "cdc-wonder-mortality",
    "npi-registry",
    # finance
    "sec-edgar-10k",
    "fdic-call-reports",
    "treasury-fiscaldata-securities",
    "cftc-swap-data",
    "ncua-credit-union-call-reports",
    "finra-trace-corporate-bonds",
    "ofr-financial-stability",
    # legal
    "patentsview",
    "scdb-supreme-court",
    "doj-enforcement-actions",
    "ftc-consent-decrees",
    "uspto-trademarks",
    "pacer-federal-dockets",
    "fec-enforcement",
    # scientific_public_sector
    "nsf-awards",
    "nih-reporter-non-clinical",
    "fema-disaster-declarations",
    "epa-water-quality-portal",
    "noaa-storm-events",
    "usda-agricultural-statistics",
    "doe-energy-research-grants",
})


def _load_holdout_v2_kgs() -> frozenset[str]:
    """Load holdout-v2 KG IDs from eval_holdout_v2/HOLDOUT_V2_MANIFEST.json.

    Searches a few plausible locations (the omnix-oss submodule lives inside
    the parent cograph repo, so the manifest is typically two or three
    parents up from this file). On any failure, returns the hardcoded
    fallback set and logs a warning so drift gets noticed.
    """
    # __file__ = .../omnix-oss/omnix/nlp/example_bank.py
    here = Path(__file__).resolve()
    candidates = [
        # cograph parent (submodule layout): .../cograph/omnix-oss/omnix/nlp/
        here.parent.parent.parent.parent / "eval_holdout_v2" / "HOLDOUT_V2_MANIFEST.json",
        # alt: one level up (standalone)
        here.parent.parent.parent / "eval_holdout_v2" / "HOLDOUT_V2_MANIFEST.json",
        # cwd fallback
        Path.cwd() / "eval_holdout_v2" / "HOLDOUT_V2_MANIFEST.json",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            with open(path) as f:
                manifest = json.load(f)
            ids = frozenset(
                kg["id"] for kg in manifest.get("kgs", []) if kg.get("id")
            )
            if not ids:
                continue
            # Drift check vs fallback
            missing_from_fallback = ids - _HOLDOUT_V2_KGS_FALLBACK
            extra_in_fallback = _HOLDOUT_V2_KGS_FALLBACK - ids
            if missing_from_fallback or extra_in_fallback:
                logger.warning(
                    "HOLDOUT_V2_KGS fallback drift vs manifest %s: "
                    "missing_from_fallback=%s extra_in_fallback=%s",
                    path, sorted(missing_from_fallback), sorted(extra_in_fallback),
                )
            logger.info("Loaded %d holdout-v2 KGs from %s", len(ids), path)
            return ids
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to read holdout-v2 manifest at %s: %s", path, exc)
            continue
    logger.warning(
        "HOLDOUT_V2_MANIFEST.json not found in any candidate path; "
        "using hardcoded HOLDOUT_V2_KGS fallback (%d entries). "
        "This is OK for prod deploys that don't ship eval_holdout_v2/.",
        len(_HOLDOUT_V2_KGS_FALLBACK),
    )
    return _HOLDOUT_V2_KGS_FALLBACK


HOLDOUT_V2_KGS: frozenset[str] = _load_holdout_v2_kgs()


# ── Benchmark-KG exclusion (ONTA-449) ────────────────────────────────────
#
# Spider4SPARQL and friends are ingested into the isolated ``spider-bench``
# tenant precisely so their schema (Singer, Stadium, Airline, Country, ...)
# never pollutes a real ontology. The example bank is a SECOND path into a
# real user's context that bypasses that isolation completely: it is scoped
# per PROCESS, not per tenant, so a benchmark answer stored during an eval run
# is few-shot injected into every LATER ``/ask``, whatever tenant asked and
# whatever KG they are querying. That is the same contamination rule being
# broken through the prompt path instead of the graph path.
#
# This is not hypothetical. The committed OSS bank carried 148 spider entries
# out of 262. Replaying production retrieval (top-10 cosine over the stored
# embeddings, then tag-diversify to 3) with each non-benchmark KG held out --
# i.e. simulating a domain the bank has never seen, which is what a real user
# looks like -- benchmark examples took **97 of 342 top-3 few-shot slots
# (28.4%)** and changed the selected set for 60 of 114 questions. Five of them
# also teach a malformed shape: a bare ``types/<T>`` IRI in PREDICATE position.
#
# Removing them is close to free. The mean cosine similarity of the selected
# set drops 0.0125 across all 114 held-out queries (0.024 if you look only at
# the 60 whose selection changed at all), because the non-benchmark replacement
# sitting just below in the ranking is almost as close. Cross-domain transfer --
# the reason the bank tolerates examples from other datasets at all -- is
# preserved by the 8 real KGs that remain.
#
# Matched by KG-name prefix rather than an exact set so a NEW benchmark split
# (``spider-tvshow``, ...) is excluded the day it is first evaluated, without
# anyone remembering to extend a list.
BENCHMARK_KG_PREFIXES: tuple[str, ...] = ("spider-",)


def is_benchmark_kg(kg_name: str) -> bool:
    """True if ``kg_name`` is an external-benchmark KG barred from the bank.

    Benchmark corpora live in a disposable tenant and must not reach any real
    user's prompt. See :data:`BENCHMARK_KG_PREFIXES`.

    Tolerates ``None``: ``Example.from_dict`` does ``d.get("kg_name", "")``, so
    a bank line with an explicit ``"kg_name": null`` yields ``None`` rather than
    a string. This is called from ``load()`` OUTSIDE its malformed-line
    ``except``, so raising here would take down the whole load -- and because
    ``get_example_bank`` assigns the singleton before calling ``load()``, it
    would fail silently, leaving the bank empty for the process lifetime.
    """
    return (kg_name or "").strip().lower().startswith(BENCHMARK_KG_PREFIXES)


def example_key(question: str, kg_name: str | None) -> tuple[str, str]:
    """Identity of an example for dedup/refresh: its question, within its KG.

    This used to be the question text ALONE, which conflated two different
    things and got both wrong:

    - The same question asked of two different KGs ("how many rows?") is two
      different examples -- different ontology, different ``FROM`` graph. Keying
      on question alone dropped whichever the eval happened to reach second, so
      the bank's contents depended on eval ORDER.
    - The same question asked of the SAME KG is one example that may have a
      better answer now (reingest changed the predicate URIs; a later run
      produced a corrected SPARQL). Keying on question alone made that a
      "duplicate" to be dropped, so fresh data lost to whatever was in the bank
      first. See :meth:`ExampleBank.add_batch`, which now refreshes instead.

    ``question`` is normalized (strip + lowercase) because it is free text a
    human typed; ``kg_name`` is a slug and is only stripped.
    """
    return (question.strip().lower(), (kg_name or "").strip())


@dataclass
class Example:
    """A single (question, SPARQL) example with metadata."""

    question: str
    sparql: str
    kg_name: str
    ontology_context: str
    pattern_tags: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        """See :func:`example_key`."""
        return example_key(self.question, self.kg_name)

    def refresh_from(self, sparql: str, ontology_context: str) -> bool:
        """Update this example's answer in place. True if anything changed.

        The embedding is NOT recomputed: it is keyed on the question, and the
        question is part of this example's identity, so by construction it has
        not changed. A refresh therefore costs zero embedding calls, which is
        also why it stays allowed when the bank is at ``MAX_BANK_SIZE``.

        Empty inputs are ignored rather than written: a caller with no ontology
        text on hand (``populate_from_eval_reports`` reading a report that
        omitted it) must not blank out context the bank already has.
        """
        changed = False
        if sparql and sparql != self.sparql:
            self.sparql = sparql
            self.pattern_tags = detect_pattern_tags(sparql)
            changed = True
        if ontology_context and ontology_context != self.ontology_context:
            self.ontology_context = ontology_context
            changed = True
        return changed

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "sparql": self.sparql,
            "kg_name": self.kg_name,
            "ontology_context": self.ontology_context,
            "pattern_tags": self.pattern_tags,
            "embedding": self.embedding,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Example":
        return cls(
            question=d["question"],
            sparql=d["sparql"],
            kg_name=d.get("kg_name", ""),
            ontology_context=d.get("ontology_context", ""),
            pattern_tags=d.get("pattern_tags", []),
            embedding=d.get("embedding", []),
        )


def detect_pattern_tags(sparql: str) -> list[str]:
    """Auto-detect query pattern tags from SPARQL text.

    Detects aggregation functions (COUNT, AVG, MAX, SUM), filtering patterns
    (FILTER, CONTAINS, date), structural patterns (JOIN, GROUP BY, multi-hop).
    """
    tags: list[str] = []

    for tag, pattern in PATTERN_DETECTORS:
        if re.search(pattern, sparql, re.IGNORECASE):
            tags.append(tag)

    # "join" — 2+ triple patterns with different subjects
    subjects = set(re.findall(r"\?\w+\s+<", sparql))
    if len(subjects) >= 2:
        tags.append("join")

    # "multi_hop" — 3+ triple patterns (lines ending with ' .')
    triple_count = len(re.findall(r"\.\s*(?:\n|$|\})", sparql))
    # Also count triples separated by ' . '
    triple_count += sparql.count(" . ")
    if triple_count >= 3:
        tags.append("multi_hop")

    return sorted(set(tags))


class ExampleBank:
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
        self._bank_path = Path(bank_path) if bank_path else DEFAULT_BANK_PATH
        self._examples: list[Example] = []
        # How many rows the last load() dropped as benchmark KGs. A writer that
        # is about to save() can use it to persist that read-side filter and
        # actually clean the file (ONTA-449 note in load()).
        self.skipped_benchmark_on_load = 0

    @property
    def size(self) -> int:
        """Number of examples in the bank."""
        return len(self._examples)

    # ── Persistence ──────────────────────────────────────────────────────

    def load(self) -> int:
        """Load examples from JSONL file. Returns number loaded."""
        self._examples = []
        self.skipped_benchmark_on_load = 0
        if not self._bank_path.exists():
            logger.info("Example bank file not found, starting empty: %s", self._bank_path)
            return 0

        skipped_benchmark = 0
        with open(self._bank_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    example = Example.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError) as exc:
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
                if is_benchmark_kg(example.kg_name):
                    skipped_benchmark += 1
                    continue
                self._examples.append(example)

        self.skipped_benchmark_on_load = skipped_benchmark
        if skipped_benchmark:
            logger.warning(
                "Example bank at %s contains %d benchmark-KG example(s); "
                "skipped (ONTA-449). The next save() drops them permanently.",
                self._bank_path, skipped_benchmark,
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
        loss is the exact thing that motivated onta-oss#291.
        """
        self._bank_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._bank_path.with_name(self._bank_path.name + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                for ex in self._examples:
                    f.write(json.dumps(ex.to_dict()) + "\n")
            os.replace(tmp_path, self._bank_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        # The benchmark rows load() filtered are now gone from disk, so the
        # debt is paid; leaving the counter set would make a SECOND save-gated
        # cycle on the same instance fire unconditionally. Read it before
        # save() if you want it for a log line.
        self.skipped_benchmark_on_load = 0
        logger.info("Saved %d examples to %s", len(self._examples), self._bank_path)

    # ── Add examples ─────────────────────────────────────────────────────

    async def add(
        self,
        question: str,
        sparql: str,
        kg_name: str,
        ontology_context: str,
    ) -> bool:
        """Embed and store an example. Returns True if the bank changed.

        Identity is ``(question, kg_name)`` -- see :func:`example_key`. An
        example that is already in the bank is REFRESHED with the new SPARQL /
        ontology context rather than dropped, and refreshing counts as a change
        (a re-add with identical content does not). Enforces MAX_BANK_SIZE for
        NEW examples only; a refresh is always allowed, since it cannot grow the
        bank. Benchmark KGs are refused outright (ONTA-449).
        """
        if is_benchmark_kg(kg_name):
            logger.debug("Refusing benchmark-KG example from %s (ONTA-449)", kg_name)
            return False

        # Already present for this KG -> refresh in place instead of dropping
        # the newer answer on the floor (onta-oss#280 follow-up).
        key = example_key(question, kg_name)
        for ex in self._examples:
            if ex.key == key:
                if ex.refresh_from(sparql, ontology_context):
                    logger.debug("Refreshed existing example: %s", question[:80])
                    return True
                logger.debug("Skipping unchanged duplicate question: %s", question[:80])
                return False

        if len(self._examples) >= MAX_BANK_SIZE:
            logger.warning("Example bank at capacity (%d), skipping add", MAX_BANK_SIZE)
            return False

        pattern_tags = detect_pattern_tags(sparql)
        embedding = await self._embed_single(question)

        self._examples.append(
            Example(
                question=question,
                sparql=sparql,
                kg_name=kg_name,
                ontology_context=ontology_context,
                pattern_tags=pattern_tags,
                embedding=embedding,
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

        Identity is ``(question, kg_name)`` -- see :func:`example_key`. Two
        consequences, both deliberate (the onta-oss#280 follow-up):

        - An item already in the bank REFRESHES it rather than being dropped.
          This is what lets a re-eval land a corrected SPARQL, and what lets the
          post-eval rebuild run repeatedly against an accumulating
          ``finetune_pairs.jsonl`` instead of no-opping forever after the first
          run.
        - Within one batch, LAST write wins. ``finetune_pairs.jsonl`` is keyed
          on ``(question, graph_uri)`` and appended in time order, so when the
          graph IRI changes under a fixed question -- a namespace rename, the
          exact case that stranded this bank on ``omnix.dev`` -- the file holds
          both pairs and the fresher one is later. First-wins kept the stale one.

        Refreshes are exempt from MAX_BANK_SIZE: they cannot grow the bank, and
        a full bank must still be correctable.
        """
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
        # Found by independent review of onta-oss#291.
        collapsed: dict[tuple[str, str], dict] = {}
        for item in items:
            kg_name = item.get("kg_name", "")
            if is_benchmark_kg(kg_name):
                continue
            collapsed[example_key(item["question"], kg_name)] = item

        refreshed: set[tuple[str, str]] = set()
        pending: dict[tuple[str, str], dict] = {}
        for key, item in collapsed.items():
            existing = by_key.get(key)
            if existing is not None:
                if existing.refresh_from(item["sparql"], item.get("ontology_context", "")):
                    refreshed.add(key)
                continue
            pending[key] = item

        new_items = list(pending.values())

        # Enforce cap on NEW examples only.
        if new_items:
            capacity = MAX_BANK_SIZE - len(self._examples)
            if capacity <= 0:
                logger.warning(
                    "Example bank at capacity (%d), skipping %d new example(s); "
                    "%d existing refreshed",
                    MAX_BANK_SIZE, len(new_items), len(refreshed),
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
                self._examples.append(
                    Example(
                        question=item["question"],
                        sparql=item["sparql"],
                        kg_name=item.get("kg_name", ""),
                        ontology_context=item.get("ontology_context", ""),
                        pattern_tags=detect_pattern_tags(item["sparql"]),
                        embedding=emb,
                    )
                )

        if new_items or refreshed:
            logger.info(
                "Added %d and refreshed %d examples (batch), bank now has %d",
                len(new_items), len(refreshed), len(self._examples),
            )
        return len(new_items) + len(refreshed)

    # ── Retrieval ────────────────────────────────────────────────────────

    async def retrieve(
        self,
        question: str,
        ontology_context: str = "",
        exclude_questions: list[str] | None = None,
        kg_name: str = "",
        top_k: int = 3,
    ) -> list[Example]:
        """Retrieve the best few-shot examples for a query.

        Algorithm:
        1. Embed the incoming question.
        2. Cosine similarity against all examples -> top-10 candidates.
        3. EXCLUDE any example whose question similarity > 0.95 to any excluded question (anti-cheat).
        4. EXCLUDE same-dataset examples with similarity > 0.85 (too close).
        5. PREFER cross-dataset examples (different kg_name scores higher).
        6. DIVERSIFY: pick top_k examples with different pattern_tags when possible.

        Args:
            question: The natural language query.
            ontology_context: Current ontology summary (used for re-ranking).
            exclude_questions: Questions to exclude from results (anti-cheat).
            kg_name: The current KG name (for cross-dataset preference).
            top_k: Number of examples to return.

        Returns:
            List of up to top_k Example objects, pattern-diverse and relevant.
        """
        if not self._examples:
            return []

        exclude_questions = exclude_questions or []

        # Step 1: Embed the question
        q_embedding = await self._embed_single(question)
        q_vec = np.array(q_embedding, dtype=np.float32)

        # Build embedding matrix
        bank_matrix = np.stack(
            [np.array(ex.embedding, dtype=np.float32) for ex in self._examples]
        )
        similarities = _cosine_similarity(q_vec, bank_matrix)

        # Step 2: Top-10 candidates by raw similarity
        candidate_indices = np.argsort(similarities)[::-1][:10].tolist()

        # Step 3: Anti-cheat — embed excluded questions and filter
        exclude_vecs: list[np.ndarray] = []
        if exclude_questions:
            exclude_embeddings = await self._embed_texts(exclude_questions)
            exclude_vecs = [np.array(e, dtype=np.float32) for e in exclude_embeddings]

        filtered: list[tuple[int, float]] = []  # (index, adjusted_score)
        for idx in candidate_indices:
            ex = self._examples[idx]
            sim = float(similarities[idx])

            # Anti-cheat: check against excluded questions
            if exclude_vecs:
                ex_vec = np.array(ex.embedding, dtype=np.float32)
                excluded = False
                for ev in exclude_vecs:
                    excl_sim = float(np.dot(ex_vec, ev) / (np.linalg.norm(ex_vec) * np.linalg.norm(ev) + 1e-9))
                    if excl_sim > ANTI_CHEAT_THRESHOLD:
                        excluded = True
                        break
                if excluded:
                    continue

            # Step 4: Same-dataset anti-cheat — EVAL ONLY.
            # Gated on exclude_questions so this only runs during eval/benchmark
            # harness calls (which always pass exclude_questions). In production
            # /ask we WANT to reuse a near-identical prior answer on the same KG:
            # that's the best possible signal, not cheating. Dropping/penalizing
            # it here would actively hurt real users. Keep this in sync with the
            # anti-cheat gate above (line ~321).
            if exclude_vecs and kg_name and ex.kg_name == kg_name:
                if sim > SAME_DATASET_MAX_SIM:
                    continue  # Too similar within same dataset
                # Penalize same-dataset slightly to prefer cross-dataset
                sim *= 0.9

            filtered.append((idx, sim))

        if not filtered:
            return []

        # Sort by adjusted score
        filtered.sort(key=lambda x: x[1], reverse=True)

        # Step 5 & 6: Diversify by pattern_tags
        selected: list[Example] = []
        used_tag_sets: list[set[str]] = []

        for idx, _score in filtered:
            if len(selected) >= top_k:
                break
            ex = self._examples[idx]
            ex_tags = set(ex.pattern_tags)

            # Check if this example's tags are too similar to already-selected ones
            if selected and ex_tags:
                too_similar = False
                for used in used_tag_sets:
                    if used and ex_tags == used:
                        too_similar = True
                        break
                if too_similar:
                    # Still consider it if we haven't filled slots
                    continue

            selected.append(ex)
            used_tag_sets.append(ex_tags)

        # If diversity filtering was too aggressive, backfill from remaining
        if len(selected) < top_k:
            selected_set = {id(ex) for ex in selected}
            for idx, _score in filtered:
                if len(selected) >= top_k:
                    break
                ex = self._examples[idx]
                if id(ex) not in selected_set:
                    selected.append(ex)
                    selected_set.add(id(ex))

        return selected[:top_k]

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
        reports_path = Path(reports_dir) if reports_dir else EVAL_REPORTS_DIR
        items: list[dict] = []
        # Keyed by :func:`example_key`, not by question alone. Question-alone
        # dropped the same wording asked of a DIFFERENT KG before add_batch ever
        # saw it, so the identity fix could not take effect on this path and the
        # winner was whichever KG `sorted(glob(...))` happened to reach first.
        # Found by independent review of onta-oss#291.
        seen_keys: set[tuple[str, str]] = set()

        # 1. Scan eval report JSON files
        for json_file in sorted(reports_path.glob("eval-*.json")):
            try:
                with open(json_file) as f:
                    report = json.load(f)

                kg_name = report.get("kg_name", "")
                if kg_name in HOLDOUT_V2_KGS:
                    logger.debug(
                        "example_bank: skipping holdout-v2 KG %s from eval report %s",
                        kg_name, json_file,
                    )
                    continue
                # ONTA-449 — gate it here as well as in add_batch so the
                # per-KG balancing below divides capacity among REAL KGs only.
                if is_benchmark_kg(kg_name):
                    logger.debug(
                        "example_bank: skipping benchmark KG %s from eval report %s",
                        kg_name, json_file,
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
                    key = example_key(question, kg_name)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    items.append({
                        "question": question,
                        "sparql": sparql,
                        "kg_name": kg_name,
                        "ontology_context": ontology,
                    })
            except (json.JSONDecodeError, OSError) as exc:
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
                            pair = json.loads(line)
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
                            key = example_key(question, kg_name)
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)
                            if kg_name in HOLDOUT_V2_KGS:
                                logger.debug(
                                    "example_bank: skipping holdout-v2 KG %s from finetune pair",
                                    kg_name,
                                )
                                continue
                            if is_benchmark_kg(kg_name):  # ONTA-449
                                logger.debug(
                                    "example_bank: skipping benchmark KG %s from finetune pair",
                                    kg_name,
                                )
                                continue
                            items.append({
                                "question": question,
                                "sparql": sparql,
                                "kg_name": kg_name,
                                "ontology_context": pair.get("ontology", ""),
                            })
                        except json.JSONDecodeError:
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
        per_kg_cap = MAX_BANK_SIZE // num_kgs
        balanced: list[dict] = []
        for kg, kg_items in by_kg.items():
            balanced.extend(kg_items[:per_kg_cap])

        # Fill remaining capacity with extras from any KG
        remaining = MAX_BANK_SIZE - len(balanced)
        if remaining > 0:
            extras = [item for item in items if item not in balanced]
            balanced.extend(extras[:remaining])

        logger.info("Found %d correct examples, balanced to %d across %d KGs", len(items), len(balanced), num_kgs)
        added = await self.add_batch(balanced)
        if added or self.skipped_benchmark_on_load:
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

        Raises :class:`~cograph_client.nlp.embed_client.EmbeddingError` — a
        ``RuntimeError`` subclass, so historical ``except RuntimeError``
        callers are unaffected.
        """
        return await embed_texts(texts, api_key=self._api_key)


# ── Prompt formatting ────────────────────────────────────────────────────

# Placeholder used when a caller formats examples without naming a target graph.
# Never reaches production /ask (the pipeline always passes the target graph);
# it exists so a bank example can be rendered standalone (docs, tests, tooling)
# without carrying whatever tenant happened to answer it first.
TARGET_GRAPH_PLACEHOLDER = "TARGET_GRAPH"

# Matches the graph IRI of a dataset clause: `FROM <...>` / `FROM NAMED <...>`.
# Group 1 keeps the keyword (and its original spacing/case) so only the IRI is
# swapped. The lookbehind is load-bearing: without it a variable that merely ENDS
# in "from" (`?from <p> ?o`, `?validFrom <p> ?o`) or a prefixed name (`ex:from
# <...>`) would have its OBJECT eaten as if it were a dataset clause, silently
# teaching the model a nonsense triple.
_FROM_GRAPH_RE = re.compile(r"(?<![\w?$:-])(FROM\s+(?:NAMED\s+)?)<[^>]*>", re.IGNORECASE)

# Backstop for any graph IRI the keyword rule cannot see: `GRAPH <...>`,
# `SERVICE <...>`, or a bare mention. The bank has none today (all 262 examples
# scope with FROM), but it is REGENERATED from LLM-written SPARQL by
# `populate_from_eval_reports`, so a future model emitting a GRAPH block would
# quietly reopen the leak. Keyed on the `/graphs/` path segment that
# `graph/queries.py` mints, so type/attribute/entity IRIs are never touched.
#
# The `scheme://` anchor and the `[^<>\s]` body are load-bearing, not tidiness:
# `<` is also the SPARQL less-than operator, so a laxer `<[^>]*/graphs/` starts
# matching at the `<` of `FILTER(?y < 2000)` and swallows everything up to the
# next `>` (the whole filter plus a following GRAPH clause). A SPARQL IRI cannot
# contain whitespace, `<`, or `>`, so excluding all three costs nothing and makes
# a comparison operator unmatchable.
_ANY_GRAPH_IRI_RE = re.compile(r"<[a-zA-Z][\w+.-]*://[^<>\s]*/graphs/[^<>\s]*>")


def sanitize_example_sparql(sparql: str, target_graph_uri: str = "") -> str:
    """Rewrite an example's dataset clause onto the CURRENT caller's graph.

    ONTA-420. The example bank is scoped per PROCESS, not per tenant: one JSONL
    file, and ``Example`` has a ``kg_name`` but no tenant. Every stored example
    was answered against whichever graph produced it (the shipped bank is 262
    examples, all ``demo-tenant``), and ``format_examples_for_prompt`` used to
    emit the SPARQL verbatim. So every self-hosted or third-party tenant's
    NL->SPARQL prompt carried our ``demo-tenant`` graph IRIs, defended only by a
    prose "adapt the URIs" instruction in the system prompt.

    The graph IRI is the ONLY tenant-identifying token in a stored example, so it
    is the only thing rewritten here. Type and attribute IRIs are left ALONE on
    purpose: they are the pattern the examples exist to teach (correct
    ``types/<T>/attrs/<a>`` and ``onto/<leaf>`` shapes, aggregation, joins), they
    name public open-data schemas rather than customer data, and abstracting them
    into placeholders would delete the pedagogical value while adding no privacy.

    Rewriting to the caller's real target graph (rather than leaving a
    placeholder in the prompt) also means the model never sees a token it could
    echo into generated SPARQL, and a cross-KG example can no longer point the
    model at a DIFFERENT KG than the one being asked about.

    Two rules, deliberately overlapping: the dataset clause (which catches a graph
    IRI of ANY shape, including a self-hoster's custom one) and any surviving
    ``/graphs/`` IRI (which catches a graph scoped some other way, e.g. a GRAPH
    block). Neither alone is sufficient.
    """
    replacement = target_graph_uri or TARGET_GRAPH_PLACEHOLDER
    out = _FROM_GRAPH_RE.sub(lambda m: f"{m.group(1)}<{replacement}>", sparql)
    return _ANY_GRAPH_IRI_RE.sub(lambda _m: f"<{replacement}>", out)


def format_examples_for_prompt(
    examples: list[Example],
    target_graph_uri: str = "",
) -> str:
    """Format retrieved examples for injection into the SPARQL generation prompt.

    Args:
        examples: Retrieved examples, in prompt order.
        target_graph_uri: The graph the CURRENT question runs against. Every
            example's ``FROM`` clause is rewritten to it (see
            :func:`sanitize_example_sparql`). When empty, a ``<TARGET_GRAPH>``
            placeholder is emitted instead and the header tells the model to
            substitute it.

    Output format:
        Similar queries that worked. Some may come from OTHER graphs, so reuse
        their SHAPE and check every type/attribute URI against the ontology
        schema above instead of copying it. Their FROM clause has been rewritten
        to your target graph.

        Example 1 (count + join):
          Q: How many events are in the Mission District?
          SPARQL: SELECT (COUNT(DISTINCT ?event) AS ?count) FROM <graph> WHERE { ... }

        Example 2 (avg + filter):
          Q: What is the average price of condos?
          SPARQL: SELECT (AVG(?price) AS ?avg) FROM <graph> WHERE { ... }
    """
    if not examples:
        return ""

    if target_graph_uri:
        from_note = "Their FROM clause has been rewritten to your target graph."
    else:
        from_note = (
            f"Their FROM clause shows <{TARGET_GRAPH_PLACEHOLDER}> as a placeholder: "
            "substitute the named graph URI given above."
        )

    # Hedged ("Some may come from OTHER graphs") on purpose. Production /ask
    # passes no exclude_questions, so the same-KG filter + penalty at
    # `retrieve()` stay OFF and a near-identical prior answer on the SAME KG is
    # both common and the best available signal. Its type/attribute URIs are
    # exactly right, so an unconditional "these belong to a DIFFERENT ontology"
    # would tell the model to distrust correct URIs.
    lines = [
        "Similar queries that worked. Some may come from OTHER graphs, so reuse "
        "their SHAPE and check every type/attribute URI against the ontology "
        "schema above instead of copying it.",
        from_note,
    ]

    for i, ex in enumerate(examples, 1):
        tag_str = " + ".join(ex.pattern_tags) if ex.pattern_tags else "basic"
        # Compact the SPARQL — collapse excessive whitespace but keep it readable
        sparql_compact = " ".join(sanitize_example_sparql(ex.sparql, target_graph_uri).split())
        lines.append("")
        lines.append(f"Example {i} ({tag_str}):")
        lines.append(f"  Q: {ex.question}")
        lines.append(f"  SPARQL: {sparql_compact}")

    return "\n".join(lines)


# ── Singleton accessor ───────────────────────────────────────────────────

_example_bank: ExampleBank | None = None


def get_example_bank() -> ExampleBank | None:
    """Lazy-init singleton for the example bank. Returns None if no API key."""
    global _example_bank
    if _example_bank is None:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            try:
                from cograph_client.config import settings
                api_key = settings.openrouter_api_key or ""
            except Exception:
                pass
        if not api_key:
            return None
        _example_bank = ExampleBank(openrouter_api_key=api_key)
        _example_bank.load()
    return _example_bank
