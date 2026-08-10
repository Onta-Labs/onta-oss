"""The post-eval example-bank rebuild MERGES; it must never replace.

Follow-up to infona-oss#280, from an independent review of it.

``eval_reports/example_bank.jsonl`` is committed, shared state: it is the file in
git, and the file an OSS checkout / local dev / CI feeds into every ``/ask``
few-shot prompt. (Two things it is NOT: the copy the parent's Dockerfile bakes
into the image is the PARENT's own 507-entry file, which this rebuild writes
package-relative and so never reaches; and since infona-oss#261 the examples are
not injected verbatim -- ``format_examples_for_prompt`` rewrites each one's
``FROM`` to the caller's own target graph.) The rebuild that runs
after each eval used to build an ``ExampleBank``, never ``load()`` it, and
``save()`` it; since ``save()`` writes ``self._examples`` wholesale, the shared
bank was REPLACED by whatever ``eval_reports/finetune_pairs.jsonl`` held on the
machine that ran the eval -- a gitignored, machine-local file. A dev who
evaluated one KG and committed the result silently shrank the bank to their own
subset. That is the most plausible origin of the 148 Spider4SPARQL entries
(ONTA-449 / infona-oss#280), whose fix -- skip ``save()`` when ``add_batch``
accepted nothing -- stopped the truncate-to-zero case only: a run that accepted
12 pairs still overwrote 114 with 12.

Merge needs three things to hold, and each has a test below:

1. ``load()`` before ``add_batch``, so the rebuild adds to the committed bank
   rather than standing in for it.
2. A re-add must REFRESH, not drop. Identity was the question text alone, so a
   corrected SPARQL for a question already in the bank was discarded as a
   duplicate -- fresh data lost to stale. Without this, merge would also mean
   the rebuild permanently no-ops after its first run, because
   ``finetune_pairs.jsonl`` is append-only and re-offers every past pair.
3. Within one batch, LAST wins. ``finetune_pairs.jsonl`` is keyed on
   ``(question, graph_uri)``, so a namespace rename (``graph.infona.ai`` ->
   ``graph.infona.ai``, 2026-04-27) appends a SECOND pair for the same question
   instead of replacing the first. First-wins kept the stale one. That was the
   first of the three reasons ``ARCHITECTURE.md`` gave for why this loop could
   not heal a stale entry; the other two (the rebuild cannot reach the shipped
   bank, production never rebuilds) are untouched and still hold, which is why
   ``tests/test_example_bank_namespace.py`` and the parent's
   ``tests/test_shipped_example_bank_namespace.py`` are still needed.
"""

import json

import pytest

from infona_client.eval import rebuild_example_bank
from infona_client.nlp import example_bank as bank_mod
from infona_client.nlp.example_bank import ExampleBank

GRAPH = "https://graph.infona.ai/graphs/demo-tenant/kg/{kg}"
# Retired brand host used only as a *stale* graph_uri in rename-healing tests.
LEGACY_GRAPH = "https://cograph.tech/graphs/demo-tenant/kg/{kg}"


def _row(kg: str, question: str, sparql: str = "") -> dict:
    """A line as it appears in the committed bank JSONL."""
    return {
        "question": question,
        "sparql": sparql or f"SELECT ?x FROM <{GRAPH.format(kg=kg)}> WHERE {{ ?x a ?t }}",
        "kg_name": kg,
        "ontology_context": f"ontology of {kg}",
        "pattern_tags": [],
        "embedding": [0.5] * 1536,
    }


def _pair(kg: str, question: str, sparql: str = "", graph: str = GRAPH) -> dict:
    """A line as it appears in the machine-local finetune_pairs.jsonl."""
    return {
        "question": question,
        "ontology": f"ontology of {kg}",
        "graph_uri": graph.format(kg=kg),
        "sparql": sparql or f"SELECT ?x FROM <{graph.format(kg=kg)}> WHERE {{ ?x a ?t }}",
        "source": "eval",
    }


def _write_jsonl(path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _read_bank(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _spy_on_save(bank) -> list[int]:
    """Record each ``save()`` call (as the size written) and still perform it.

    Needed because a load/save round-trip of an already-clean bank is
    byte-identical, so comparing file contents cannot detect a save that should
    not have happened.
    """
    calls: list[int] = []
    real_save = bank.save

    def _save():
        calls.append(bank.size)
        real_save()

    bank.save = _save  # type: ignore[method-assign]
    return calls


@pytest.fixture
def make_bank(tmp_path):
    """An ExampleBank over a tmp file, with embedding calls counted, not made."""

    def _make(rows: list[dict] | None = None):
        path = tmp_path / "example_bank.jsonl"
        _write_jsonl(path, rows or [])
        bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
        embedded: list[str] = []

        async def _fake_embed(texts):
            embedded.extend(texts)
            return [[0.1] * 1536 for _ in texts]

        bank._embed_texts = _fake_embed  # type: ignore[method-assign]
        bank.embedded = embedded  # type: ignore[attr-defined]
        return bank

    return _make


# ── 1. The regression: merge, don't replace ──────────────────────────────


async def test_rebuild_merges_into_the_committed_bank(tmp_path, make_bank):
    """A dev who evaluated ONE KG must not shrink the bank to that one KG."""
    committed = [
        _row("imdb-movies", "how many films"),
        _row("events-sf", "how many events"),
        _row("exoplanets", "how many planets"),
    ]
    bank = make_bank(committed)
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(ft, [_pair("coffee-quality", "how many lots")])

    accepted = await rebuild_example_bank(ft, bank=bank)

    assert accepted == 1
    saved = _read_bank(bank._bank_path)
    assert [r["kg_name"] for r in saved] == [
        "imdb-movies", "events-sf", "exoplanets", "coffee-quality",
    ], "the rebuild replaced the committed bank instead of merging into it"
    # The pre-existing rows survive byte-for-byte, embeddings included.
    assert saved[:3] == committed


async def test_rebuild_does_not_re_embed_what_is_already_in_the_bank(tmp_path, make_bank):
    """Merging must cost one embedding per NEW example, not per pair on disk."""
    bank = make_bank([_row("imdb-movies", "how many films")])
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(ft, [_pair("imdb-movies", "how many films"), _pair("events-sf", "how many events")])

    await rebuild_example_bank(ft, bank=bank)

    assert bank.embedded == ["how many events"]


# ── 2. A re-add refreshes ────────────────────────────────────────────────


async def test_rebuild_refreshes_a_stale_answer_for_the_same_kg(tmp_path, make_bank):
    """A corrected SPARQL must land, not be dropped as a duplicate question."""
    stale = _row("imdb-movies", "how many films", sparql="SELECT ?x WHERE { ?x a <stale> }")
    bank = make_bank([stale])
    fresh_sparql = "SELECT (COUNT(?x) AS ?n) WHERE { ?x a <fresh> }"
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(ft, [_pair("imdb-movies", "how many films", sparql=fresh_sparql)])

    accepted = await rebuild_example_bank(ft, bank=bank)

    assert accepted == 1
    saved = _read_bank(bank._bank_path)
    assert len(saved) == 1, "a refresh must update in place, not append a twin"
    assert saved[0]["sparql"] == fresh_sparql
    assert "count" in saved[0]["pattern_tags"], "pattern tags must be re-detected"
    # The question is unchanged by definition, so its embedding is still valid.
    assert saved[0]["embedding"] == stale["embedding"]
    assert bank.embedded == []


async def test_rebuild_keeps_the_same_question_asked_of_a_different_kg(tmp_path, make_bank):
    """Identity is (question, kg): the same wording is a different example.

    Keying on question alone made the bank's contents depend on which KG the
    eval happened to reach first.
    """
    bank = make_bank([_row("imdb-movies", "how many rows")])
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(ft, [_pair("events-sf", "how many rows")])

    accepted = await rebuild_example_bank(ft, bank=bank)

    assert accepted == 1
    assert [r["kg_name"] for r in _read_bank(bank._bank_path)] == ["imdb-movies", "events-sf"]


async def test_rebuild_is_idempotent(tmp_path, make_bank):
    """Re-running with unchanged pairs must not rewrite the file.

    ``finetune_pairs.jsonl`` is append-only, so every rebuild re-offers every
    past pair. Refreshing on identical content would produce a no-op diff on
    the committed bank after every eval run.
    """
    bank = make_bank([_row("imdb-movies", "how many films")])
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(ft, [_pair("imdb-movies", "how many films"), _pair("events-sf", "how many events")])

    assert await rebuild_example_bank(ft, bank=bank) == 1
    first = bank._bank_path.read_text()

    bank2 = ExampleBank(openrouter_api_key="unused", bank_path=bank._bank_path)

    async def _boom(_texts):
        raise AssertionError("nothing new; must not embed")

    bank2._embed_texts = _boom  # type: ignore[method-assign]
    saves = _spy_on_save(bank2)

    assert await rebuild_example_bank(ft, bank=bank2) == 0
    assert saves == [], "nothing changed; the second run must not rewrite the file"
    assert bank._bank_path.read_text() == first


# ── 3. Last write wins within a batch ────────────────────────────────────


async def test_rebuild_prefers_the_later_pair_when_the_graph_iri_changed(tmp_path, make_bank):
    """The namespace-rename case: two pairs, same question, fresher one last.

    ``finetune_pairs.jsonl`` upserts on ``(question, graph_uri)`` and the graph
    IRI carries the namespace, so a post-rename run APPENDS rather than
    replaces. First-wins kept the pre-rename answer forever.
    """
    bank = make_bank([])
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(
        ft,
        [
            _pair("imdb-movies", "how many films", sparql="SELECT ?x WHERE { ?x <https://cograph.tech/onto/a> ?y }", graph=LEGACY_GRAPH),
            _pair("imdb-movies", "how many films", sparql="SELECT ?x WHERE { ?x <https://graph.infona.ai/onto/a> ?y }"),
        ],
    )

    await rebuild_example_bank(ft, bank=bank)

    saved = _read_bank(bank._bank_path)
    assert len(saved) == 1
    assert "graph.infona.ai" in saved[0]["sparql"]
    assert "cograph.tech" not in saved[0]["sparql"]
    # One embedding call: the pair was collapsed before embedding, not after.
    assert bank.embedded == ["how many films"]


# ── The ONTA-449 guarantees, now checked on the real path ────────────────


async def test_rebuild_leaves_the_bank_untouched_when_nothing_is_accepted(tmp_path, make_bank):
    """infona-oss#280's guarantee, re-asserted behaviourally.

    It was previously pinned by a source-level ``"if rebuilt:" in block``
    assertion because ``run_full_eval`` needs a live API and graph store. The
    rebuild is its own function now, so the real path is reachable.

    Asserts that ``save()`` is not CALLED, not merely that the bytes are equal.
    A load/save round-trip of a clean bank is byte-identical -- the fixture rows
    are already in ``Example.to_dict()`` order -- so a content comparison alone
    cannot tell "skipped the write" from "rewrote the same bytes", and an
    unconditional ``save()`` passed it. (Found by independent review.)
    """
    committed = [_row("imdb-movies", "how many films"), _row("events-sf", "how many events")]
    bank = make_bank(committed)
    before = bank._bank_path.read_text()
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(ft, [_pair("spider-concert-singer", "how many singers"), _pair("spider-world-1", "how many countries")])

    saves = _spy_on_save(bank)

    assert await rebuild_example_bank(ft, bank=bank) == 0
    assert saves == [], "nothing was accepted; the committed bank must not be rewritten at all"
    assert bank._bank_path.read_text() == before


async def test_rebuild_purges_benchmark_rows_already_on_disk(tmp_path, make_bank):
    """``load()`` filters them on read; saving is what removes them for good.

    Before, the rebuild wrote a bank it had never read, so it could not clean
    one. This is the self-heal the ONTA-449 notes said the loop did not have.
    """
    bank = make_bank([
        _row("spider-world-1", "how many countries"),
        _row("imdb-movies", "how many films"),
        _row("spider-flight-2", "how many flights"),
    ])
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(ft, [_pair("spider-car-1", "how many car makers")])

    assert await rebuild_example_bank(ft, bank=bank) == 0
    assert [r["kg_name"] for r in _read_bank(bank._bank_path)] == ["imdb-movies"]


async def test_rebuild_survives_a_missing_or_malformed_pairs_file(tmp_path, make_bank):
    bank = make_bank([_row("imdb-movies", "how many films")])
    before = bank._bank_path.read_text()

    assert await rebuild_example_bank(tmp_path / "nope.jsonl", bank=bank) == 0
    assert bank._bank_path.read_text() == before

    ft = tmp_path / "finetune_pairs.jsonl"
    ft.write_text("{not json\n" + json.dumps({"question": "q"}) + "\n" + json.dumps(_pair("events-sf", "how many events")) + "\n")

    assert await rebuild_example_bank(ft, bank=bank) == 1
    assert [r["kg_name"] for r in _read_bank(bank._bank_path)] == ["imdb-movies", "events-sf"]


# ── Capacity: a full bank must still be correctable ──────────────────────


async def test_a_full_bank_still_accepts_refreshes(tmp_path, make_bank, monkeypatch):
    """A refresh cannot grow the bank, so MAX_BANK_SIZE must not block it.

    Merge makes this reachable in a way regenerate never was: the rebuild now
    starts from a bank that may already be at the cap. The parent repo's shipped
    copy carries 507 entries against a 500 cap, and ``load()`` does not trim.
    """
    monkeypatch.setattr(bank_mod, "MAX_BANK_SIZE", 2)
    bank = make_bank([_row("imdb-movies", "how many films"), _row("events-sf", "how many events")])
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(
        ft,
        [
            _pair("imdb-movies", "how many films", sparql="SELECT ?fresh WHERE { ?x a ?t }"),
            _pair("coffee-quality", "how many lots"),
        ],
    )

    assert await rebuild_example_bank(ft, bank=bank) == 1

    saved = _read_bank(bank._bank_path)
    assert [r["kg_name"] for r in saved] == ["imdb-movies", "events-sf"], "cap must still hold for NEW examples"
    assert saved[0]["sparql"] == "SELECT ?fresh WHERE { ?x a ?t }"


async def test_rebuild_is_idempotent_when_one_key_has_two_pairs(tmp_path, make_bank):
    """The idempotence test above, on the fixture last-wins exists for.

    A namespace rename leaves TWO pairs for one question in
    ``finetune_pairs.jsonl`` permanently: that file keys on
    ``(question, graph_uri)`` and the IRIs differ, while the rebuild truncates
    both to the same ``kg_name``. Refreshing eagerly per item flipped the stored
    example old -> new -> old within a single batch, so BOTH writes reported
    "changed" and every later eval re-saved a byte-identical bank while logging
    a bogus accepted count. Collapsing the batch by key first is what fixes it.
    (Found by independent review.)
    """
    bank = make_bank([])
    ft = tmp_path / "finetune_pairs.jsonl"
    _write_jsonl(
        ft,
        [
            _pair("imdb-movies", "how many films", sparql="SELECT ?stale", graph=LEGACY_GRAPH),
            _pair("imdb-movies", "how many films", sparql="SELECT ?fresh"),
        ],
    )

    assert await rebuild_example_bank(ft, bank=bank) == 1
    first = bank._bank_path.read_text()

    # A second bank over the SAME file — not make_bank(), which would rewrite it.
    bank2 = ExampleBank(openrouter_api_key="unused", bank_path=bank._bank_path)

    async def _boom(_texts):
        raise AssertionError("nothing new; must not embed")

    bank2._embed_texts = _boom  # type: ignore[method-assign]
    saves = _spy_on_save(bank2)

    assert await rebuild_example_bank(ft, bank=bank2) == 0, "the same two pairs changed nothing"
    assert saves == []
    assert bank._bank_path.read_text() == first
    assert "SELECT ?fresh" in first and "SELECT ?stale" not in first


# ── save() must not be able to destroy the bank it is rewriting ──────────


def test_a_failed_save_leaves_the_previous_bank_intact(tmp_path, monkeypatch, make_bank):
    """``open(path, "w")`` truncates before the first byte lands.

    This file is committed to git and is now rewritten routinely, so a save that
    dies partway must not be able to leave a truncated bank -- losing it is the
    exact thing this PR exists to prevent. save() writes a temp file and
    os.replace()s it. (Found by independent review.)
    """
    bank = make_bank([_row("imdb-movies", "how many films"), _row("events-sf", "how many events")])
    bank.load()
    before = bank._bank_path.read_text()

    real_dumps = bank_mod.json.dumps
    calls = {"n": 0}

    def _explode(obj, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk went away mid-save")
        return real_dumps(obj, *a, **kw)

    monkeypatch.setattr(bank_mod.json, "dumps", _explode)

    with pytest.raises(RuntimeError):
        bank.save()

    assert bank._bank_path.read_text() == before, "a failed save truncated the committed bank"
    assert list(tmp_path.glob("*.tmp")) == [], "the temp file must be cleaned up"


def test_save_clears_the_pending_benchmark_purge(tmp_path, make_bank):
    """Otherwise a second save-gated cycle on the same instance fires blind."""
    bank = make_bank([_row("spider-world-1", "how many countries"), _row("imdb-movies", "how many films")])

    assert bank.load() == 1
    assert bank.skipped_benchmark_on_load == 1
    bank.save()
    assert bank.skipped_benchmark_on_load == 0


# ── The same semantics on the singular add() ─────────────────────────────
#
# add() has no production caller today (only the class docstring's example), but
# its docstring makes the same three promises add_batch does, and an independent
# review found every one of them survived a mutation that reverted the method
# wholesale to question-only drop. A documented public API that no test pins is
# a trap for the next caller.


async def test_add_refreshes_an_existing_example_for_the_same_kg(make_bank):
    bank = make_bank([_row("imdb-movies", "how many films", sparql="SELECT ?x WHERE { ?x a <stale> }")])
    bank.load()

    changed = await bank.add("how many films", "SELECT ?x WHERE { ?x a <fresh> }", "imdb-movies", "ctx")

    assert changed is True
    assert bank.size == 1, "a refresh must not append a twin"
    assert bank._examples[0].sparql == "SELECT ?x WHERE { ?x a <fresh> }"
    assert bank.embedded == [], "the question is unchanged; no re-embedding"


async def test_add_reports_no_change_for_an_identical_re_add(make_bank):
    bank = make_bank([_row("imdb-movies", "how many films")])
    bank.load()
    same = bank._examples[0].sparql

    assert await bank.add("How Many Films", same, "imdb-movies", "ontology of imdb-movies") is False
    assert bank.size == 1


async def test_add_keeps_the_same_question_asked_of_a_different_kg(make_bank):
    bank = make_bank([_row("imdb-movies", "how many rows")])
    bank.load()

    assert await bank.add("how many rows", "SELECT ?x", "events-sf", "ctx") is True
    assert [ex.kg_name for ex in bank._examples] == ["imdb-movies", "events-sf"]


async def test_add_rejects_a_new_example_at_capacity_but_still_refreshes(make_bank, monkeypatch):
    monkeypatch.setattr(bank_mod, "MAX_BANK_SIZE", 1)
    bank = make_bank([_row("imdb-movies", "how many films")])
    bank.load()

    assert await bank.add("how many events", "SELECT ?x", "events-sf", "ctx") is False
    assert bank.size == 1

    assert await bank.add("how many films", "SELECT ?fresh", "imdb-movies", "ctx") is True
    assert bank._examples[0].sparql == "SELECT ?fresh"


# ── populate_from_eval_reports carries the same save gate ────────────────


async def test_populate_keeps_the_same_question_from_two_different_kgs(tmp_path, make_bank):
    """The other ingestion path must use the same identity as add_batch.

    It deduped on question alone, dropping the second KG's example BEFORE
    add_batch could apply the (question, kg_name) identity -- so the winner was
    whichever KG ``sorted(glob("eval-*.json"))`` reached first. (Found by
    independent review.)
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    for kg in ("events-sf", "imdb-movies"):
        (reports / f"eval-{kg}.json").write_text(json.dumps({
            "kg_name": kg,
            "ontology": f"ontology of {kg}",
            "queries": {"results": [
                {"question": "how many rows", "sparql": f"SELECT ?x # {kg}", "verdict": "correct"},
            ]},
        }))

    bank = make_bank([])
    assert await bank.populate_from_eval_reports(reports) == 2
    assert sorted(ex.kg_name for ex in bank._examples) == ["events-sf", "imdb-movies"]


async def test_populate_does_not_rewrite_the_bank_when_nothing_is_accepted(tmp_path, make_bank):
    """It had the same unconditional save() the rebuild did.

    The report must offer a REAL example that happens to be a no-op re-add:
    an all-benchmark report leaves ``items`` empty and returns before reaching
    the gate at all, so it would pass this test either way.
    """
    committed = _row("imdb-movies", "how many films")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "eval-imdb.json").write_text(json.dumps({
        "kg_name": "imdb-movies",
        "ontology": committed["ontology_context"],
        "queries": {"results": [
            {"question": "how many films", "sparql": committed["sparql"], "verdict": "correct"},
        ]},
    }))

    bank = make_bank([committed])
    bank.load()
    saves = _spy_on_save(bank)

    assert await bank.populate_from_eval_reports(reports) == 0
    assert saves == [], "nothing accepted; the bank on disk must not be rewritten"
    assert [r["kg_name"] for r in _read_bank(bank._bank_path)] == ["imdb-movies"]


# ── The wiring ───────────────────────────────────────────────────────────


def test_run_full_eval_still_calls_the_rebuild():
    """The helper is only worth testing if the eval still routes through it."""
    import inspect

    from infona_client import eval as eval_mod

    src = inspect.getsource(eval_mod.run_full_eval)
    assert "await rebuild_example_bank(ft_path)" in src, (
        "run_full_eval no longer delegates its example-bank rebuild to "
        "rebuild_example_bank(); the merge semantics tested in this file only "
        "protect the committed bank if the eval actually goes through them."
    )
