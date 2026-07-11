"""ONTA-262 — provenance companions are attribute METADATA, not attributes.

MECHANISM tests on INVENTED types/attrs (Widget/sku, Gadget/material, Sprocket/
finish) — no persona/domain token. Proves:
  * companions mint on the dedicated ``attr_meta/`` namespace, and the shared
    predicate-hygiene rule excludes that namespace from every attribute surface;
  * LEGACY companions (attribute-namespace ``<attr>_<suffix>``) are classified
    set-wise — only when the base attribute is present — and dropped from the
    Explorer summary panel without touching real attributes or relationships;
  * enrichment declares ONLY primary attributes (no companion pseudo-attributes);
  * the migration planner maps exactly the legacy companions to their attr_meta
    URIs, and the server-side predicate rewrite preserves a typed xsd:dateTime
    stamp on a REAL SPARQL engine (the ONTA-247 datatype lesson);
  * the NL prompt teaches the attr_meta freshness convention.
"""

from __future__ import annotations

import json

import pytest

from cograph_client.graph.predicates import (
    ATTR_META_NS,
    companion_leaves,
    is_internal_predicate,
)
from cograph_client.graph.provenance import (
    attr_provenance_companion_uri,
    build_attribute_provenance_companions,
    legacy_attr_companion_uri,
)

XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"


# --- namespace + hygiene rule -------------------------------------------------

def test_companions_mint_on_attr_meta_namespace():
    trips = build_attribute_provenance_companions(
        "urn:w:e1", "Widget", "sku",
        source_url="https://a.example/sku", provenance="a.example",
    )
    preds = {p for _s, p, _o in trips}
    assert preds == {
        f"{ATTR_META_NS}Widget/sku/source_url",
        f"{ATTR_META_NS}Widget/sku/provenance",
        f"{ATTR_META_NS}Widget/sku/verified_at",
    }
    # No companion ever lands on the attribute namespace.
    assert not any(p.startswith("https://cograph.tech/types/") for p in preds)


def test_attr_meta_namespace_is_internal_everywhere():
    """The whole attr_meta namespace is excluded as a user-facing predicate —
    including from the is_relationship exemption (companions are always literal)."""
    p = attr_provenance_companion_uri("Widget", "sku", "verified_at")
    assert is_internal_predicate(p) is True
    assert is_internal_predicate(p, is_relationship=True) is True
    # Sanity: a real attribute predicate is NOT internal.
    assert is_internal_predicate("https://cograph.tech/types/Widget/attrs/sku") is False


def test_legacy_helper_still_builds_old_shape():
    assert legacy_attr_companion_uri("Widget", "sku", "verified_at") == (
        "https://cograph.tech/types/Widget/attrs/sku_verified_at"
    )


# --- legacy set-wise classification -------------------------------------------

def test_companion_leaves_requires_base_presence():
    leaves = {
        "sku", "sku_provenance", "sku_verified_at", "sku_source_url",
        # base absent → NOT classified, even with a companion-shaped tail:
        "orphan_provenance",
        # a real attribute that merely contains a suffix word elsewhere:
        "provenance_note",
        "color",
    }
    got = companion_leaves(leaves)
    assert got == {"sku_provenance", "sku_verified_at", "sku_source_url"}


def test_companion_leaves_handles_chained_source_url():
    # The per-record `source_url` citation column is itself a base attribute;
    # ITS companions classify, the base survives.
    leaves = {"source_url", "source_url_provenance", "source_url_verified_at"}
    assert companion_leaves(leaves) == {
        "source_url_provenance",
        "source_url_verified_at",
    }


def test_assemble_summary_drops_legacy_companions_keeps_real_fields():
    from cograph_client.api.routes.explore import _assemble_summary

    attrs = "https://cograph.tech/types/Widget/attrs/"
    onto = "https://cograph.tech/onto/"
    pred_records = [
        {"p": f"{attrs}sku", "cnt": 10, "rel": 0},
        {"p": f"{attrs}sku_provenance", "cnt": 10, "rel": 0},
        {"p": f"{attrs}sku_verified_at", "cnt": 10, "rel": 0},
        # base-absent companion-shaped attr survives (deterministic rule):
        {"p": f"{attrs}orphan_provenance", "cnt": 3, "rel": 0},
        # a relationship named like a companion of `city` must survive:
        {"p": f"{onto}city", "cnt": 8, "rel": 8},
        {"p": f"{onto}city_provenance", "cnt": 2, "rel": 2},
    ]
    out = _assemble_summary(
        "Widget", {}, None, 10, pred_records, attr_defs={}, index_flags=None
    )
    attr_names = {a["name"] for a in out["attributes"]}
    rel_names = {r["name"] for r in out["relationships"]}
    assert attr_names == {"sku", "orphan_provenance"}
    assert rel_names == {"city", "city_provenance"}


# --- enrichment declares only primaries ----------------------------------------

def test_applied_attribute_values_excludes_companions():
    from unittest.mock import AsyncMock

    from cograph_client.enrichment.cache import EnrichmentCache
    from cograph_client.enrichment.executor import EnrichmentExecutor
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import ConflictPolicy, RowResult, Verdict

    executor = EnrichmentExecutor(
        AsyncMock(), InMemoryJobStore(), EnrichmentCache(), AsyncMock()
    )
    rows = [
        RowResult(
            entity_uri="urn:w:e1", attribute="sku", action="filled",
            verdict=Verdict(value="WX-1", confidence=0.95, source="wikidata",
                            source_url="https://a.example/sku"),
        )
    ]
    out = executor._applied_attribute_values(rows, ConflictPolicy.overwrite)
    assert set(out) == {"sku"}, (
        "only the PRIMARY attribute may be declared — companions are attr_meta "
        f"metadata (got {set(out)})"
    )


# --- migration planner + real-engine rewrite ------------------------------------

def test_plan_migration_maps_only_base_present_companions():
    from cograph_client.graph.attr_meta_migration import plan_migration

    attrs = "https://cograph.tech/types/Gadget/attrs/"
    preds = [
        f"{attrs}material",
        f"{attrs}material_provenance",
        f"{attrs}material_verified_at",
        f"{attrs}orphan_source_url",      # base absent → stays
        f"{attrs}weight_kg",              # plain attr → stays
        "https://cograph.tech/onto/city", # relationship pred → not attrs-shaped
    ]
    mapping = plan_migration(preds)
    assert set(mapping) == {
        f"{attrs}material_provenance",
        f"{attrs}material_verified_at",
    }
    new_pred, type_name, leaf = mapping[f"{attrs}material_verified_at"]
    assert new_pred == attr_provenance_companion_uri("Gadget", "material", "verified_at")
    assert (type_name, leaf) == ("Gadget", "material_verified_at")


def test_predicate_rewrite_preserves_typed_datetime_on_real_engine():
    """The server-side DELETE/INSERT/WHERE re-key keeps the object term's exact
    datatype — a typed xsd:dateTime freshness stamp survives the migration (a
    client-side read-then-reinsert would strip it; ONTA-247)."""
    pytest.importorskip("pyoxigraph")
    from pyoxigraph import QueryResultsFormat, Store

    from cograph_client.graph.queries import rewrite_predicate_update

    store = Store()
    graph = "https://cograph.tech/graphs/test-tenant/kg/kg"
    old = legacy_attr_companion_uri("Widget", "sku", "verified_at")
    new = attr_provenance_companion_uri("Widget", "sku", "verified_at")
    store.update(
        f"INSERT DATA {{ GRAPH <{graph}> {{ "
        f'<urn:w:e1> <{old}> "2026-07-01T00:00:00+00:00"^^<{XSD_DATETIME}> . '
        f"}} }}"
    )
    store.update(rewrite_predicate_update(graph, old, new))

    res = json.loads(
        store.query(
            f"SELECT ?p ?o FROM <{graph}> WHERE {{ <urn:w:e1> ?p ?o }}"
        ).serialize(format=QueryResultsFormat.JSON)
    )
    rows = res["results"]["bindings"]
    assert len(rows) == 1
    assert rows[0]["p"]["value"] == new
    assert rows[0]["o"]["datatype"] == XSD_DATETIME, (
        "typed dateTime must survive the predicate rewrite"
    )
    # Idempotent: re-running is a no-op.
    store.update(rewrite_predicate_update(graph, old, new))
    res2 = json.loads(
        store.query(
            f"SELECT (COUNT(*) AS ?n) FROM <{graph}> WHERE {{ ?s ?p ?o }}"
        ).serialize(format=QueryResultsFormat.JSON)
    )
    assert res2["results"]["bindings"][0]["n"]["value"] == "1"


def test_delete_attribute_declaration_targets_schema_subject_only():
    from cograph_client.graph.ontology_queries import delete_attribute_declaration

    sparql = delete_attribute_declaration(
        "https://cograph.tech/graphs/tenant", "Widget", "sku_verified_at"
    )
    assert "https://cograph.tech/types/Widget/attrs/sku_verified_at" in sparql
    assert sparql.startswith("WITH <https://cograph.tech/graphs/tenant>")
    assert "DELETE" in sparql and "WHERE" in sparql


# --- NL prompt ------------------------------------------------------------------

def test_freshness_prompt_teaches_attr_meta_convention():
    from cograph_client.nlp.prompts import SPARQL_GENERATION_SYSTEM

    p = SPARQL_GENERATION_SYSTEM
    assert "attr_meta" in p
    assert "verified_at" in p
    # Legacy declared-attribute fallback stays taught for un-migrated graphs.
    assert "_verified_at" in p
