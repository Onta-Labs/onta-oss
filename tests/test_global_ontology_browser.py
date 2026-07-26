"""Operator-only Global ontology browser — GET /operator/ontology/global.

The fixtures below are the triple shapes
``cograph/governance/writer.py::GlobalShapeWriter.write_approved_shape`` emits
(``rdf:type rdfs:Class`` + ``rdfs:label`` + optional type ``rdfs:comment``; per
slot ``rdf:Property`` + ``rdfs:label`` + ``rdfs:domain`` + ``rdfs:range`` +
``onto/coreSlot "true"^^xsd:boolean`` + ``rdfs:comment`` = the slot rationale,
under ``types/public/<T>/attrs/<slot>``). The premium module is NOT imported
(OSS boundary) — its shapes are transcribed here with this note.

SCOPE — what this file does NOT prove. ``_rows_for`` is a hand-rolled
reproduction of ``full_ontology_detail_query``'s semantics, not a SPARQL
engine. It therefore validates the READER's folding, classification, ordering
and degradation logic against writer-shaped data, but it structurally CANNOT
catch a divergence between the actual SPARQL text and what the reader expects
of it — change the query's projection or patterns and these tests keep passing.
That gap was closed once out-of-band (the reader was run against real
``GlobalShapeWriter`` output through pyoxigraph and round-tripped correctly,
typed boolean included); re-do that by hand if the query changes materially.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cograph_client.api.deps import get_neptune_client
from cograph_client.api.routes import operator as operator_routes
from cograph_client.auth import api_keys
from cograph_client.auth.api_keys import TenantContext
from cograph_client.graph.layers import enhanced_graph_uri, public_graph_uri

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns"
RDFS = "http://www.w3.org/2000/01/rdf-schema"
XSD = "http://www.w3.org/2001/XMLSchema"
ONTO = "https://cograph.tech/onto"
PUB = "https://cograph.tech/types/public"
ENH = "https://cograph.tech/types/x"


# --- writer-shaped fixture builders -----------------------------------------


def shape_triples(
    type_ns: str,
    name: str,
    comment: str | None = None,
    parent_uri: str | None = None,
    slots: list[dict] | None = None,
) -> list[tuple[str, str, str]]:
    """Emit the triples GlobalShapeWriter.write_approved_shape would write."""
    t_uri = f"{type_ns}/{name}"
    triples = [
        (t_uri, f"{RDF}#type", f"{RDFS}#Class"),
        (t_uri, f"{RDFS}#label", name),
    ]
    if comment:
        triples.append((t_uri, f"{RDFS}#comment", comment))
    if parent_uri:
        # Both governance writers DO emit rdfs:subClassOf (the premium
        # GlobalShapeWriter gained it with ancestor synthesis; the OSS writer
        # always had it), so this is the real written shape, not a
        # reader-only affordance. The comment here used to claim the opposite —
        # it was written before the premium writer emitted the edge and went
        # stale; do not reintroduce that claim.
        triples.append((t_uri, f"{RDFS}#subClassOf", parent_uri))
    for slot in slots or []:
        a_uri = f"{t_uri}/attrs/{slot['name']}"
        triples += [
            (a_uri, f"{RDF}#type", f"{RDF}#Property"),
            (a_uri, f"{RDFS}#label", slot["name"]),
            (a_uri, f"{RDFS}#domain", t_uri),
            (a_uri, f"{RDFS}#range", slot.get("range", f"{XSD}#string")),
        ]
        if slot.get("core", True):
            # The writer emits a TYPED literal; a SPARQL JSON result carries the
            # lexical form ("true") plus a datatype the parser drops. Seeded in
            # the writer's own shape so the marker is never accidentally
            # exercised only in its plain-literal form.
            triples.append((a_uri, f"{ONTO}/coreSlot", f'"true"^^{XSD}#boolean'))
        if slot.get("why"):
            triples.append((a_uri, f"{RDFS}#comment", slot["why"]))
    return triples


def function_triples(
    type_ns: str,
    type_name: str,
    name: str,
    description: str | None = None,
    endpoint: str | None = None,
) -> list[tuple[str, str, str]]:
    """The triples ``queries.register_function_triple`` writes, but attached to a
    LAYER-QUALIFIED type URI — the shape a future global-layer function writer
    must emit for this read path to see it. ``register_function_triple`` itself
    still mints the BARE tenant ``types/<T>`` URI, which is exactly why no
    function surfaces here today."""
    f_uri = f"https://cograph.tech/functions/{name}"
    t_uri = f"{type_ns}/{type_name}"
    triples = [
        (f_uri, f"{ONTO}/attachedTo", t_uri),
        (f_uri, f"{ONTO}/name", name),
    ]
    if endpoint:
        triples.append((f_uri, f"{ONTO}/endpointUrl", endpoint))
    if description:
        triples.append((f_uri, f"{ONTO}/description", description))
    return triples


# --- a minimal evaluator of full_ontology_detail_query ----------------------


def _rows_for(triples: list[tuple[str, str, str]]) -> list[dict]:
    """Reproduce full_ontology_detail_query's row semantics over `triples`."""
    def objs(s, p):
        return [o for (ss, pp, o) in triples if ss == s and pp == p]

    rows: list[dict] = []
    classes = [s for (s, p, o) in triples if p == f"{RDF}#type" and o == f"{RDFS}#Class"]
    for t in sorted(set(classes)):
        labels = objs(t, f"{RDFS}#label")
        if not labels:
            continue  # label is a required pattern, not OPTIONAL
        comments = objs(t, f"{RDFS}#comment") or [None]
        parents = objs(t, f"{RDFS}#subClassOf") or [None]
        attrs = sorted({s for (s, p, o) in triples if p == f"{RDFS}#domain" and o == t})
        attr_rows: list[dict] = []
        for a in attrs:
            a_labels = objs(a, f"{RDFS}#label")
            if not a_labels:
                continue
            attr_rows.append({
                "attr": a,
                "attrLabel": a_labels[0],
                "attrComment": (objs(a, f"{RDFS}#comment") or [None])[0],
                "range": (objs(a, f"{RDFS}#range") or [None])[0],
                "core": (objs(a, f"{ONTO}/coreSlot") or [None])[0],
            })
        if not attr_rows:
            attr_rows = [{}]  # the attribute block is OPTIONAL
        funcs = sorted({s for (s, p, o) in triples if p == f"{ONTO}/attachedTo" and o == t})
        func_rows: list[dict] = []
        for f in funcs:
            f_names = objs(f, f"{ONTO}/name")
            if not f_names:
                continue  # onto/name is a required pattern inside the block
            func_rows.append({
                "funcName": f_names[0],
                "funcDesc": (objs(f, f"{ONTO}/description") or [None])[0],
                "funcEndpoint": (objs(f, f"{ONTO}/endpointUrl") or [None])[0],
            })
        if not func_rows:
            func_rows = [{}]  # the function block is OPTIONAL too
        for label in labels:
            for comment in comments:
                for parent in parents:
                    # The two OPTIONAL blocks are independent, so the engine
                    # returns their CROSS PRODUCT — reproduce that, since the
                    # reader's fold has to be idempotent under the repetition.
                    for ar in attr_rows:
                        for fr in func_rows:
                            row = {"type": t, "typeLabel": label}
                            if comment is not None:
                                row["typeComment"] = comment
                            if parent is not None:
                                row["parent"] = parent
                            for k, v in list(ar.items()) + list(fr.items()):
                                if v is not None:
                                    row[k] = v
                            rows.append(row)
    return rows


def _binding(value: str) -> dict:
    """SPARQL-JSON binding for one value.

    A typed literal (``"true"^^<dt>``) is split the way a real engine reports
    it: ``value`` carries only the LEXICAL form and the datatype rides in its
    own key — which is exactly why ``parse_sparql_results`` (which keeps only
    ``value``) hands the reader a bare ``"true"``.
    """
    if value.startswith('"') and '"^^' in value:
        literal, datatype = value[1:].split('"^^', 1)
        return {"type": "literal", "value": literal, "datatype": datatype}
    if value.startswith("http"):
        return {"type": "uri", "value": value}
    return {"type": "literal", "value": value}


def _sparql_json(rows: list[dict]) -> dict:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    return {
        "head": {"vars": keys},
        "results": {
            "bindings": [{k: _binding(v) for k, v in r.items()} for r in rows]
        },
    }


class FakeNeptune:
    """Dispatches on the FROM <graph> in the query; raises for `failing` graphs."""

    def __init__(self, by_graph: dict[str, list[tuple[str, str, str]]], failing=()):
        self.by_graph = by_graph
        self.failing = set(failing)
        self.queries: list[str] = []

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        for graph in list(self.by_graph) + list(self.failing):
            if f"FROM <{graph}>" in sparql:
                if graph in self.failing:
                    raise RuntimeError(f"graph unreachable: {graph}")
                return _sparql_json(_rows_for(self.by_graph[graph]))
        return _sparql_json([])


def _app(neptune, is_operator: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(operator_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: neptune
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: TenantContext(
            tenant_id="demo-tenant", api_key="k", is_operator=is_operator
        )
    )
    return TestClient(app)


# --- gating ------------------------------------------------------------------


def test_non_operator_gets_403():
    client = _app(FakeNeptune({}), is_operator=False)
    r = client.get("/operator/ontology/global")
    assert r.status_code == 403
    assert r.json()["detail"] == "operator only"


def test_operator_gets_200():
    client = _app(FakeNeptune({public_graph_uri(): [], enhanced_graph_uri(): []}))
    assert client.get("/operator/ontology/global").status_code == 200


# --- empty global ontology (today's expected state) --------------------------


def test_empty_global_graphs_return_200_with_no_types():
    neptune = FakeNeptune({public_graph_uri(): [], enhanced_graph_uri(): []})
    r = _app(neptune).get("/operator/ontology/global")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["types"] == []
    assert [layer["layer"] for layer in body["layers"]] == ["public", "enhanced"]
    assert [layer["graph_uri"] for layer in body["layers"]] == [
        public_graph_uri(),
        enhanced_graph_uri(),
    ]
    assert all(layer["type_count"] == 0 for layer in body["layers"])
    assert all(layer["available"] is True for layer in body["layers"])
    # One batched query per layer — no N+1 per-type round trips.
    assert len(neptune.queries) == 2


# --- a seeded global ontology round-trips ------------------------------------


def _seeded() -> FakeNeptune:
    public = (
        shape_triples(
            PUB,
            "Organization",
            comment="An organized body of people with a particular purpose.",
            parent_uri=f"{PUB}/Thing",
            slots=[
                {
                    "name": "legalName",
                    "range": f"{XSD}#string",
                    "why": "Registered legal name.",
                },
                {
                    "name": "headquarters",
                    "range": f"{PUB}/Place",
                    "why": "Primary office location.",
                    "core": False,
                },
                {"name": "employeeCount", "range": f"{XSD}#integer"},
            ],
        )
        + shape_triples(PUB, "Thing")
        + shape_triples(PUB, "Place", slots=[{"name": "address"}])
        + shape_triples(PUB, "Hospital", parent_uri=f"{PUB}/Organization")
    )
    enhanced = shape_triples(
        ENH,
        "Clinic",
        comment="A premium-layer refinement.",
        parent_uri=f"{PUB}/Organization",
        slots=[{"name": "npi", "range": f"{XSD}#string", "why": "National Provider Id."}],
    )
    return FakeNeptune({public_graph_uri(): public, enhanced_graph_uri(): enhanced})


@pytest.fixture
def seeded_body():
    r = _app(_seeded()).get("/operator/ontology/global")
    assert r.status_code == 200, r.text
    return r.json()


def test_layer_counts_and_availability(seeded_body):
    layers = {layer["layer"]: layer for layer in seeded_body["layers"]}
    assert layers["public"]["type_count"] == 4
    assert layers["enhanced"]["type_count"] == 1
    assert layers["public"]["available"] is True
    assert layers["enhanced"]["available"] is True


def test_types_sorted_alphabetically_case_insensitively(seeded_body):
    names = [t["name"] for t in seeded_body["types"]]
    assert names == ["Clinic", "Hospital", "Organization", "Place", "Thing"]


def test_layer_is_stamped_per_type(seeded_body):
    by_name = {t["name"]: t for t in seeded_body["types"]}
    assert by_name["Organization"]["layer"] == "public"
    assert by_name["Clinic"]["layer"] == "enhanced"


def test_type_description_from_rdfs_comment_null_when_absent(seeded_body):
    by_name = {t["name"]: t for t in seeded_body["types"]}
    assert by_name["Organization"]["description"] == (
        "An organized body of people with a particular purpose."
    )
    assert by_name["Thing"]["description"] is None


def test_parent_and_subtypes_are_plain_names_across_layers(seeded_body):
    by_name = {t["name"]: t for t in seeded_body["types"]}
    assert by_name["Organization"]["parent_type"] == "Thing"
    assert by_name["Thing"]["subtypes"] == ["Organization"]
    # Cross-layer: an Enhanced type subclassing a Public one is listed on the
    # Public parent, alongside the same-layer child.
    assert by_name["Organization"]["subtypes"] == ["Clinic", "Hospital"]
    assert by_name["Clinic"]["parent_type"] == "Organization"
    assert by_name["Place"]["subtypes"] == []


def test_literal_slots_are_attributes_with_primitive_datatypes(seeded_body):
    org = next(t for t in seeded_body["types"] if t["name"] == "Organization")
    attrs = {a["name"]: a for a in org["attributes"]}
    assert set(attrs) == {"employeeCount", "legalName"}
    assert attrs["legalName"]["datatype"] == "string"
    assert attrs["employeeCount"]["datatype"] == "integer"


def test_type_ranged_slots_are_relationships(seeded_body):
    org = next(t for t in seeded_body["types"] if t["name"] == "Organization")
    assert [r["name"] for r in org["relationships"]] == ["headquarters"]
    rel = org["relationships"][0]
    assert rel["target_type"] == "Place"  # a NAME, not a URI
    assert rel["description"] == "Primary office location."


def test_slot_descriptions_and_null_when_absent(seeded_body):
    org = next(t for t in seeded_body["types"] if t["name"] == "Organization")
    attrs = {a["name"]: a for a in org["attributes"]}
    assert attrs["legalName"]["description"] == "Registered legal name."
    assert attrs["employeeCount"]["description"] is None


def test_core_slot_marker_round_trips(seeded_body):
    org = next(t for t in seeded_body["types"] if t["name"] == "Organization")
    attrs = {a["name"]: a for a in org["attributes"]}
    assert attrs["legalName"]["core_slot"] is True
    # Written without the marker -> default false, never a crash.
    assert org["relationships"][0]["core_slot"] is False


def test_slots_sorted_alphabetically(seeded_body):
    org = next(t for t in seeded_body["types"] if t["name"] == "Organization")
    assert [a["name"] for a in org["attributes"]] == ["employeeCount", "legalName"]
    place = next(t for t in seeded_body["types"] if t["name"] == "Place")
    assert [a["name"] for a in place["attributes"]] == ["address"]


def test_type_with_no_slots_still_appears(seeded_body):
    thing = next(t for t in seeded_body["types"] if t["name"] == "Thing")
    assert thing["attributes"] == []
    assert thing["relationships"] == []


def test_enhanced_layer_slot_ranges_resolve(seeded_body):
    clinic = next(t for t in seeded_body["types"] if t["name"] == "Clinic")
    assert [a["name"] for a in clinic["attributes"]] == ["npi"]
    assert clinic["attributes"][0]["description"] == "National Provider Id."


# --- degradation -------------------------------------------------------------


def test_one_layer_down_degrades_to_available_false_not_500():
    seeded = _seeded()
    neptune = FakeNeptune(
        {public_graph_uri(): seeded.by_graph[public_graph_uri()]},
        failing=[enhanced_graph_uri()],
    )
    r = _app(neptune).get("/operator/ontology/global")
    assert r.status_code == 200, r.text
    body = r.json()
    layers = {layer["layer"]: layer for layer in body["layers"]}
    assert layers["enhanced"]["available"] is False
    assert layers["enhanced"]["type_count"] == 0
    assert layers["public"]["available"] is True
    assert layers["public"]["type_count"] == 4
    # The downed layer contributes no types; the healthy one is unaffected.
    assert [t["name"] for t in body["types"]] == [
        "Hospital", "Organization", "Place", "Thing",
    ]
    assert all(t["layer"] == "public" for t in body["types"])


def test_both_layers_down_is_still_200():
    neptune = FakeNeptune({}, failing=[public_graph_uri(), enhanced_graph_uri()])
    r = _app(neptune).get("/operator/ontology/global")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["types"] == []
    assert all(layer["available"] is False for layer in body["layers"])


# --- shadowing is SHOWN, not applied ----------------------------------------


def test_same_name_in_both_layers_yields_two_entries():
    neptune = FakeNeptune({
        public_graph_uri(): shape_triples(PUB, "Person", slots=[{"name": "name"}]),
        enhanced_graph_uri(): shape_triples(ENH, "Person", slots=[{"name": "npi"}]),
    })
    body = _app(neptune).get("/operator/ontology/global").json()
    entries = [t for t in body["types"] if t["name"] == "Person"]
    assert len(entries) == 2
    assert [t["layer"] for t in entries] == ["enhanced", "public"]


def test_subtype_attaches_to_the_parents_LAYER_not_its_bare_name():
    """A homonym parent in the other layer must NOT inherit the subtype.

    `types/x/Doctor subClassOf types/x/Person` belongs to the ENHANCED Person
    only; the unrelated PUBLIC `Person` is a different type that merely shares a
    name. Keying the children map on the bare name listed Doctor under both —
    the precise shadowing confusion this payload exists to make visible.
    """
    neptune = FakeNeptune({
        public_graph_uri(): shape_triples(PUB, "Person"),
        enhanced_graph_uri(): (
            shape_triples(ENH, "Person")
            + shape_triples(ENH, "Doctor", parent_uri=f"{ENH}/Person")
        ),
    })
    body = _app(neptune).get("/operator/ontology/global").json()
    by_layer = {t["layer"]: t for t in body["types"] if t["name"] == "Person"}
    assert by_layer["enhanced"]["subtypes"] == ["Doctor"]
    assert by_layer["public"]["subtypes"] == []


def test_cross_layer_subclass_still_attaches():
    """The legitimate case must keep working: Enhanced child, Public parent."""
    neptune = FakeNeptune({
        public_graph_uri(): shape_triples(PUB, "Organization"),
        enhanced_graph_uri(): shape_triples(
            ENH, "Clinic", parent_uri=f"{PUB}/Organization"
        ),
    })
    body = _app(neptune).get("/operator/ontology/global").json()
    org = next(t for t in body["types"] if t["name"] == "Organization")
    assert org["layer"] == "public"
    assert org["subtypes"] == ["Clinic"]


def test_parent_outside_every_layer_namespace_is_dropped_not_invented():
    neptune = FakeNeptune({
        public_graph_uri(): shape_triples(
            PUB, "Thing", parent_uri=f"{RDFS}#Resource"
        ),
        enhanced_graph_uri(): [],
    })
    body = _app(neptune).get("/operator/ontology/global").json()
    assert body["types"][0]["parent_type"] is None


# --- determinism under multi-valued predicates -------------------------------


def test_pick_is_the_lexicographic_minimum_not_set_iteration_order():
    """Pins the fold to a TOTAL order, which is what makes it stable across
    PROCESSES — not merely within one.

    The two round-trip tests below collect candidates into a set, so they are
    row-order independent no matter how _pick chooses; they would still pass
    with `next(iter(values))`. But `str` hashing is randomized per interpreter
    (PYTHONHASHSEED), so set iteration order differs BETWEEN processes: two API
    workers would then disagree about the same graph. Only a value-ordered pick
    closes that, so assert the ordering itself.
    """
    import random
    import string

    from cograph_client.graph.global_ontology import _pick

    assert _pick(set()) is None
    assert _pick({"only"}) == "only"

    # Assert the contract over MANY sets rather than hunting for one that
    # iterates out of order. Two earlier versions of this test got the trade
    # wrong in opposite directions: hardcoded probes passed against a broken
    # `next(iter(values))` (they happened to iterate minimum-first), and then
    # SEARCHING for an out-of-order probe made the test fail on hash seeds
    # where no such probe turned up — i.e. flaky against CORRECT code.
    # `min()` satisfies this for every set, so a correct implementation can
    # never fail here; a first-iteration pick would have to iterate
    # minimum-first for all 200 sets to survive, which it will not.
    rng = random.Random(20260724)
    for _ in range(200):
        probe = set(rng.sample(string.ascii_letters, rng.randint(2, 12)))
        assert _pick(probe) == min(probe)
    assert _pick({f"{PUB}/Place", f"{XSD}#string"}) == f"{XSD}#string"


def test_conflicting_ranges_fold_deterministically_regardless_of_row_order():
    """Two declared ranges must not flip a slot between attributes and
    relationships depending on the engine's (unspecified) row order."""
    from cograph_client.graph.global_ontology import _TypeAccumulator

    rows = [
        {"typeLabel": "Org", "attrLabel": "hq", "range": f"{XSD}#string"},
        {"typeLabel": "Org", "attrLabel": "hq", "range": f"{PUB}/Place"},
    ]
    forward = _TypeAccumulator("Org", "public")
    for row in rows:
        forward.absorb(row)
    reverse = _TypeAccumulator("Org", "public")
    for row in reversed(rows):
        reverse.absorb(row)
    assert forward.build([]).model_dump() == reverse.build([]).model_dump()


def test_conflicting_comments_and_parents_fold_deterministically():
    from cograph_client.graph.global_ontology import _TypeAccumulator

    rows = [
        {"typeLabel": "Org", "typeComment": "b", "parent": f"{PUB}/Thing"},
        {"typeLabel": "Org", "typeComment": "a", "parent": f"{ENH}/Thing"},
    ]
    forward = _TypeAccumulator("Org", "public")
    for row in rows:
        forward.absorb(row)
    reverse = _TypeAccumulator("Org", "public")
    for row in reversed(rows):
        reverse.absorb(row)
    assert forward.build([]).model_dump() == reverse.build([]).model_dump()
    assert forward.parent() == reverse.parent()


# --- the operator gate is router-wide, not per-route opt-in -----------------


def _dependency_callables(dependant) -> set:
    """Every callable in a route's dependency tree, recursively."""
    found = set()
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            found.add(dep.call)
        stack.extend(dep.dependencies)
    return found


def test_every_operator_route_is_gated():
    """A future /operator/* route that forgets Depends(require_operator) would be
    ungated AND cross-tenant. The gate is declared on the ROUTER, so this holds
    for routes that do not exist yet — assert it rather than trust review."""
    from cograph_client.api.routes.operator import require_operator, router

    routes = [r for r in router.routes if hasattr(r, "dependant")]
    assert routes, "no routes found on the operator router"
    for route in routes:
        assert require_operator in _dependency_callables(route.dependant), (
            f"{route.path} is not behind require_operator"
        )


def test_router_declares_the_gate_itself():
    from cograph_client.api.routes.operator import require_operator, router

    assert any(d.dependency is require_operator for d in router.dependencies)


def test_preexisting_job_trace_route_still_gated_403_and_200():
    """The router-level dependency must not change existing route behavior."""
    from cograph_client.api.deps import get_enrichment_job_store

    class _Store:
        async def get(self, job_id):
            return None

    for is_operator, expected in ((False, 403), (True, 404)):
        app = FastAPI()
        app.include_router(operator_routes.router)
        app.dependency_overrides[get_enrichment_job_store] = _Store
        app.dependency_overrides[api_keys.get_tenant] = (
            lambda tenant=None, api_key=None, request=None, _o=is_operator: TenantContext(
                tenant_id="t", api_key="k", is_operator=_o
            )
        )
        r = TestClient(app).get("/operator/jobs/nope/trace")
        # 403 when not an operator; past the gate (404 unknown job) when one.
        assert r.status_code == expected, (is_operator, r.text)


# --- query-builder parameterization: existing callers unaffected ------------


def test_query_builders_default_to_tenant_namespace():
    from cograph_client.graph.ontology_queries import (
        get_attribute_range_query,
        get_subtypes_query,
        get_type_attributes_query,
        get_type_detail_query,
    )

    g = "https://omnix.dev/graphs/t"
    assert "<https://cograph.tech/types/Place>" in get_type_detail_query(g, "Place")
    assert "<https://cograph.tech/types/Place>" in get_type_attributes_query(g, "Place")
    assert "<https://cograph.tech/types/Place>" in get_subtypes_query(g, "Place")
    assert "<https://cograph.tech/types/Place/attrs/city>" in get_attribute_range_query(
        g, "Place", "city"
    )


def test_batched_query_is_deterministically_ordered():
    """No ORDER BY => unspecified solution order => a non-deterministic fold."""
    from cograph_client.graph.ontology_queries import full_ontology_detail_query

    sparql = full_ontology_detail_query(public_graph_uri())
    assert "ORDER BY" in sparql
    ordered = sparql.split("ORDER BY", 1)[1]
    # Every projected variable participates, so no tie is left to the engine.
    for var in (
        "?type", "?typeLabel", "?typeComment", "?parent",
        "?attr", "?attrLabel", "?attrComment", "?range", "?core",
        "?funcName", "?funcDesc", "?funcEndpoint",
    ):
        assert var in ordered, var


def test_oss_boundary_no_proprietary_import():
    """The reader must never reach into the proprietary parent package."""
    from pathlib import Path

    import cograph_client.graph.global_ontology as mod

    src = Path(mod.__file__).read_text()
    assert "from cograph." not in src
    assert "import cograph." not in src


# ============================================================================
# Data sources attached to a type (fuzzy coverage match, NOT a stored link)
# ============================================================================


def _spec(slug: str, kinds: list[str], **kw):
    """A minimal catalog entry. Only the fields this overlay reads are set."""
    from cograph_client.api_registry.spec import ApiSourceSpec, Coverage

    return ApiSourceSpec(
        slug=slug,
        title=kw.pop("title", slug.upper()),
        publisher=kw.pop("publisher", "Somebody"),
        coverage=Coverage(entity_kinds=list(kinds)),
        verified_at=kw.pop("verified_at", "2026-07-04"),
        layer=kw.pop("layer", "global_public"),
        **kw,
    )


def _catalog(*specs):
    from cograph_client.api_registry.catalog import ApiSourceCatalog

    return ApiSourceCatalog(entries={s.slug: s for s in specs})


def _fetch(neptune, **kw):
    """Call the reader directly (async) so the registry overlay's `catalog` /
    `today` seams are injectable — a freshness assertion pinned to the process
    catalog and the real clock would rot on its own."""
    import asyncio

    from cograph_client.graph.global_ontology import fetch_global_ontology

    return asyncio.run(fetch_global_ontology(neptune, **kw)).model_dump()


def _types_of(body) -> dict:
    return {t["name"]: t for t in body["types"]}


def _one_public_type(name: str, **shape_kw) -> FakeNeptune:
    return FakeNeptune({
        public_graph_uri(): shape_triples(PUB, name, **shape_kw),
        enhanced_graph_uri(): [],
    })


def test_sources_attach_by_coverage_token_match():
    body = _fetch(
        _one_public_type("Hospital"),
        catalog=_catalog(
            _spec("nppes", ["healthcare_provider", "clinic", "hospital"]),
            _spec("geonames", ["place", "city"]),
        ),
    )
    hospital = _types_of(body)["Hospital"]
    assert [s["slug"] for s in hospital["sources"]] == ["nppes"]


def test_source_carries_only_real_registry_fields():
    body = _fetch(
        _one_public_type("Hospital"),
        catalog=_catalog(
            _spec(
                "nppes",
                ["hospital"],
                title="NPPES NPI Registry",
                publisher="CMS",
                layer="global_enhanced",
                enabled=False,
            )
        ),
        today=date(2026, 7, 25),
    )
    src = _types_of(body)["Hospital"]["sources"][0]
    assert src == {
        "slug": "nppes",
        "title": "NPPES NPI Registry",
        "publisher": "CMS",
        "registry_layer": "global_enhanced",
        "authority_level": "authoritative",
        "enabled": False,
        "verified_at": "2026-07-04",
        "freshness": "OK",
        "entity_kinds": ["hospital"],
    }
    # Nothing invented: no volume / cadence / health columns.
    assert "volume" not in src and "cadence" not in src


def test_registry_layer_is_a_different_axis_from_the_ontology_layer():
    """The two vocabularies collide in one payload and must not be conflated.

    A source's catalog layer ("global_public" / "global_enhanced") has NOTHING
    to do with the ontology layer ("public" / "enhanced") of the type it hangs
    off: here a global_enhanced API covers a public-layer type. The field is
    named `registry_layer` so a UI cannot reach for the same badge by habit —
    a bare `layer` sitting next to `GlobalOntologyType.layer` would read as the
    same axis.
    """
    body = _fetch(
        _one_public_type("Hospital"),
        catalog=_catalog(_spec("nppes", ["hospital"], layer="global_enhanced")),
    )
    hospital = _types_of(body)["Hospital"]
    assert hospital["layer"] == "public"           # ONTOLOGY layer
    src = hospital["sources"][0]
    assert src["registry_layer"] == "global_enhanced"  # REGISTRY layer
    # The ambiguous key must not exist at all — not even as an alias, which
    # would let a reader bind to it and silently conflate the two axes again.
    assert "layer" not in src


def test_type_with_no_covering_source_gets_empty_list():
    body = _fetch(
        _one_public_type("Spacecraft"),
        catalog=_catalog(_spec("nppes", ["hospital", "clinic"])),
    )
    assert _types_of(body)["Spacecraft"]["sources"] == []


def test_generic_type_name_does_not_match_on_a_generic_token_alone():
    """The matcher's generic-token guard must not be bypassed here.

    A bare `Organization` matching `health_organization` on the shared generic
    token would fire a spurious "this API covers you" on the page AND is exactly
    what the enrichment rail refuses to do — the two must agree.
    """
    body = _fetch(
        _one_public_type("Organization"),
        catalog=_catalog(_spec("nppes", ["health_organization"])),
    )
    assert _types_of(body)["Organization"]["sources"] == []


def test_membership_is_exactly_the_shared_matcher_over_the_real_seed_catalog():
    """Convergence guard: the page's answer == `matching.type_matches`, entry for
    entry, over the SHIPPED seed catalog — not a lookalike heuristic.

    A second, drifting matcher (or a filter layered on top, e.g. silently
    dropping disabled entries) is caught here even when it agrees on the common
    cases.
    """
    from cograph_client.api_registry.catalog import make_api_source_catalog
    from cograph_client.api_registry.matching import type_matches

    catalog = make_api_source_catalog()
    assert catalog.slugs(), "seed catalog is empty — the guard would be vacuous"
    names = ["Hospital", "Place", "City", "Organization", "Thing", "LineItem",
             "ClinicalTrial", "Physician", "Spacecraft"]
    neptune = FakeNeptune({
        public_graph_uri(): sum(
            (shape_triples(PUB, n) for n in names), start=[]
        ),
        enhanced_graph_uri(): [],
    })
    body = _fetch(neptune, catalog=catalog)
    for name, t in _types_of(body).items():
        expected = sorted(
            spec.slug for spec in catalog.all() if type_matches(spec, name)
        )
        assert [s["slug"] for s in t["sources"]] == expected, name


def test_sources_are_sorted_by_slug():
    body = _fetch(
        _one_public_type("Hospital"),
        catalog=_catalog(
            _spec("zeta", ["hospital"]),
            _spec("alpha", ["hospital"]),
            _spec("mid", ["hospital"]),
        ),
    )
    assert [s["slug"] for s in _types_of(body)["Hospital"]["sources"]] == [
        "alpha", "mid", "zeta",
    ]


def test_same_name_in_both_layers_gets_the_same_sources():
    """The registry knows nothing about ontology layers, so a shadowed name must
    not get two different answers."""
    neptune = FakeNeptune({
        public_graph_uri(): shape_triples(PUB, "Hospital"),
        enhanced_graph_uri(): shape_triples(ENH, "Hospital"),
    })
    body = _fetch(neptune, catalog=_catalog(_spec("nppes", ["hospital"])))
    entries = [t for t in body["types"] if t["name"] == "Hospital"]
    assert len(entries) == 2
    assert all([s["slug"] for s in t["sources"]] == ["nppes"] for t in entries)


# --- freshness reuses the EXISTING catalog audit's grading -------------------


@pytest.mark.parametrize(
    "verified_at,expected",
    [
        ("2026-07-04", "OK"),
        ("2020-01-01", "STALE"),
        ("", "UNVERIFIED"),
        ("not-a-date", "UNVERIFIED"),
        ("2030-01-01", "FUTURE"),
    ],
)
def test_freshness_grades_come_from_catalog_audit(verified_at, expected):
    body = _fetch(
        _one_public_type("Hospital"),
        catalog=_catalog(_spec("s", ["hospital"], verified_at=verified_at)),
        today=date(2026, 7, 25),
    )
    src = _types_of(body)["Hospital"]["sources"][0]
    assert src["freshness"] == expected
    assert src["verified_at"] == verified_at


def test_freshness_never_reports_a_live_smoke_status():
    """The grade is OFFLINE by construction: an ontology read must not make
    network calls, so EMPTY / UNREACHABLE can never appear."""
    from cograph_client.api_registry import catalog_audit

    calls = []

    async def _boom(*a, **kw):  # pragma: no cover - must never run
        calls.append(a)
        raise AssertionError("live smoke ran during an ontology read")

    original = catalog_audit._smoke_entry
    catalog_audit._smoke_entry = _boom
    try:
        body = _fetch(
            _one_public_type("Hospital"),
            catalog=_catalog(_spec("s", ["hospital"])),
        )
    finally:
        catalog_audit._smoke_entry = original
    assert not calls
    assert _types_of(body)["Hospital"]["sources"][0]["freshness"] in (
        "OK", "STALE", "UNVERIFIED", "FUTURE",
    )


# --- degradation: a broken registry must never take down the ontology --------


def test_registry_unavailable_degrades_to_empty_sources_not_500(monkeypatch):
    from cograph_client.api_registry import catalog as catalog_mod

    def _explode(*a, **kw):
        raise RuntimeError("registry is down")

    monkeypatch.setattr(catalog_mod, "get_api_source_catalog", _explode)
    r = _app(_seeded()).get("/operator/ontology/global")
    assert r.status_code == 200, r.text
    body = r.json()
    # The ONTOLOGY is intact — only the overlay degraded.
    assert [t["name"] for t in body["types"]] == [
        "Clinic", "Hospital", "Organization", "Place", "Thing",
    ]
    assert all(t["sources"] == [] for t in body["types"])


def test_registry_audit_failure_also_degrades(monkeypatch):
    from cograph_client.api_registry import catalog_audit

    async def _explode(*a, **kw):
        raise RuntimeError("audit blew up")

    monkeypatch.setattr(catalog_audit, "audit_catalog", _explode)
    body = _fetch(_one_public_type("Hospital"))
    assert _types_of(body)["Hospital"]["sources"] == []


def test_a_matcher_failure_degrades_that_type_only(monkeypatch):
    """A spec the matcher chokes on must cost that type its overlay, not the
    whole request."""
    from cograph_client.api_registry import matching

    def _explode(spec, entity_type):
        raise RuntimeError("bad spec")

    monkeypatch.setattr(matching, "type_matches", _explode)
    body = _fetch(
        _one_public_type("Hospital"),
        catalog=_catalog(_spec("nppes", ["hospital"])),
    )
    assert _types_of(body)["Hospital"]["sources"] == []


# --- tenant isolation: a cross-tenant page shows the GLOBAL catalog only -----


def test_a_tenants_private_sources_never_appear():
    """`/operator/ontology/global` is cross-tenant. Passing a tenant_id into the
    catalog would leak one workspace's private entries onto the shared canon."""
    from cograph_client.api_registry.catalog import (
        reset_api_source_catalog,
        set_tenant_custom_specs,
    )

    reset_api_source_catalog()
    set_tenant_custom_specs("demo-tenant", [_spec("private-hospital-api", ["hospital"])])
    try:
        body = _fetch(_one_public_type("Hospital"))
        slugs = [s["slug"] for s in _types_of(body)["Hospital"]["sources"]]
        assert "private-hospital-api" not in slugs
        # Sanity: the global seed DID attach, so the assertion above is not
        # passing merely because the overlay was empty.
        assert "nppes" in slugs
    finally:
        reset_api_source_catalog()


# ============================================================================
# Attached functions — EXECUTABLE code on a type (read path only)
# ============================================================================


def test_function_attached_to_a_layer_qualified_type_surfaces():
    neptune = FakeNeptune({
        public_graph_uri(): [],
        enhanced_graph_uri(): (
            shape_triples(ENH, "Place", slots=[{"name": "address"}])
            + function_triples(
                ENH, "Place", "distance_to",
                description="Great-circle distance between two places.",
                endpoint="https://fn.example/distance",
            )
        ),
    })
    place = _types_of(_fetch(neptune))["Place"]
    assert place["functions"] == [{
        "name": "distance_to",
        "entity_type": "Place",
        "description": "Great-circle distance between two places.",
        "endpoint_url": "https://fn.example/distance",
        # Not stored in the graph — the model default, same as the tenant route.
        "tier": "custom",
    }]
    # The function join must not disturb the slot fold (row cross-product).
    assert [a["name"] for a in place["attributes"]] == ["address"]


def test_functions_are_sorted_and_optional_fields_degrade():
    neptune = FakeNeptune({
        public_graph_uri(): (
            shape_triples(PUB, "Place")
            + function_triples(PUB, "Place", "zeta")
            + function_triples(PUB, "Place", "alpha", endpoint="https://fn/a")
        ),
        enhanced_graph_uri(): [],
    })
    funcs = _types_of(_fetch(neptune))["Place"]["functions"]
    assert [f["name"] for f in funcs] == ["alpha", "zeta"]
    assert funcs[0]["endpoint_url"] == "https://fn/a"
    # Absent description/endpoint are reported, never fabricated.
    assert funcs[1]["endpoint_url"] is None
    assert funcs[1]["description"] == ""


def test_type_without_functions_reports_an_empty_list(seeded_body):
    assert all(t["functions"] == [] for t in seeded_body["types"])


def test_the_only_function_WRITER_still_targets_the_bare_tenant_namespace():
    """Pins the known gap the contract documents: `functions` is empty in
    practice because `register_function_triple` — the one writer — attaches to
    `https://cograph.tech/types/<T>`, never to a layer-qualified
    `types/public/<T>` / `types/x/<T>`.

    This fails the day a global-layer function writer lands, which is exactly
    when the "empty until a writer exists" note in the model + module docstrings
    stops being true and must be rewritten.
    """
    from cograph_client.graph.ontology_queries import type_uri
    from cograph_client.graph.queries import register_function_triple

    sparql = register_function_triple(
        public_graph_uri(), entity_type="Place", function_name="f",
        endpoint_url="https://fn/a",
    )
    assert f"<{type_uri('Place')}>" in sparql
    assert f"<{PUB}/Place>" not in sparql and f"<{ENH}/Place>" not in sparql


def test_a_function_attached_to_the_BARE_tenant_uri_does_not_surface():
    """The read-side half of the gap above: such a triple attaches to a
    DIFFERENT subject than the layer-qualified type this browser reads, so it
    contributes nothing even when it sits inside a layer graph."""
    neptune = FakeNeptune({
        public_graph_uri(): (
            shape_triples(PUB, "Place")
            + function_triples("https://cograph.tech/types", "Place", "legacy_fn")
        ),
        enhanced_graph_uri(): [],
    })
    assert _types_of(_fetch(neptune))["Place"]["functions"] == []


def test_query_builder_joins_attached_functions():
    from cograph_client.graph.ontology_queries import full_ontology_detail_query

    sparql = full_ontology_detail_query(public_graph_uri())
    assert "https://cograph.tech/onto/attachedTo" in sparql
    assert "?funcName" in sparql
    assert "?funcEndpoint" in sparql


def test_function_fold_is_deterministic_regardless_of_row_order():
    from cograph_client.graph.global_ontology import _TypeAccumulator

    rows = [
        {"typeLabel": "Place", "funcName": "f", "funcDesc": "b", "funcEndpoint": "https://z"},
        {"typeLabel": "Place", "funcName": "f", "funcDesc": "a", "funcEndpoint": "https://a"},
    ]
    forward = _TypeAccumulator("Place", "public")
    for row in rows:
        forward.absorb(row)
    reverse = _TypeAccumulator("Place", "public")
    for row in reversed(rows):
        reverse.absorb(row)
    assert forward.build([]).model_dump() == reverse.build([]).model_dump()
    assert len(forward.build([]).functions) == 1


# ============================================================================
# Attached SKILLS — curated PROSE on a type (read path only, GLOBAL layers only)
#
# Skills TEACH, functions COMPUTE (boundary doc §27). These assertions are about
# three things and nothing else: that curated global content surfaces, that the
# BODY is not dragged through an ontology read, and that a workspace's private
# skills are structurally unreachable from this cross-tenant page.
# ============================================================================


@pytest.fixture
def clean_skill_registry():
    """Each skills test owns the process-wide registry outright.

    The registry is module-global and memoized, so a test that registers content
    would otherwise bleed into the next one — and the OSS seed dir ships empty,
    so a clean registry means a genuinely empty overlay to assert against.
    """
    from cograph_client.skills import reset_skill_layers

    reset_skill_layers()
    try:
        yield
    finally:
        reset_skill_layers()


def _register(*skills, layer=None):
    """Register curated skills through the subsystem's OWN public seam — the
    same call the premium overlay makes. Never by poking module state: a test
    that bypasses `register_skill_layer` would also bypass the tenant guard it
    enforces, which is half of what these tests exist to check."""
    from cograph_client.graph.layers import Layer
    from cograph_client.skills import register_skill_layer

    register_skill_layer(layer or Layer.PUBLIC, list(skills))


def _skill(slug: str, type_name: str, body: str = "Some guidance.", **kw):
    from cograph_client.skills import TypeSkill

    return TypeSkill(slug=slug, type_name=type_name, body=body, **kw)


def test_curated_global_skill_surfaces_on_its_type(clean_skill_registry):
    _register(
        _skill(
            "billing-not-buildings",
            "Hospital",
            body="A Hospital is a billing entity, not a building.",
            title="Billing, not buildings",
            summary="Never merge two hospitals sharing a street address.",
        )
    )
    hospital = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]
    assert [s["slug"] for s in hospital["skills"]] == ["billing-not-buildings"]
    assert hospital["skills"][0]["title"] == "Billing, not buildings"
    assert hospital["skills"][0]["summary"] == (
        "Never merge two hospitals sharing a street address."
    )


def test_skill_carries_only_real_fields_and_no_body(clean_skill_registry):
    """The contract mirrors the skills API's own list projection (SkillSummary):
    identity + authored metadata + a derived preview. A `body` key here would be
    the performance regression this design exists to avoid."""
    _register(_skill("s1", "Hospital", body="B" * 50, title="T", summary="S", version=3))
    skill = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]["skills"][0]
    assert set(skill) == {
        "slug", "type_name", "title", "summary", "excerpt",
        "body_chars", "layer", "enabled", "version",
    }
    assert "body" not in skill
    assert skill == {
        "slug": "s1",
        "type_name": "Hospital",
        "title": "T",
        "summary": "S",
        "excerpt": "B" * 50,
        "body_chars": 50,
        "layer": "public",
        "enabled": True,
        "version": 3,
    }


def test_a_long_body_is_excerpted_not_inlined(clean_skill_registry):
    """A 20k body (the validator's ceiling) must not ride along whole."""
    from cograph_client.graph.global_ontology import SKILL_EXCERPT_CHARS
    from cograph_client.skills import MAX_BODY_CHARS

    # A 9-char word deliberately does NOT divide the excerpt budget, so the raw
    # cut lands MID-WORD ("…alph") and the word-boundary rule is observable. A
    # word length that divides it evenly would make this test pass with the
    # boundary logic deleted.
    body = ("alphabet " * 2500)[:MAX_BODY_CHARS]
    assert SKILL_EXCERPT_CHARS % len("alphabet ") != 0
    _register(_skill("long", "Hospital", body=body))
    skill = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]["skills"][0]
    assert skill["body_chars"] == len(body) == MAX_BODY_CHARS
    # Bounded — and the +1 is only the announcing ellipsis.
    assert len(skill["excerpt"]) <= SKILL_EXCERPT_CHARS + 1
    assert skill["excerpt"].endswith("…")
    # Cut on a word boundary: no half word before the ellipsis.
    assert skill["excerpt"][:-1].endswith("alphabet")


def test_excerpt_collapses_whitespace_and_keeps_short_bodies_whole(clean_skill_registry):
    _register(
        _skill("md", "Hospital", body="# Heading\n\n- one\n-  two\n\n\nTrailing.\n")
    )
    skill = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]["skills"][0]
    assert skill["excerpt"] == "# Heading - one - two Trailing."
    # A body that fits is carried WHOLE and is not falsely marked truncated.
    assert not skill["excerpt"].endswith("…")
    assert skill["body_chars"] == len("# Heading\n\n- one\n-  two\n\n\nTrailing.\n")


def test_skills_sorted_by_slug_then_layer_so_an_override_pair_is_adjacent(
    clean_skill_registry,
):
    """A slug curated in BOTH global layers is the OVERRIDE case (Enhanced wins
    at resolution). This is the operator's raw browse view, so both rows are
    shown — adjacent and in a fixed order, never split by the registry's own
    precedence ordering."""
    from cograph_client.graph.layers import Layer

    _register(_skill("zeta", "Hospital"), _skill("naming", "Hospital"))
    _register(_skill("naming", "Hospital"), layer=Layer.ENHANCED)
    skills = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]["skills"]
    assert [(s["slug"], s["layer"]) for s in skills] == [
        ("naming", "enhanced"),
        ("naming", "public"),
        ("zeta", "public"),
    ]


def test_a_skills_layer_may_differ_from_the_types_layer(clean_skill_registry):
    """Unlike `registry_layer`, this IS the ontology-layer axis — but it is the
    SKILL's layer, so a curated ENHANCED skill can legitimately hang off a
    PUBLIC type. The UI may share the badge; it must not assume the values
    agree."""
    from cograph_client.graph.layers import Layer

    _register(_skill("premium-guidance", "Hospital"), layer=Layer.ENHANCED)
    hospital = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]
    assert hospital["layer"] == "public"
    assert hospital["skills"][0]["layer"] == "enhanced"


def test_attachment_is_case_insensitive_and_type_name_is_the_skills_own(
    clean_skill_registry,
):
    """`global_skills_for_type` casefolds, so the skill's own spelling can
    differ from the type's — and it is the skill's spelling the canonical
    `/skills/{type_name}/{slug}` read wants."""
    _register(_skill("s", "hospital"))
    hospital = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]
    assert [s["type_name"] for s in hospital["skills"]] == ["hospital"]


def test_type_with_no_curated_skill_gets_an_empty_list(clean_skill_registry):
    _register(_skill("s", "Hospital"))
    body = _fetch(_one_public_type("Place"))
    assert _types_of(body)["Place"]["skills"] == []


def test_same_name_in_both_layers_gets_the_same_skills(clean_skill_registry):
    """The registry is keyed by type NAME and knows nothing about ontology
    layers, so the two entries of a shadowed name must not disagree."""
    _register(_skill("s", "Organization"))
    neptune = FakeNeptune({
        public_graph_uri(): shape_triples(PUB, "Organization"),
        enhanced_graph_uri(): shape_triples(ENH, "Organization"),
    })
    entries = [t for t in _fetch(neptune)["types"] if t["name"] == "Organization"]
    assert len(entries) == 2
    assert entries[0]["skills"] == entries[1]["skills"]
    assert entries[0]["skills"][0]["slug"] == "s"


def test_a_disabled_skill_is_still_listed_and_says_so(clean_skill_registry):
    """The raw browse view shows what is authored; `enabled: false` is the fact
    that it is not injected into any prompt (and that it suppresses a same-slug
    skill below it), not a reason to hide the row."""
    _register(_skill("off", "Hospital", enabled=False))
    skills = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]["skills"]
    assert [(s["slug"], s["enabled"]) for s in skills] == [("off", False)]


def test_skills_subsystem_failure_degrades_to_empty_not_500(
    monkeypatch, clean_skill_registry
):
    import cograph_client.skills as skills_pkg

    def _explode(*a, **kw):
        raise RuntimeError("skills registry is down")

    # Patch the attribute the assembler's LAZY IMPORT actually binds
    # (`from cograph_client.skills import global_skills_for_type`) — patching
    # `skills.registry` instead would leave the package re-export untouched and
    # this test would pass without exercising the degradation at all.
    monkeypatch.setattr(skills_pkg, "global_skills_for_type", _explode)
    # Register content for a seeded type so a NON-degraded run would return a
    # non-empty overlay: without this the assertion below holds trivially.
    _register(_skill("s", "Hospital"))
    r = _app(_seeded()).get("/operator/ontology/global")
    assert r.status_code == 200, r.text
    body = r.json()
    # The ONTOLOGY is intact — only the overlay degraded.
    assert [t["name"] for t in body["types"]] == [
        "Clinic", "Hospital", "Organization", "Place", "Thing",
    ]
    assert all(t["skills"] == [] for t in body["types"])


# --- tenant isolation: a cross-tenant page shows the GLOBAL layers only -------


def test_a_tenants_private_skills_never_appear(clean_skill_registry):
    """`/operator/ontology/global` is cross-tenant. A workspace's authored
    skills are its own data and must not surface on the shared canon."""
    import asyncio

    from cograph_client.graph.layers import Layer
    from cograph_client.skills import InMemoryTypeSkillStore, TypeSkill

    store = InMemoryTypeSkillStore()
    asyncio.run(
        store.upsert(
            TypeSkill(
                slug="workspace-private",
                type_name="Hospital",
                body="Internal-only guidance for this one workspace.",
                layer=Layer.TENANT,
                tenant_id="demo-tenant",
            )
        )
    )
    # Sanity: the tenant DID author it, and its own workspace can resolve it —
    # so the assertion below is not passing merely because the write no-oped.
    resolved = asyncio.run(
        __import__(
            "cograph_client.skills", fromlist=["resolve_skills"]
        ).resolve_skills("Hospital", tenant_id="demo-tenant", store=store)
    )
    assert [s.slug for s in resolved] == ["workspace-private"]

    _register(_skill("curated", "Hospital"))
    hospital = _types_of(_fetch(_one_public_type("Hospital")))["Hospital"]
    slugs = [s["slug"] for s in hospital["skills"]]
    assert "workspace-private" not in slugs
    # And the global overlay DID attach, so this is a real exclusion rather
    # than an empty overlay passing for one.
    assert slugs == ["curated"]


def test_the_read_function_cannot_carry_a_tenant_row_at_all(clean_skill_registry):
    """Structural, not filtered: the ONLY writer into the registry this page
    reads REFUSES the tenant layer outright and blanks `tenant_id` on
    everything it accepts. There is no tenant row for the assembler to have to
    filter out — which is why `_SkillIndex` does not have such a filter."""
    from cograph_client.graph.layers import Layer
    from cograph_client.skills import global_skills_for_type, register_skill_layer

    with pytest.raises(ValueError, match="GLOBAL layers only"):
        register_skill_layer(Layer.TENANT, [_skill("t", "Hospital")])

    register_skill_layer(
        Layer.PUBLIC,
        [_skill("smuggled", "Hospital", tenant_id="demo-tenant", layer=Layer.TENANT)],
    )
    got = global_skills_for_type("Hospital")
    assert [(s.slug, s.layer, s.tenant_id) for s in got] == [
        ("smuggled", Layer.PUBLIC, None)
    ]


def test_skills_reader_takes_no_tenant_context(clean_skill_registry):
    """A guard on the SHAPE of the seam: give `global_skills_for_type` a tenant
    parameter and this page would be one keyword argument away from leaking."""
    import inspect

    from cograph_client.skills import global_skills_for_type

    params = inspect.signature(global_skills_for_type).parameters
    assert list(params) == ["type_name", "layer"]


def test_the_source_reader_never_passes_a_tenant_to_the_catalog():
    """A guard on the SHAPE of the source seam, mirroring
    ``test_skills_reader_takes_no_tenant_context`` for the other overlay.

    The skills side cannot leak by construction: ``global_skills_for_type``
    has no tenant parameter at all. The sources side is different —
    ``get_api_source_catalog`` DOES take ``tenant_id``, and passing it would
    merge a workspace's private ``tenant_custom`` entries into a cross-tenant
    operator view. Until this test, the only thing standing between that and
    production was one behavioural test plus a comment; a reviewer flagged the
    asymmetry. Reading the call site's AST makes the omission structural: add
    the argument back and this fails as a shape violation, whatever the merged
    catalog happens to return for the fixtures a behavioural test picked.
    """
    import ast
    import inspect

    from cograph_client.graph import global_ontology as mod

    tree = ast.parse(inspect.getsource(mod))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "get_api_source_catalog"
    ]
    assert calls, "the catalog call vanished — this guard is now watching nothing"
    for call in calls:
        assert not call.args, "positional arg to get_api_source_catalog"
        passed = {kw.arg for kw in call.keywords}
        assert "tenant_id" not in passed, (
            "get_api_source_catalog was given a tenant_id inside the CROSS-TENANT "
            "operator ontology reader: a workspace's private tenant_custom "
            "sources would leak onto the shared Global canon."
        )
