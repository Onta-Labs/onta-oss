"""INF-567: Blueprint questions must not leak via the process-scoped bank.

The bank is one JSONL; ``Example`` used to carry ``kg_name`` but no tenant.
That is the ONTA-449 hole: a row stored during one tenant's work is few-shot
injected into every later ``/ask``. A Blueprint ships ``evals[]`` and
supported questions. If those land unscoped, one publisher's questions appear
in unrelated tenants' prompts — and ``format_examples_for_prompt`` rewrites
``FROM`` to the caller's graph, so the leak looks legitimate.

Decision: admit Blueprint examples with a ``tenant_id``. Refuse them at
``add`` / ``add_batch`` / ``populate_from_eval_reports`` / ``load`` when
that tenant is missing (same four-layer shape as the spider- prefix block).
Retrieval is fail-closed: a scoped row is visible only to the matching
caller. This still lets a Blueprint's questions work inside the installing
tenant.

Install/fork itself is out of scope — these tests are the contract that
path will call.
"""

import json

import pytest

from infona_client.nlp.example_bank import (
    BLUEPRINT_ORIGIN,
    DEFAULT_BANK_PATH,
    Example,
    ExampleBank,
    example_matches_kg_purge,
    example_visible_to_tenant,
    format_examples_for_prompt,
    is_blueprint_origin,
    is_unscoped_blueprint_example,
)

GRAPH = "https://graph.infona.ai/graphs/{tenant}/kg/{kg}"
PUBLISHER_Q = "Which Phase 3 obesity trials are currently recruiting?"
PUBLISHER_SPARQL = (
    "SELECT ?nct FROM <https://graph.infona.ai/graphs/publisher-ws/kg/clinical-trials> "
    "WHERE { ?t a ?type }"
)


def _row(
    kg: str,
    question: str,
    *,
    tenant_id: str = "",
    origin: str = "",
    sparql: str | None = None,
) -> dict:
    tenant = tenant_id or "demo-tenant"
    return {
        "question": question,
        "sparql": sparql
        or f"SELECT ?x FROM <{GRAPH.format(tenant=tenant, kg=kg)}> WHERE {{ ?x a ?t }}",
        "kg_name": kg,
        "ontology_context": "",
        "pattern_tags": [],
        "embedding": [0.0] * 1536,
        **({"tenant_id": tenant_id} if tenant_id else {}),
        **({"origin": origin} if origin else {}),
    }


def _write_bank(path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _bank(tmp_path) -> ExampleBank:
    return ExampleBank(openrouter_api_key="unused", bank_path=tmp_path / "bank.jsonl")


async def _no_embed(_texts):
    raise AssertionError("must refuse before spending an embedding call")


async def _fake_embed(texts):
    return [[0.0] * 1536 for _ in texts]


# ── The predicates ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "origin,tenant_id,blueprint_id,expect",
    [
        ("blueprint", "", None, True),
        ("BLUEPRINT", "", None, True),
        ("blueprint", "   ", None, True),
        (None, None, "infona/clinical-trials", True),
        ("blueprint", "acme-ws", None, False),
        ("", "", None, False),
        ("eval", "", None, False),
        (None, None, None, False),
    ],
)
def test_unscoped_blueprint_predicate(origin, tenant_id, blueprint_id, expect):
    assert (
        is_unscoped_blueprint_example(
            origin, tenant_id, blueprint_id=blueprint_id
        )
        is expect
    )


def test_blueprint_id_implies_origin():
    assert is_blueprint_origin(blueprint_id="infona/clinical-trials")
    assert not is_blueprint_origin("")
    assert not is_blueprint_origin(None)


@pytest.mark.parametrize(
    "example_tenant,caller,expect",
    [
        ("", "acme-ws", True),  # shared bank
        ("", "", True),
        ("acme-ws", "acme-ws", True),
        ("acme-ws", "other-ws", False),
        ("acme-ws", "", False),  # fail-closed
        ("acme-ws", None, False),
        (None, "acme-ws", True),
    ],
)
def test_visibility_is_fail_closed_for_scoped_rows(example_tenant, caller, expect):
    assert example_visible_to_tenant(example_tenant, caller) is expect


def test_null_isolation_fields_do_not_raise():
    """``load()`` calls these outside the malformed-line except."""
    assert is_unscoped_blueprint_example(None, None) is False
    assert example_visible_to_tenant(None, None) is True


# ── Layer 1: the committed data ──────────────────────────────────────────


def test_committed_bank_has_no_blueprint_examples():
    """The shipped OSS bank is the shared open-data set, not an install."""
    assert DEFAULT_BANK_PATH.exists(), f"example bank missing at {DEFAULT_BANK_PATH}"
    offenders = []
    for line in DEFAULT_BANK_PATH.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        origin = rec.get("origin") or ""
        if rec.get("blueprint_id") or is_blueprint_origin(origin):
            offenders.append(rec.get("question", "")[:80])
        if (rec.get("tenant_id") or "").strip():
            offenders.append(rec.get("question", "")[:80])
    assert not offenders, (
        f"{DEFAULT_BANK_PATH} contains Blueprint or tenant-scoped examples "
        f"{offenders}. The package bank is injected into every process; "
        "tenant-specific Blueprint questions belong in the installing "
        "workspace, not the image (INF-567)."
    )


# ── Layer 2: the write path ──────────────────────────────────────────────


async def test_add_refuses_an_unscoped_blueprint_example(tmp_path):
    bank = _bank(tmp_path)
    bank._embed_texts = _no_embed  # type: ignore[method-assign]

    assert (
        await bank.add(
            PUBLISHER_Q,
            PUBLISHER_SPARQL,
            "clinical-trials",
            "",
            origin=BLUEPRINT_ORIGIN,
        )
        is False
    )
    assert bank.size == 0


async def test_add_accepts_a_scoped_blueprint_example(tmp_path):
    bank = _bank(tmp_path)
    bank._embed_texts = _fake_embed  # type: ignore[method-assign]

    assert (
        await bank.add(
            PUBLISHER_Q,
            PUBLISHER_SPARQL,
            "clinical-trials",
            "",
            tenant_id="acme-ws",
            origin=BLUEPRINT_ORIGIN,
        )
        is True
    )
    assert bank.size == 1
    assert bank._examples[0].tenant_id == "acme-ws"
    assert bank._examples[0].origin == BLUEPRINT_ORIGIN


async def test_add_does_not_collide_across_tenants(tmp_path):
    """Same Blueprint question in two workspaces is two rows, not a refresh."""
    bank = _bank(tmp_path)
    bank._embed_texts = _fake_embed  # type: ignore[method-assign]

    await bank.add(
        PUBLISHER_Q, "SELECT ?a", "clinical-trials", "",
        tenant_id="acme-ws", origin=BLUEPRINT_ORIGIN,
    )
    await bank.add(
        PUBLISHER_Q, "SELECT ?b", "clinical-trials", "",
        tenant_id="other-ws", origin=BLUEPRINT_ORIGIN,
    )
    assert bank.size == 2
    by_tenant = {ex.tenant_id: ex.sparql for ex in bank._examples}
    assert by_tenant == {"acme-ws": "SELECT ?a", "other-ws": "SELECT ?b"}


async def test_add_batch_drops_unscoped_blueprint_and_keeps_the_rest(tmp_path):
    bank = _bank(tmp_path)
    bank._embed_texts = _fake_embed  # type: ignore[method-assign]

    added = await bank.add_batch(
        [
            {
                "question": PUBLISHER_Q,
                "sparql": "SELECT ?x",
                "kg_name": "clinical-trials",
                "ontology_context": "",
                "origin": BLUEPRINT_ORIGIN,
            },
            {
                "question": "how many films",
                "sparql": "SELECT ?x",
                "kg_name": "imdb-movies",
                "ontology_context": "",
            },
            {
                "question": PUBLISHER_Q,
                "sparql": "SELECT ?y",
                "kg_name": "clinical-trials",
                "ontology_context": "",
                "origin": BLUEPRINT_ORIGIN,
                "tenant_id": "acme-ws",
            },
        ]
    )
    assert added == 2
    assert sorted((ex.kg_name, ex.tenant_id) for ex in bank._examples) == [
        ("clinical-trials", "acme-ws"),
        ("imdb-movies", ""),
    ]


async def test_add_batch_treats_blueprint_id_as_origin(tmp_path):
    bank = _bank(tmp_path)
    bank._embed_texts = _no_embed  # type: ignore[method-assign]

    added = await bank.add_batch(
        [
            {
                "question": PUBLISHER_Q,
                "sparql": "SELECT ?x",
                "kg_name": "clinical-trials",
                "ontology_context": "",
                "blueprint_id": "infona/clinical-trials",
            }
        ]
    )
    assert added == 0
    assert bank.size == 0


async def test_populate_from_eval_reports_skips_unscoped_blueprint(tmp_path):
    reports = tmp_path / "eval_reports"
    reports.mkdir()

    def _report(kg: str, question: str, **extra) -> dict:
        return {
            "kg_name": kg,
            "ontology": "",
            "queries": {
                "results": [
                    {"question": question, "sparql": "SELECT ?x", "verdict": "correct"}
                ]
            },
            **extra,
        }

    (reports / "eval-blueprint.json").write_text(
        json.dumps(_report("clinical-trials", PUBLISHER_Q, origin="blueprint"))
    )
    (reports / "eval-imdb.json").write_text(
        json.dumps(_report("imdb-movies", "how many films"))
    )
    (reports / "finetune_pairs.jsonl").write_text(
        json.dumps(
            {
                "question": "how many stadiums",
                "sparql": "SELECT ?x",
                "graph_uri": GRAPH.format(tenant="publisher-ws", kg="clinical-trials"),
                "origin": "blueprint",
            }
        )
        + "\n"
        + json.dumps(
            {
                "question": PUBLISHER_Q,
                "sparql": "SELECT ?y",
                "graph_uri": GRAPH.format(tenant="acme-ws", kg="clinical-trials"),
                "origin": "blueprint",
                "tenant_id": "acme-ws",
            }
        )
        + "\n"
        + json.dumps(
            {
                "question": "how many events",
                "sparql": "SELECT ?x",
                "graph_uri": GRAPH.format(tenant="demo-tenant", kg="events-sf"),
            }
        )
        + "\n"
    )

    bank = ExampleBank(openrouter_api_key="unused", bank_path=tmp_path / "bank.jsonl")
    bank._embed_texts = _fake_embed  # type: ignore[method-assign]

    await bank.populate_from_eval_reports(reports)
    got = sorted((ex.kg_name, ex.tenant_id, ex.origin) for ex in bank._examples)
    assert got == [
        ("clinical-trials", "acme-ws", "blueprint"),
        ("events-sf", "", ""),
        ("imdb-movies", "", ""),
    ]


# ── Layer 3: the read path ───────────────────────────────────────────────


def test_load_filters_unscoped_blueprint_rows(tmp_path):
    path = tmp_path / "bank.jsonl"
    _write_bank(
        path,
        [
            _row("clinical-trials", PUBLISHER_Q, origin="blueprint"),
            _row("imdb-movies", "how many films"),
            _row(
                "clinical-trials",
                PUBLISHER_Q,
                tenant_id="acme-ws",
                origin="blueprint",
            ),
        ],
    )

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 2
    assert bank.skipped_unscoped_blueprint_on_load == 1
    assert sorted((ex.kg_name, ex.tenant_id) for ex in bank._examples) == [
        ("clinical-trials", "acme-ws"),
        ("imdb-movies", ""),
    ]


def test_load_leaves_a_clean_bank_untouched(tmp_path):
    path = tmp_path / "bank.jsonl"
    rows = [_row("imdb-movies", "how many films"), _row("events-sf", "how many events")]
    _write_bank(path, rows)

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 2
    assert bank.skipped_unscoped_blueprint_on_load == 0


def test_load_survives_null_isolation_fields(tmp_path):
    path = tmp_path / "bank.jsonl"
    nulls = _row("imdb-movies", "how many films")
    nulls["origin"] = None
    nulls["tenant_id"] = None
    _write_bank(path, [nulls, _row("events-sf", "how many events")])

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 2


# ── Retrieval: the done-when test ────────────────────────────────────────


async def test_blueprint_questions_cannot_reach_another_tenant(tmp_path):
    """A publisher's Blueprint question must not enter another tenant's pool.

    This is the ticket's acceptance: modelled on the ONTA-449 retrieve-shaped
    leak, plus the ONTA-420 rewrite that would make the leak look legitimate.
    """
    path = tmp_path / "bank.jsonl"
    publisher = _row(
        "clinical-trials",
        PUBLISHER_Q,
        tenant_id="publisher-ws",
        origin="blueprint",
        sparql=PUBLISHER_SPARQL,
    )
    shared = _row("imdb-movies", "how many films")
    installer = _row(
        "clinical-trials",
        "Who is the lead sponsor of this NCT?",
        tenant_id="acme-ws",
        origin="blueprint",
    )
    _write_bank(path, [publisher, shared, installer])

    bank = ExampleBank(openrouter_api_key="unused", bank_path=path)
    assert bank.load() == 3
    bank._embed_texts = _fake_embed  # type: ignore[method-assign]

    other = await bank.retrieve(
        PUBLISHER_Q, tenant_id="other-ws", top_k=10, language="sparql"
    )
    assert PUBLISHER_Q not in {ex.question for ex in other}
    assert "publisher-ws" not in {ex.tenant_id for ex in other}
    assert {ex.kg_name for ex in other} == {"imdb-movies"}

    installer_hit = await bank.retrieve(
        "Who is the lead sponsor of this NCT?",
        tenant_id="acme-ws",
        top_k=10,
        language="sparql",
    )
    questions = {ex.question for ex in installer_hit}
    assert "Who is the lead sponsor of this NCT?" in questions
    assert PUBLISHER_Q not in questions
    assert {ex.kg_name for ex in installer_hit} <= {"clinical-trials", "imdb-movies"}

    forgotten = await bank.retrieve(PUBLISHER_Q, top_k=10, language="sparql")
    assert all(not (ex.tenant_id or "").strip() for ex in forgotten)
    assert PUBLISHER_Q not in {ex.question for ex in forgotten}


async def test_leaked_blueprint_example_would_look_legitimate():
    """Pin why retrieve-time isolation is load-bearing, not format-time.

    If a publisher row ever reached ``format_examples_for_prompt``, the
    ``FROM`` rewrite would stamp the caller's graph onto it. The filter
    above is what stops that; this test keeps the rewrite behaviour so a
    later 'fix' cannot drop isolation and rely on an obviously-foreign
    graph IRI as the defence.
    """
    caller_graph = GRAPH.format(tenant="other-ws", kg="clinical-trials")
    text = format_examples_for_prompt(
        [
            Example(
                question=PUBLISHER_Q,
                sparql=PUBLISHER_SPARQL,
                kg_name="clinical-trials",
                tenant_id="publisher-ws",
                origin=BLUEPRINT_ORIGIN,
            )
        ],
        target_graph_uri=caller_graph,
    )
    assert caller_graph in text
    assert "publisher-ws" not in text


def test_kg_delete_does_not_purge_another_tenant_blueprint_row():
    publisher = Example(
        question=PUBLISHER_Q,
        sparql="SELECT ?x",
        kg_name="clinical-trials",
        tenant_id="publisher-ws",
        origin=BLUEPRINT_ORIGIN,
    )
    installer = Example(
        question=PUBLISHER_Q,
        sparql="SELECT ?y",
        kg_name="clinical-trials",
        tenant_id="acme-ws",
        origin=BLUEPRINT_ORIGIN,
    )
    shared = Example(
        question="how many films",
        sparql="SELECT ?x",
        kg_name="imdb-movies",
    )
    assert example_matches_kg_purge(
        publisher, tenant_id="acme-ws", kg_name="clinical-trials"
    ) is False
    assert example_matches_kg_purge(
        installer, tenant_id="acme-ws", kg_name="clinical-trials"
    ) is True
    assert example_matches_kg_purge(
        shared, tenant_id="acme-ws", kg_name="imdb-movies"
    ) is True
