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


def test_migrate_kg_end_to_end_on_real_engine(monkeypatch):
    """migrate_kg against a real store: legacy instance companions re-key to
    attr_meta (typed stamp intact), their ontology declarations purge, real
    attributes and base-absent lookalikes survive untouched; a second run is a
    no-op. migrate_tenant's final sweep also purges an ORPHANED companion
    declaration (no instance data behind it)."""
    pytest.importorskip("pyoxigraph")
    import asyncio
    import json as _json

    from pyoxigraph import QueryResultsFormat, Store

    import cograph_client.graph.attr_meta_migration as mig
    from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri

    class PyoxiNeptune:
        def __init__(self) -> None:
            self.store = Store()

        async def query(self, sparql: str) -> dict:
            res = self.store.query(sparql, use_default_graph_as_union=True)
            return _json.loads(res.serialize(format=QueryResultsFormat.JSON))

        async def update(self, sparql: str) -> None:
            self.store.update(sparql)

    async def _noop_refresh(*a, **k):
        return None

    monkeypatch.setattr(mig, "refresh_after_write", _noop_refresh)

    tenant, kg = "test-tenant", "kg"
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(tenant, kg), tenant_graph_uri(tenant)
    attrs = "https://cograph.tech/types/Widget/attrs/"
    rdf = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

    async def run():
        await n.update(
            f"INSERT DATA {{ GRAPH <{kgg}> {{ "
            f'<urn:w:e1> <{attrs}sku> "WX-1" ; '
            f'<{attrs}sku_source_url> "https://a.example/sku" ; '
            f'<{attrs}sku_verified_at> "2026-07-01T00:00:00+00:00"^^<{XSD_DATETIME}> ; '
            f'<{attrs}orphan_provenance> "not a companion" . }} }}'
        )
        await n.update(
            f"INSERT DATA {{ GRAPH <{onto}> {{ "
            f"<{attrs}sku> <{rdf}type> <{rdf}Property> . "
            f"<{attrs}sku_verified_at> <{rdf}type> <{rdf}Property> . "
            # Orphaned companion declaration: declared, but ZERO instance triples.
            f"<{attrs}sku_provenance> <{rdf}type> <{rdf}Property> . }} }}"
        )

        first = await mig.migrate_kg(n, tenant, kg)
        assert first["migrated"] == 2  # sku_source_url + sku_verified_at
        # One idempotent purge statement per distinct migrated companion (the
        # count is attempts issued; sku_source_url simply had no declaration).
        assert first["declarations_purged"] == 2

        rows = (await n.query(
            f"SELECT ?p ?o FROM <{kgg}> WHERE {{ <urn:w:e1> ?p ?o }}"
        ))["results"]["bindings"]
        by_pred = {r["p"]["value"]: r["o"] for r in rows}
        assert f"{ATTR_META_NS}Widget/sku/source_url" in by_pred
        stamp = by_pred[f"{ATTR_META_NS}Widget/sku/verified_at"]
        assert stamp["datatype"] == XSD_DATETIME  # typed stamp survived
        assert f"{attrs}sku" in by_pred  # primary untouched
        assert f"{attrs}orphan_provenance" in by_pred  # base-absent survives
        assert f"{attrs}sku_source_url" not in by_pred  # legacy gone

        # Idempotent second run finds nothing.
        second = await mig.migrate_kg(n, tenant, kg)
        assert second["companions_found"] == 0

        # migrate_kg's purge is instance-driven, so the ORPHANED declaration
        # (sku_provenance) is still there…
        decls = (await n.query(
            f"SELECT ?s FROM <{onto}> WHERE {{ ?s <{rdf}type> <{rdf}Property> }}"
        ))["results"]["bindings"]
        assert any(r["s"]["value"] == f"{attrs}sku_provenance" for r in decls)
        # …until the tenant-wide final sweep purges it.
        sweep = await mig.sweep_orphaned_companion_declarations(n, tenant)
        assert sweep["declarations_purged"] == 1
        decls2 = (await n.query(
            f"SELECT ?s FROM <{onto}> WHERE {{ ?s <{rdf}type> <{rdf}Property> }}"
        ))["results"]["bindings"]
        left = {r["s"]["value"] for r in decls2}
        assert left == {f"{attrs}sku"}

    asyncio.run(run())


def test_delete_attribute_declaration_targets_schema_subject_only():
    from cograph_client.graph.ontology_queries import delete_attribute_declaration

    sparql = delete_attribute_declaration(
        "https://cograph.tech/graphs/tenant", "Widget", "sku_verified_at"
    )
    assert "https://cograph.tech/types/Widget/attrs/sku_verified_at" in sparql
    assert sparql.startswith("WITH <https://cograph.tech/graphs/tenant>")
    assert "DELETE" in sparql and "WHERE" in sparql


# --- NL prompt + pipeline --------------------------------------------------------

def test_freshness_prompt_teaches_attr_meta_convention():
    from cograph_client.nlp.prompts import SPARQL_GENERATION_SYSTEM

    p = SPARQL_GENERATION_SYSTEM
    assert "attr_meta" in p
    assert "verified_at" in p
    # Legacy declared-attribute fallback stays taught for un-migrated graphs.
    assert "_verified_at" in p


def test_uri_repair_never_rewrites_constructed_attr_meta_predicates():
    """The freshness prompt teaches the planner to CONSTRUCT the attr_meta stamp
    URI (it is deliberately absent from the ontology summary), so the post-hoc
    fuzzy URI repair must treat attr_meta/ as a system namespace and leave it
    alone. Without the skip, the repair cross-wires the constructed predicate
    onto whatever declared attribute fuzzy-matches best (measured 0.846 against
    a legacy `fax_verified_at`) — silently answering the freshness question with
    the WRONG attribute's stamp."""
    from cograph_client.nlp.pipeline import NLQueryPipeline

    stamp = attr_provenance_companion_uri("Physician", "bio", "verified_at")
    # An ontology summary carrying tempting fuzzy-match bait, including a legacy
    # declared companion from an un-migrated KG.
    summary = (
        "Type: Physician — URI: <https://cograph.tech/types/Physician>\n"
        "  - bio (string) URI: <https://cograph.tech/types/Physician/attrs/bio>\n"
        "  - fax (string) URI: <https://cograph.tech/types/Physician/attrs/fax>\n"
        "  - fax_verified_at (datetime) URI: "
        "<https://cograph.tech/types/Physician/attrs/fax_verified_at>\n"
    )
    sparql = (
        f"SELECT ?e FROM <urn:g> WHERE {{ ?e <{stamp}> ?ts . "
        f'FILTER(?ts >= (NOW() - "P7D"^^<http://www.w3.org/2001/XMLSchema#duration>)) }}'
    )
    fixed = NLQueryPipeline._fix_attribute_uris(sparql, summary)
    assert f"<{stamp}>" in fixed, fixed
    # And a genuinely-wrong attrs/ URI is still repaired (the net stays).
    typo = sparql.replace(f"<{stamp}>", "<https://cograph.tech/types/Physician/attrs/fx>")
    refixed = NLQueryPipeline._fix_attribute_uris(typo, summary)
    assert "attrs/fax>" in refixed, refixed
