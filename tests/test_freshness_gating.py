"""ONTA-247 — freshness stamp + generic recency filter.

MECHANISM tests on INVENTED types/attrs across ≥2 unrelated domains (Widget/sku,
Gadget/weight_kg, Sprocket/diameter_mm) — no persona token appears. Proves:
  * an enriched value's per-attribute freshness stamp lands as a real, dated,
    order-comparable timestamp on the entity's citation record;
  * a generic NOW()-relative "last N days" FILTER (the pattern the NL prompt now
    teaches) selects fresh rows and excludes stale ones on a REAL SPARQL engine;
  * discovery (not only enrichment) stamps the per-fact typed stamp.

**Ported by ONTA-527** (first test only). It used to assert that the emitted
SPARQL carried ``"…"^^<xsd:dateTime>`` for the ``attr_meta/<T>/<attr>/verified_at``
companion. Enrichment writes through ``GraphStore`` now: ``insert_facts`` folds
attr_meta companions onto Assertion provenance / an ``:AttrCitation`` node
(ADR 0013, ``graph/pg_ops.py``), and ``parse_attr_meta_citations`` STRIPS the
``^^<datatype>`` tail — the property graph has no RDF datatype slot for a
citation field, so the stamp is stored as an ISO-8601 string. The capability the
datatype existed for survives in that representation and is what is pinned here
instead: the stamp is a real tz-aware instant, and it ORDERS correctly under the
plain string comparison ``neo4j_store``'s "changed since" query
(``a.verified_at > $since``) uses. The remaining assertions — the stamp lands per
attribute and never on the attribute namespace — are unchanged, because they are
about where the fact goes, not how it is serialized.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.executor import EnrichmentExecutor, _attr_uri, _now
from infona_client.enrichment.job_store import InMemoryJobStore
from infona_client.enrichment.models import ConflictPolicy, Verdict
from infona_client.graph.provenance import (
    attr_provenance_companion_uri,
    build_attribute_provenance_companions,
)
from infona_client.graph.store import get_graph_store

from tests._enrichment_prov_helpers import (
    DOMAINS,
    XSD_DATETIME,
    FakeWikidata,
    all_updates,
    entities_query_response,
    make_job,
    query_router,
)

# A stale stamp from a previous era, used to prove the fresh one still ORDERS
# after it under the plain string comparison the store's recency query uses.
STALE_STAMP = "2020-01-01T00:00:00+00:00"


def _citation(store, entity_uri: str, attr: str) -> dict | None:
    """The ``:AttrCitation`` row enrichment wrote for one (entity, attribute)."""
    for row in store.snapshot_citations():
        if row["entity_id"] == entity_uri and row["attr"] == attr:
            return row
    return None


def _entity_props(store, entity_uri: str) -> dict:
    for row in store.snapshot_entities():
        if row["id"] == entity_uri:
            return row["props"]
    return {}


@pytest.mark.parametrize("type_name,attr,label,value,src", DOMAINS)
def test_verified_at_is_a_dated_order_comparable_stamp(
    type_name, attr, label, value, src, monkeypatch
):
    """An enriched value's per-attribute freshness stamp is recorded as a real
    tz-aware instant that sorts after an older stamp — so a "verified in the last
    N days" window still discriminates — and never lands on the attribute
    namespace. Two unrelated invented domains."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        entity = f"https://graph.infona.ai/entities/{type_name}/e1"
        rows = [{"uri": entity, "label": label, "vals": ""}]
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

        store = get_graph_store()
        citation = _citation(store, entity, attr)
        assert citation is not None, "the enriched fact carries no freshness stamp"
        stamp = citation["verified_at"]
        # A real instant, not a formatted blob: parseable AND timezone-aware.
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None, stamp
        # ... and ordered correctly by the STRING comparison the store's recency
        # query runs (`a.verified_at > $since`), which is what the xsd:dateTime
        # annotation used to buy on the SPARQL path.
        assert STALE_STAMP < stamp
        assert datetime.fromisoformat(STALE_STAMP) < parsed

        # The stamp is metadata OF the attribute — never an attribute of its own
        # (ONTA-262). It is not an entity property and it never reaches SPARQL.
        assert f"{attr}_verified_at" not in _entity_props(store, entity)
        assert "verified_at" not in _entity_props(store, entity)
        writes = all_updates(neptune)
        assert attr_provenance_companion_uri(type_name, attr, "verified_at") not in writes
        assert _attr_uri(type_name, f"{attr}_verified_at") not in writes

    asyncio.run(run())


def test_recency_filter_selects_and_excludes_by_window():
    """A generic NOW()-relative recency FILTER over a typed `<attr>_verified_at`
    (the EXACT pattern the NL prompt now teaches, AFTER the Neptune-safe duration
    normalizer runs) returns fresh rows and excludes stale ones. Runs against a REAL
    pyoxigraph SPARQL engine on invented schema."""
    pytest.importorskip("pyoxigraph")
    from pyoxigraph import QueryResultsFormat, Store

    from infona_client.nlp.pipeline import _neptune_safe_duration

    store = Store()
    graph = "https://graph.infona.ai/graphs/test-tenant/kg/kg"
    vpred = attr_provenance_companion_uri("Widget", "sku", "verified_at")
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
    from infona_client.nlp.pipeline import _neptune_safe_duration

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
    assert attr_provenance_companion_uri("Gadget", "material", "verified_at") in preds
    assert attr_provenance_companion_uri("Gadget", "material", "source_url") in preds
    # Companions never land on the attribute namespace (ONTA-262).
    assert _attr_uri("Gadget", "material_verified_at") not in preds
    stamp = next(o for _s, p, o in trips if p.endswith("material/verified_at"))
    assert stamp.endswith(f"^^{XSD_DATETIME}"), "discovery stamp must be typed xsd:dateTime"


def test_freshness_prompt_teaches_relative_window():
    """The NL generation prompt teaches a NOW()-relative recency window keyed off
    dateTime attributes — generically (NOW() minus a duration), not a hardcoded
    field or absolute date, and using the Neptune-valid `xsd:duration` datatype."""
    from infona_client.nlp.prompts import SPARQL_GENERATION_SYSTEM

    p = SPARQL_GENERATION_SYSTEM
    assert "NOW()" in p
    # Must teach the Neptune-valid duration datatype in the FILTER pattern.
    assert "XMLSchema#duration>" in p
    assert "_verified_at" in p
    # It must be RELATIVE, not steer the model to a hardcoded absolute date.
    assert "do NOT hardcode an" in p or "RELATIVE" in p
