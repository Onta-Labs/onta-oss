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
        # The writer itself does not emit subClassOf today, but the layer graphs
        # are ordinary ontology graphs and the contract exposes parent/subtypes.
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
        for label in labels:
            for comment in comments:
                for parent in parents:
                    for ar in attr_rows:
                        row = {"type": t, "typeLabel": label}
                        if comment is not None:
                            row["typeComment"] = comment
                        if parent is not None:
                            row["parent"] = parent
                        for k, v in ar.items():
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
    import string

    from cograph_client.graph.global_ontology import _pick

    assert _pick(set()) is None
    assert _pick({"only"}) == "only"

    # WHICH sets iterate out of order depends on the hash seed, so SEARCH for a
    # probe instead of hardcoding one — otherwise this test silently stops
    # discriminating on a run where the hardcoded set happens to iterate
    # minimum-first (it did, which is how this assertion earned its keep).
    probe = next(
        (
            s
            for s in (set(string.ascii_lowercase[:n]) for n in range(2, 27))
            if next(iter(s)) != min(s)
        ),
        None,
    )
    assert probe is not None, "could not construct an out-of-order probe set"
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
    ):
        assert var in ordered, var


def test_oss_boundary_no_proprietary_import():
    """The reader must never reach into the proprietary parent package."""
    from pathlib import Path

    import cograph_client.graph.global_ontology as mod

    src = Path(mod.__file__).read_text()
    assert "from cograph." not in src
    assert "import cograph." not in src
