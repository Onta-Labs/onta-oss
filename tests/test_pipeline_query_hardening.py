"""NL->SPARQL query-pipeline hardening against a flaky/weak generator.

Real persona-eval failure modes these guard (query model = a Cerebras reasoning
model, but nothing here depends on that): the SPARQL-gen model returns an
empty/blank query and the retry loop replays the IDENTICAL reduced context 3x
("Could not answer … Last error: Empty query"); a name lookup is bound to ONE
guessed subtype and returns zero rows even though a supertype spans all
subtypes; and the generation response is malformed JSON (code fences /
unterminated string) that throws an uncaught parse error.

The three fixes and what these assert (MECHANISM only — invented tokens, no
real ontology / physician example):
  1. Empty/blank SPARQL ESCALATES: the next retry widens a semantic subset to
     the FULL ontology and demands a non-empty SELECT, and the pipeline returns
     the recovered answer instead of "Could not answer".
  2. Name-lookup BROADENING: a zero-row lookup pinned to a single subtype is
     re-issued against the type's supertype (subclass closure then spans every
     sibling subtype), so an instance of a *different* subtype is found.
  3. Malformed-JSON TOLERANCE: code fences / an unterminated string are parsed
     tolerantly (salvage the query, else degrade to empty -> escalation) rather
     than raising.

**LOST CAPABILITY (ONTA-527) — fixes 1 and 2, at the `ask()` level only.** Both
are implemented in ``nlp/pipeline.py::ask``'s SPARQL retry loop, and ``POST
/ask`` takes ``_ask_cypher`` now. ``_ask_cypher`` DOES reject an empty
generation (``confine_generated_cypher`` raises "Generated Cypher is empty.")
and retries once with that as feedback — but it never widens the ontology,
because it has no notion of a reduced-vs-full context to widen (it reads one
ontology, from the catalog or ``_fetch_ontology``), and there is no
``ontology_escalated_to_full_attempt`` signal. Name-lookup broadening does not
exist on the Cypher path at all; a zero-row valid query is final.

Fix 3 (``_parse_sparql_gen_json`` / ``_salvage_sparql_field``) and the
``_broaden_name_lookup`` UNIT cases are untouched and still green — those
helpers are live and independently reachable. Only the three end-to-end
``ask()`` cases are xfailed.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from infona_client.nlp import pipeline as pipeline_mod
from infona_client.nlp.pipeline import (
    NLQueryPipeline,
    _parse_sparql_gen_json,
    _salvage_sparql_field,
)

_NO_ONTOLOGY_ESCALATION = (
    "LOST CAPABILITY (ONTA-527): the empty-query ONTOLOGY ESCALATION "
    "(semantic subset → full schema, timing['ontology_escalated_to_full_attempt']) "
    "lives in nlp/pipeline.py::ask's SPARQL retry loop. /ask takes _ask_cypher, "
    "which retries an empty generation with error feedback but never widens the "
    "schema context and emits no escalation signal."
)

_NO_NAME_LOOKUP_BROADENING = (
    "LOST CAPABILITY (ONTA-527): nlp/pipeline.py::_broaden_name_lookup is called "
    "only from ask()'s SPARQL branch (the zero-row post-execution step). "
    "_ask_cypher never calls it, so a name lookup pinned to the wrong subtype "
    "returns zero rows and stops. The helper itself is unchanged and still unit-"
    "tested above."
)

# --- invented ontology tokens (never a real type/attribute) -----------------
SUBSET_ONTOLOGY = "SEMANTIC_SUBSET_ONTOLOGY_TOKEN"
FULL_ONTOLOGY = "FULL_ONTOLOGY_TOKEN"

# A per-KG instance graph, not a bare tenant URI: since ONTA-527 the ask
# path derives (tenant, kg) from it and refuses a tenant graph outright, so a
# bare tenant URI would make the cases below fail on scope resolution and
# hide the capability each one is actually about.
KG_GRAPH = "https://graph.infona.ai/graphs/t1/kg/widgets"

TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
TYPES = "https://graph.infona.ai/types"


def _rows(vars_, *value_rows) -> dict:
    """A SPARQL JSON result with `vars_` columns and one dict per row."""
    return {
        "head": {"vars": list(vars_)},
        "results": {
            "bindings": [
                {k: {"type": "literal", "value": v} for k, v in row.items()}
                for row in value_rows
            ]
        },
    }


def _uri_rows(pairs) -> dict:
    """A ?child ?parent result of URI-valued rows (for the parent map)."""
    return {
        "head": {"vars": ["child", "parent"]},
        "results": {
            "bindings": [
                {
                    "child": {"type": "uri", "value": c},
                    "parent": {"type": "uri", "value": p},
                }
                for c, p in pairs
            ]
        },
    }


EMPTY_RESULT = {"head": {"vars": ["name"]}, "results": {"bindings": []}}


# =========================================================================== #
# Fix 1: empty/blank first SPARQL ESCALATES (subset -> full + explicit feedback)
# =========================================================================== #
@pytest.mark.asyncio
async def test_empty_first_sparql_escalates_to_full_ontology_and_recovers():
    """Attempt-1 returns a blank query; attempt-2 must run against the FULL
    ontology (not the identical semantic subset) with explicit non-empty
    feedback, and the pipeline returns attempt-2's answer — not "Could not
    answer"."""
    from infona_client.graph.memory_store import MemoryGraphStore
    from unittest.mock import MagicMock

    neptune = AsyncMock()
    p = NLQueryPipeline(neptune, "invented-anthropic-key", graph_store=MemoryGraphStore())
    p._openrouter_key = ""

    embed = MagicMock()
    embed.retrieve = AsyncMock(return_value=SUBSET_ONTOLOGY)
    embed.type_names = AsyncMock(return_value={"Widget"})

    gen = AsyncMock(side_effect=[
        {"cypher": "", "params": {}, "explanation": "", "functions_needed": []},
        {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "RETURN e.name AS name LIMIT 5"
            ),
            "params": {},
            "explanation": "ok",
            "functions_needed": [],
        },
    ])

    async def fake_exec(session, g, cypher, forced_params):
        rec = MagicMock()
        rec.keys.return_value = ["name"]
        rec.get.side_effect = lambda k, d=None: "widget-a" if k == "name" else d
        return [rec], "execute_read"

    with patch.object(pipeline_mod, "get_embedding_service", return_value=embed), \
         patch.object(p, "_fetch_ontology", new=AsyncMock(return_value=FULL_ONTOLOGY)) as fetch_full, \
         patch.object(p, "_try_llm_cypher", new=gen), \
         patch.object(p, "_execute_confined_cypher", new=fake_exec), \
         patch.object(p, "_rephrase_via_openrouter", new=AsyncMock(return_value="")), \
         patch.object(p, "_active_types", new=AsyncMock(return_value={"Widget"})):
        result = await p.ask("show details for zzqx", "https://graph.infona.ai/graphs/t1", KG_GRAPH)

    assert "Could not answer" not in result.answer
    assert result.timing.get("attempts") == 2
    assert result.timing.get("ontology_escalated_to_full_attempt") == 1
    assert gen.await_count == 2
    first_ontology = gen.call_args_list[0].args[1]
    second_ontology = gen.call_args_list[1].args[1]
    assert first_ontology == SUBSET_ONTOLOGY
    assert second_ontology == FULL_ONTOLOGY
    feedback = gen.call_args_list[1].kwargs.get("error_feedback", "")
    assert "EMPTY" in feedback.upper()
    assert "non-empty" in feedback.lower()
    fetch_full.assert_awaited()


@pytest.mark.asyncio
async def test_empty_sparql_does_not_escalate_when_already_full():
    """When the context is ALREADY the full ontology (no semantic subset), the
    escalation must not re-fetch it — it only adds the non-empty feedback."""
    from infona_client.graph.memory_store import MemoryGraphStore
    from unittest.mock import MagicMock

    neptune = AsyncMock()
    p = NLQueryPipeline(neptune, "invented-anthropic-key", graph_store=MemoryGraphStore())
    p._openrouter_key = ""

    gen = AsyncMock(side_effect=[
        {"cypher": "   ", "params": {}, "explanation": "", "functions_needed": []},
        {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "RETURN e.name AS name LIMIT 5"
            ),
            "params": {},
            "explanation": "ok",
            "functions_needed": [],
        },
    ])
    fetch_full = AsyncMock(return_value=FULL_ONTOLOGY)

    async def fake_exec(session, g, cypher, forced_params):
        rec = MagicMock()
        rec.keys.return_value = ["name"]
        rec.get.side_effect = lambda k, d=None: "widget-a" if k == "name" else d
        return [rec], "execute_read"

    with patch.object(pipeline_mod, "get_embedding_service", return_value=None), \
         patch.object(p, "_fetch_ontology", new=fetch_full), \
         patch.object(p, "_try_llm_cypher", new=gen), \
         patch.object(p, "_execute_confined_cypher", new=fake_exec), \
         patch.object(p, "_rephrase_via_openrouter", new=AsyncMock(return_value="")):
        result = await p.ask("show details for zzqx", "https://graph.infona.ai/graphs/t1", KG_GRAPH)

    assert "Could not answer" not in result.answer
    assert fetch_full.await_count == 1
    assert "ontology_escalated_to_full_attempt" not in result.timing
    assert "non-empty" in gen.call_args_list[1].kwargs.get("error_feedback", "").lower()


# =========================================================================== #
# Fix 2: name-lookup BROADENING to the supertype                               #
# =========================================================================== #
def _single_subtype_name_lookup(subtype: str) -> str:
    """A name lookup as it appears AFTER the pipeline's closure rewrite: an
    rdf:type/subClassOf* pinned to ONE subtype, plus a CONTAINS(LCASE(...))
    name filter."""
    return (
        f"SELECT ?name WHERE {{ "
        f"?x <{TYPE}>/<{SUBCLASS}>* <{TYPES}/{subtype}> . "
        f"?x <{LABEL}> ?name . "
        f'FILTER(CONTAINS(LCASE(?name), "rex")) }}'
    )


@pytest.mark.asyncio
async def test_broaden_name_lookup_swaps_subtype_for_supertype_and_finds_row():
    """Invented hierarchy Animal -> {Dog, Cat}. A name lookup pinned to the WRONG
    subtype (Cat) finds nothing; broadening swaps Cat for its supertype Animal,
    re-queries, and finds Rex (a Dog)."""
    sub = "Cat"
    parent_map = _uri_rows([
        (f"{TYPES}/Dog", f"{TYPES}/Animal"),
        (f"{TYPES}/Cat", f"{TYPES}/Animal"),
    ])
    rex = _rows(["name"], {"name": "Rex"})

    async def query(sparql, *a, **k):
        if "?child" in sparql:            # parent_map_query
            return parent_map
        if f"{TYPES}/Animal" in sparql:   # the broadened (supertype) query
            return rex
        return EMPTY_RESULT               # the original Cat query

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "invented-anthropic-key")

    original = _single_subtype_name_lookup(sub)
    out = await p._broaden_name_lookup(original, "https://graph.infona.ai/graphs/t1")

    assert out is not None, "broadening should fire for a single-subtype name lookup"
    broadened_sparql, raw = out
    # The MECHANISM: subtype URI replaced by the supertype URI, then re-queried.
    assert f"{TYPES}/Animal" in broadened_sparql
    assert f"{TYPES}/Cat" not in broadened_sparql
    _vars = raw["results"]["bindings"]
    assert _vars and _vars[0]["name"]["value"] == "Rex"


@pytest.mark.asyncio
async def test_broaden_name_lookup_noop_without_a_name_filter():
    """A NON-name query (no CONTAINS/LCASE filter) is never broadened, even at
    zero rows — broadening is scoped to name lookups only."""
    neptune = AsyncMock()
    neptune.query = AsyncMock(return_value=EMPTY_RESULT)
    p = NLQueryPipeline(neptune, "k")
    q = f"SELECT ?x WHERE {{ ?x <{TYPE}>/<{SUBCLASS}>* <{TYPES}/Cat> }}"
    assert await p._broaden_name_lookup(q, "https://graph.infona.ai/graphs/t1") is None


@pytest.mark.asyncio
async def test_broaden_name_lookup_noop_when_type_has_no_supertype():
    """A subtype with no parent in the hierarchy is not broadened (no over-broadening
    to an unrelated/root type when there is nothing above it)."""
    async def query(sparql, *a, **k):
        if "?child" in sparql:
            return _uri_rows([])  # empty parent map -> no supertype
        return EMPTY_RESULT

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "k")
    q = _single_subtype_name_lookup("Cat")
    assert await p._broaden_name_lookup(q, "https://graph.infona.ai/graphs/t1") is None


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason=_NO_NAME_LOOKUP_BROADENING)
async def test_ask_broadens_zero_row_name_lookup_end_to_end():
    """End-to-end through ask(): the generator emits a name lookup bound to the
    wrong subtype (Cat) -> 0 rows -> the broadening step re-queries the supertype
    (Animal) and the answer surfaces Rex."""
    parent_map = _uri_rows([
        (f"{TYPES}/Dog", f"{TYPES}/Animal"),
        (f"{TYPES}/Cat", f"{TYPES}/Animal"),
    ])
    rex = _rows(["name"], {"name": "Rex"})

    async def query(sparql, *a, **k):
        if "?child" in sparql:
            return parent_map
        if f"{TYPES}/Animal" in sparql:
            return rex
        return EMPTY_RESULT

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""

    # Generator returns a plain single-subtype name lookup; ask() applies the
    # closure rewrite + broadening.
    gen = AsyncMock(return_value={
        "sparql": (
            f"SELECT ?name WHERE {{ ?x <{TYPE}> <{TYPES}/Cat> . "
            f'?x <{LABEL}> ?name . FILTER(CONTAINS(LCASE(?name), "rex")) }}'
        ),
        "explanation": "lookup",
        "functions_needed": [],
    })

    with patch.object(pipeline_mod, "get_embedding_service", return_value=None), \
         patch.object(p, "_fetch_ontology", new=AsyncMock(return_value=FULL_ONTOLOGY)), \
         patch.object(p, "_generate_sparql", new=gen):
        result = await p.ask("show details for Rex", "https://graph.infona.ai/graphs/t1", KG_GRAPH)

    # NLResult.timing is typed dict[str, float], so the True flag surfaces as 1.0
    # — assert it fired (truthy), which is the mechanism we care about.
    assert result.timing.get("name_lookup_broadened")
    assert "Rex" in result.answer
    # The returned query is the broadened (supertype) one.
    assert f"{TYPES}/Animal" in result.sparql


# --- reviewer defect A: broadening must be scoped to real NAME lookups ------
@pytest.mark.asyncio
async def test_broaden_skips_contains_filter_on_non_name_attribute():
    """A single-subtype query whose CONTAINS filter targets a NON-name attribute
    (tags) must NOT broaden, even at zero rows — otherwise an honest "no results"
    for MortgageComplaint would surface a sibling CreditCardComplaint row (wrong
    TYPE). It short-circuits before any Neptune probe. A true label lookup over
    the SAME hierarchy still qualifies."""
    neptune = AsyncMock()
    neptune.query = AsyncMock(return_value=EMPTY_RESULT)
    p = NLQueryPipeline(neptune, "k")

    non_name = (
        f"SELECT ?tags WHERE {{ "
        f"?x <{TYPE}>/<{SUBCLASS}>* <{TYPES}/MortgageComplaint> . "
        f"?x <{TYPES}/MortgageComplaint/attrs/tags> ?tags . "
        f'FILTER(CONTAINS(LCASE(?tags), "escrow")) }}'
    )
    assert await p._broaden_name_lookup(non_name, "https://graph.infona.ai/graphs/t1") is None
    neptune.query.assert_not_awaited()  # gated out before the parent-map probe

    # Contrast, SAME hierarchy: a CONTAINS over an rdfs:label variable DOES qualify.
    label_lookup = (
        f"SELECT ?name WHERE {{ "
        f"?x <{TYPE}>/<{SUBCLASS}>* <{TYPES}/MortgageComplaint> . "
        f"?x <{LABEL}> ?name . "
        f'FILTER(CONTAINS(LCASE(?name), "escrow")) }}'
    )
    assert p._targets_label_name_var(label_lookup) is True
    assert p._targets_label_name_var(non_name) is False
    # A name-attribute (types/<T>/attrs/name) binding also qualifies.
    attr_name_lookup = (
        f"SELECT ?nm WHERE {{ "
        f"?x <{TYPE}>/<{SUBCLASS}>* <{TYPES}/MortgageComplaint> . "
        f"?x <{TYPES}/MortgageComplaint/attrs/name> ?nm . "
        f'FILTER(CONTAINS(LCASE(?nm), "escrow")) }}'
    )
    assert p._targets_label_name_var(attr_name_lookup) is True


# --- reviewer defect B: rewrite ONLY the exact type-object, not prefix twins -
@pytest.mark.asyncio
async def test_broaden_rewrites_only_the_type_object_not_prefixed_uris():
    """Broadening must rewrite ONLY the exact bracketed type-object. Attribute URIs
    and sibling-type URIs that share the 'Cat' prefix (types/Cat/attrs/breed,
    types/CatFood/attrs/flavor) must be left intact — a raw substring replace would
    corrupt them into non-existent URIs on the abstract supertype."""
    parent_map = _uri_rows([(f"{TYPES}/Cat", f"{TYPES}/Animal")])
    rex = _rows(["name"], {"name": "Rex"})

    async def query(sparql, *a, **k):
        if "?child" in sparql:
            return parent_map
        return rex

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "k")

    original = (
        f"SELECT ?name ?breed ?flavor WHERE {{ "
        f"?x <{TYPE}>/<{SUBCLASS}>* <{TYPES}/Cat> . "
        f"?x <{LABEL}> ?name . "
        f"?x <{TYPES}/Cat/attrs/breed> ?breed . "        # type-specific attribute URI
        f"?x <{TYPES}/CatFood/attrs/flavor> ?flavor . "  # sibling type sharing the 'Cat' prefix
        f'FILTER(CONTAINS(LCASE(?name), "rex")) }}'
    )
    out = await p._broaden_name_lookup(original, "https://graph.infona.ai/graphs/t1")
    assert out is not None
    broadened, _ = out

    # ONLY the exact type-object was rewritten to the supertype.
    assert f"<{TYPES}/Animal>" in broadened
    assert f"<{TYPES}/Cat>" not in broadened
    # Prefix-sharing URIs are preserved verbatim (NOT corrupted).
    assert f"<{TYPES}/Cat/attrs/breed>" in broadened
    assert f"<{TYPES}/CatFood/attrs/flavor>" in broadened
    assert f"<{TYPES}/Animal/attrs/breed>" not in broadened
    assert f"<{TYPES}/AnimalFood/attrs/flavor>" not in broadened


# =========================================================================== #
# Fix 3: malformed-JSON TOLERANCE                                              #
# =========================================================================== #
def test_parse_sparql_gen_json_happy_path_is_identical_to_json_loads():
    """A well-formed response parses byte-identically to json.loads (happy path
    unchanged)."""
    content = json.dumps({
        "sparql": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1",
        "explanation": "e", "functions_needed": [],
    })
    assert _parse_sparql_gen_json(content) == json.loads(content)


def test_parse_sparql_gen_json_strips_code_fences():
    """```json fences wrapping the JSON are tolerated (a common reasoning-model
    habit)."""
    inner = json.dumps({
        "sparql": "SELECT ?x WHERE { ?x ?p ?o }",
        "explanation": "e", "functions_needed": [],
    })
    fenced = f"```json\n{inner}\n```"
    out = _parse_sparql_gen_json(fenced)
    assert out["sparql"].startswith("SELECT")


def test_parse_sparql_gen_json_salvages_unterminated_string():
    """A response truncated mid-`sparql` (no closing quote/brace) still yields the
    recovered query instead of raising."""
    truncated = '{"sparql": "SELECT ?x WHERE { ?x ?p ?o }'  # cut off, unterminated
    out = _parse_sparql_gen_json(truncated)
    assert out["sparql"] == "SELECT ?x WHERE { ?x ?p ?o }"
    assert out["functions_needed"] == []


def test_parse_sparql_gen_json_degrades_to_empty_on_garbage():
    """Unrecoverable garbage degrades to an EMPTY sparql (which triggers the
    ask() escalation), never an uncaught exception."""
    out = _parse_sparql_gen_json("this is not json at all <<< >>>")
    assert out["sparql"] == ""


def test_salvage_sparql_field_honors_json_escapes():
    """The salvage un-escapes JSON string escapes so a filter value with an
    escaped quote survives truncation."""
    # Raw string: the literal chars include a JSON-escaped quote (\") around the
    # filter value, and the JSON string is cut off before its closing quote.
    text = r'{"sparql": "SELECT ?x WHERE { ?x <p> ?n . FILTER(?n = \"a b\") }'
    salvaged = _salvage_sparql_field(text)
    assert salvaged.startswith("SELECT ?x")
    assert '"a b"' in salvaged  # \" un-escaped to ", read up to the cut-off


# ------- integration: the Cerebras path tolerates fenced/truncated content ---
_RealAsyncClient = httpx.AsyncClient


def _post_factory(payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    def factory(*a, **k):
        return _RealAsyncClient(transport=transport)

    return factory


def _resp(content) -> dict:
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.asyncio
async def test_cerebras_generation_tolerates_malformed_json(monkeypatch):
    """A fenced-and-truncated Cerebras response no longer throws an uncaught
    JSONDecodeError out of the generator — it salvages the query."""
    p = NLQueryPipeline(AsyncMock(), "k")
    p._query_provider = "cerebras"
    p._cerebras_key = "invented-cerebras-key"
    p._query_model = "invented-model"

    fenced_truncated = '```json\n{"sparql": "SELECT ?s WHERE { ?s ?p ?o }'  # fence + cut off
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", _post_factory(_resp(fenced_truncated)))
    out = await p._generate_via_cerebras("give me sparql")
    assert out["sparql"] == "SELECT ?s WHERE { ?s ?p ?o }"
