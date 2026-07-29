"""ONTA-411 / ONTA-417: the ontology handed to the SPARQL prompt must be scoped
to the KG being queried.

Root cause (ONTA-411): the ontology graph is TENANT-WIDE while the instance graph
is PER-KG. `routes/ask.py` splits them correctly, but the semantic retrieval path
(`OntologyEmbeddingService.retrieve`) ranked purely on cosine similarity over
every type the tenant owns. A question about a sibling KG's subject matter
retrieved THAT KG's schema at max similarity, and the prompt then aimed it at the
target graph: syntactically valid SPARQL, another graph's types, zero rows. Users
read that as the translator "recycling" their previous query.

The fix DEMOTES and ANNOTATES, it does not hard-drop. Hiding a declared type is
the ONTA-258 regression: the LLM then asserts the type "does not exist" or
silently substitutes a populated one.

ONTA-417 (half A) makes the prompt say which graph is the target, so the
"[no instances]" mark reads as "declared tenant-wide, no data HERE" instead of
"declared here but empty". Half B evicts the tenant's ontology caches on KG
delete so a deleted KG's schema stops competing for retrieval slots.

Invented tokens only (Widget / Sprocket / Gadget) so the assertions are about the
MECHANISM, not any domain.
"""

from __future__ import annotations

import numpy as np
import pytest

from cograph_client.nlp.ontology_embeddings import (
    NO_INSTANCES_MARK,
    OntologyEmbeddingService,
    TenantEmbeddingStore,
    TypeChunk,
    _mark_no_instances,
)
from cograph_client.nlp.pipeline import (
    NLQueryPipeline,
    ONTOLOGY_EMPTY,
    _active_types_cache,
    _ontology_cache,
)
from cograph_client.nlp.prompts import SPARQL_GENERATION_SYSTEM, build_generation_prompt

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TYPES = "https://cograph.tech/types/"
GRAPH = "https://cograph.tech/graphs/inv-tenant"
KG = "https://cograph.tech/graphs/inv-tenant/kg/TargetKG"
SIBLING_KG = "https://cograph.tech/graphs/inv-tenant/kg/SiblingKG"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _chunk(name: str, vec: list[float], targets: list[str] | None = None) -> TypeChunk:
    return TypeChunk(
        type_name=name,
        chunk_text=f"Type: {name} — URI: <{TYPES}{name}>\n  Attributes: code (string)",
        embedding=np.array(vec, dtype=np.float32),
        attributes=["code (string)"],
        relationship_targets=list(targets or []),
    )


def _svc(monkeypatch, chunks: dict[str, TypeChunk], question_vec: list[float]):
    """Service preloaded with `chunks`, whose question embedding is fixed."""
    svc = OntologyEmbeddingService(openrouter_api_key="fake", s3_bucket="", s3_prefix="test")
    store = TenantEmbeddingStore()
    store.chunks.update(chunks)
    svc._stores[GRAPH] = store

    async def fake_embed(texts):
        return [list(question_vec) for _ in texts]

    monkeypatch.setattr(svc, "_embed_texts", fake_embed)
    return svc


def _type_lines(text: str) -> dict[str, bool]:
    """Map type name -> whether its header carries the [no instances] mark."""
    out: dict[str, bool] = {}
    for line in text.splitlines():
        if line.startswith("Type: "):
            name = line.split("Type: ", 1)[1].split(" ", 1)[0]
            out[name] = NO_INSTANCES_MARK in line
    return out


# --------------------------------------------------------------------------- #
# ONTA-411: scope-aware ranking in semantic retrieval
# --------------------------------------------------------------------------- #


async def test_out_of_kg_type_loses_the_slot_to_an_in_kg_type(monkeypatch):
    """THE BUG. The question is closest to Sprocket, which belongs to a SIBLING
    KG; with one slot available it must go to Widget, the type this graph has."""
    svc = _svc(
        monkeypatch,
        {
            "Widget": _chunk("Widget", [0.0, 1.0, 0.0]),
            "Sprocket": _chunk("Sprocket", [1.0, 0.0, 0.0]),
        },
        question_vec=[1.0, 0.0, 0.0],  # max cosine with Sprocket
    )

    # Unscoped (today's production behaviour): the foreign type wins outright.
    unscoped = await svc.retrieve(GRAPH, "any question", top_k=1)
    assert _type_lines(unscoped) == {"Sprocket": False}

    # Scoped to a KG that only carries Widget: Widget takes the slot.
    scoped = await svc.retrieve(GRAPH, "any question", top_k=1, active_types={"Widget"})
    assert _type_lines(scoped) == {"Widget": False}


async def test_out_of_kg_type_is_demoted_not_dropped(monkeypatch):
    """Demote, do NOT hard-drop (ONTA-258). With slots to spare, the foreign type
    still appears, annotated so the model knows it holds no data here."""
    svc = _svc(
        monkeypatch,
        {
            "Widget": _chunk("Widget", [0.0, 1.0, 0.0]),
            "Sprocket": _chunk("Sprocket", [1.0, 0.0, 0.0]),
            "Gadget": _chunk("Gadget", [0.0, 0.0, 1.0]),
        },
        question_vec=[1.0, 0.0, 0.0],
    )

    text = await svc.retrieve(GRAPH, "any question", top_k=3, active_types={"Widget"})
    parsed = _type_lines(text)
    assert set(parsed) == {"Widget", "Sprocket", "Gadget"}, "a declared type was dropped"
    assert parsed["Widget"] is False
    assert parsed["Sprocket"] is True
    assert parsed["Gadget"] is True
    # The declared schema of an out-of-KG type is still visible (the model must be
    # able to write a valid zero-row query against it when asked by name).
    assert "code (string)" in text


async def test_in_kg_types_are_never_annotated(monkeypatch):
    """Control: when every retrieved type has instances here, nothing is marked."""
    svc = _svc(
        monkeypatch,
        {"Widget": _chunk("Widget", [1.0, 0.0]), "Sprocket": _chunk("Sprocket", [0.0, 1.0])},
        question_vec=[1.0, 0.0],
    )
    text = await svc.retrieve(
        GRAPH, "any question", top_k=2, active_types={"Widget", "Sprocket"}
    )
    assert NO_INSTANCES_MARK not in text


async def test_active_types_none_is_the_pre_onta411_behaviour(monkeypatch):
    """Not KG-scoped (bare tenant graph, or a failed probe) => byte-identical to
    the unscoped retrieval, with no annotation invented."""
    chunks = {
        "Widget": _chunk("Widget", [0.0, 1.0, 0.0]),
        "Sprocket": _chunk("Sprocket", [1.0, 0.0, 0.0]),
    }
    svc = _svc(monkeypatch, chunks, question_vec=[1.0, 0.0, 0.0])
    text = await svc.retrieve(GRAPH, "any question", top_k=5, active_types=None)
    assert NO_INSTANCES_MARK not in text
    assert set(_type_lines(text)) == set(chunks)


async def test_named_out_of_kg_type_is_still_force_included(monkeypatch):
    """ONTA-258 force-include survives the demotion: a type the question NAMES is
    surfaced even when it belongs to a sibling KG: annotated, never hidden, so
    the model answers "declared, no data here" instead of "does not exist"."""
    svc = _svc(
        monkeypatch,
        {
            "Widget": _chunk("Widget", [1.0, 0.0, 0.0]),
            "Sprocket": _chunk("Sprocket", [0.0, 1.0, 0.0]),
            "Gadget": _chunk("Gadget", [0.0, 0.0, 1.0]),
        },
        question_vec=[1.0, 0.0, 0.0],  # closest to Widget
    )
    text = await svc.retrieve(
        GRAPH, "list all Sprockets", top_k=1, active_types={"Widget"}
    )
    parsed = _type_lines(text)
    assert "Sprocket" in parsed, "named type must be force-included despite demotion"
    assert parsed["Sprocket"] is True
    assert parsed["Widget"] is False


async def test_expansion_prefers_in_kg_neighbours(monkeypatch):
    """The 1-hop expansion cap must spend its slots on neighbours that exist in
    this KG before a sibling KG's neighbour."""
    svc = _svc(
        monkeypatch,
        {
            # Widget points at both an in-KG and an out-of-KG neighbour.
            "Widget": _chunk("Widget", [1.0, 0.0, 0.0], targets=["Sprocket", "Gadget"]),
            "Sprocket": _chunk("Sprocket", [0.0, 1.0, 0.0]),
            "Gadget": _chunk("Gadget", [0.0, 0.0, 1.0]),
        },
        question_vec=[1.0, 0.0, 0.0],
    )
    # top_k=1 => 1 selected + max_total 2 => exactly one expansion slot.
    text = await svc.retrieve(
        GRAPH, "any question", top_k=1, active_types={"Widget", "Gadget"}
    )
    parsed = _type_lines(text)
    assert set(parsed) == {"Widget", "Gadget"}, "expansion slot went to the foreign type"


def test_mark_no_instances_rides_the_header_and_is_idempotent():
    chunk = "Type: Widget — URI: <x>\n  Attributes: code (string)"
    marked = _mark_no_instances(chunk)
    assert marked.splitlines()[0].endswith(NO_INSTANCES_MARK)
    assert marked.splitlines()[1] == "  Attributes: code (string)"
    assert _mark_no_instances(marked) == marked


# --------------------------------------------------------------------------- #
# ONTA-411: the shared active-type probe
# --------------------------------------------------------------------------- #


class ProbeNeptune:
    """Serves the active-types probe and counts how often it is asked."""

    def __init__(self, active=("Widget",)):
        self.active = tuple(active)
        self.probe_count = 0

    async def query(self, sparql: str):
        if "SELECT DISTINCT ?type" in sparql:
            self.probe_count += 1
            return {
                "head": {"vars": ["type"]},
                "results": {
                    "bindings": [
                        {"type": {"type": "uri", "value": f"{TYPES}{t}"}} for t in self.active
                    ]
                },
            }
        return {"head": {"vars": []}, "results": {"bindings": []}}


def _pipe(neptune):
    return NLQueryPipeline(neptune, anthropic_key="dummy")


async def test_active_types_probes_a_kg_graph():
    neptune = ProbeNeptune(active=("Widget", "Gadget"))
    assert await _pipe(neptune)._active_types(KG, GRAPH) == {"Widget", "Gadget"}
    assert neptune.probe_count == 1


async def test_active_types_is_none_when_there_is_nothing_to_scope():
    """No instance graph, or the instance graph IS the ontology graph => every
    declared type is in scope, so no probe and no demotion."""
    neptune = ProbeNeptune()
    pipe = _pipe(neptune)
    assert await pipe._active_types(None, GRAPH) is None
    assert await pipe._active_types(GRAPH, GRAPH) is None
    assert neptune.probe_count == 0


async def test_active_types_is_ttl_cached():
    neptune = ProbeNeptune()
    pipe = _pipe(neptune)
    await pipe._active_types(KG, GRAPH)
    await pipe._active_types(KG, GRAPH)
    assert neptune.probe_count == 1, "the probe must not repeat within the TTL"
    # A DIFFERENT KG is a different cache entry, not a stale hit.
    await pipe._active_types(SIBLING_KG, GRAPH)
    assert neptune.probe_count == 2


class SchemaNeptune(ProbeNeptune):
    """ProbeNeptune plus a one-type ontology schema so `_fetch_ontology` runs end
    to end. ``active`` is reassignable, standing in for an ingest landing."""

    ONTOLOGY_ROWS = [
        {
            "type": {"type": "uri", "value": f"{TYPES}Widget"},
            "typeLabel": {"type": "literal", "value": "Widget"},
            "attr": {"type": "uri", "value": f"{TYPES}Widget/attrs/serial"},
            "attrLabel": {"type": "literal", "value": "serial"},
            "range": {"type": "uri", "value": "http://www.w3.org/2001/XMLSchema#string"},
        }
    ]

    async def query(self, sparql: str):
        if "?typeLabel" in sparql:
            return {
                "head": {"vars": ["type", "typeLabel", "attr", "attrLabel", "range"]},
                "results": {"bindings": self.ONTOLOGY_ROWS},
            }
        if "COUNT(DISTINCT ?val)" in sparql:
            return {
                "head": {"vars": ["cnt"]},
                "results": {"bindings": [{"cnt": {"type": "literal", "value": "3"}}]},
            }
        if "SELECT DISTINCT ?val" in sparql:
            return {
                "head": {"vars": ["val"]},
                "results": {"bindings": [{"val": {"type": "literal", "value": "alpha"}}]},
            }
        return await super().query(sparql)


async def test_an_empty_probe_is_never_served_from_the_cache():
    """An empty result is the "might be mid-ingest" case. Caching it would make a
    freshly-populated KG keep answering "empty" for a whole TTL on any worker
    that did not run the write, and re-probing an empty graph is the cheapest
    query there is."""
    neptune = ProbeNeptune(active=())
    pipe = _pipe(neptune)
    assert await pipe._active_types(KG, GRAPH) == set()
    assert await pipe._active_types(KG, GRAPH) == set()
    assert neptune.probe_count == 2, "an empty probe must be retried, not cached"


async def test_a_kg_populated_after_an_empty_ask_recovers_within_the_ttl():
    """End-to-end: ask while the KG is empty, an ingest lands elsewhere, ask
    again inside the TTL. `_fetch_ontology` deliberately does NOT cache
    ONTOLOGY_EMPTY so the next ask re-reads; a cached empty PROBE would have
    reinstated the staleness one layer down."""
    _ontology_cache.clear()
    neptune = SchemaNeptune(active=())
    pipe = _pipe(neptune)

    assert await pipe._fetch_ontology(GRAPH, KG) == ONTOLOGY_EMPTY

    neptune.active = ("Widget",)
    summary = await pipe._fetch_ontology(GRAPH, KG)
    assert summary != ONTOLOGY_EMPTY, "stale empty probe survived the ingest"
    assert "Widget" in summary


async def test_invalidate_cache_clears_active_types_for_the_tenant():
    """A write that adds instances must not keep demoting the new types for the
    rest of the TTL. `invalidate_cache` runs after every converged write."""
    neptune = ProbeNeptune()
    pipe = _pipe(neptune)
    await pipe._active_types(KG, GRAPH)
    assert KG in _active_types_cache
    NLQueryPipeline.invalidate_cache(GRAPH)
    assert KG not in _active_types_cache


# --------------------------------------------------------------------------- #
# ONTA-411: ask() threads the scope into semantic retrieval
# --------------------------------------------------------------------------- #


class _RecordingService:
    """Stand-in for OntologyEmbeddingService that records the retrieve() call."""

    def __init__(self):
        self.calls: list[dict] = []

    async def retrieve(self, graph_uri, question, top_k=15, active_types=None):
        self.calls.append({"graph_uri": graph_uri, "active_types": active_types})
        return f"Type: Widget — URI: <{TYPES}Widget>"


async def _ask_with_recorder(monkeypatch, neptune, instance_graph):
    svc = _RecordingService()
    monkeypatch.setattr("cograph_client.nlp.pipeline.get_embedding_service", lambda: svc)
    pipe = _pipe(neptune)

    async def fake_generate(question, ontology, graph_uri="", **kwargs):
        return {
            "sparql": f"SELECT ?s FROM <{graph_uri}> WHERE {{ ?s <{RDF_TYPE}> <{TYPES}Widget> }}",
            "explanation": "",
            "functions_needed": [],
        }

    async def fake_rephrase(question, bindings, max_rows=None):
        return ""

    monkeypatch.setattr(pipe, "_generate_sparql", fake_generate)
    monkeypatch.setattr(pipe, "_rephrase_via_openrouter", fake_rephrase)
    result = await pipe.ask("any question", GRAPH, instance_graph=instance_graph)
    return svc, result


async def test_ask_scopes_semantic_retrieval_to_the_target_kg(monkeypatch):
    _ontology_cache.clear()
    svc, result = await _ask_with_recorder(
        monkeypatch, ProbeNeptune(active=("Widget",)), KG
    )
    assert svc.calls, "semantic retrieval was never called"
    assert svc.calls[0]["active_types"] == {"Widget"}
    # The ontology store is still read from the TENANT graph (that is where the
    # embeddings live); only the RANKING is KG-scoped.
    assert svc.calls[0]["graph_uri"] == GRAPH
    assert result.timing.get("ontology_source") == "semantic"
    assert result.timing.get("ontology_scope") == "kg"


async def test_ask_degrades_to_unscoped_retrieval_when_the_probe_fails(monkeypatch):
    """A probe failure must cost the SCOPE, not the semantic subset. The
    pre-ONTA-411 behaviour is the degraded mode, not an error."""
    _ontology_cache.clear()

    class BrokenProbe(ProbeNeptune):
        async def query(self, sparql: str):
            if "SELECT DISTINCT ?type" in sparql:
                raise RuntimeError("neptune throttled")
            return await super().query(sparql)

    svc, result = await _ask_with_recorder(monkeypatch, BrokenProbe(), KG)
    assert svc.calls[0]["active_types"] is None
    assert result.timing.get("ontology_source") == "semantic"
    assert result.timing.get("ontology_scope") == "tenant"


async def test_ask_without_a_kg_is_not_scoped(monkeypatch):
    """Asking against the bare tenant graph: no KG to scope to, no demotion."""
    _ontology_cache.clear()
    svc, _ = await _ask_with_recorder(monkeypatch, ProbeNeptune(), None)
    assert svc.calls[0]["active_types"] is None


# --------------------------------------------------------------------------- #
# ONTA-417 half A: the prompt names the target graph
# --------------------------------------------------------------------------- #


def test_prompt_names_the_target_kg():
    prompt = build_generation_prompt(
        "any question", "Type: Widget", graph_uri=KG, kg_name="TargetKG"
    )
    assert "Target knowledge graph: TargetKG" in prompt
    # And explains that the schema spans the whole tenant, which is WHY a
    # [no instances] entry may belong to a different graph entirely.
    low = prompt.lower()
    assert "tenant" in low and "shared across" in low


def test_prompt_without_a_kg_name_is_unchanged():
    """A non-KG graph (bare tenant/ontology graph) adds no header."""
    prompt = build_generation_prompt("any question", "Type: Widget", graph_uri=GRAPH)
    assert prompt.startswith("Ontology schema:")
    assert "Target knowledge graph" not in prompt


def test_system_prompt_rule_is_cross_kg_aware():
    """The rule must explain that the ontology is tenant-wide, so a marked entry
    may belong to another of the tenant's graphs rather than being empty here."""
    p = SPARQL_GENERATION_SYSTEM.lower()
    assert "[no instances]" in p
    assert "tenant" in p
    assert "shared across every knowledge graph" in p


def test_system_prompt_keeps_the_onta258_absolutes():
    """The cross-KG rewrite must not LOOSEN ONTA-258. The prohibition on
    substituting a populated type is absolute, and the honest-zero-row guarantee
    covers every declared-but-empty target, not just explicitly-named ones."""
    p = SPARQL_GENERATION_SYSTEM
    assert "NEVER silently substitute a different, populated type" in p
    # No licence to swap the target as long as the swap is narrated: a narrated
    # substitution still answers a different question, which IS the ONTA-258 harm.
    low = p.lower()
    assert "substituting is allowed" not in low
    assert (
        "a zero-row answer for a declared-but-empty target is the correct, "
        "honest answer" in low
    )
    assert "explicitly-requested declared-but-empty" not in low
    assert "does not exist" in low


def test_system_prompt_states_no_false_premise_for_a_single_kg_tenant():
    """A single-KG tenant's "[no instances]" means exactly what it meant
    pre-ONTA-411 ("declared here, no data yet"), and SPARQL_GENERATION_SYSTEM is
    unconditional, so the rule must not ASSERT that a marked entry belongs to
    another graph, and the populated-type preference must be scoped to questions
    that name no target."""
    low = SPARQL_GENERATION_SYSTEM.lower()
    # Conditional framing ("may ... or may ..."), never an assertion of fact.
    assert "most often because it belongs" not in low
    assert "may belong" in low
    # The preference survives, but only as an open-ended tie-break.
    assert "only when the question names no specific type" in low
    assert "never a licence to redirect" in low


@pytest.mark.parametrize(
    "graph_uri,expected",
    [
        (KG, "TargetKG"),
        (GRAPH, ""),  # bare tenant graph => no KG header
    ],
)
async def test_generate_sparql_derives_kg_name_from_the_graph_uri(
    monkeypatch, graph_uri, expected
):
    pipe = _pipe(ProbeNeptune())
    captured: dict[str, str] = {}

    async def capture(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return {"sparql": "", "explanation": "", "functions_needed": []}

    for method in ("_generate_via_cerebras", "_generate_via_openrouter", "_generate_via_anthropic"):
        monkeypatch.setattr(pipe, method, capture)

    await pipe._generate_sparql("any question", "Type: Widget", graph_uri)
    if expected:
        assert f"Target knowledge graph: {expected}" in captured["prompt"]
    else:
        assert "Target knowledge graph" not in captured["prompt"]


# --------------------------------------------------------------------------- #
# ONTA-417 half B: delete_kg evicts the tenant's ontology caches
# --------------------------------------------------------------------------- #


def test_delete_kg_evicts_ontology_and_active_type_caches(client, mock_neptune, auth_headers):
    """A deleted KG's schema must stop competing for semantic-retrieval slots,
    and a KG recreated under the same name must not inherit the dead one's
    cached scope."""
    tenant = "test-tenant"
    kg = "TargetKG"
    base = f"https://cograph.tech/graphs/{tenant}"
    instance = f"{base}/kg/{kg}"

    _ontology_cache[f"{base}|{instance}|"] = ("Type: Widget", 1e12)
    _active_types_cache[instance] = ({"Widget"}, 1e12)

    resp = client.delete(f"/graphs/{tenant}/kgs/{kg}", headers=auth_headers)
    assert resp.status_code == 200

    assert f"{base}|{instance}|" not in _ontology_cache
    assert instance not in _active_types_cache
