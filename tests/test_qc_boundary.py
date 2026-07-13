"""Boundary-fixture characterization tests (ONTA-266).

The harness (``cograph_client.qc.boundary``) renders the ingestion pipeline's three
inter-stage artifacts — A2 candidate facts, A4 verified facts, A5 placement plan — from
the canonical decomp datasets through the pipeline's REAL deterministic transforms
(``validate_triple``, ``resolve_attribute``, ``normalize_predicate``, the URI/ontology
builders). These tests are the diff guard: a re-render must byte-match the frozen
fixtures, so a later refactor that changes what one stage hands the next is caught.

Fully deterministic + offline — no LLM, no store. To intentionally update the frozen
fixtures after a real behavior change:  ``python -m cograph_client.qc.boundary --freeze``.
"""
from __future__ import annotations

import json

import pytest

from cograph_client.qc import boundary as b
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionResult,
)

DOMAINS = list(b.DEFAULT_DOMAINS)
TIERS = list(b.TIERS)


# --------------------------------------------------------------------------- #
# (3) The diff test: a re-render must match the frozen fixtures, per domain × tier.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("tier", TIERS)
def test_frozen_fixture_matches_rerender(domain, tier):
    """The characterization guard: re-rendering a domain's artifact from the canonical
    dataset reproduces the committed fixture byte-for-byte. If this fails, either a stage
    transform changed (investigate) or the change is intentional (re-freeze)."""
    path = b._fixture_path(b.default_fixtures_dir(), domain, tier)
    assert path.exists(), f"missing frozen fixture {path} — run `--freeze`"
    frozen = path.read_text()
    rendered = b._dumps(b.render_domain(domain).tier(tier))
    assert rendered == frozen, (
        f"{domain}.{tier} drifted from its frozen fixture. "
        f"If intentional, re-freeze with `python -m cograph_client.qc.boundary --freeze`."
    )


def test_check_reports_no_drift_for_all_domains():
    """The programmatic diff (used by the CLI + CI gate) is clean against the repo."""
    assert b.check() == []


# --------------------------------------------------------------------------- #
# Coverage / shape: >=3 representative domains, all three tiers non-trivial.
# --------------------------------------------------------------------------- #
def test_at_least_three_domains_with_all_tiers_populated():
    assert len(DOMAINS) >= 3
    for domain in DOMAINS:
        arts = b.render_domain(domain)
        assert arts.a2["entities"], f"{domain}: empty A2"
        assert arts.a4["verified_facts"], f"{domain}: empty A4"
        assert arts.a5["types"] and arts.a5["relationship_edges"], f"{domain}: empty A5"


# --------------------------------------------------------------------------- #
# Determinism — the property that lets the artifact be frozen at all.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("domain", DOMAINS)
def test_render_is_deterministic(domain):
    first = b.render_domain(domain)
    second = b.render_domain(domain)
    for tier in TIERS:
        assert b._dumps(first.tier(tier)) == b._dumps(second.tier(tier))


# --------------------------------------------------------------------------- #
# (3) The guard bites: a mutated artifact is detected as divergence.
# --------------------------------------------------------------------------- #
def test_diff_flags_divergence_when_frozen_file_mutated(tmp_path):
    """Freeze into a temp dir, corrupt one artifact, and confirm ``check`` flags exactly
    that domain/tier — proving the diff test would actually catch a refactor regression."""
    b.freeze(["coffee_shops"], out_dir=tmp_path)
    assert b.check(["coffee_shops"], out_dir=tmp_path) == []

    target = b._fixture_path(tmp_path, "coffee_shops", "a5")
    payload = json.loads(target.read_text())
    payload["types"].append({"name": "Injected", "uri": "x", "subclass_of": [], "kind": "primary"})
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    diffs = b.check(["coffee_shops"], out_dir=tmp_path)
    assert len(diffs) == 1
    assert diffs[0].domain == "coffee_shops" and diffs[0].tier == "a5"
    assert "types" in diffs[0].reason


def test_check_flags_missing_fixture(tmp_path):
    diffs = b.check(["llm_models"], out_dir=tmp_path)  # nothing frozen here
    assert {(d.tier, "missing" in d.reason) for d in diffs} == {("a2", True), ("a4", True), ("a5", True)}


def test_freeze_roundtrips(tmp_path):
    written = b.freeze(out_dir=tmp_path)
    assert len(written) == len(DOMAINS) * len(TIERS)
    assert all(p.exists() for p in written)
    assert b.check(out_dir=tmp_path) == []


# --------------------------------------------------------------------------- #
# A4 is the REAL verification stage (validate_triple), not a re-implementation.
# --------------------------------------------------------------------------- #
def test_a4_types_and_canonicalizes_literals_via_real_validator():
    """A4 must carry the validator's typed/canonical literal forms — a bare float gets an
    xsd:float tag, a boolean is lower-cased to its canonical lexical form."""
    ext = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Widget",
                id="W1",
                attributes=[
                    ExtractedAttribute(name="rating", value="4.6", datatype="float"),
                    ExtractedAttribute(name="active", value="True", datatype="boolean"),
                ],
            )
        ]
    )
    a4 = b.capture_boundary(ext, "unit").a4
    objs = {f["predicate"].rsplit("/", 1)[-1]: f["object"] for f in a4["verified_facts"]}
    assert objs["rating"] == "4.6^^http://www.w3.org/2001/XMLSchema#float"
    # "True" is accepted by validate_value but canonicalized to "true" by _typed_value.
    assert objs["active"] == "true^^http://www.w3.org/2001/XMLSchema#boolean"


def test_a4_records_coercion_outcome():
    """A value that must be COERCED to fit its datatype is captured with the COERCED
    outcome and its original value — straight from the real validator."""
    ext = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Widget",
                id="W1",
                # "4.6" does not conform to integer, but coerces to "4".
                attributes=[ExtractedAttribute(name="count", value="4.6", datatype="integer")],
            )
        ]
    )
    fact = b.capture_boundary(ext, "unit").a4["verified_facts"][0]
    assert fact["outcome"] == "COERCED"
    assert fact["object"] == "4^^http://www.w3.org/2001/XMLSchema#integer"
    assert fact["original_value"] == "4.6"


def test_a4_captures_rejections():
    """A value the validator can neither accept nor coerce lands in A4 rejections, not
    verified_facts — so the boundary snapshot never hides a dropped value."""
    ext = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Widget",
                id="W1",
                attributes=[ExtractedAttribute(name="when", value="not-a-date", datatype="datetime")],
            )
        ]
    )
    a4 = b.capture_boundary(ext, "unit").a4
    assert a4["verified_facts"] == []
    assert len(a4["rejections"]) == 1 and a4["rejections"][0]["expected_datatype"] == "datetime"


# --------------------------------------------------------------------------- #
# A5 is the ontology mapping through the REAL URI + predicate conventions.
# --------------------------------------------------------------------------- #
def test_a5_places_literals_and_edges_on_the_right_predicates():
    """A literal attribute is DECLARED on ``attrs/<leaf>``; a relationship's INSTANCE
    edge lands on ``onto/<leaf>`` (the NL-queryable predicate) — the exact convention the
    writer enforces (see graph/predicates.py). Getting these swapped is the classic
    NL-invisible-edge bug, so the boundary snapshot pins them."""
    ext = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="CoffeeShop",
                id="Fernwood",
                attributes=[ExtractedAttribute(name="rating", value="4.6", datatype="float")],
            ),
            ExtractedEntity(type_name="City", id="Portland"),
        ],
        relationships=[
            ExtractedRelationship(source_id="Fernwood", predicate="located_in", target_id="Portland")
        ],
    )
    a5 = b.capture_boundary(ext, "unit").a5

    literal = next(d for d in a5["attribute_declarations"] if d["name"] == "rating")
    assert literal["kind"] == "literal"
    assert literal["uri"] == "https://cograph.tech/types/CoffeeShop/attrs/rating"

    rel_decl = next(d for d in a5["attribute_declarations"] if d["name"] == "located_in")
    assert rel_decl["kind"] == "relationship"
    assert rel_decl["range"] == "https://cograph.tech/types/City"

    edge = a5["relationship_edges"][0]
    assert edge["predicate"] == "https://cograph.tech/onto/located_in"
    assert edge["subject"] == "https://cograph.tech/entities/CoffeeShop/Fernwood"
    assert edge["object"] == "https://cograph.tech/entities/City/Portland"

    # the target City is materialized as a first-class node (rdf:type + label).
    node = next(m for m in a5["node_materializations"] if m["uri"].endswith("/City/Portland"))
    assert node["rdf_type"] == "https://cograph.tech/types/City" and node["label"] == "Portland"


def test_a5_captures_subtype_lineage():
    """A subtype entity (parent_chain) yields a subClassOf chain in A5 — child ->
    parent[0] -> parent[1] — the ADR 0001 lineage the writer closes."""
    ext = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="NursePractitioner",
                id="NP1",
                parent_chain=["HealthcareProvider", "Person"],
            )
        ]
    )
    types = {t["name"]: t for t in b.capture_boundary(ext, "unit").a5["types"]}
    assert types["NursePractitioner"]["subclass_of"] == ["https://cograph.tech/types/HealthcareProvider"]
    assert types["HealthcareProvider"]["subclass_of"] == ["https://cograph.tech/types/Person"]
    assert types["Person"]["subclass_of"] == []


def test_a5_reuses_shared_target_nodes_across_rows():
    """Two entities pointing at the same real-world value share ONE target node URI (the
    reuse the placement plan should show), not two."""
    ext = ExtractionResult(
        entities=[
            ExtractedEntity(type_name="CoffeeShop", id="A"),
            ExtractedEntity(type_name="CoffeeShop", id="B"),
            ExtractedEntity(type_name="City", id="Portland"),
        ],
        relationships=[
            ExtractedRelationship(source_id="A", predicate="located_in", target_id="Portland"),
            ExtractedRelationship(source_id="B", predicate="located_in", target_id="Portland"),
        ],
    )
    a5 = b.capture_boundary(ext, "unit").a5
    cities = [m for m in a5["node_materializations"] if "/City/" in m["uri"]]
    assert len(cities) == 1  # one shared Portland node


# --------------------------------------------------------------------------- #
# A2 is the candidate-fact extraction, and it re-renders from the canonical dataset.
# --------------------------------------------------------------------------- #
def test_a2_decomposes_rows_into_typed_entities_and_edges():
    from cograph_client.qc.boundary import BOUNDARY_SPECS, render_extraction, _load_rows

    spec = BOUNDARY_SPECS["coffee_shops"]
    ext = render_extraction(_load_rows("coffee_shops"), spec)
    primaries = [e for e in ext.entities if e.type_name == "CoffeeShop"]
    assert primaries, "no primary CoffeeShop entities extracted"
    # multi-valued 'categories' became repeated has_category edges (more than one shop).
    cat_edges = [r for r in ext.relationships if r.predicate == "has_category"]
    assert len(cat_edges) > len(primaries)
    # the key column stayed a literal attribute on the primary entity.
    assert any(a.name == "shop_id" for a in primaries[0].attributes)
