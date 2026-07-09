"""ONTA-258: a DECLARED-but-unpopulated type must stay VISIBLE to the SPARQL-
generation LLM.

Root cause: `_fetch_ontology` filtered the ontology summary down to types that
currently have instances (`active_types`) and SKIPPED every declared type absent
from that set. A declared-but-empty type became indistinguishable from a
nonexistent one, so the LLM claimed the type "does not exist" (or silently
queried the closest wrong type) instead of returning an honest zero-row answer.

The fix mirrors the ONTA-248 treatment of declared-but-empty ATTRIBUTES /
RELATIONSHIPS: keep the declared thing, annotate it "[no instances]", never drop
it. These tests assert the MECHANISM on an INVENTED ontology (Widget / Sprocket /
Gadget) so they hold for any domain — no persona/domain tokens.
"""

from __future__ import annotations

import re

import pytest

from cograph_client.nlp.pipeline import (
    NLQueryPipeline,
    ONTOLOGY_EMPTY,
    ONTOLOGY_FETCH_ERROR,
    _ontology_cache,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TYPES = "https://cograph.tech/types/"
ONTO = "https://cograph.tech/onto/"
STR = "http://www.w3.org/2001/XMLSchema#string"
INT = "http://www.w3.org/2001/XMLSchema#integer"
FLOAT = "http://www.w3.org/2001/XMLSchema#float"

GRAPH = "https://cograph.tech/graphs/inv-tenant"
KG = "https://cograph.tech/graphs/inv-tenant/kg/InventedKG"


def _row(**cells):
    return {k: {"type": "uri", "value": v} for k, v in cells.items()}


def _results(rows):
    vars_ = sorted({k for r in rows for k in r})
    return {"head": {"vars": vars_}, "results": {"bindings": rows}}


class WidgetNeptune:
    """Routes SPARQL by shape for an INVENTED 3-type ontology.

    Declares Widget{serial, color} —pairs_with→ Sprocket, Sprocket{torque}
    —mounts→ Gadget, Gadget{weight}. Only Widget has instances: the active-types
    probe returns Widget alone, so Sprocket and Gadget are DECLARED-but-empty.

    Records every query so a test can assert that NO cardinality COUNT is issued
    against an empty type's predicates (the empty-type probes are skipped).
    """

    ONTOLOGY_ROWS = [
        _row(type=f"{TYPES}Widget", typeLabel="Widget",
             attr=f"{TYPES}Widget/attrs/serial", attrLabel="serial", range=STR),
        _row(type=f"{TYPES}Widget", typeLabel="Widget",
             attr=f"{TYPES}Widget/attrs/color", attrLabel="color", range=STR),
        _row(type=f"{TYPES}Widget", typeLabel="Widget",
             attr=f"{TYPES}Widget/attrs/pairs_with", attrLabel="pairs_with",
             range=f"{TYPES}Sprocket"),
        _row(type=f"{TYPES}Sprocket", typeLabel="Sprocket",
             attr=f"{TYPES}Sprocket/attrs/torque", attrLabel="torque", range=INT),
        _row(type=f"{TYPES}Sprocket", typeLabel="Sprocket",
             attr=f"{TYPES}Sprocket/attrs/mounts", attrLabel="mounts",
             range=f"{TYPES}Gadget"),
        _row(type=f"{TYPES}Gadget", typeLabel="Gadget",
             attr=f"{TYPES}Gadget/attrs/weight", attrLabel="weight", range=FLOAT),
    ]

    def __init__(self, *, active=("Widget",), count_value=5, pred_map=None):
        self.active = tuple(active)
        self.count_value = count_value
        # Instance-graph predicates per type leaf, served to the schema-missing
        # fallback's `SELECT DISTINCT ?p` probe (used by the disjoint test).
        self.pred_map: dict[str, list[str]] = dict(pred_map or {})
        self.queries: list[str] = []

    async def query(self, sparql: str):
        self.queries.append(sparql)
        s = sparql
        # Active-types probe.
        if "SELECT DISTINCT ?type" in s and "rdf-syntax-ns#type" in s:
            return _results([_row(type=f"{TYPES}{t}") for t in self.active])
        # Full-ontology schema query.
        if "?typeLabel" in s:
            return _results(self.ONTOLOGY_ROWS)
        # Schema-missing fallback: predicates used on a type's instances.
        if "SELECT DISTINCT ?p" in s:
            for leaf, preds in self.pred_map.items():
                if f"<{TYPES}{leaf}>" in s:
                    return _results([_row(p=p) for p in preds])
            return _results([])
        # Cardinality COUNT(DISTINCT ?val).
        if "COUNT(DISTINCT ?val)" in s:
            return _results([{"cnt": {"type": "literal", "value": str(self.count_value)}}])
        # Enum value fetch.
        if "SELECT DISTINCT ?val" in s:
            return _results([{"val": {"type": "literal", "value": "alpha"}}])
        # Anything else (e.g. a generated ask() SELECT) → zero rows.
        return _results([])


def _pipe(neptune):
    return NLQueryPipeline(neptune, anthropic_key="dummy")


def _parse_types(summary: str) -> dict[str, bool]:
    """Map declared type name -> whether its header is annotated [no instances]."""
    out: dict[str, bool] = {}
    for line in summary.splitlines():
        m = re.match(r"Type: (\w+) ", line)
        if m:
            out[m.group(1)] = "[no instances]" in line
    return out


# --------------------------------------------------------------------------- #
# _fetch_ontology mechanism
# --------------------------------------------------------------------------- #

async def test_declared_empty_types_kept_and_annotated():
    """Sprocket/Gadget are declared but have no instances — they must appear,
    annotated [no instances], NOT be dropped (ONTA-258)."""
    _ontology_cache.clear()
    summary = await _pipe(WidgetNeptune(active=("Widget",)))._fetch_ontology(GRAPH, KG)

    assert summary not in (ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR)
    parsed = _parse_types(summary)
    # Every DECLARED type is present — none is invisible / "absent".
    assert set(parsed) == {"Widget", "Sprocket", "Gadget"}
    # The populated type is NOT annotated; the empty ones ARE.
    assert parsed["Widget"] is False
    assert parsed["Sprocket"] is True
    assert parsed["Gadget"] is True
    # Concretely: the annotation rides on the type header.
    assert "Type: Sprocket" in summary and "[no instances]" in summary
    assert "Type: Gadget" in summary


async def test_empty_type_schema_still_shown():
    """A declared-but-empty type still exposes its declared attributes so the LLM
    can write a valid (zero-row) query against it, not invent columns."""
    _ontology_cache.clear()
    summary = await _pipe(WidgetNeptune(active=("Widget",)))._fetch_ontology(GRAPH, KG)
    # Sprocket's declared attribute is visible.
    assert "torque" in summary
    # Gadget's declared attribute is visible.
    assert "weight" in summary


async def test_no_cardinality_probes_for_empty_types():
    """Empty types have zero instances by definition — the fix must NOT issue a
    cardinality COUNT against their predicates (no wasted Neptune round-trips)."""
    _ontology_cache.clear()
    neptune = WidgetNeptune(active=("Widget",))
    await _pipe(neptune)._fetch_ontology(GRAPH, KG)
    counts = [q for q in neptune.queries if "COUNT(DISTINCT ?val)" in q]
    # Widget (populated) attrs/rel MAY be probed; empty types must NOT be.
    for q in counts:
        assert "Sprocket/attrs" not in q, "empty type Sprocket should not be probed"
        assert "Gadget/attrs" not in q, "empty type Gadget should not be probed"


async def test_all_populated_types_not_annotated():
    """Control: when every declared type has instances, none is annotated empty
    (no false [no instances] on populated types)."""
    _ontology_cache.clear()
    neptune = WidgetNeptune(active=("Widget", "Sprocket", "Gadget"))
    summary = await _pipe(neptune)._fetch_ontology(GRAPH, KG)
    parsed = _parse_types(summary)
    assert set(parsed) == {"Widget", "Sprocket", "Gadget"}
    assert not any(parsed.values()), "no populated type should carry [no instances]"


async def test_mechanism_empty_set_equals_declared_minus_active():
    """Generic mechanism: the set of types annotated [no instances] is exactly
    (declared − active), for ANY active subset — not tied to this example."""
    for active in (("Widget",), ("Sprocket",), ("Widget", "Gadget")):
        _ontology_cache.clear()
        summary = await _pipe(WidgetNeptune(active=active))._fetch_ontology(GRAPH, KG)
        parsed = _parse_types(summary)
        declared = {"Widget", "Sprocket", "Gadget"}
        annotated_empty = {t for t, empty in parsed.items() if empty}
        assert set(parsed) == declared, f"a declared type went missing for active={active}"
        assert annotated_empty == declared - set(active), (
            f"empty set != declared−active for active={active}"
        )


# --------------------------------------------------------------------------- #
# disjoint active_matched == 0 fallback (ONTA-248-preserving)
# --------------------------------------------------------------------------- #

async def test_disjoint_instance_types_route_to_instance_fallback():
    """When the instance graph reports types but NONE overlap the DECLARED
    ontology (active_matched == 0), _fetch_ontology must route to the
    instance-derived fallback — exactly as the old `if not types:` guard did —
    NOT render an all-[no instances] summary of the disjoint declared types.

    This locks in the ONTA-248-preserving behavior: keeping declared-but-empty
    types visible (ONTA-258) must NOT swallow the schema-missing fallback."""
    _ontology_cache.clear()
    # Instance graph reports only "Gizmo" — a type the declared ontology
    # (Widget/Sprocket/Gadget) does NOT contain. So declared ∩ active = ∅.
    neptune = WidgetNeptune(
        active=("Gizmo",),
        pred_map={"Gizmo": [f"{TYPES}Gizmo/attrs/spin", f"{ONTO}links"]},
    )
    summary = await _pipe(neptune)._fetch_ontology(GRAPH, KG)

    # Routed to the schema-missing, instance-derived fallback (its diagnostic
    # prefix), surfacing the ACTUAL instance type + its probed predicates.
    assert "has not been written yet" in summary
    assert "Type: Gizmo" in summary
    assert "spin" in summary  # instance-derived attribute predicate
    assert "links" in summary  # instance-derived relationship predicate

    # The disjoint DECLARED types are NOT rendered, and nothing is falsely
    # annotated "[no instances]" — an all-empty declared summary would be worse
    # than the instance-derived fallback (the whole point of active_matched==0).
    assert "[no instances]" not in summary
    for t in ("Widget", "Sprocket", "Gadget"):
        assert f"Type: {t}" not in summary

    # ONTA-248 behavior preserved: this path issues ZERO cardinality COUNT probes.
    assert not any("COUNT(DISTINCT ?val)" in q for q in neptune.queries)


# --------------------------------------------------------------------------- #
# ask() end-to-end: honest zero-row answer, no substitution, no "absent" claim
# --------------------------------------------------------------------------- #

async def test_ask_declared_empty_type_is_honest(monkeypatch):
    """`ask('list all Sprockets')` must see Sprocket [no instances] in the
    ontology it feeds the LLM, run a Sprocket query, and return 0 rows — never
    claiming Sprocket is absent nor substituting the populated Widget type."""
    _ontology_cache.clear()
    # Keep the full (non-semantic) ontology path so _fetch_ontology runs.
    monkeypatch.setattr("cograph_client.nlp.pipeline.get_embedding_service", lambda: None)

    pipe = _pipe(WidgetNeptune(active=("Widget",)))

    captured = {}

    async def fake_generate(question, ontology, graph_uri="", error_feedback="", examples_text=""):
        captured["ontology"] = ontology
        # A well-behaved LLM, given "Sprocket [no instances]", queries Sprocket
        # honestly instead of substituting Widget or denying the type.
        return {
            "sparql": (
                f"SELECT ?s FROM <{KG}> WHERE {{ ?s <{RDF_TYPE}> <{TYPES}Sprocket> }}"
            ),
            "explanation": "Sprocket is declared in the ontology but has no instances yet.",
            "functions_needed": [],
        }

    async def fake_rephrase(question, bindings, max_rows=None):
        return ""

    monkeypatch.setattr(pipe, "_generate_sparql", fake_generate)
    monkeypatch.setattr(pipe, "_rephrase_via_openrouter", fake_rephrase)

    result = await pipe.ask("list all Sprockets", GRAPH, instance_graph=KG)

    # The ontology the LLM actually saw exposes Sprocket as declared-but-empty.
    assert "Sprocket" in captured["ontology"]
    assert "[no instances]" in captured["ontology"]
    assert "Sprocket" in result.ontology

    # The query ran against Sprocket (NOT substituted to the populated Widget).
    assert f"{TYPES}Sprocket" in result.sparql
    assert f"{TYPES}Widget" not in result.sparql

    # Zero rows, and no "does not exist" claim leaked into the answer/explanation.
    assert result.answer == "No results found."
    combined = (result.answer + " " + (result.explanation or "")).lower()
    assert "does not exist" not in combined
    assert "not in the schema" not in combined


# --------------------------------------------------------------------------- #
# generation prompt guidance (step 3)
# --------------------------------------------------------------------------- #

def test_generation_prompt_teaches_no_instances_handling():
    """The SPARQL-generation system prompt must explain how to handle a
    "[no instances]" target: query it honestly (zero rows), never claim it is
    absent, never substitute a populated type."""
    from cograph_client.nlp.prompts import SPARQL_GENERATION_SYSTEM

    p = SPARQL_GENERATION_SYSTEM.lower()
    assert "[no instances]" in p
    # Forbids the two failure modes from the ticket.
    assert "does not exist" in p or 'not in the schema' in p
    assert "substitute" in p
