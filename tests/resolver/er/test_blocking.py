"""Regression tests for SparqlBlocker.

Guards the index-write path that ingest depends on. A refactor once moved
`index_triples` out of the class (a module-level function below it absorbed it
as dead code), so ER wrote no blockKey triples on fresh ingests and entity
resolution silently stopped working for all new data. These tests fail loudly
if that recurs.
"""

from __future__ import annotations

import pytest

from infona_client.resolver.er.blocking import (
    SparqlBlocker,
    _bindings_to_signals,
    generate_block_keys,
)
from infona_client.resolver.er.normalize import DefaultNormalizer
from infona_client.resolver.er.scoring import DefaultScorer
from infona_client.resolver.er.types import DEFAULT_ORG_CONFIG, EntitySignals


N = DefaultNormalizer()


def test_index_triples_is_a_class_staticmethod():
    # The ingest path calls `self._er._blocker.index_triples(...)`; if this
    # isn't an attribute of the class, every ingest logs er_pipeline_failed and
    # writes zero ER index triples.
    assert hasattr(SparqlBlocker, "index_triples")
    assert callable(SparqlBlocker.index_triples)


def test_index_triples_emits_blockkey_and_signals():
    normalized = N.normalize(
        EntitySignals(name="John Smith", email="john@x.com", phone="+12125550001")
    )
    keys = generate_block_keys(normalized)
    assert keys, "expected block keys for a name+email+phone entity"
    triples = SparqlBlocker.index_triples("uri:person1", normalized, keys)
    preds = {p for _, p, _ in triples}
    assert any("blockKey" in p for p in preds)
    assert any("erSignal_email" in p for p in preds)
    # Subject is the entity URI for every triple.
    assert all(s == "<uri:person1>" for s, _, _ in triples)


def test_bindings_to_signals_remains_module_level():
    # It must stay a module function (not swallow the class methods after it).
    assert callable(_bindings_to_signals)
    assert _bindings_to_signals([]) == {}


def test_org_name_variants_share_block_key():
    """Acme Corp / ACME Corporation / Acme Corp. must co-block (dogfood S4).

    Pre-fix, person-shaped soundex(last)+first_initial never co-blocked them
    (Corp→C610 vs Corporation→C616). Legal-suffix strip + name_core /
    soundex_core keys fix that.
    """
    variants = ["Acme Corp", "ACME Corporation", "Acme Corp."]
    key_sets = []
    for name in variants:
        keys = generate_block_keys(N.normalize(EntitySignals(name=name)))
        key_sets.append({(k.kind, k.value) for k in keys})
        kinds = {k.kind for k in keys}
        assert "name_core" in kinds or "soundex_core" in kinds, (
            f"{name!r} produced no org-friendly block key: {keys}"
        )

    shared = key_sets[0]
    for ks in key_sets[1:]:
        shared &= ks
    assert shared, (
        f"org variants share no block key: {[sorted(ks) for ks in key_sets]}"
    )
    assert any(kind in {"name_core", "soundex_core"} for kind, _ in shared)


def test_org_variants_score_above_auto_merge():
    """Shared block is not enough — score must clear DEFAULT_ORG_CONFIG threshold."""
    a = N.normalize(EntitySignals(name="Acme Corp"))
    b = N.normalize(EntitySignals(name="ACME Corporation"))
    c = N.normalize(EntitySignals(name="Acme Corp."))
    scorer = DefaultScorer()
    for left, right in ((a, b), (a, c), (b, c)):
        result = scorer.score(left, right, DEFAULT_ORG_CONFIG)
        assert result.score >= DEFAULT_ORG_CONFIG.auto_merge_threshold, (
            f"{left.name!r} vs {right.name!r}: score={result.score} "
            f"< auto_merge={DEFAULT_ORG_CONFIG.auto_merge_threshold}"
        )


def test_person_email_phone_blocking_still_works():
    """Org keys must not displace person email / phone block strategies."""
    normalized = N.normalize(
        EntitySignals(name="John Smith", email="john@x.com", phone="+12125550001")
    )
    keys = generate_block_keys(normalized)
    kinds = {k.kind for k in keys}
    assert "email_local" in kinds
    assert "lastname3_phone4" in kinds
    # Two-token person name still gets soundex_finit
    assert "soundex_finit" in kinds
    # Org-friendly keys are additive, not exclusive
    assert "name_core" in kinds
    assert "soundex_core" in kinds


@pytest.mark.asyncio
async def test_candidate_lookup_is_scoped_by_graph_type_and_block_keys():
    """The candidate lookup must really apply all three of its arguments.

    ONTA-527 port: this used to assert that the lookup's SPARQL string embedded
    the graph / type / block keys rather than raw ``{instance_graph}`` braces —
    the regression being a template that never got ``.format()``'d, so every
    lookup ran unscoped. That SPARQL is gone (a GraphStore is always present, so
    ``SparqlBlocker`` delegates to ``GraphStoreBlocker``), but the property it
    was a proxy for is unchanged and now checked directly: seed one KG, and the
    lookup must find the match HERE and nothing at all under a different KG or a
    different type. Neptune is passed as None — reaching for it would raise.
    """
    from infona_client.graph.kg_writer import insert_facts

    rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    graph = "https://graph.infona.ai/graphs/t/kg/k"
    other_graph = "https://graph.infona.ai/graphs/t/kg/other"
    person = "https://graph.infona.ai/types/Person"
    org = "https://graph.infona.ai/types/Organization"

    normalized = N.normalize(
        EntitySignals(name="A B", email="a@b.com", phone="+12125550000")
    )
    keys = generate_block_keys(normalized)
    assert keys, "expected block keys for a name+email+phone entity"

    match = "https://graph.infona.ai/entities/Person/ab"
    stranger = "https://graph.infona.ai/entities/Person/zz"
    await insert_facts(
        None,
        graph,
        [(match, rdf_type, person)]
        + SparqlBlocker.index_triples(match, normalized, keys),
    )
    # Same KG + type, but no block key in common → must not be a candidate.
    other_norm = N.normalize(
        EntitySignals(name="Z Q", email="zq@elsewhere.com", phone="+12125559999")
    )
    await insert_facts(
        None,
        graph,
        [(stranger, rdf_type, person)]
        + SparqlBlocker.index_triples(
            stranger, other_norm, generate_block_keys(other_norm)
        ),
    )
    # An identical entity in a DIFFERENT KG must stay invisible.
    await insert_facts(
        None,
        other_graph,
        [("https://graph.infona.ai/entities/Person/ab", rdf_type, person)]
        + SparqlBlocker.index_triples(
            "https://graph.infona.ai/entities/Person/ab", normalized, keys
        ),
    )

    blocker = SparqlBlocker(None)
    assert list(
        await blocker.candidates_with_signals(graph, person, keys)
    ) == [match]
    # …scoped by TYPE…
    assert await blocker.candidates_with_signals(graph, org, keys) == {}
    # …and by block KEYS (an entity sharing none of them is not a candidate).
    assert stranger not in await blocker.candidates_with_signals(graph, person, keys)
    # …and no keys at all short-circuits to no candidates.
    assert await blocker.candidates_with_signals(graph, person, []) == {}


def test_northern_lights_org_variants_share_block_key():
    """Anti-overfit: org ER must work beyond Acme Corp fixtures."""
    variants = [
        "Northern Lights Logistics",
        "Northern Lights Logistics Inc",
        "NORTHERN LIGHTS LOGISTICS",
    ]
    cores = []
    for name in variants:
        norm = N.normalize(EntitySignals(name=name))
        keys = {(k.kind, k.value) for k in generate_block_keys(norm)}
        cores.append(keys)
    shared = cores[0]
    for c in cores[1:]:
        shared &= c
    assert shared, f"no shared block keys across variants: {cores}"


def test_polar_freight_llc_variants_score_for_org_merge():
    """Anti-overfit: LLC / L.L.C. collapse under DEFAULT_ORG_CONFIG."""
    a = N.normalize(EntitySignals(name="Polar Freight LLC"))
    b = N.normalize(EntitySignals(name="Polar Freight L.L.C."))
    score = DefaultScorer().score(a, b, DEFAULT_ORG_CONFIG)
    assert score.score >= DEFAULT_ORG_CONFIG.auto_merge_threshold
