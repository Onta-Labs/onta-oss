"""Regression tests for SparqlBlocker.

Guards the index-write path that ingest depends on. A refactor once moved
`index_triples` out of the class (a module-level function below it absorbed it
as dead code), so ER wrote no blockKey triples on fresh ingests and entity
resolution silently stopped working for all new data. These tests fail loudly
if that recurs.
"""

from __future__ import annotations

from cograph_client.resolver.er.blocking import (
    SparqlBlocker,
    _bindings_to_signals,
    generate_block_keys,
)
from cograph_client.resolver.er.normalize import DefaultNormalizer
from cograph_client.resolver.er.scoring import DefaultScorer
from cograph_client.resolver.er.types import DEFAULT_ORG_CONFIG, EntitySignals


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
