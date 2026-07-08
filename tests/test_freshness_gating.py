"""ONTA-247 — freshness stamp typed xsd:dateTime + generic recency filter.

MECHANISM tests on INVENTED types/attrs across ≥2 unrelated domains (Widget/sku,
Gadget/weight_kg, Sprocket/diameter_mm) — no persona token appears. Proves:
  * an enriched value's `<attr>_verified_at` lands as a TYPED xsd:dateTime literal;
  * a generic NOW()-relative "last N days" FILTER (the pattern the NL prompt now
    teaches) selects fresh rows and excludes stale ones on a REAL SPARQL engine;
  * discovery (not only enrichment) stamps the per-fact typed stamp.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import EnrichmentExecutor, _attr_uri, _now
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import ConflictPolicy, Verdict
from cograph_client.graph.provenance import build_attribute_provenance_companions

from tests._enrichment_prov_helpers import (
    DOMAINS,
    XSD_DATETIME,
    FakeWikidata,
    all_updates,
    entities_query_response,
    make_job,
    query_router,
)


@pytest.mark.parametrize("type_name,attr,label,value,src", DOMAINS)
def test_verified_at_is_typed_datetime_literal(type_name, attr, label, value, src, monkeypatch):
    """An enriched value's `<attr>_verified_at` stamp is written as a TYPED
    xsd:dateTime literal (not a plain string), so SPARQL date arithmetic can filter
    it. Two unrelated invented domains."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [{"uri": f"https://cograph.tech/entities/{type_name}/e1", "label": label, "vals": ""}]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({(label, attr): [Verdict(value=value, confidence=0.95, source="wikidata")]}),
        )
        job = make_job(type_name=type_name, attributes=[attr], policy=ConflictPolicy.overwrite)
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        writes = all_updates(neptune)
        assert _attr_uri(type_name, f"{attr}_verified_at") in writes
        assert XSD_DATETIME in writes, "verified_at must be a typed xsd:dateTime literal"

    asyncio.run(run())


def test_recency_filter_selects_and_excludes_by_window():
    """A generic NOW()-relative recency FILTER over a typed `<attr>_verified_at`
    (the EXACT pattern the NL prompt now teaches, AFTER the Neptune-safe duration
    normalizer runs) returns fresh rows and excludes stale ones. Runs against a REAL
    pyoxigraph SPARQL engine on invented schema."""
    pytest.importorskip("pyoxigraph")
    from pyoxigraph import QueryResultsFormat, Store

    from cograph_client.nlp.pipeline import _neptune_safe_duration

    store = Store()
    graph = "https://cograph.tech/graphs/test-tenant/kg/kg"
    vpred = _attr_uri("Widget", "sku_verified_at")
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=3)).isoformat()
    stale = (now - timedelta(days=30)).isoformat()

    store.update(
        f"INSERT DATA {{ GRAPH <{graph}> {{ "
        f'<urn:w:fresh> <{vpred}> "{fresh}"^^<{XSD_DATETIME}> . '
        f'<urn:w:stale> <{vpred}> "{stale}"^^<{XSD_DATETIME}> . '
        f"}} }}"
    )

    for window, expect in [("P7D", {"urn:w:fresh"}), ("P60D", {"urn:w:fresh", "urn:w:stale"})]:
        # Start from the dayTimeDuration form the LLM tends to emit (SPARQL 1.1 spec),
        # then run it through the normalizer the pipeline applies before execution.
        raw = (
            f"SELECT ?e FROM <{graph}> WHERE {{ "
            f"?e <{vpred}> ?ts . "
            f'FILTER(?ts >= (NOW() - "{window}"^^'
            f"<http://www.w3.org/2001/XMLSchema#dayTimeDuration>)) }}"
        )
        q = _neptune_safe_duration(raw)
        # The Neptune-unsupported datatype must be gone (this is the crux of the fix —
        # the query as sent to Neptune must never carry dayTimeDuration).
        assert "dayTimeDuration" not in q
        assert "XMLSchema#duration" in q
        res = json.loads(store.query(q).serialize(format=QueryResultsFormat.JSON))
        got = {b["e"]["value"] for b in res["results"]["bindings"]}
        assert got == expect, f"{window}: {got} != {expect}"


def test_neptune_safe_duration_rewrites_all_surface_forms():
    """The normalizer rewrites every surface form of a duration-subtype datatype to
    `duration` while preserving the prefix/IRI style the LLM emitted, and leaves an
    already-`duration` literal (and unrelated SPARQL) untouched. Reproduces the exact
    400-causing construct from the persona-eval m3 run and asserts the replacement.

    Neptune reproduction (deployed cluster, invented data-free probes):
      * `SELECT ((NOW() - "P14D"^^xsd:dayTimeDuration) AS ?cutoff) …`  → ?cutoff DROPPED
        (silently unbound); over real rows the recency FILTER returned COUNT 0.
      * same query with `xsd:duration`                               → ?cutoff computes;
        recency FILTER returned the 33 fresh rows.
    """
    from cograph_client.nlp.pipeline import _neptune_safe_duration

    XSD = "http://www.w3.org/2001/XMLSchema#"
    cases = [
        # full IRI in angle brackets (what the prompt teaches / the LLM emits)
        (f'FILTER(?ts >= (NOW() - "P7D"^^<{XSD}dayTimeDuration>))',
         f'FILTER(?ts >= (NOW() - "P7D"^^<{XSD}duration>))'),
        # yearMonthDuration is likewise unsupported by Neptune
        (f'BIND((NOW() - "P1M"^^<{XSD}yearMonthDuration>) AS ?c)',
         f'BIND((NOW() - "P1M"^^<{XSD}duration>) AS ?c)'),
        # bare xsd: prefix, no angle brackets → keep the prefix
        ('FILTER(?ts >= (NOW() - "PT48H"^^xsd:dayTimeDuration))',
         'FILTER(?ts >= (NOW() - "PT48H"^^xsd:duration))'),
        # full IRI WITHOUT angle brackets
        (f'BIND((NOW() - "P14D"^^{XSD}dayTimeDuration) AS ?cutoff)',
         f'BIND((NOW() - "P14D"^^{XSD}duration) AS ?cutoff)'),
    ]
    for raw, expected in cases:
        out = _neptune_safe_duration(raw)
        assert out == expected, f"{raw!r} -> {out!r} != {expected!r}"
        assert "dayTimeDuration" not in out and "yearMonthDuration" not in out
        # Idempotent: running twice changes nothing more.
        assert _neptune_safe_duration(out) == expected

    # Already-valid and unrelated queries are untouched.
    valid = f'FILTER(?ts >= (NOW() - "P7D"^^<{XSD}duration>))'
    assert _neptune_safe_duration(valid) == valid
    unrelated = "SELECT ?x WHERE { ?x <urn:p> ?y . FILTER(?y > 3) }"
    assert _neptune_safe_duration(unrelated) == unrelated


def test_discovery_stamps_per_fact_verified_at():
    """The shared companion builder both rails use types the freshness stamp as
    xsd:dateTime (discovery gets the SAME per-fact recency signal as enrichment)."""
    trips = build_attribute_provenance_companions(
        "urn:g:e1", "Gadget", "material",
        source_url="https://specs.example/e1", provenance="specs.example",
        verified_at=_now(),
    )
    preds = {p for _s, p, _o in trips}
    assert _attr_uri("Gadget", "material_verified_at") in preds
    assert _attr_uri("Gadget", "material_source_url") in preds
    stamp = next(o for _s, p, o in trips if p.endswith("material_verified_at"))
    assert stamp.endswith(f"^^{XSD_DATETIME}"), "discovery stamp must be typed xsd:dateTime"


def test_freshness_prompt_teaches_relative_window():
    """The NL generation prompt teaches a NOW()-relative recency window keyed off
    dateTime attributes — generically (NOW() minus a duration), not a hardcoded
    field or absolute date, and using the Neptune-valid `xsd:duration` datatype."""
    from cograph_client.nlp.prompts import SPARQL_GENERATION_SYSTEM

    p = SPARQL_GENERATION_SYSTEM
    assert "NOW()" in p
    # Must teach the Neptune-valid duration datatype in the FILTER pattern.
    assert "XMLSchema#duration>" in p
    assert "_verified_at" in p
    # It must be RELATIVE, not steer the model to a hardcoded absolute date.
    assert "do NOT hardcode an" in p or "RELATIVE" in p
