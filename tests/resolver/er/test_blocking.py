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
from cograph_client.resolver.er.types import EntitySignals


def test_index_triples_is_a_class_staticmethod():
    # The ingest path calls `self._er._blocker.index_triples(...)`; if this
    # isn't an attribute of the class, every ingest logs er_pipeline_failed and
    # writes zero ER index triples.
    assert hasattr(SparqlBlocker, "index_triples")
    assert callable(SparqlBlocker.index_triples)


def test_index_triples_emits_blockkey_and_signals():
    normalized = DefaultNormalizer().normalize(
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
