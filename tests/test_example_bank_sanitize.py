"""ONTA-420: the few-shot example bank must not leak one tenant's graph IRIs
into another tenant's SPARQL prompt.

The bank is scoped per PROCESS: ``DEFAULT_BANK_PATH`` is a single JSONL file and
``Example`` carries a ``kg_name`` but no tenant. The shipped bank is 262
examples across 12 KGs, all ``demo-tenant``. Before this change
``format_examples_for_prompt`` did nothing but collapse whitespace, so those
``FROM <https://cograph.tech/graphs/demo-tenant/kg/...>`` clauses went verbatim
into every self-hosted and third-party tenant's prompt. The only defense was a
prose line in the system prompt.

The fix SANITIZES rather than filters: retrieval is untouched (all 262 examples
stay available to every tenant, which is the cross-domain pattern transfer the
bank exists for), but the graph IRI, the only tenant-identifying token in a
stored example, is rewritten to the caller's own target graph at format time.

Type and attribute IRIs are deliberately NOT abstracted. They teach the URI
shapes the generator must produce, they name public benchmark schemas rather
than customer data, and placeholdering them would destroy the pattern-transfer
value with no privacy gain. These tests pin that decision so it stays a choice
rather than an oversight.
"""

import json
import re

import pytest

from cograph_client.nlp.example_bank import (
    DEFAULT_BANK_PATH,
    TARGET_GRAPH_PLACEHOLDER,
    Example,
    format_examples_for_prompt,
    sanitize_example_sparql,
)

FOREIGN_GRAPH = "https://cograph.tech/graphs/demo-tenant/kg/imdb-movies"
TARGET_GRAPH = "https://cograph.tech/graphs/acme-corp/kg/vendor-catalog"

_SAMPLE_SPARQL = (
    f"SELECT ?title FROM <{FOREIGN_GRAPH}> WHERE {{ "
    "?m <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://cograph.tech/types/Movie> . "
    "?m <https://cograph.tech/types/Movie/attrs/title> ?title . "
    "?m <https://cograph.tech/onto/directedBy> ?d }"
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
    assert "<https://cograph.tech/types/Movie>" in out
    assert "<https://cograph.tech/types/Movie/attrs/title>" in out
    assert "<https://cograph.tech/onto/directedBy>" in out


@pytest.mark.parametrize("keyword", ["FROM", "from", "From", "FROM NAMED", "from named"])
def test_keyword_casing_and_from_named_are_handled(keyword):
    out = sanitize_example_sparql(f"SELECT ?x {keyword} <{FOREIGN_GRAPH}> WHERE {{ ?x ?p ?o }}", TARGET_GRAPH)
    assert FOREIGN_GRAPH not in out
    assert f"{keyword} <{TARGET_GRAPH}>" in out


def test_every_from_clause_is_rewritten_not_just_the_first():
    sparql = (
        f"SELECT ?x FROM <{FOREIGN_GRAPH}> "
        "FROM <https://cograph.tech/graphs/demo-tenant> WHERE { ?x ?p ?o }"
    )
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert "demo-tenant" not in out
    assert out.count(f"<{TARGET_GRAPH}>") == 2


def test_query_without_a_from_clause_is_untouched():
    sparql = "SELECT ?x WHERE { ?x <https://cograph.tech/types/City/attrs/name> ?n }"
    assert sanitize_example_sparql(sparql, TARGET_GRAPH) == sparql


def test_where_clause_body_is_not_rewritten():
    """Only the dataset clause moves. A `?from` variable must survive intact."""
    sparql = f"SELECT ?from FROM <{FOREIGN_GRAPH}> WHERE {{ ?x <https://cograph.tech/onto/from> ?from }}"
    out = sanitize_example_sparql(sparql, TARGET_GRAPH)
    assert "<https://cograph.tech/onto/from> ?from" in out
    assert "SELECT ?from" in out


# ── format_examples_for_prompt ───────────────────────────────────────────


def test_formatted_block_carries_no_foreign_graph_uri():
    text = format_examples_for_prompt([_example()], TARGET_GRAPH)
    assert "demo-tenant" not in text
    assert f"FROM <{TARGET_GRAPH}>" in text


def test_header_labels_examples_as_coming_from_other_graphs():
    text = format_examples_for_prompt([_example()], TARGET_GRAPH)
    header = text.splitlines()[0] + " " + text.splitlines()[1]
    assert "OTHER graphs" in header
    assert "illustrative" in header


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
    assert "<https://cograph.tech/types/Movie/attrs/title>" in text


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
        g for g in re.findall(r"https://cograph\.tech/graphs/[^\s>]+", text) if g != TARGET_GRAPH
    }
    assert not other_graphs, f"foreign graph IRIs survived formatting: {sorted(other_graphs)[:5]}"


# ── end to end through the pipeline ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_prompt_for_another_tenant_contains_no_demo_tenant_graph():
    """The real leak path: /ask -> retrieve -> format -> LLM prompt.

    Asserts on the prompt actually handed to the generator, not on the helper,
    so a future caller that forgets to pass the target graph is caught here.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from cograph_client.nlp.pipeline import NLQueryPipeline

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

    with patch("cograph_client.nlp.example_bank.get_example_bank", return_value=bank), \
            patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as create:
        create.return_value = message
        await pipeline.ask(
            "Which movies did she direct?",
            "https://cograph.tech/graphs/acme-corp",
            TARGET_GRAPH,
        )

    assert create.call_args is not None, "generator was never called"
    prompt = create.call_args.kwargs["messages"][0]["content"]
    assert "Q: Which movies did she direct?" in prompt, "examples were not injected"
    assert "demo-tenant" not in prompt
    assert f"FROM <{TARGET_GRAPH}>" in prompt
