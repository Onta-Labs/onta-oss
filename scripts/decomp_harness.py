"""Ontology-decomposition quality harness.

Ingests deliberately "trappy" rows through SchemaResolver under one or more
extraction POLICIES, reads the resulting ontology back via SPARQL, and scores it
against a per-domain EXPECTED-SHAPE spec with deterministic structural checks.

The point: a good ingest DECOMPOSES rows into a well-shaped ontology (reusable
real-world things become entity NODES with relationships, multi-valued fields
become REPEATED edges, roles become correct SUBTYPES, identifiers/measurements
stay LITERALS). A bad ingest mirrors the flat column shape. This harness measures
which.

POLICIES
  A "constrained"   — today's discovery cage: constrain_types=[T] + flat
                      constrain_attributes (reproduces the bug).
  B "unconstrained" — the document-ingest path: no constraint (the target shape).
  C "seed"          — the fix (added later): target-schema prior that guides
                      without flattening.

Requires a local SPARQL store (scripts/local_sparql.py) on --endpoint and
OPENROUTER_API_KEY in the env (source the repo .env).

WARNING: each run issues ``DROP ALL`` to isolate its ontology, so point
``--endpoint`` at a LOCAL, DISPOSABLE store only — NEVER a shared or production
SPARQL endpoint.

    python scripts/decomp_harness.py --domain healthcare_providers --policy A,B
    python scripts/decomp_harness.py --all --policy A,B
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve()
_OSS_ROOT = _HERE.parent.parent  # cograph-oss/
sys.path.insert(0, str(_OSS_ROOT))

from cograph_client.graph.client import NeptuneClient  # noqa: E402
from cograph_client.graph.queries import tenant_graph_uri  # noqa: E402
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402

FIXTURES = _OSS_ROOT / "tests" / "fixtures" / "decomp"

TYPES_NS = "https://cograph.tech/types/"
ENTITIES_NS = "/entities/"

# Upper-ontology parents the extractor synthesizes to close a subclass chain
# (Physician -> HealthcareProvider -> Person; City -> Place). They are correct,
# not fragmentation, so they don't count toward the "too many types" band.
_ABSTRACT_ANCESTORS = {"Person", "Place", "Thing", "Entity", "Agent", "Location"}


# --------------------------------------------------------------------------- #
# Expected-shape specs (the assertion contract). Seeded for physicians (known);
# other domains filled from tests/fixtures/decomp/README.md as it lands.
# --------------------------------------------------------------------------- #
@dataclass
class DomainSpec:
    name: str
    proposed_type: str            # what discovery would confirm as THE type
    key_attr: str = "name"
    # structural expectations for scoring:
    expect_node_types: list[str] = field(default_factory=list)   # should be minted as NODES
    expect_subtypes: list[str] = field(default_factory=list)     # distinct role subtypes
    expect_multivalued_edges: list[str] = field(default_factory=list)  # a predicate that should repeat
    expect_literal_attrs: list[str] = field(default_factory=list)      # must stay literal
    expect_primary_count: int | None = None      # dedupe headline: exact instance count of proposed_type
    expect_node_counts: dict[str, int] = field(default_factory=dict)   # reusability: type -> distinct instances
    new_types_range: tuple[int, int] = (2, 12)  # sane band: catch flatten (1) and fragment (20)
    seed_ontology: str | None = None             # fixture file to pre-ingest (reconcile test)

    @property
    def fixture(self) -> Path:
        return FIXTURES / f"{self.name}.json"


SPECS: dict[str, DomainSpec] = {
    # ranges + headline checks per tests/fixtures/decomp/README.md
    "healthcare_providers": DomainSpec(
        name="healthcare_providers",
        proposed_type="Physician",           # what discovery confirms; Provider is the ideal parent
        key_attr="npi",
        expect_node_types=["Organization", "City", "State", "Specialty"],
        expect_subtypes=["Physician", "NursePractitioner", "PhysicianAssistant"],
        expect_multivalued_edges=["has_specialty", "specialty"],
        expect_literal_attrs=["npi", "phone"],
        # README baseline [6,9]; widened to 11 for the extractor's LEGIT extra org
        # subtypes (Hospital + Clinic — both appear in the fixture affiliations) and
        # the Provider parent. Real types, not fragmentation; the node/subtype/
        # multi-value checks are the hard signal, this band is a coarse guardrail.
        new_types_range=(6, 11),
    ),
    "coffee_shops": DomainSpec(
        name="coffee_shops",
        proposed_type="CoffeeShop",
        key_attr="shop_id",
        expect_node_types=["City"],
        expect_multivalued_edges=["has_category", "category", "in_category"],
        expect_literal_attrs=["rating", "price_level", "address"],
        expect_primary_count=15,             # dedupe trap: same-name-diff-city stay distinct
        new_types_range=(2, 4),
    ),
    "llm_models": DomainSpec(
        name="llm_models",
        proposed_type="Model",
        key_attr="model_id",
        expect_node_types=["Organization"],
        expect_multivalued_edges=["supports_modality", "has_modality", "modality"],
        expect_literal_attrs=["context_length", "input_price", "output_price"],
        expect_node_counts={"Organization": 5},   # reusability: 15 models -> 5 orgs
        new_types_range=(2, 3),
    ),
    "sp500_companies": DomainSpec(
        name="sp500_companies",
        proposed_type="Company",
        key_attr="ticker",
        expect_node_types=["Sector", "Industry", "City", "State", "Person"],
        expect_literal_attrs=["ticker", "market_cap", "employees"],
        new_types_range=(4, 6),              # HQ "City, State" must split -> City+State
    ),
    "clinical_trials": DomainSpec(
        name="clinical_trials",
        proposed_type="ClinicalTrial",
        key_attr="nct_id",
        expect_node_types=["Condition", "Organization"],   # both pre-seeded -> REUSE
        expect_literal_attrs=["nct_id", "enrollment", "start_year"],
        new_types_range=(0, 1),              # only ClinicalTrial is new; >=2 => reconcile FAILED
        seed_ontology="clinical_trials.seed_ontology.json",
    ),
}


# --------------------------------------------------------------------------- #
# Store + resolver
# --------------------------------------------------------------------------- #
def make_resolver(neptune: NeptuneClient) -> SchemaResolver:
    # Fresh verdict cache per resolver so type-match decisions don't leak across
    # runs. anthropic_key unused on the OpenRouter extraction path.
    cache = JsonVerdictCache(Path("/tmp") / f"decomp_verdicts_{time.time_ns()}.json")
    return SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=cache,
        embedding_service=None,
    )


async def clear_all(neptune: NeptuneClient) -> None:
    # Wipe the WHOLE store before each run. The local pyoxigraph store queries the
    # default graph as the UNION of all named graphs (Neptune semantics), so
    # leftover types from a prior run/tenant get reused by _fetch_ontology and are
    # never re-declared into THIS run's graph — corrupting per-run shape
    # measurement. A clean store per run also matches the README's "into an empty
    # ontology" assumption for the new-types band.
    await neptune.update("DROP ALL")


_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


def _lit(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


async def seed_ontology(neptune: NeptuneClient, tenant: str, spec: dict) -> None:
    """Deterministically seed a pre-existing ontology (types + attributes +
    instances) via SPARQL — no LLM — so the reconcile test can assert the ingest
    REUSES these exact types instead of minting near-synonym duplicates. Uses the
    same URI helpers the resolver writes with, so TypeMatcher/_fetch_ontology see
    them as genuine existing types."""
    from cograph_client.graph.ontology_queries import (
        attr_uri, insert_attribute, insert_type, type_uri,
    )
    from cograph_client.resolver.schema_resolver import _safe_id

    g = tenant_graph_uri(tenant)
    for t in spec.get("types", []):
        tname = t["type_name"]
        await neptune.update(insert_type(g, tname, t.get("gloss", "")))
        for a in t.get("attributes", []):
            await neptune.update(
                insert_attribute(g, tname, a["name"], a.get("gloss", ""),
                                 a.get("datatype", "string"))
            )
        triples: list[str] = []
        for inst in t.get("seed_instances", []):
            euri = f"https://cograph.tech/entities/{tname}/{_safe_id(inst['name'])}"
            triples.append(f"<{euri}> <{_RDF_TYPE}> <{type_uri(tname)}> .")
            triples.append(f"<{euri}> <{_RDFS_LABEL}> {_lit(inst['name'])} .")
            for k, v in inst.items():
                if k == "name":
                    continue
                triples.append(f"<{euri}> <{attr_uri(tname, k)}> {_lit(v)} .")
        if triples:
            await neptune.update(
                f"INSERT DATA {{ GRAPH <{g}> {{ {chr(10).join(triples)} }} }}"
            )


# --------------------------------------------------------------------------- #
# Readback — the minted ontology shape, scoped to ONE tenant graph
# --------------------------------------------------------------------------- #
@dataclass
class Shape:
    tenant: str
    types_created: list[str]
    attributes_added: list[str]
    entities_resolved: int
    triples: int
    classes: list[str]                       # rdfs:Class labels present in graph
    subclass: dict[str, str]                 # child -> parent (labels)
    obj_predicates: dict[str, int]           # onto predicate -> edge count (obj-valued)
    instances_by_type: dict[str, int]        # type label -> instance count
    literal_attrs: list[str]                 # attribute local-names that are literal-ranged


def _local(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


async def read_shape(neptune: NeptuneClient, tenant: str, res) -> Shape:
    g = tenant_graph_uri(tenant)

    async def q(sparql: str):
        r = await neptune.query(sparql)
        return r["results"]["bindings"]

    # classes + subclass edges
    classes: list[str] = []
    subclass: dict[str, str] = {}
    for b in await q(
        f"SELECT ?t ?l WHERE {{ GRAPH <{g}> {{ ?t a <http://www.w3.org/2000/01/rdf-schema#Class> . "
        f"OPTIONAL {{ ?t <http://www.w3.org/2000/01/rdf-schema#label> ?l }} }} }}"
    ):
        label = b.get("l", {}).get("value") or _local(b["t"]["value"])
        classes.append(label)
    for b in await q(
        f"SELECT ?c ?p WHERE {{ GRAPH <{g}> {{ ?c <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?p }} }}"
    ):
        subclass[_local(b["c"]["value"])] = _local(b["p"]["value"])

    # object-valued predicates (edges to /entities/) + counts
    obj_predicates: dict[str, int] = {}
    for b in await q(
        f"SELECT ?p (COUNT(*) AS ?n) WHERE {{ GRAPH <{g}> {{ ?s ?p ?o . "
        f"FILTER(isIRI(?o)) FILTER(CONTAINS(STR(?o),'{ENTITIES_NS}')) }} }} GROUP BY ?p"
    ):
        obj_predicates[_local(b["p"]["value"])] = int(b["n"]["value"])

    # instance counts by asserted type
    instances_by_type: dict[str, int] = {}
    for b in await q(
        f"SELECT ?t (COUNT(?s) AS ?n) WHERE {{ GRAPH <{g}> {{ ?s a ?t . "
        f"FILTER(CONTAINS(STR(?t),'{TYPES_NS}')) }} }} GROUP BY ?t"
    ):
        instances_by_type[_local(b["t"]["value"])] = int(b["n"]["value"])

    # attribute declarations whose range is a literal (xsd) datatype
    literal_attrs: list[str] = []
    for b in await q(
        f"SELECT ?p ?r WHERE {{ GRAPH <{g}> {{ "
        f"?p a <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property> . "
        f"OPTIONAL {{ ?p <http://www.w3.org/2000/01/rdf-schema#range> ?r }} }} }}"
    ):
        rng = b.get("r", {}).get("value", "")
        if "XMLSchema" in rng or not rng:
            literal_attrs.append(_local(b["p"]["value"]))

    return Shape(
        tenant=tenant,
        types_created=list(res.types_created),
        attributes_added=list(res.attributes_added),
        entities_resolved=int(getattr(res, "entities_resolved", 0) or 0),
        triples=int(getattr(res, "triples_inserted", 0) or 0),
        classes=sorted(set(classes)),
        subclass=subclass,
        obj_predicates=obj_predicates,
        instances_by_type=instances_by_type,
        literal_attrs=sorted(set(literal_attrs)),
    )


# --------------------------------------------------------------------------- #
# Scoring — deterministic structural checks
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def score(spec: DomainSpec, shape: Shape) -> list[Check]:
    minted = set(shape.types_created) | set(shape.classes)
    checks: list[Check] = []

    # 1. expected reusable NODE types present
    for t in spec.expect_node_types:
        present = t in minted
        checks.append(Check(f"node:{t}", present,
                             "present" if present else "missing"))

    # 2. role SUBTYPES present (distinct role types, not one bucket)
    for t in spec.expect_subtypes:
        present = t in minted
        checks.append(Check(f"subtype:{t}", present,
                             "present" if present else "folded away / missing"))

    # 3. multi-valued SPLIT: the best-matching synonym predicate (OR semantics —
    #    the README accepts a synonym set) must be an EDGE that REPEATS past
    #    1-per-instance, i.e. more edges than primary instances — the honest signal
    #    that comma-joined values were split, not that a single edge exists. Any
    #    edge already beats the flat "glued literal" cage; > prim is the strict bar.
    prim = shape.instances_by_type.get(spec.proposed_type, 0)
    if spec.expect_multivalued_edges:
        best_pred, best_n = "", 0
        for pred in spec.expect_multivalued_edges:
            n = shape.obj_predicates.get(pred, 0)
            if n > best_n:
                best_pred, best_n = pred, n
        ok = best_n > prim if prim else best_n > 0
        note = f"{best_pred or '(none)'}={best_n} edges vs {prim} primary instances"
        checks.append(Check("multivalued_split", ok,
                            note if ok else "not split into >1-per-instance edges"))

    # 4. literal attrs stayed literals (not promoted to a node type)
    for a in spec.expect_literal_attrs:
        pas = a.replace("_", " ").title().replace(" ", "")
        promoted = a.capitalize() in minted or pas in minted
        checks.append(Check(f"literal:{a}", not promoted,
                             "literal" if not promoted else "wrongly promoted to a node"))

    # 5. dedupe headline: exact primary instance count (same-name-diff-city stay distinct)
    if spec.expect_primary_count is not None:
        got = shape.instances_by_type.get(spec.proposed_type, 0)
        ok = got == spec.expect_primary_count
        checks.append(Check(f"dedupe:{spec.proposed_type}", ok,
                            f"got {got} instances, expected {spec.expect_primary_count}"))

    # 6. reusability headline: shared dimension nodes collapse to N distinct
    for t, want in spec.expect_node_counts.items():
        got = shape.instances_by_type.get(t, 0)
        ok = got == want
        checks.append(Check(f"reuse:{t}", ok, f"got {got} distinct, expected {want}"))

    # 7. new-type count in the sane band (excluding synthesized abstract ancestors)
    n_new = len(set(shape.types_created) - _ABSTRACT_ANCESTORS)
    lo, hi = spec.new_types_range
    ok = lo <= n_new <= hi
    checks.append(Check("new_types_in_band", ok,
                        f"{n_new} new (band {lo}-{hi}) -> "
                        f"{'ok' if ok else ('FLATTENED' if n_new < lo else 'FRAGMENTED')}"))

    return checks


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
POLICY_NAMES = {"A": "constrained", "B": "unconstrained", "C": "seed"}


def load_rows(spec: DomainSpec) -> list[dict]:
    if spec.fixture.exists():
        return json.loads(spec.fixture.read_text())
    raise FileNotFoundError(f"fixture missing: {spec.fixture} (run the fixtures agent first)")


def flat_attributes(rows: list[dict], key_attr: str) -> list[str]:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k != key_attr and k not in keys:
                keys.append(k)
    return keys


async def run_policy(neptune, spec: DomainSpec, policy: str, rows: list[dict]) -> Shape:
    tenant = f"decomp-{spec.name}-{policy}"
    await clear_all(neptune)

    # reconcile test: pre-seed a small existing ontology (deterministic, no LLM)
    if spec.seed_ontology:
        seed_path = FIXTURES / spec.seed_ontology
        if seed_path.exists():
            await seed_ontology(neptune, tenant, json.loads(seed_path.read_text()))

    resolver = make_resolver(neptune)
    kwargs: dict = dict(content_type="json", source=f"decomp:{policy}")
    if policy in ("A", "C"):
        # Same focus type + attribute set discovery confirms with the user.
        # A = HARD cage (flatten); C = SOFT seed (decompose, the fix).
        kwargs["constrain_types"] = [spec.proposed_type]
        kwargs["constrain_attributes"] = {
            spec.proposed_type: flat_attributes(rows, spec.key_attr)
        }
        if policy == "C":
            kwargs["constrain_soft"] = True
    # policy B: no constraint (open document path — the correctness reference).

    t0 = time.monotonic()
    res = await resolver.ingest(json.dumps(rows), tenant, **kwargs)
    dt = time.monotonic() - t0
    shape = await read_shape(neptune, tenant, res)
    shape_dt = dt
    print(f"    [{policy}/{POLICY_NAMES[policy]}] ingest {dt:5.1f}s  "
          f"types={sorted(set(res.types_created))}")
    return shape


def print_scorecard(spec: DomainSpec, results: dict[str, Shape]) -> dict:
    print(f"\n===== {spec.name} =====")
    summary = {}
    for policy, shape in results.items():
        checks = score(spec, shape)
        passed = sum(1 for c in checks if c.ok)
        total = len(checks)
        summary[policy] = {"passed": passed, "total": total,
                           "types": sorted(set(shape.types_created))}
        print(f"\n  policy {policy} ({POLICY_NAMES[policy]}): {passed}/{total} checks")
        print(f"    types_created : {sorted(set(shape.types_created))}")
        print(f"    obj_edges     : {shape.obj_predicates}")
        print(f"    subclass      : {shape.subclass}")
        for c in checks:
            print(f"      [{'PASS' if c.ok else 'FAIL'}] {c.name:24s} {c.detail}")
    return summary


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://127.0.0.1:3030")
    ap.add_argument("--domain", default=None, help="one domain key")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--policy", default="A,B", help="comma list of A,B,C")
    args = ap.parse_args()

    if not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OMNIX_OPENROUTER_API_KEY")):
        print("!! OPENROUTER_API_KEY not set — source the repo .env first", file=sys.stderr)
        sys.exit(2)

    policies = [p.strip() for p in args.policy.split(",") if p.strip()]
    domains = (
        list(SPECS) if args.all
        else [d.strip() for d in (args.domain or "").split(",") if d.strip()]
    )
    if not domains:
        print("specify --domain <key[,key...]> or --all", file=sys.stderr)
        sys.exit(2)

    neptune = NeptuneClient(endpoint=args.endpoint, backend="fuseki")
    if not await neptune.health():
        print(f"!! store not healthy at {args.endpoint}", file=sys.stderr)
        sys.exit(2)

    overall = {}
    for dkey in domains:
        spec = SPECS[dkey]
        try:
            rows = load_rows(spec)
        except FileNotFoundError as e:
            print(f"  skip {dkey}: {e}")
            continue
        print(f"\n#### {dkey}: {len(rows)} rows, proposed_type={spec.proposed_type}")
        results: dict[str, Shape] = {}
        for policy in policies:
            try:
                results[policy] = await run_policy(neptune, spec, policy, rows)
            except Exception as e:  # noqa: BLE001 — one domain must not sink the run
                print(f"    [{policy}] ERROR: {type(e).__name__}: {e}")
        if results:
            overall[dkey] = print_scorecard(spec, results)

    print("\n\n========== SUMMARY ==========")
    for dkey, summ in overall.items():
        line = "  ".join(f"{p}:{s['passed']}/{s['total']}" for p, s in summ.items())
        print(f"  {dkey:22s} {line}")
    await neptune.close()


if __name__ == "__main__":
    asyncio.run(main())
