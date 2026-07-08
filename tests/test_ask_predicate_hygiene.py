"""ER/ingest internals must NOT leak into the NL `ask` answer text.

The Explorer already hides internal/housekeeping predicates (`er/blockKey`,
`er/erSignal_*`, `onto/batch_id`, `onto/norm/*`) from its Attributes /
Relationships panels via `_is_internal_predicate`, but the NL `ask` path rendered
every SPARQL binding verbatim — so a "describe this entity" (`SELECT ?p ?o`) or a
"list all predicates" (`SELECT DISTINCT ?p`) query dumped the ER/ingest plumbing
straight into the answer.

The fix lifts the filter into `cograph_client.graph.predicates.is_internal_predicate`
(ONE definition, shared by `explore.py` AND the pipeline) and applies it at
render-time in `_format_answer` (and the narrative rephrase). These tests assert
on the MECHANISM using INVENTED predicate/entity tokens across two unrelated
domains — no persona-token special-casing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cograph_client.graph.predicates import is_internal_predicate
from cograph_client.nlp.pipeline import (
    NLQueryPipeline,
    _drop_internal_predicate_rows,
)

ER = "https://cograph.tech/er/"
ONTO = "https://cograph.tech/onto/"
ONTO_NORM = ONTO + "norm/"
ATTRS = "https://cograph.tech/types/{t}/attrs/{a}"
ENTITIES = "https://cograph.tech/entities/"


def _empty_neptune():
    """Neptune stub: label-resolution queries return no rows (URIs display as-is)."""
    n = AsyncMock()
    n.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    return n


def _pipe():
    return NLQueryPipeline(_empty_neptune(), anthropic_key="dummy")


# --------------------------------------------------------------- shared-filter one def
def test_filter_is_the_one_shared_definition():
    """explore.py must re-export the SAME helper object, not a copy."""
    from cograph_client.api.routes import explore

    assert explore._is_internal_predicate is is_internal_predicate


# ------------------------------------------------------------------ row-level dropper
def test_drop_er_and_ingest_rows_keeps_attributes_and_relationships():
    # Invented voice-AI-ish domain, but tokens are made up (no persona overfit).
    rows = [
        {"p": ATTRS.format(t="VoiceEngine", a="latency_ms"), "o": "42"},
        {"p": ER + "blockKey", "o": "soundex_finit:Q914z"},
        {"p": ER + "erSignal_name", "o": "acme voice"},
        {"p": ER + "erSignal_endpoint", "o": "wss://x"},
        {"p": ONTO + "batch_id", "o": "b2c1-uuid-0000"},
        {"p": ONTO_NORM + "canonical_name", "o": "acme"},
        # a REAL relationship edge on onto/<leaf> → an entity IRI: must survive.
        {"p": ONTO + "poweredBy", "o": ENTITIES + "Vendor/acme"},
    ]
    kept = _drop_internal_predicate_rows(rows)
    kept_preds = {r["p"] for r in kept}
    assert ATTRS.format(t="VoiceEngine", a="latency_ms") in kept_preds
    assert ONTO + "poweredBy" in kept_preds  # relationship preserved
    for internal in (ER + "blockKey", ER + "erSignal_name", ER + "erSignal_endpoint",
                     ONTO + "batch_id", ONTO_NORM + "canonical_name"):
        assert internal not in kept_preds


def test_drop_does_not_touch_ordinary_attribute_projection():
    # A `SELECT ?name ?empty` shape: literal values (incl. empty string) never
    # look like a predicate URI, so no row is dropped.
    rows = [
        {"name": "Warehouse North", "capacity": "1200"},
        {"name": "Warehouse East", "capacity": ""},
    ]
    assert _drop_internal_predicate_rows(rows) == rows


# --------------------------------------------------------------- _format_answer (E2E)
async def test_format_answer_hides_er_internals_voice_domain():
    pipe = _pipe()
    bindings = [
        {"p": ATTRS.format(t="TTSEngine", a="sample_rate"), "o": "24000"},
        {"p": ER + "blockKey", "o": "soundex_finit:T320g"},
        {"p": ER + "erSignal_phone_e164", "o": "+15550001"},
        {"p": ONTO + "batch_id", "o": "9f1c-uuid"},
    ]
    out = await pipe._format_answer(bindings, explanation="describe engine")
    assert "sample_rate" in out and "24000" in out
    assert "blockKey" not in out
    assert "erSignal_phone_e164" not in out
    assert "batch_id" not in out


async def test_format_answer_hides_er_internals_logistics_domain():
    # A totally unrelated invented domain — proves the fix is general.
    pipe = _pipe()
    bindings = [
        {"p": ATTRS.format(t="Depot", a="dock_count"), "o": "8"},
        {"p": ER + "erSignal_address", "o": "12 industrial way"},
        {"p": ONTO_NORM + "canonical_city", "o": "springfield"},
        # real relationship to an entity — must remain
        {"p": ONTO + "servesRegion", "o": ENTITIES + "Region/midwest"},
    ]
    out = await pipe._format_answer(bindings, explanation="describe depot")
    assert "dock_count" in out
    assert "servesRegion" in out  # relationship preserved
    assert "erSignal_address" not in out
    assert "canonical_city" not in out


async def test_format_answer_all_internal_reports_empty():
    # A dump that is ENTIRELY ER/ingest plumbing must not surface any of it.
    pipe = _pipe()
    bindings = [
        {"p": ER + "blockKey", "o": "x"},
        {"p": ER + "erSignal_name", "o": "y"},
        {"p": ONTO + "batch_id", "o": "z"},
    ]
    out = await pipe._format_answer(bindings, explanation="describe")
    assert "No results found" in out
    for leak in ("blockKey", "erSignal_name", "batch_id"):
        assert leak not in out


async def test_format_answer_select_distinct_predicates_filtered():
    # `SELECT DISTINCT ?p` shape: a single ?p column per row.
    pipe = _pipe()
    bindings = [
        {"p": ATTRS.format(t="Depot", a="name")},
        {"p": ER + "blockKey"},
        {"p": ONTO + "batch_id"},
        {"p": ONTO + "servesRegion"},  # relationship predicate (no object col) — keep
    ]
    out = await pipe._format_answer(bindings, explanation="list predicates")
    assert "name" in out
    assert "servesRegion" in out
    assert "blockKey" not in out
    assert "batch_id" not in out
