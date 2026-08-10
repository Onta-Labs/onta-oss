"""ONTA-449: external-benchmark answers must never reach the few-shot bank.

Spider4SPARQL and friends are ingested into the disposable ``spider-bench``
tenant exactly so their schema (Singer, Stadium, Airline, Country, ...) cannot
pollute a real ontology. The example bank quietly bypassed that: it is scoped
per PROCESS, not per tenant, so a benchmark answer stored during an eval run
was few-shot injected into every LATER ``/ask``, whatever tenant asked. Same
isolation rule, broken through the prompt path instead of the graph path.

The committed OSS bank carried 148 spider entries out of 262 -- and they were
not inert. Replaying production retrieval (top-10 cosine over the stored
embeddings, then tag-diversify to 3) with each real KG held out, so the query
looks like a domain the bank has never seen, benchmark examples took 97 of 342
top-3 slots. Five of them also teach a malformed shape: a bare ``types/<T>``
IRI in PREDICATE position, which appears in no non-benchmark example.

Three layers, because each one alone leaks:

- **Data** -- the committed JSONL has none. Checked here so a regenerated bank
  cannot reintroduce them through a commit.
- **Write** -- ``add`` / ``add_batch`` / ``populate_from_eval_reports`` refuse
  them, so the post-eval rebuild cannot put them back on a dev's machine.
- **Read** -- ``load`` filters them, which is the only layer that helps a bank
  file that already exists on disk (a machine-local one regenerated before
  this landed, or a stale copy baked into a deployed image).
"""

import json

import pytest

from infona_client.nlp.example_bank import (
    BENCHMARK_KG_PREFIXES,
    DEFAULT_BANK_PATH,
    ExampleBank,
    is_benchmark_kg,
)

GRAPH = "https://graph.infona.ai/graphs/demo-tenant/kg/{kg}"


def _row(kg: str, question: str) -> dict:
    return {
        "question": question,
        "sparql": f"SELECT ?x FROM <{GRAPH.format(kg=kg)}> WHERE {{ ?x a ?t }}",
        "kg_name": kg,
        "ontology_context": "",
        "pattern_tags": [],
        "embedding": [0.0] * 1536,
    }


def _write_bank(path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# ── The predicate ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kg_name",
    [
        "spider-world-1",
        "spider-concert-singer",
        "SPIDER-CAR-1",
        "  spider-flight-2  ",
        "eval-mh-concert-singer-v3",
        "EVAL-MH-pets-1-v3",
        "  eval-mh-network-1-v3  ",
    ],
)
def test_benchmark_kgs_are_recognized(kg_name):
    assert is_benchmark_kg(kg_name)


@pytest.mark.parametrize(
    "kg_name",
    [
        "imdb-movies",
        "events-sf",
        "",
        # Prefix, not substring: a real KG that merely CONTAINS the word must
        # not be swept up. `spider-` includes the dash for this reason.
        "spiderman-box-office",
        "arachnid-spider-survey",
        # eval-mh- is prefix with dash; bare "eval" must not match
        "eval-production-kg",
        "clinical-trials",
    ],
)
def test_real_kgs_are_not_mistaken_for_benchmarks(kg_name):
    assert not is_benchmark_kg(kg_name)


def test_prefixes_carry_the_separator():
    """A bare `spider` prefix would also match `spiderman-box-office`."""
    assert all(p.endswith("-") for p in BENCHMARK_KG_PREFIXES), BENCHMARK_KG_PREFIXES


def test_a_null_kg_name_does_not_raise():
    """``Example.from_dict`` does ``d.get("kg_name", "")``, so an explicit
    ``"kg_name": null`` in the JSONL yields None, not a string. ``load()``
    calls this OUTSIDE its malformed-line ``except``, so raising here would
    take down the whole load and leave the bank empty for the process
    lifetime -- and because ``get_example_bank`` assigns the singleton before
    calling ``load()``, it would fail silently rather than loudly.
    """
    assert is_benchmark_kg(None) is False  # type: ignore[arg-type]


# ── Layer 1: the committed data ──────────────────────────────────────────


def test_committed_bank_has_no_benchmark_examples():
    assert DEFAULT_BANK_PATH.exists(), f"example bank missing at {DEFAULT_BANK_PATH}"
    offenders = sorted(
        {
            rec["kg_name"]
            for line in DEFAULT_BANK_PATH.read_text().splitlines()
            if line.strip()
            for rec in [json.loads(line)]
            if is_benchmark_kg(rec.get("kg_name", ""))
        }
    )
    assert not offenders, (
        f"{DEFAULT_BANK_PATH} contains benchmark-KG examples {offenders}. "
        "The bank is injected verbatim into every /ask few-shot prompt, so "
        "benchmark schema would be taught to real users' NL->SPARQL "
        "generation. Regenerate the bank without them (ONTA-449)."
    )


def test_committed_bank_still_spans_multiple_real_kgs():
    """The purge must not have collapsed the cross-domain transfer the bank is for."""
    kgs = {
        json.loads(line)["kg_name"]
        for line in DEFAULT_BANK_PATH.read_text().splitlines()
        if line.strip()
    }
    assert len(kgs) >= 5, f"bank collapsed to {len(kgs)} KG(s): {sorted(kgs)}"


# ── Layer 2: the write path ──────────────────────────────────────────────


async def test_add_refuses_a_benchmark_example(tmp_path):
    bank = ExampleBank(openrouter_api_key="unused", bank_path=tmp_path / "bank.jsonl")

    async def _boom(_texts):
        raise AssertionError("must refuse before spending an embedding call")

    bank._embed_texts = _boom  # type: ignore[method-assign]

    assert await bank.add("q", "SELECT ?x", "spider-world-1", "") is False
    assert bank.size == 0


async def test_add_batch_drops_benchmark_items_and_keeps_the_rest(tmp_path):
    bank = ExampleBank(openrouter_api_key="unused", bank_path=tmp_path / "bank.jsonl")

    async def _fake_embed(texts):
        return [[0.0] * 1536 for _ in texts]

    bank._embed_texts = _fake_embed  # type: ignore[method-assign]

    added = await bank.add_batch(
        [
            {"question": "how many singers", "sparql": "SELECT ?x", "kg_name": "spider-concert-singer", "ontology_context": ""},
            {"question": "how many films", "sparql": "SELECT ?x", "kg_name": "imdb-movies", "ontology_context": ""},
        ]
    )
    assert added == 1
    assert [ex.kg_name for ex in bank._examples] == ["imdb-movies"]


async def test_populate_from_eval_reports_skips_benchmark_reports(tmp_path):
    reports = tmp_path / "eval_reports"
    reports.mkdir()

    def _report(kg: str, question: str) -> dict:
        return {
            "kg_name": kg,
            "ontology": "",
            "queries": {"results": [{"question": question, "sparql": "SELECT ?x", "verdict": "correct"}]},
        }

    (reports / "eval-spider.json").write_text(json.dumps(_report("spider-car-1", "how many car makers")))
    (reports / "eval-imdb.json").write_text(json.dumps(_report("imdb-movies", "how many films")))
    # Finetune pairs take a different code path: kg_name is derived from the
    # graph IRI, so it needs its own gate.
    (reports / "finetune_pairs.jsonl").write_text(
        json.dumps({"question": "how many stadiums", "sparql": "SELECT ?x", "graph_uri": GRAPH.format(kg="spider-concert-singer")})
        + "\n"
        + json.dumps({"question": "how many events", "sparql": "SELECT ?x", "graph_uri": GRAPH.format(kg="events-sf")})
        + "\n"
    )

    bank = ExampleBank(openrouter_api_key="unused", bank_path=tmp_path / "bank.jsonl")

    async def _fake_embed(texts):
        return [[0.0] * 1536 for _ in texts]

    bank._embed_texts = _fake_embed  # type: ignore[method-assign]

    await bank.populate_from_eval_reports(reports)
    assert sorted(ex.kg_name for ex in bank._examples) == ["events-sf", "imdb-movies"]


# ── Layer 3: the read path ───────────────────────────────────────────────


def test_load_filters_a_bank_file_that_already_has_benchmark_rows(tmp_path):
    """The write gates cannot clean a file already on disk. This is what does."""
    path = tmp_path / "bank.jsonl"
    _write_bank(
        path,
        [
            _row("spider-world-1", "how many countries"),
            _row("imdb-movies", "how many films"),
            _row("spider-flight-2", "how many flights"),
        ],
    )

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 1
    assert [ex.kg_name for ex in bank._examples] == ["imdb-movies"]


def test_load_leaves_a_clean_bank_untouched(tmp_path):
    path = tmp_path / "bank.jsonl"
    rows = [_row("imdb-movies", "how many films"), _row("events-sf", "how many events")]
    _write_bank(path, rows)

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 2
    assert [ex.sparql for ex in bank._examples] == [r["sparql"] for r in rows]


def test_load_still_survives_a_malformed_line(tmp_path):
    """The benchmark filter must not swallow the pre-existing malformed-line path."""
    path = tmp_path / "bank.jsonl"
    path.write_text("{not json\n" + json.dumps(_row("imdb-movies", "how many films")) + "\n")

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 1


def test_load_survives_a_null_kg_name(tmp_path):
    """The new filter runs outside the malformed-line except; it must not raise."""
    path = tmp_path / "bank.jsonl"
    null_kg = _row("imdb-movies", "how many films")
    null_kg["kg_name"] = None
    _write_bank(path, [null_kg, _row("events-sf", "how many events")])

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 2


# ── The eval rebuild must not truncate the bank to nothing ───────────────


async def test_an_all_benchmark_rebuild_batch_adds_nothing(tmp_path):
    """The trigger condition for the guard below.

    Now that add_batch drops benchmark KGs, a benchmark-tenant eval run offers
    the rebuild a batch from which nothing survives filtering.
    """
    bank = ExampleBank(openrouter_api_key="unused", bank_path=tmp_path / "bank.jsonl")

    async def _boom(_texts):
        raise AssertionError("nothing survived filtering; must not embed")

    bank._embed_texts = _boom  # type: ignore[method-assign]

    added = await bank.add_batch(
        [
            {"question": "how many singers", "sparql": "SELECT ?x", "kg_name": "spider-concert-singer", "ontology_context": ""},
            {"question": "how many countries", "sparql": "SELECT ?x", "kg_name": "spider-world-1", "ontology_context": ""},
        ]
    )
    assert added == 0
    assert bank.size == 0


async def test_an_all_benchmark_rebuild_leaves_the_committed_bank_alone(tmp_path):
    """...and that empty result must not be written over the committed bank.

    This was a source-level guard (``"if rebuilt:" in block``) because
    ``run_full_eval`` needs a live API, an ingested graph store and a provider
    key. The follow-up lifted the rebuild into ``eval.rebuild_example_bank``, so the
    real path is reachable and the guarantee is asserted directly. Note the
    rebuild now ``load()``s first, so a stray ``save()`` would no longer zero
    the file -- but it would still rewrite it for no reason, and the
    load-then-merge property has its own tests in
    ``test_example_bank_rebuild_merge.py``.
    """
    from infona_client.eval import rebuild_example_bank

    bank_path = tmp_path / "bank.jsonl"
    _write_bank(bank_path, [_row("imdb-movies", "how many films"), _row("events-sf", "how many events")])
    before = bank_path.read_text()

    pairs = tmp_path / "finetune_pairs.jsonl"
    pairs.write_text(
        "".join(
            json.dumps({"question": q, "sparql": "SELECT ?x", "graph_uri": GRAPH.format(kg=kg), "ontology": ""}) + "\n"
            for kg, q in [("spider-concert-singer", "how many singers"), ("spider-world-1", "how many countries")]
        )
    )

    bank = ExampleBank(openrouter_api_key="unused", bank_path=bank_path)

    async def _boom(_texts):
        raise AssertionError("nothing survived filtering; must not embed")

    bank._embed_texts = _boom  # type: ignore[method-assign]

    assert await rebuild_example_bank(pairs, bank=bank) == 0
    assert bank_path.read_text() == before
