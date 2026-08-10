"""ONTA-420: the few-shot example bank must not leak one tenant's graph IRIs
into another tenant's SPARQL prompt.

The bank is scoped per PROCESS: ``DEFAULT_BANK_PATH`` is a single JSONL file and
``Example`` carries a ``kg_name`` but no tenant. The shipped bank is 262
examples across 12 KGs, all ``demo-tenant``. Before this change
``format_examples_for_prompt`` did nothing but collapse whitespace, so those
``FROM <https://graph.onta.sh/graphs/demo-tenant/kg/...>`` clauses went verbatim
into every self-hosted and third-party tenant's prompt. The only defense was a
prose line in the system prompt.

The fix SANITIZES rather than filters: retrieval is untouched (all 262 examples
stay available to every tenant, which is the cross-domain pattern transfer the
bank exists for), but the graph IRI, the only tenant-identifying token in a
stored example, is rewritten to the caller's own target graph at format time.

Type and attribute IRIs are deliberately NOT abstracted. They teach the URI
shapes the generator must produce, they name public open-data schemas (IMDB,
CFPB, exoplanets, ...) rather than customer data, and placeholdering them would
destroy the pattern-transfer value with no privacy gain. These tests pin that
decision so it stays a choice rather than an oversight.
"""

import json
import re

import pytest

from infona_client.nlp.example_bank import (
    DEFAULT_BANK_PATH,
    TARGET_GRAPH_PLACEHOLDER,
    Example,
    format_examples_for_prompt,
    sanitize_example_sparql,
)

FOREIGN_GRAPH = "https://graph.onta.sh/graphs/demo-tenant/kg/imdb-movies"
TARGET_GRAPH = "https://graph.onta.sh/graphs/acme-corp/kg/vendor-catalog"

_SAMPLE_SPARQL = (
    f"SELECT ?title FROM <{FOREIGN_GRAPH}> WHERE {{ "
    "?m <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://graph.onta.sh/types/Movie> . "
    "?m <https://graph.onta.sh/types/Movie/attrs/title> ?title . "
    "?m <https://graph.onta.sh/onto/directedBy> ?d }"
)


def _example(sparql: str = _SAMPLE_SPARQL, kg_name: str = "imdb-movies") -> Example:
    return Example(
        question="Which movies did she direct?",
        sparql=sparql,
        kg_name=kg_name,
        ontology_context="Type: Movie",
        pattern_tags=["join"],
    )


# ── sanitize_example_sparql ──────────────────────────────────────────────


def test_from_clause_rewritten_to_target_graph():
    out = sanitize_example_sparql(_SAMPLE_SPARQL, TARGET_GRAPH)
    assert f"FROM <{TARGET_GRAPH}>" in out
    assert FOREIGN_GRAPH not in out
    assert "demo-tenant" not in out


def test_placeholder_used_when_no_target_graph_given():
    out = sanitize_example_sparql(_SAMPLE_SPARQL)
    assert f"FROM <{TARGET_GRAPH_PLACEHOLDER}>" in out
    assert "demo-tenant" not in out


def test_type_and_attribute_uris_are_preserved():
    """Deliberate: the foreign SCHEMA is the pattern being taught, not a leak.

    Abstracting these into placeholders would delete the URI-shape lesson the
    bank exists to give (``types/<T>/attrs/<a>`` for literals, ``onto/<leaf>``
    for relationship edges) while protecting nothing: they are public benchmark
    schema names, not tenant identity or customer data.
    """
    out = sanitize_example_sparql(_SAMPLE_SPARQL, TARGET_GRAPH)
    assert "<https://graph.onta.sh/types/Movie>" in out
    assert "<https://graph.onta.sh/types/Movie/attrs/title>" in out
    assert "<https://graph.onta.sh/onto/directedBy>" in out


@pytest.mark.parametrize("keyword", ["FROM", "from", "From", "FROM NAMED", "from named"])
def test_keyword_casing_and_from_named_are_handled(keyword):
    out = sanitize_example_sparql(f"SELECT ?x {keyword} <{FOREIGN_GRAPH}> WHERE {{ ?x ?p ?o }}", TARGET_GRAPH)
    assert FOREIGN_GRAPH not in out
    assert f"{keyword} <{TARGET_GRAPH}>" in out


def test_every_from_clause_is_rewritten_not_just_the_first():
    sparql = (
        f"SELECT ?x FROM <{FOREIGN_GRAPH}> "
        "FROM <https://graph.onta.sh/graphs/demo-tenant> WHERE { ?x ?p ?o }"
    )
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert "demo-tenant" not in out
    assert out.count(f"<{TARGET_GRAPH}>") == 2


def test_query_without_a_from_clause_is_untouched():
    sparql = "SELECT ?x WHERE { ?x <https://graph.onta.sh/types/City/attrs/name> ?n }"
    assert sanitize_example_sparql(sparql, TARGET_GRAPH) == sparql


@pytest.mark.parametrize("subject", ["?from", "?validFrom", "ex:from"])
def test_a_token_merely_ENDING_in_from_does_not_eat_the_next_iri(subject):
    """The dataset-clause rule must not fire on `?from <predicate> ?o`.

    Without a lookbehind, ``FROM\\s+<...>`` matches the tail of ``?from
    <https://.../onto/actedIn>`` and replaces the PREDICATE with the target
    graph, silently teaching the model a nonsense triple. No example in the
    shipped bank has such a variable, so this is only reachable once the bank is
    regenerated from new LLM output, which is exactly when nobody is looking.
    """
    sparql = (
        f"SELECT ?m FROM <{FOREIGN_GRAPH}> WHERE {{ "
        f"{subject} <https://graph.onta.sh/onto/actedIn> ?m }}"
    )
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert f"{subject} <https://graph.onta.sh/onto/actedIn> ?m" in out
    assert f"FROM <{TARGET_GRAPH}>" in out


@pytest.mark.parametrize("keyword", ["GRAPH", "SERVICE"])
def test_a_graph_iri_scoped_without_from_is_still_rewritten(keyword):
    """Backstop for the keyword rule.

    Every one of the 262 shipped examples scopes with ``FROM``, but the bank is
    REGENERATED from LLM-written SPARQL by ``populate_from_eval_reports``. A
    future model emitting a ``GRAPH`` block would reopen the leak against a
    keyword-only rule.
    """
    sparql = f"SELECT ?x WHERE {{ {keyword} <{FOREIGN_GRAPH}> {{ ?x ?p ?o }} }}"
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert FOREIGN_GRAPH not in out
    assert "demo-tenant" not in out
    assert f"{keyword} <{TARGET_GRAPH}>" in out


def test_a_less_than_operator_is_not_mistaken_for_the_start_of_an_iri():
    """`<` is also SPARQL's less-than. A backstop keyed only on `/graphs/` starts
    matching at the `<` of `FILTER(?y < 2000)` and swallows everything through the
    next `>`, eating the filter and any clause between it and the graph IRI. The
    scheme anchor plus the whitespace exclusion make an operator unmatchable.
    """
    sparql = (
        f"SELECT ?x FROM <{FOREIGN_GRAPH}> WHERE {{ "
        "?x <https://graph.onta.sh/types/M/attrs/year> ?y . FILTER(?y < 2000) } "
        f"GRAPH <{FOREIGN_GRAPH}> {{ ?a ?b ?c }}"
    )
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert "FILTER(?y < 2000)" in out
    assert "demo-tenant" not in out
    assert out.count(f"<{TARGET_GRAPH}>") == 2


def test_a_less_than_operator_before_a_graph_iri_with_no_space():
    """The tightest form of the same trap: `?y <GRAPH-IRI-looking-thing`.

    A SPARQL IRI cannot contain whitespace, `<` or `>`, so the pattern excludes
    all three. This pins the `<` half specifically: without it the match could
    still span a nested `<`.
    """
    sparql = f"SELECT ?x WHERE {{ FILTER(?a <?b) GRAPH <{FOREIGN_GRAPH}> {{ ?a ?b ?c }} }}"
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert "FILTER(?a <?b)" in out
    assert "demo-tenant" not in out


@pytest.mark.parametrize(
    "expr",
    ["FILTER(?y < 2000)", "FILTER(?y <= 2000 && ?y > 1990)", "FILTER(?a <?b)"],
)
def test_comparison_operators_survive_untouched(expr):
    sparql = f"SELECT ?x FROM <{FOREIGN_GRAPH}> WHERE {{ ?x ?p ?y . {expr} }}"
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert expr in out


def test_sanitizing_twice_changes_nothing_the_second_time():
    once = sanitize_example_sparql(_SAMPLE_SPARQL, TARGET_GRAPH)
    assert sanitize_example_sparql(once, TARGET_GRAPH) == once


def test_the_graph_backstop_does_not_touch_type_or_entity_iris():
    """It keys on the `/graphs/` path segment, not on the host."""
    sparql = (
        "SELECT ?x WHERE { ?x <https://graph.onta.sh/types/City/attrs/name> ?n . "
        "?x <https://graph.onta.sh/onto/locatedIn> <https://graph.onta.sh/entities/City/san-jose> }"
    )
    assert sanitize_example_sparql(sparql, TARGET_GRAPH) == sparql


# ── format_examples_for_prompt ───────────────────────────────────────────


def test_formatted_block_carries_no_foreign_graph_uri():
    text = format_examples_for_prompt([_example()], TARGET_GRAPH)
    assert "demo-tenant" not in text
    assert f"FROM <{TARGET_GRAPH}>" in text


def test_header_warns_that_examples_may_come_from_other_graphs():
    text = format_examples_for_prompt([_example()], TARGET_GRAPH)
    header = text.splitlines()[0] + " " + text.splitlines()[1]
    assert "OTHER graphs" in header
    assert "ontology schema above" in header
    assert "rewritten to your target graph" in header


def test_header_is_hedged_not_absolute_about_foreign_ontologies():
    """Production /ask leaves the same-KG filter off (it is gated on
    ``exclude_questions``, which only the eval harness passes), so a retrieved
    example is OFTEN from the caller's own KG with exactly correct URIs. An
    unconditional "these belong to a DIFFERENT ontology" would tell the model to
    distrust them.
    """
    header = format_examples_for_prompt([_example()], TARGET_GRAPH).splitlines()[0]
    assert "Some may come from OTHER graphs" in header
    assert "DIFFERENT ontology" not in header


def test_header_explains_the_placeholder_when_no_target_graph():
    text = format_examples_for_prompt([_example()])
    assert TARGET_GRAPH_PLACEHOLDER in text
    assert "placeholder" in text


def test_empty_example_list_still_yields_empty_string():
    assert format_examples_for_prompt([], TARGET_GRAPH) == ""


def test_question_tags_and_sparql_body_survive_formatting():
    text = format_examples_for_prompt([_example()], TARGET_GRAPH)
    assert "Q: Which movies did she direct?" in text
    assert "Example 1 (join):" in text
    assert "<https://graph.onta.sh/types/Movie/attrs/title>" in text


# ── the real shipped bank ────────────────────────────────────────────────


def _bank_examples() -> list[Example]:
    assert DEFAULT_BANK_PATH.exists(), f"example bank missing at {DEFAULT_BANK_PATH}"
    with open(DEFAULT_BANK_PATH) as f:
        return [Example.from_dict(json.loads(line)) for line in f if line.strip()]


def test_shipped_bank_is_all_one_tenant_which_is_why_this_guard_exists():
    """Documents the leak surface. If the bank ever becomes multi-tenant, the
    format-time rewrite still covers it, but the premise below should be reread.
    """
    tenants = {
        m.split("/graphs/")[1].split("/")[0]
        for ex in _bank_examples()
        for m in re.findall(r"FROM\s*<([^>]+)>", ex.sparql, re.IGNORECASE)
    }
    assert tenants, "no FROM clauses found in the shipped bank"
    assert tenants == {"demo-tenant"}, f"bank now spans tenants {sorted(tenants)}"


def test_no_shipped_example_leaks_its_origin_graph_into_a_foreign_prompt():
    examples = _bank_examples()
    assert len(examples) >= 100, "bank unexpectedly small; guard may be vacuous"
    text = format_examples_for_prompt(examples, TARGET_GRAPH)
    assert "demo-tenant" not in text
    # The graph IRI is the only tenant-bearing token, so nothing else should
    # survive that names a graph other than the caller's.
    other_graphs = {
        g for g in re.findall(r"https://graph\.onta\.sh/graphs/[^\s>]+", text) if g != TARGET_GRAPH
    }
    assert not other_graphs, f"foreign graph IRIs survived formatting: {sorted(other_graphs)[:5]}"


def test_sanitizing_a_shipped_example_onto_its_own_graph_is_byte_identical():
    """The same-KG no-op. This is the load-bearing safety property.

    This change ships WITHOUT an accuracy eval (see the PR body: `eval.py` has
    no offline path, and no provider key was available). The argument for that
    rests on a claim about the size of the prompt delta, and this is the half of
    it that a regression can silently break.

    When a retrieved example already belongs to the graph being asked about --
    the common case in production `/ask`, where the same-KG filter and penalty
    are OFF -- BOTH rewrite rules must be exact no-ops, so the SPARQL the model
    sees is byte-for-byte what it saw before this change. Verified across every
    example actually shipped, not on a synthetic fixture, because the risk being
    guarded is a regex that over-matches on some real query shape.

    (The other half of the argument, that a CROSS-KG example's `FROM` moves onto
    the graph the question is genuinely about and so moves toward the SPARQL the
    model must emit, is a directional claim about generation quality that no
    unit test can settle. It needs the eval.)
    """
    examples = _bank_examples()
    assert len(examples) >= 100, "bank unexpectedly small; guard may be vacuous"

    altered: list[str] = []
    checked = 0
    for ex in examples:
        own = re.findall(r"FROM\s+(?:NAMED\s+)?<([^>]+)>", ex.sparql, re.IGNORECASE)
        if not own:
            continue  # an example with no dataset clause has nothing to rewrite
        # An example scoped to several graphs has no single "own" graph, so a
        # rewrite legitimately changes it. Out of scope for this property.
        if len(set(own)) != 1:
            continue
        checked += 1
        if sanitize_example_sparql(ex.sparql, own[0]) != ex.sparql:
            altered.append(ex.question)

    assert checked >= 100, f"only {checked} single-graph examples; guard may be vacuous"
    assert not altered, (
        f"{len(altered)} of {checked} shipped examples are MUTATED when sanitized "
        f"onto their own graph, e.g. {altered[:3]}. A same-KG retrieval would then "
        "hand the model different SPARQL than before ONTA-420, which is exactly "
        "the risk the missing eval cannot rule out."
    )


# ── end to end through the pipeline ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_prompt_for_another_tenant_contains_no_demo_tenant_graph():
    """The real leak path: /ask -> retrieve -> format -> LLM prompt.

    Asserts on the prompt actually handed to the generator, not on the helper,
    so a future caller that forgets to pass the target graph is caught here.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from infona_client.nlp.pipeline import NLQueryPipeline

    neptune = AsyncMock()
    neptune.query.return_value = {
        "head": {"vars": ["title"]},
        "results": {"bindings": [{"title": {"type": "literal", "value": "Arrival"}}]},
    }
    pipeline = NLQueryPipeline(neptune, "fake-key")

    bank = MagicMock()
    bank._examples = [_example()]
    bank.retrieve = AsyncMock(return_value=[_example()])

    llm_response = json.dumps({
        "sparql": f"SELECT ?title FROM <{TARGET_GRAPH}> WHERE {{ ?s ?p ?title }}",
        "explanation": "ok",
        "functions_needed": [],
    })
    message = MagicMock()
    message.content = [MagicMock(text=llm_response)]

    with patch("infona_client.nlp.example_bank.get_example_bank", return_value=bank), \
            patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as create:
        create.return_value = message
        await pipeline.ask(
            "Which movies did she direct?",
            "https://graph.onta.sh/graphs/acme-corp",
            TARGET_GRAPH,
        )

    assert create.call_args is not None, "generator was never called"
    prompt = create.call_args.kwargs["messages"][0]["content"]
    assert "Q: Which movies did she direct?" in prompt, "examples were not injected"
    assert "demo-tenant" not in prompt
    assert f"FROM <{TARGET_GRAPH}>" in prompt
