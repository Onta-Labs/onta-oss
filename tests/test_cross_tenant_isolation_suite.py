"""ONTA-402b — adversarial cross-tenant isolation suite.

Isolation is by **scope**, not by type URI: two tenants' ``Hotel`` share
``https://graph.infona.ai/types/Hotel``. On the SPARQL rails that scope was a
named graph and layered reads (ONTA-397) unioned Public / Enhanced / tenant
graphs via ``LayerStack`` — that union is the leak surface this suite attacks.

**Ported by ONTA-527 — read this before changing an assertion.** Several routes
here (ontology workspace/list/get, explore records, per-KG type-counts, literal
grep) are served from the property-graph store now, so the ``IsolationNeptune``
fixture that answers SPARQL shapes no longer sees their reads at all: those
tests were passing against an empty store, i.e. proving nothing. They are
re-seeded into a ``MemoryGraphStore`` through the REAL write paths
(``ontology_catalog.upsert_type`` / ``upsert_attribute`` for the catalog,
``kg_writer.insert_facts`` for instances) with the SAME adversarial markers, so
the same questions are asked of the shipped read path.

What changed, precisely, in isolation terms:

* **The unit of confinement.** On SPARQL it was the ``FROM <graph>`` the server
  built; on the store it is the ``GraphScope`` a session is opened with, whose
  ``$tenant_id`` / ``$kg`` params are FORCED over anything a caller supplied
  (``graph/store.py::merge_scope_params``, ``assert_cypher_is_scoped``). Where a
  test used to assert "every query names exactly one graph, the caller's", it now
  records the scope of every session the request opens and asserts the same
  thing about those. That is a like-for-like port, not a weakening: it is the
  same server-derived-identity property, checked one layer lower.
* **Layered catalog reads restored (ONTA-535).** ``_workspace_ontology_store``
  merges Tenant > Enhanced (entitled) > Public from the GraphStore ontology
  catalog and populates the viewer's per-layer status strip (``layers`` is
  non-empty). This fixture seeds **tenant catalogs only**, so the shared Public
  ``BaseHotel`` is still invisible; isolation remains "no peer markers / peer
  type descriptions." Shadowing (one ``Hotel`` per tenant, carrying that
  tenant's own description) is still pinned below.
* **No isolation assertion was relaxed, and no leak was found.** Every
  ``_assert_no_peer_markers`` / peer-entity-URI check in this file survives, and
  the planted-violation self-tests were re-planted against the store so they
  still prove the assertions can fail.

What this suite pins (Linear ONTA-402 Testing section):

1. **Adversarial fixture** — two tenants, colliding type names, colliding
   attribute names, tenant shadowing Public; every private string is a unique
   non-substring marker (no coincidence pass).
2. **Every ontology-touching route** — ontology list/get/workspace, explore
   summary/search/records, type-counts, ask (ontology summary material),
   skills, functions; MCP/CLI pin the same server routes.
3. **Cache poisoning** — ``explore._summary_cache`` and
   ``nlp.pipeline._ontology_cache`` never serve tenant A's content to B.
4. **Prompt leakage** — NL ontology summary for B never contains A-only
   markers.
5. **Operator global browser** — zero tenant-layer content.
6. **Consent** re-asserted: without consent no promotion write.
7. **Planted-violation self-tests** prove the assertions catch real leaks.

All mocked — no live Neptune, no LLM, no network. OSS only.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes import ask as ask_routes
from infona_client.api.routes import explore as explore_routes
from infona_client.api.routes import functions as functions_routes
from infona_client.api.routes import grep as grep_routes
from infona_client.api.routes import knowledge_graphs as kg_routes
from infona_client.api.routes import ontology as ontology_routes
from infona_client.api.routes import operator as operator_routes
from infona_client.api.routes import skills as skills_routes
from infona_client.auth import api_keys
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.entitlement import register_entitlement_checker
from infona_client.graph.global_ontology import fetch_global_ontology, fetch_ontology
from infona_client.graph.layers import (
    Layer,
    LayerStack,
    enhanced_graph_uri,
    public_graph_uri,
)
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import configure_graph_store
from infona_client.models.query import NLResult
from infona_client.resolver.promotion_consent import (
    PromotionConsentError,
    register_promotion_consent_provider,
    require_promotion_consent,
)
from infona_client.skills.models import TypeSkill
from infona_client.skills.store import InMemoryTypeSkillStore, reset_type_skill_store

from tests.test_global_ontology_browser import (
    PUB,
    function_triples,
    shape_triples,
    _rows_for,
    _sparql_json,
)

# ---------------------------------------------------------------------------
# Unique non-substring markers (positive/specific cross-tenant assertions)
# ---------------------------------------------------------------------------

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns"
RDFS = "http://www.w3.org/2000/01/rdf-schema"
XSD = "http://www.w3.org/2001/XMLSchema"
ONTO = "https://graph.infona.ai/onto"
TENANT_NS = "https://graph.infona.ai/types"
ENTITY_NS = "https://graph.infona.ai/entities"

TENANT_A = "iso-acme"
TENANT_B = "iso-globex"
GRAPH_A = tenant_graph_uri(TENANT_A)
GRAPH_B = tenant_graph_uri(TENANT_B)
KG_NAME = "hotels"
KG_A = kg_graph_uri(TENANT_A, KG_NAME)
KG_B = kg_graph_uri(TENANT_B, KG_NAME)
STATS_A = KG_A + "/stats"
STATS_B = KG_B + "/stats"

# Type names COLLIDE across tenants (isolation is by graph, not URI).
TYPE_NAME = "Hotel"
# Shared Public type that both see under LayerStack (layered reads).
PUBLIC_TYPE = "BaseHotel"

# Tenant-private markers — deliberately long/unique so a substring coincidence
# cannot pass a leak assertion.
A_TYPE_DESC = "ACME_ISO402B_TYPE_hotel_franchise_x7k9qp"
B_TYPE_DESC = "GLOBEX_ISO402B_TYPE_hotel_corporate_y8l0wr"
# Same attribute leaf on both tenants — values/descriptions must not cross.
SHARED_ATTR = "status"
A_ATTR_WHY = "ACME_ISO402B_ATTR_status_franchise_q2m1vn"
B_ATTR_WHY = "GLOBEX_ISO402B_ATTR_status_corp_r3n2xm"
# Tenant-private attribute names (collide with nothing on the peer).
A_ATTR_PRIVATE = "acmeFranchiseCode"
B_ATTR_PRIVATE = "globexSuiteCode"
A_ATTR_PRIVATE_WHY = "ACME_ISO402B_ATTR_franchise_p4n8zj"
B_ATTR_PRIVATE_WHY = "GLOBEX_ISO402B_ATTR_suite_s5o9yk"

A_SKILL_SLUG = "check-in-policy"
B_SKILL_SLUG = "check-in-policy"  # same slug, different body
A_SKILL_BODY = "ACME_ISO402B_SKILL_checkin_franchise_only_t6p1"
B_SKILL_BODY = "GLOBEX_ISO402B_SKILL_checkin_corporate_only_u7q2"
A_SKILL_TITLE = "ACME_ISO402B_SKILL_TITLE_franchise"
B_SKILL_TITLE = "GLOBEX_ISO402B_SKILL_TITLE_corporate"

A_FUNC_NAME = "acme_loyalty_score"
B_FUNC_NAME = "globex_risk_score"
A_FUNC_DESC = "ACME_ISO402B_FUNC_loyalty_w3j6hb"
B_FUNC_DESC = "GLOBEX_ISO402B_FUNC_risk_v4k7ic"

A_ENTITY = f"{ENTITY_NS}/{TYPE_NAME}/acme_ritz"
B_ENTITY = f"{ENTITY_NS}/{TYPE_NAME}/globex_plaza"
A_ENTITY_LABEL = "ACME_ISO402B_ENTITY_ritz_a1b2cd"
B_ENTITY_LABEL = "GLOBEX_ISO402B_ENTITY_plaza_c3d4ef"
A_STATUS_VAL = "ACME_ISO402B_STATUS_val_franchise"
B_STATUS_VAL = "GLOBEX_ISO402B_STATUS_val_corp"

PUBLIC_DESC = "PUBLIC_ISO402B_base_hotel_shared"
PUBLIC_ATTR = "name"

A_MARKERS = (
    A_TYPE_DESC,
    A_ATTR_WHY,
    A_ATTR_PRIVATE,
    A_ATTR_PRIVATE_WHY,
    A_SKILL_BODY,
    A_SKILL_TITLE,
    A_FUNC_NAME,
    A_FUNC_DESC,
    A_ENTITY_LABEL,
    A_STATUS_VAL,
)
B_MARKERS = (
    B_TYPE_DESC,
    B_ATTR_WHY,
    B_ATTR_PRIVATE,
    B_ATTR_PRIVATE_WHY,
    B_SKILL_BODY,
    B_SKILL_TITLE,
    B_FUNC_NAME,
    B_FUNC_DESC,
    B_ENTITY_LABEL,
    B_STATUS_VAL,
)


def _assert_no_peer_markers(blob: str, *, peer: str) -> None:
    """Every marker belonging to ``peer`` must be absent from ``blob``."""
    markers = A_MARKERS if peer == "A" else B_MARKERS
    for m in markers:
        assert m not in blob, f"cross-tenant leak: peer marker {m!r} found in response"


def _assert_own_markers_present(blob: str, *, owner: str, required: tuple[str, ...]) -> None:
    """Positive assertion: the owner's private strings ARE visible to them."""
    for m in required:
        assert m in blob, f"owner {owner} missing expected private marker {m!r}"


# ---------------------------------------------------------------------------
# IsolationNeptune — graph-scoped triple store answering every query shape
# ---------------------------------------------------------------------------


def _binding(value: str) -> dict:
    if value.startswith('"') and '"^^' in value:
        literal, datatype = value[1:].split('"^^', 1)
        return {"type": "literal", "value": literal, "datatype": datatype}
    if value.startswith("http://") or value.startswith("https://"):
        return {"type": "uri", "value": value}
    return {"type": "literal", "value": value}


def _sparql_from_rows(rows: list[dict]) -> dict:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    return {
        "head": {"vars": keys},
        "results": {
            "bindings": [{k: _binding(v) for k, v in r.items()} for r in rows]
        },
    }


def _empty_sparql() -> dict:
    return {"head": {"vars": []}, "results": {"bindings": []}}


def _extract_from_graphs(sparql: str) -> list[str]:
    return re.findall(r"FROM <([^>]+)>", sparql)


class IsolationNeptune:
    """Minimal SPARQL-shape dispatcher over a per-graph triple store.

    Covers the query shapes issued by ontology routes, explore summary/search/
    records, type-counts, functions list, and the NL pipeline ontology summary.
    Not a full SPARQL engine — only enough to exercise isolation.
    """

    def __init__(
        self,
        by_graph: dict[str, list[tuple[str, str, str]]],
        *,
        # Adversarial: when set, answer tenant B's ontology graph with A's triples
        # (planted leak used by the self-test that proves assertions catch leaks).
        plant_a_into_b: bool = False,
    ):
        self.by_graph = {g: list(t) for g, t in by_graph.items()}
        self.plant_a_into_b = plant_a_into_b
        self.queries: list[str] = []

    def _triples(self, graph: str) -> list[tuple[str, str, str]]:
        if self.plant_a_into_b and graph == GRAPH_B:
            return self.by_graph.get(GRAPH_A, [])
        return self.by_graph.get(graph, [])

    def _objs(self, triples, s, p):
        return [o for (ss, pp, o) in triples if ss == s and pp == p]

    def _classes(self, triples) -> list[str]:
        return sorted({
            s for (s, p, o) in triples
            if p == f"{RDF}#type" and o == f"{RDFS}#Class"
        })

    def _list_types_rows(self, triples) -> list[dict]:
        rows = []
        for t in self._classes(triples):
            labels = self._objs(triples, t, f"{RDFS}#label")
            if not labels:
                continue
            row = {"type": t, "label": labels[0]}
            comments = self._objs(triples, t, f"{RDFS}#comment")
            if comments:
                row["comment"] = comments[0]
            parents = self._objs(triples, t, f"{RDFS}#subClassOf")
            if parents:
                row["parent"] = parents[0]
            rows.append(row)
        return rows

    def _detail_rows(self, triples) -> list[dict]:
        # Reuse the browser fixture's writer-shaped fold (typeLabel etc.).
        return _rows_for(triples)

    def _full_ontology_rows(self, triples) -> list[dict]:
        """Projection used by get_full_ontology_query (NL pipeline)."""
        detail = self._detail_rows(triples)
        # Detail already carries type/typeLabel/attr/attrLabel/range/funcName.
        return detail

    def _type_detail(self, triples, type_uri: str) -> list[dict]:
        labels = self._objs(triples, type_uri, f"{RDFS}#label")
        if not labels:
            return []
        row: dict = {"label": labels[0]}
        comments = self._objs(triples, type_uri, f"{RDFS}#comment")
        if comments:
            row["comment"] = comments[0]
        parents = self._objs(triples, type_uri, f"{RDFS}#subClassOf")
        if parents:
            row["parent"] = parents[0]
        return [row]

    def _attr_defs(self, triples, type_uri: str) -> list[dict]:
        attrs = sorted({
            s for (s, p, o) in triples
            if p == f"{RDFS}#domain" and o == type_uri
        })
        rows = []
        for a in attrs:
            labels = self._objs(triples, a, f"{RDFS}#label")
            if not labels:
                continue
            row = {"attr": a, "attrLabel": labels[0]}
            ranges = self._objs(triples, a, f"{RDFS}#range")
            if ranges:
                row["range"] = ranges[0]
            comments = self._objs(triples, a, f"{RDFS}#comment")
            if comments:
                row["attrComment"] = comments[0]
            rows.append(row)
        return rows

    def _functions(self, triples) -> list[dict]:
        rows = []
        for s, p, o in triples:
            if p != f"{ONTO}/attachedTo":
                continue
            names = self._objs(triples, s, f"{ONTO}/name")
            if not names:
                continue
            row = {"name": names[0], "type": o}
            endpoints = self._objs(triples, s, f"{ONTO}/endpointUrl")
            if endpoints:
                row["endpoint"] = endpoints[0]
            descs = self._objs(triples, s, f"{ONTO}/description")
            if descs:
                row["desc"] = descs[0]
            rows.append(row)
        return rows

    def _instance_entities(self, triples, type_uri: str | None = None) -> list[str]:
        ents = []
        for s, p, o in triples:
            if p != f"{RDF}#type":
                continue
            if type_uri is not None and o != type_uri:
                continue
            if s.startswith(ENTITY_NS):
                ents.append(s)
        return sorted(set(ents))

    def _entity_preds(self, triples, entity: str) -> list[tuple[str, str]]:
        return [(p, o) for (s, p, o) in triples if s == entity]

    def _grep_scope(self, graphs: list[str]) -> list[tuple[str, str, str]]:
        """Graphs the literal grep is allowed to read. Overridden by the planted
        self-test to simulate a writer that ignores the server-built FROM."""
        out: list[tuple[str, str, str]] = []
        for g in graphs:
            out.extend(self._triples(g))
        return out

    async def query(self, sparql: str, *, timeout: float | None = None) -> dict:
        # ``timeout`` accepted because the literal grep route (ONTA-416) passes a
        # dedicated short one; ignored here, this store never blocks.
        self.queries.append(sparql)
        graphs = _extract_from_graphs(sparql)
        # Multi-FROM: union the triples (layer stack reads).
        if not graphs:
            return _empty_sparql()

        # Planted-leak path only remaps the tenant ontology graph.
        triple_sets = [self._triples(g) for g in graphs]
        triples: list[tuple[str, str, str]] = []
        for ts in triple_sets:
            triples.extend(ts)

        # --- literal grep scan (ONTA-416): SELECT ?s ?p ?o + isLiteral CONTAINS --
        # Answered BEFORE the ontology shapes because it is the one query whose
        # whole point is dumping raw instance literals — precisely what a
        # graph-scoping bug would leak across tenants.
        if "isLiteral(?o)" in sparql:
            m = re.search(r'CONTAINS\((?:LCASE\()?STR\(\?o\)\)?, "([^"]*)"', sparql)
            needle = (m.group(1) if m else "").lower()
            rows = [
                {"s": s, "p": p, "o": o}
                for (s, p, o) in self._grep_scope(graphs)
                if needle and needle in o.lower()
            ]
            return _sparql_from_rows(rows)

        # --- literal grep decoration: VALUES ?s + OPTIONAL label/type ---
        if "VALUES ?s" in sparql:
            subjects = re.findall(r"<(https://graph\.infona\.ai/entities/[^>]+)>", sparql)
            rows = []
            for s in subjects:
                for p, o in self._entity_preds(self._grep_scope(graphs), s):
                    if p == f"{RDFS}#label":
                        rows.append({"s": s, "label": o})
                    elif p == f"{RDF}#type":
                        rows.append({"s": s, "type": o})
            return _sparql_from_rows(rows)

        # --- list_types_query / explore search type list ---
        if "?label" in sparql and "rdfs#Class" in sparql.replace(
            "http://www.w3.org/2000/01/rdf-schema#", "rdfs#"
        ).replace(f"{RDFS}#", "rdfs#"):
            # list_types_query: SELECT ?type ?label ?comment ?parent
            if "SELECT ?type ?label" in sparql or (
                "?type" in sparql and "?label" in sparql and "Class" in sparql
            ):
                # Per-graph for list_types (single FROM usually).
                if len(graphs) == 1:
                    return _sparql_from_rows(self._list_types_rows(self._triples(graphs[0])))
                rows = []
                for g in graphs:
                    rows.extend(self._list_types_rows(self._triples(g)))
                return _sparql_from_rows(rows)

        # --- full ontology detail (fetch_ontology / operator) ---
        if "?typeLabel" in sparql and "?typeComment" in sparql:
            if len(graphs) == 1:
                return _sparql_json(self._detail_rows(self._triples(graphs[0])))
            rows = []
            for g in graphs:
                rows.extend(self._detail_rows(self._triples(g)))
            return _sparql_json(rows)

        # --- get_full_ontology_query (NL pipeline) ---
        if "?typeLabel" in sparql and "?funcName" in sparql:
            if len(graphs) == 1:
                return _sparql_from_rows(
                    self._full_ontology_rows(self._triples(graphs[0]))
                )
            rows = []
            for g in graphs:
                rows.extend(self._full_ontology_rows(self._triples(g)))
            return _sparql_from_rows(rows)

        # --- get_type_detail / explore summary onto lookup ---
        if "?label" in sparql and "subClassOf" in sparql and "SELECT ?label" in sparql:
            # Extract type URI from `<uri> <...#label>`
            m = re.search(r"<(https://graph\.infona\.ai/types[^>]*)>\s+<(?:[^>]*#label)", sparql)
            if m:
                t_uri = m.group(1)
                g = graphs[0]
                return _sparql_from_rows(self._type_detail(self._triples(g), t_uri))

        # --- attribute defs ---
        if "attrLabel" in sparql and "domain" in sparql:
            m = re.search(r"domain>\s+<(https://graph\.infona\.ai/types[^>]*)>", sparql)
            if m:
                t_uri = m.group(1)
                g = graphs[0]
                return _sparql_from_rows(self._attr_defs(self._triples(g), t_uri))
            # multi-from explore search attr
            rows = []
            for g in graphs:
                for t in self._classes(self._triples(g)):
                    rows.extend(self._attr_defs(self._triples(g), t))
            return _sparql_from_rows(rows)

        # --- list_functions_query ---
        if "endpointUrl" in sparql and "attachedTo" in sparql and "?name" in sparql:
            g = graphs[0]
            return _sparql_from_rows(self._functions(self._triples(g)))

        # --- stats entity count ---
        if "entityCount" in sparql or "stats/entityCount" in sparql:
            g = graphs[0]
            rows = []
            for s, p, o in self._triples(g):
                if "entityCount" in p:
                    rows.append({"ec": o})
            return _sparql_from_rows(rows) if rows else _empty_sparql()

        # --- stats forType/forPred ---
        if "forType" in sparql or "stats/forType" in sparql:
            return _empty_sparql()  # force live-scan path for records/summary attrs

        # --- type-counts GROUP BY ---
        if "COUNT(DISTINCT ?e)" in sparql and "GROUP BY ?type" in sparql:
            g = graphs[0]
            counts: dict[str, set[str]] = {}
            for s, p, o in self._triples(g):
                if p == f"{RDF}#type" and o.startswith(f"{TENANT_NS}/"):
                    leaf = o[len(f"{TENANT_NS}/"):]
                    if "/" in leaf:
                        continue
                    counts.setdefault(o, set()).add(s)
            rows = [
                {"type": t, "cnt": str(len(ents))}
                for t, ents in counts.items()
            ]
            return _sparql_from_rows(rows)

        # --- DISTINCT ?type probe (NL pipeline active types) ---
        if "SELECT DISTINCT ?type" in sparql and "rdf-syntax-ns#type" in sparql:
            g = graphs[0]
            types = sorted({
                o for (s, p, o) in self._triples(g)
                if p == f"{RDF}#type" and o.startswith(f"{TENANT_NS}/")
            })
            return _sparql_from_rows([{"type": t} for t in types])

        # --- COUNT(DISTINCT ?val) cardinality (NL pipeline) ---
        if "COUNT(DISTINCT ?val)" in sparql:
            return _sparql_from_rows([{"cnt": "3"}])

        # --- DISTINCT ?val enum ---
        if "SELECT DISTINCT ?val" in sparql:
            return _empty_sparql()

        # --- entity page (records) ---
        if "DISTINCT ?e" in sparql and "ORDER BY ?e" in sparql:
            g = graphs[0]
            m = re.search(r"<(https://graph\.infona\.ai/types/[^>]+)>", sparql)
            t_uri = m.group(1) if m else None
            ents = self._instance_entities(self._triples(g), t_uri)
            return _sparql_from_rows([{"e": e} for e in ents])

        # --- VALUES ?e attribute dump ---
        if "VALUES ?e" in sparql:
            g = graphs[0]
            # Extract entity URIs from VALUES block
            ents = re.findall(r"<(https://graph\.infona\.ai/entities/[^>]+)>", sparql)
            rows = []
            for e in ents:
                for p, o in self._entity_preds(self._triples(g), e):
                    rows.append({"e": e, "p": p, "o": o})
            return _sparql_from_rows(rows)

        # --- COUNT DISTINCT entities for explore search ---
        if "COUNT(DISTINCT ?e)" in sparql:
            g = graphs[0]
            m = re.search(r"<(https://graph\.infona\.ai/types/[^>]+)>", sparql)
            t_uri = m.group(1) if m else None
            ents = self._instance_entities(self._triples(g), t_uri)
            return _sparql_from_rows([{"n": str(len(ents))}])

        # --- live scan fallback (summary without stats) ---
        if "?p" in sparql and "COUNT" in sparql:
            return _empty_sparql()

        return _empty_sparql()

    async def update(self, sparql: str) -> None:
        return None

    async def health(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Seed builders
# ---------------------------------------------------------------------------


def _seed_adversarial(*, plant_a_into_b: bool = False) -> IsolationNeptune:
    """Two tenants, colliding Hotel + status, private attrs, shadowing Public."""
    public = shape_triples(
        PUB,
        PUBLIC_TYPE,
        comment=PUBLIC_DESC,
        slots=[{"name": PUBLIC_ATTR, "range": f"{XSD}#string", "why": "display"}],
    )
    # Also put a Public Hotel so shadowing is exercised: tenant Hotel wins.
    public_hotel = shape_triples(
        PUB,
        TYPE_NAME,
        comment="public-hotel-shadowed",
        slots=[{"name": "stars", "range": f"{XSD}#integer", "why": "rating"}],
    )
    a_onto = shape_triples(
        TENANT_NS,
        TYPE_NAME,
        comment=A_TYPE_DESC,
        slots=[
            {
                "name": SHARED_ATTR,
                "range": f"{XSD}#string",
                "why": A_ATTR_WHY,
            },
            {
                "name": A_ATTR_PRIVATE,
                "range": f"{XSD}#string",
                "why": A_ATTR_PRIVATE_WHY,
            },
        ],
    )
    a_onto += function_triples(
        TENANT_NS, TYPE_NAME, A_FUNC_NAME,
        description=A_FUNC_DESC,
        endpoint="https://acme.example/loyalty",
    )
    b_onto = shape_triples(
        TENANT_NS,
        TYPE_NAME,
        comment=B_TYPE_DESC,
        slots=[
            {
                "name": SHARED_ATTR,
                "range": f"{XSD}#string",
                "why": B_ATTR_WHY,
            },
            {
                "name": B_ATTR_PRIVATE,
                "range": f"{XSD}#string",
                "why": B_ATTR_PRIVATE_WHY,
            },
        ],
    )
    b_onto += function_triples(
        TENANT_NS, TYPE_NAME, B_FUNC_NAME,
        description=B_FUNC_DESC,
        endpoint="https://globex.example/risk",
    )

    t_uri = f"{TENANT_NS}/{TYPE_NAME}"
    a_inst = [
        (A_ENTITY, f"{RDF}#type", t_uri),
        (A_ENTITY, f"{RDFS}#label", A_ENTITY_LABEL),
        (A_ENTITY, f"{ONTO}/{SHARED_ATTR}", A_STATUS_VAL),
        (A_ENTITY, f"{ONTO}/{A_ATTR_PRIVATE}", "FR-001"),
    ]
    b_inst = [
        (B_ENTITY, f"{RDF}#type", t_uri),
        (B_ENTITY, f"{RDFS}#label", B_ENTITY_LABEL),
        (B_ENTITY, f"{ONTO}/{SHARED_ATTR}", B_STATUS_VAL),
        (B_ENTITY, f"{ONTO}/{B_ATTR_PRIVATE}", "SUITE-99"),
    ]
    # Minimal stats so type-counts / summary entityCount resolve.
    a_stats = [(t_uri, "https://graph.infona.ai/stats/entityCount", "1")]
    b_stats = [(t_uri, "https://graph.infona.ai/stats/entityCount", "1")]

    return IsolationNeptune(
        {
            public_graph_uri(): public + public_hotel,
            enhanced_graph_uri(): [],
            GRAPH_A: a_onto,
            GRAPH_B: b_onto,
            KG_A: a_inst,
            KG_B: b_inst,
            STATS_A: a_stats,
            STATS_B: b_stats,
        },
        plant_a_into_b=plant_a_into_b,
    )


# ---------------------------------------------------------------------------
# Property-graph twin of the same adversarial fixture (ONTA-527)
# ---------------------------------------------------------------------------
#
# Same two tenants, same colliding ``Hotel``, same colliding ``status`` leaf,
# same unique markers — written through the REAL catalog + instance write paths
# into ONE ``MemoryGraphStore``. Both workspaces share the store, so scope is the
# only thing keeping them apart: exactly the adversarial premise the named-graph
# fixture set up, restated for the backend that ships.


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


async def _seed_tenant_store(
    store: MemoryGraphStore,
    tenant_id: str,
    *,
    type_desc: str,
    shared_attr_why: str,
    private_attr: str,
    private_attr_why: str,
    entity: str,
    entity_label: str,
    status_value: str,
    private_value: str,
) -> None:
    await upsert_type(
        name=TYPE_NAME,
        description=type_desc,
        tenant_id=tenant_id,
        layer="tenant",
        store=store,
    )
    await upsert_attribute(
        type_name=TYPE_NAME,
        attr_name=SHARED_ATTR,
        description=shared_attr_why,
        tenant_id=tenant_id,
        layer="tenant",
        store=store,
    )
    await upsert_attribute(
        type_name=TYPE_NAME,
        attr_name=private_attr,
        description=private_attr_why,
        tenant_id=tenant_id,
        layer="tenant",
        store=store,
    )
    await insert_facts(
        None,
        kg_graph_uri(tenant_id, KG_NAME),
        [
            (entity, f"{RDF}#type", f"{TENANT_NS}/{TYPE_NAME}"),
            (entity, f"{RDFS}#label", entity_label),
            (entity, f"{ONTO}/{SHARED_ATTR}", status_value),
            (entity, f"{ONTO}/{private_attr}", private_value),
        ],
        store=store,
    )


def _seed_adversarial_store(store: MemoryGraphStore | None = None) -> MemoryGraphStore:
    """Seed BOTH tenants into one store and install it as the process store."""
    store = store if store is not None else MemoryGraphStore()

    async def seed() -> None:
        await _seed_tenant_store(
            store,
            TENANT_A,
            type_desc=A_TYPE_DESC,
            shared_attr_why=A_ATTR_WHY,
            private_attr=A_ATTR_PRIVATE,
            private_attr_why=A_ATTR_PRIVATE_WHY,
            entity=A_ENTITY,
            entity_label=A_ENTITY_LABEL,
            status_value=A_STATUS_VAL,
            private_value="FR-001",
        )
        await _seed_tenant_store(
            store,
            TENANT_B,
            type_desc=B_TYPE_DESC,
            shared_attr_why=B_ATTR_WHY,
            private_attr=B_ATTR_PRIVATE,
            private_attr_why=B_ATTR_PRIVATE_WHY,
            entity=B_ENTITY,
            entity_label=B_ENTITY_LABEL,
            status_value=B_STATUS_VAL,
            private_value="SUITE-99",
        )

    _run(seed())
    configure_graph_store(store)
    return store


class _ScopeRecordingStore:
    """Records the :class:`GraphScope` of every session a request opens.

    The property-graph replacement for ``IsolationNeptune.queries``: on SPARQL
    the confinement was visible in the ``FROM <graph>`` of each emitted query, so
    a structural test could read it off the text. Here it is the scope a session
    is opened with — every read through that session has ``$tenant_id`` / ``$kg``
    forced from it (``graph/store.py::merge_scope_params``), so recording the
    scopes IS reading the confinement.
    """

    def __init__(self, inner: MemoryGraphStore):
        self._inner = inner
        self.scopes: list[GraphScope] = []

    def session(self, scope: GraphScope):
        self.scopes.append(scope)
        return self._inner.session(scope)

    def __getattr__(self, name: str) -> Any:  # health / kg_registry_* / internals
        return getattr(self._inner, name)


class _UnionSession:
    """A session that ignores its own scope and also answers with a peer's rows.

    The property-graph shape of the SPARQL union-default failure mode the
    ``LeakyGrepNeptune`` self-test planted: a store that reads past the scope it
    was handed.
    """

    def __init__(self, real, peer):
        self._real = real
        self._peer = peer

    @property
    def scope(self):
        return self._real.scope

    async def execute_template(self, name: str, params=None):
        rows = list(await self._real.execute_template(name, params))
        rows.extend(await self._peer.execute_template(name, params))
        return rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _LeakyStore:
    """Hands out :class:`_UnionSession`s that also read ``peer_scope``."""

    def __init__(self, inner: MemoryGraphStore, peer_scope: GraphScope):
        self._inner = inner
        self._peer_scope = peer_scope

    def session(self, scope: GraphScope):
        return _UnionSession(
            self._inner.session(scope), self._inner.session(self._peer_scope)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# App factories
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_global_state():
    register_entitlement_checker(None)
    register_promotion_consent_provider(None)
    reset_type_skill_store()
    explore_routes._summary_cache.clear()
    from infona_client.nlp.pipeline import _ontology_cache
    _ontology_cache.clear()
    yield
    register_entitlement_checker(None)
    register_promotion_consent_provider(None)
    reset_type_skill_store()
    explore_routes._summary_cache.clear()
    _ontology_cache.clear()


def _ctx(tenant_id: str, *, entitled: bool = False, is_operator: bool = False) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        api_key=f"key-{tenant_id}",
        enhanced_entitled=entitled,
        is_operator=is_operator,
    )


async def _seed_skills_async() -> InMemoryTypeSkillStore:
    store = InMemoryTypeSkillStore()
    import infona_client.skills.store as skill_store_mod

    skill_store_mod._store = store  # type: ignore[attr-defined]
    await store.upsert(
        TypeSkill(
            slug=A_SKILL_SLUG,
            type_name=TYPE_NAME,
            body=A_SKILL_BODY,
            title=A_SKILL_TITLE,
            summary="Franchise only.",
            layer=Layer.TENANT,
            tenant_id=TENANT_A,
        )
    )
    await store.upsert(
        TypeSkill(
            slug=B_SKILL_SLUG,
            type_name=TYPE_NAME,
            body=B_SKILL_BODY,
            title=B_SKILL_TITLE,
            summary="Corporate only.",
            layer=Layer.TENANT,
            tenant_id=TENANT_B,
        )
    )
    return store


def _app(neptune, tenant_id: str, *routers, entitled: bool = False, is_operator: bool = False):
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    app.dependency_overrides[get_neptune_client] = lambda: neptune
    ctx = _ctx(tenant_id, entitled=entitled, is_operator=is_operator)
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: ctx
    )
    return TestClient(app)


def _ontology_client(neptune, tenant_id: str, **kw) -> TestClient:
    return _app(neptune, tenant_id, ontology_routes.router, **kw)


def _explore_client(neptune, tenant_id: str, **kw) -> TestClient:
    return _app(neptune, tenant_id, explore_routes.router, **kw)


def _skills_client(neptune, tenant_id: str, **kw) -> TestClient:
    return _app(neptune, tenant_id, skills_routes.router, **kw)


def _functions_client(neptune, tenant_id: str, **kw) -> TestClient:
    return _app(neptune, tenant_id, functions_routes.router, **kw)


def _kg_client(neptune, tenant_id: str, **kw) -> TestClient:
    return _app(neptune, tenant_id, kg_routes.router, **kw)


def _grep_client(neptune, tenant_id: str, **kw) -> TestClient:
    return _app(neptune, tenant_id, grep_routes.router, **kw)


def _operator_client(neptune) -> TestClient:
    return _app(neptune, TENANT_A, operator_routes.router, is_operator=True)


# ===========================================================================
# 1. Ontology routes — list / get / workspace
# ===========================================================================


def test_ontology_workspace_get_isolated():
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    a = _ontology_client(neptune, TENANT_A).get(f"/graphs/{TENANT_A}/ontology")
    b = _ontology_client(neptune, TENANT_B).get(f"/graphs/{TENANT_B}/ontology")
    assert a.status_code == 200 and b.status_code == 200
    a_body, b_body = a.json(), b.json()
    assert a_body["tenant_id"] == TENANT_A
    assert b_body["tenant_id"] == TENANT_B

    a_dump, b_dump = str(a_body), str(b_body)
    _assert_own_markers_present(a_dump, owner="A", required=(A_TYPE_DESC, A_ATTR_PRIVATE))
    _assert_own_markers_present(b_dump, owner="B", required=(B_TYPE_DESC, B_ATTR_PRIVATE))
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")

    # Colliding type name resolves per-tenant (shadowing), not both.
    a_hotels = [t for t in a_body["types"] if t["name"] == TYPE_NAME]
    b_hotels = [t for t in b_body["types"] if t["name"] == TYPE_NAME]
    assert len(a_hotels) == 1 and a_hotels[0]["description"] == A_TYPE_DESC
    assert len(b_hotels) == 1 and b_hotels[0]["description"] == B_TYPE_DESC
    assert a_hotels[0]["layer"] == "tenant"
    assert b_hotels[0]["layer"] == "tenant"

    # The colliding ``status`` leaf carries each tenant's OWN description — the
    # sharpest per-tenant check available now that the type name is the same
    # string on both sides.
    def _status_why(body):
        hotel = [t for t in body["types"] if t["name"] == TYPE_NAME][0]
        return {
            a["name"]: a.get("description") for a in hotel["attributes"]
        }[SHARED_ATTR]

    assert _status_why(a_body) == A_ATTR_WHY
    assert _status_why(b_body) == B_ATTR_WHY

    # ONTA-535: layered catalog merge restores the status strip (tenant +
    # public; enhanced only when entitled). Strip entries are status-only —
    # they must not name the peer tenant. Public types only appear when
    # seeded into the public catalog; this fixture seeds tenant catalogs
    # only, so BaseHotel stays absent (still not a cross-tenant leak).
    a_layer_names = {L["layer"] for L in a_body["layers"]}
    b_layer_names = {L["layer"] for L in b_body["layers"]}
    assert "tenant" in a_layer_names and "public" in a_layer_names
    assert "tenant" in b_layer_names and "public" in b_layer_names
    assert all(TENANT_B not in str(L) for L in a_body["layers"])
    assert all(TENANT_A not in str(L) for L in b_body["layers"])
    assert not any(t["name"] == PUBLIC_TYPE for t in a_body["types"])
    assert not any(t["name"] == PUBLIC_TYPE for t in b_body["types"])
    assert PUBLIC_DESC not in a_dump and PUBLIC_DESC not in b_dump


def test_ontology_list_types_isolated():
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    a = _ontology_client(neptune, TENANT_A).get(f"/graphs/{TENANT_A}/ontology/types")
    b = _ontology_client(neptune, TENANT_B).get(f"/graphs/{TENANT_B}/ontology/types")
    assert a.status_code == 200 and b.status_code == 200
    a_dump, b_dump = str(a.json()), str(b.json())
    _assert_own_markers_present(a_dump, owner="A", required=(A_TYPE_DESC,))
    _assert_own_markers_present(b_dump, owner="B", required=(B_TYPE_DESC,))
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")


def test_ontology_get_type_isolated():
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    a = _ontology_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/ontology/types/{TYPE_NAME}"
    )
    b = _ontology_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/ontology/types/{TYPE_NAME}"
    )
    assert a.status_code == 200 and b.status_code == 200
    a_body, b_body = a.json(), b.json()
    assert a_body["description"] == A_TYPE_DESC
    assert b_body["description"] == B_TYPE_DESC
    a_attrs = {x["name"] for x in a_body.get("attributes", [])}
    b_attrs = {x["name"] for x in b_body.get("attributes", [])}
    assert A_ATTR_PRIVATE in a_attrs
    assert B_ATTR_PRIVATE not in a_attrs
    assert B_ATTR_PRIVATE in b_attrs
    assert A_ATTR_PRIVATE not in b_attrs
    _assert_no_peer_markers(str(a_body), peer="B")
    _assert_no_peer_markers(str(b_body), peer="A")


def test_planted_catalog_scope_leak_is_caught():
    """Self-test: a catalog session that reads past its scope MUST turn red.

    Without this the isolation cases above could pass merely because the peer's
    catalog rows were never written — this proves they fail when a leak is real.

    ONTA-535 layered merge shadows by type **name** (first-visible wins), so a
    peer's colliding ``Hotel`` type description may not surface. The leak still
    lands as **peer attributes** under the winning type (attrs are keyed by
    domain leaf, not shadowed away) — that is the surface the planted
    ``_UnionSession`` exercises against the current GraphStore path.
    """
    store = _seed_adversarial_store()
    leaky = _LeakyStore(store, GraphScope.for_catalog(layer="tenant", tenant_id=TENANT_B))
    configure_graph_store(leaky)

    res = _ontology_client(_seed_adversarial(), TENANT_A).get(
        f"/graphs/{TENANT_A}/ontology"
    )
    assert res.status_code == 200, res.text
    dump = str(res.json())
    # Attribute-level leak (type-name shadowing can hide B_TYPE_DESC).
    assert (
        B_ATTR_PRIVATE in dump
        or B_ATTR_PRIVATE_WHY in dump
        or B_ATTR_WHY in dump
    ), "premise: the planted leak must leak peer attributes via the unioned session"
    with pytest.raises(AssertionError, match="cross-tenant leak"):
        _assert_no_peer_markers(dump, peer="B")


@pytest.mark.asyncio
async def test_fetch_ontology_under_graph_union_isolated():
    """Both tenants' triples live in one store; LayerStack must not union them."""
    neptune = _seed_adversarial()
    body_a = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_A, entitled=False).layer_pairs(),
        entitled=False,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    body_b = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_B, entitled=False).layer_pairs(),
        entitled=False,
        tenant_id=TENANT_B,
        apply_shadowing=True,
    )
    a_dump, b_dump = str(body_a.model_dump()), str(body_b.model_dump())
    _assert_own_markers_present(a_dump, owner="A", required=(A_TYPE_DESC, A_ATTR_PRIVATE, A_FUNC_NAME))
    _assert_own_markers_present(b_dump, owner="B", required=(B_TYPE_DESC, B_ATTR_PRIVATE, B_FUNC_NAME))
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")


# ===========================================================================
# 2. Explorer — summary / search / records / type-counts
# ===========================================================================


def test_explore_summary_isolated():
    # P-A1a: /summary is served from the process GraphStore. Seed both tenants
    # via the real write path (same adversarial twin as records/type-counts).
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    a = _explore_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/explore/kgs/{KG_NAME}/types/{TYPE_NAME}/summary"
    )
    b = _explore_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/explore/kgs/{KG_NAME}/types/{TYPE_NAME}/summary"
    )
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    a_body, b_body = a.json(), b.json()
    assert a_body.get("description") == A_TYPE_DESC
    assert b_body.get("description") == B_TYPE_DESC
    a_attr_names = {x["name"] for x in a_body.get("attributes", [])}
    b_attr_names = {x["name"] for x in b_body.get("attributes", [])}
    # Ontology-declared attrs should surface when defs resolve.
    # Private peer attrs must never appear.
    assert B_ATTR_PRIVATE not in a_attr_names
    assert A_ATTR_PRIVATE not in b_attr_names
    _assert_no_peer_markers(str(a_body), peer="B")
    _assert_no_peer_markers(str(b_body), peer="A")


def test_explore_kg_schema_isolated():
    # ONTA-418: the whole-KG schema read touches the ontology (declarations)
    # AND the KG (population), so it belongs in this enumeration like every
    # other ontology-touching route.
    neptune = _seed_adversarial()
    a = _explore_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/explore/kgs/{KG_NAME}/schema"
    )
    b = _explore_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/explore/kgs/{KG_NAME}/schema"
    )
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    a_dump, b_dump = str(a.json()), str(b.json())
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")


def test_explore_search_isolated():
    neptune = _seed_adversarial()
    a = _explore_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/explore/search",
        params={"kg": KG_NAME, "q": "Hotel", "kind": "type"},
    )
    b = _explore_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/explore/search",
        params={"kg": KG_NAME, "q": "Hotel", "kind": "type"},
    )
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    # Both see Hotel (colliding name) — isolation is that the COUNT/URI path
    # used their own KG graph, not that the name differs.
    a_names = {r.get("name") for r in a.json()} if isinstance(a.json(), list) else set()
    b_names = {r.get("name") for r in b.json()} if isinstance(b.json(), list) else set()
    # Response shape may be {results: [...]} — accept both.
    if not a_names and isinstance(a.json(), dict):
        a_names = {r.get("name") for r in a.json().get("results", a.json().get("types", []))}
        b_names = {r.get("name") for r in b.json().get("results", b.json().get("types", []))}
    assert TYPE_NAME in a_names or TYPE_NAME in str(a.json())
    assert TYPE_NAME in b_names or TYPE_NAME in str(b.json())
    _assert_no_peer_markers(str(a.json()), peer="B")
    _assert_no_peer_markers(str(b.json()), peer="A")


def test_explore_records_isolated():
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    a = _explore_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/explore/kgs/{KG_NAME}/types/{TYPE_NAME}/records"
    )
    b = _explore_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/explore/kgs/{KG_NAME}/types/{TYPE_NAME}/records"
    )
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    a_dump, b_dump = str(a.json()), str(b.json())
    _assert_own_markers_present(a_dump, owner="A", required=(A_ENTITY_LABEL,))
    _assert_own_markers_present(b_dump, owner="B", required=(B_ENTITY_LABEL,))
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")
    # Peer entity URI must not appear.
    assert A_ENTITY not in b_dump
    assert B_ENTITY not in a_dump


def test_type_counts_isolated():
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    # knowledge_graphs router is mounted with prefix /graphs/{tenant}/kgs
    a = _kg_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/kgs/{KG_NAME}/type-counts"
    )
    b = _kg_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/kgs/{KG_NAME}/type-counts"
    )
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    _assert_no_peer_markers(str(a.json()), peer="B")
    _assert_no_peer_markers(str(b.json()), peer="A")
    # Both report Hotel instances — counts come from their own KG graph.
    a_names = {r["name"] for r in a.json()}
    b_names = {r["name"] for r in b.json()}
    assert TYPE_NAME in a_names
    assert TYPE_NAME in b_names


# ===========================================================================
# 2b. Literal grep (ONTA-416) — the rawest instance read in the API
# ===========================================================================
#
# Grep is the one route whose entire job is dumping raw instance LITERALS, so a
# scoping slip here leaks tenant data verbatim rather than as a type name.
# It is isolated by construction (``get_tenant`` + a server-built scope from
# ``tenant.tenant_id`` + a charset-validated kg; no caller value reaches it), and
# these tests pin that rather than assume it.
#
# ONTA-527: the route runs a property-graph scan, so the two structural tests
# below read the SCOPE of the sessions the request opens instead of the ``FROM``
# of the SPARQL it used to emit. Same property, one layer down — and the reads
# are no longer answered by a mock, so the peer's rows really are sitting in the
# same store waiting to leak.

GREP_COLLIDING_NEEDLE = "ISO402B_ENTITY"  # substring of BOTH tenants' labels


def _grep(client, tenant_id: str, needle: str = GREP_COLLIDING_NEEDLE):
    return client.post(
        f"/graphs/{tenant_id}/grep",
        json={"q": needle, "kg_name": KG_NAME},
    )


def test_grep_isolated():
    """A needle that matches BOTH tenants' entities returns only your own."""
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    a = _grep(_grep_client(neptune, TENANT_A), TENANT_A)
    b = _grep(_grep_client(neptune, TENANT_B), TENANT_B)
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    a_dump, b_dump = str(a.json()), str(b.json())
    # Positive: each tenant DOES see its own literal (so the test isn't vacuous).
    _assert_own_markers_present(a_dump, owner="A", required=(A_ENTITY_LABEL,))
    _assert_own_markers_present(b_dump, owner="B", required=(B_ENTITY_LABEL,))
    # Negative: no peer marker, no peer entity URI.
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")
    assert B_ENTITY not in a_dump
    assert A_ENTITY not in b_dump


def test_grep_scans_only_the_callers_scope():
    """Structural pin: every session the route opens is scoped to exactly the
    caller's workspace + KG, derived from the RESOLVED tenant id — never the
    path string.

    Replaces the ``FROM <graph>`` scan of the SPARQL era. ``$tenant_id`` / ``$kg``
    are forced from this scope onto every statement the session runs
    (``graph/store.py::merge_scope_params``), so a session opened for
    ``(TENANT_A, hotels)`` cannot read another workspace even if the Cypher tried.
    """
    neptune = _seed_adversarial()
    recorder = _ScopeRecordingStore(_seed_adversarial_store())
    configure_graph_store(recorder)

    res = _grep(_grep_client(neptune, TENANT_A), TENANT_A)
    assert res.status_code == 200, res.text

    assert recorder.scopes, "the grep must open a scoped session"
    for scope in recorder.scopes:
        assert (scope.tenant_id, scope.kg) == (TENANT_A, KG_NAME)
    # And no SPARQL was emitted at all.
    assert neptune.queries == []


def test_grep_path_tenant_cannot_override_the_key_tenant():
    """B's key hitting A's path still scans B's data (auth resolves the tenant;
    the path segment never reaches the scope)."""
    neptune = _seed_adversarial()
    recorder = _ScopeRecordingStore(_seed_adversarial_store())
    configure_graph_store(recorder)

    res = _grep_client(neptune, TENANT_B).post(
        f"/graphs/{TENANT_A}/grep",
        json={"q": GREP_COLLIDING_NEEDLE, "kg_name": KG_NAME},
    )
    assert res.status_code == 200, res.text
    assert recorder.scopes
    for scope in recorder.scopes:
        assert (scope.tenant_id, scope.kg) == (TENANT_B, KG_NAME)
    # B's own row comes back; A's — whose id is in the URL — does not.
    _assert_own_markers_present(str(res.json()), owner="B", required=(B_ENTITY_LABEL,))
    _assert_no_peer_markers(str(res.json()), peer="A")


def test_caller_supplied_scope_params_cannot_widen_a_session():
    """T2, checked directly: a session's scope OVERWRITES caller-supplied scope.

    The property-graph counterpart of the SPARQL dataset-clause guard this suite
    grew up next to (``test_query_tenant_scoping.py``): there, the danger was
    caller text naming another workspace's graph in a ``FROM``; here it is
    caller-supplied ``tenant_id`` / ``kg`` parameters riding along with a
    template. ``graph/store.py::merge_scope_params`` applies the session scope
    LAST, so the smuggled values are discarded — asserted against a store that
    really does hold the peer's rows.
    """
    store = _seed_adversarial_store()
    session = store.session(GraphScope.for_instance(TENANT_A, KG_NAME))

    rows = _run(
        session.execute_template(
            "entity_literal_grep",
            {
                "needle": GREP_COLLIDING_NEEDLE,
                "case_sensitive": False,
                "type_name": None,
                "predicate_leaf": None,
                "limit": 50,
                # Smuggled scope — must be ignored, not honoured.
                "tenant_id": TENANT_B,
                "kg": KG_NAME,
            },
        )
    )
    dump = str([r.to_dict() for r in rows])
    _assert_own_markers_present(dump, owner="A", required=(A_ENTITY_LABEL,))
    _assert_no_peer_markers(dump, peer="B")
    assert B_ENTITY not in dump


def test_grep_private_attribute_value_never_crosses():
    """A's private attribute value is invisible to B even when B names it."""
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    res = _grep(_grep_client(neptune, TENANT_B), TENANT_B, needle=A_STATUS_VAL)
    assert res.status_code == 200, res.text
    assert res.json()["count"] == 0
    _assert_no_peer_markers(str(res.json()), peer="A")
    # Not vacuous: B DOES find its own value with the same query shape.
    own = _grep(_grep_client(neptune, TENANT_B), TENANT_B, needle=B_STATUS_VAL)
    assert own.json()["count"] >= 1


def test_planted_grep_leak_is_caught():
    """Self-test: a store that reads past the scope it was handed (the
    union-default failure mode) MUST fail the assertions above — proving they
    are not vacuously passing."""
    store = _seed_adversarial_store()
    leaky = _LeakyStore(store, GraphScope.for_instance(TENANT_B, KG_NAME))
    configure_graph_store(leaky)

    res = _grep(_grep_client(_seed_adversarial(), TENANT_A), TENANT_A)
    assert res.status_code == 200, res.text
    assert B_ENTITY_LABEL in str(res.json()), "premise: the planted leak must leak"
    with pytest.raises(AssertionError):
        _assert_no_peer_markers(str(res.json()), peer="B")


# ===========================================================================
# 3. Skills + functions
# ===========================================================================


@pytest.mark.asyncio
async def test_skills_list_and_prompt_block_isolated():
    neptune = _seed_adversarial()
    await _seed_skills_async()

    a = _skills_client(neptune, TENANT_A).get(f"/graphs/{TENANT_A}/skills")
    b = _skills_client(neptune, TENANT_B).get(f"/graphs/{TENANT_B}/skills")
    assert a.status_code == 200 and b.status_code == 200
    a_dump, b_dump = str(a.json()), str(b.json())
    _assert_own_markers_present(a_dump, owner="A", required=(A_SKILL_TITLE,))
    _assert_own_markers_present(b_dump, owner="B", required=(B_SKILL_TITLE,))
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")

    a_pb = _skills_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/skills/prompt-block",
        params=[("type_name", TYPE_NAME)],
    )
    b_pb = _skills_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/skills/prompt-block",
        params=[("type_name", TYPE_NAME)],
    )
    assert a_pb.status_code == 200 and b_pb.status_code == 200
    a_text = a_pb.json().get("text", "")
    b_text = b_pb.json().get("text", "")
    assert A_SKILL_BODY in a_text
    assert B_SKILL_BODY in b_text
    assert B_SKILL_BODY not in a_text
    assert A_SKILL_BODY not in b_text
    _assert_no_peer_markers(a_text, peer="B")
    _assert_no_peer_markers(b_text, peer="A")


def test_functions_list_isolated():
    neptune = _seed_adversarial()
    a = _functions_client(neptune, TENANT_A).get(f"/graphs/{TENANT_A}/functions")
    b = _functions_client(neptune, TENANT_B).get(f"/graphs/{TENANT_B}/functions")
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    a_dump, b_dump = str(a.json()), str(b.json())
    _assert_own_markers_present(a_dump, owner="A", required=(A_FUNC_NAME, A_FUNC_DESC))
    _assert_own_markers_present(b_dump, owner="B", required=(B_FUNC_NAME, B_FUNC_DESC))
    _assert_no_peer_markers(a_dump, peer="B")
    _assert_no_peer_markers(b_dump, peer="A")


# ===========================================================================
# 4. Ask route — layer stack + prompt/ontology material
# ===========================================================================


def test_ask_route_layer_graph_uris_are_tenant_scoped():
    """ask must thread the caller's tenant graph, never the peer's."""
    app = FastAPI()
    app.include_router(ask_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: AsyncMock()
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: _ctx(TENANT_B)
    )
    from infona_client.api.deps import get_enrichment_job_store
    app.dependency_overrides[get_enrichment_job_store] = lambda: None

    captured: dict = {}

    async def _fake_ask(self, question, graph_uri, instance_graph=None,
                        exclude_questions=None, layer_graph_uris=None, **kw):
        captured["layer_graph_uris"] = list(layer_graph_uris or [])
        captured["graph_uri"] = graph_uri
        return NLResult(answer="ok", sparql="SELECT * WHERE {}", explanation="")

    client = TestClient(app)
    with patch.object(ask_routes.NLQueryPipeline, "ask", _fake_ask):
        r = client.post(f"/graphs/{TENANT_B}/ask", json={"question": "list hotels"})
    assert r.status_code == 200, r.text
    uris = captured["layer_graph_uris"]
    assert GRAPH_B in uris
    assert GRAPH_A not in uris
    assert public_graph_uri() in uris
    assert captured["graph_uri"] == GRAPH_B


@pytest.mark.asyncio
async def test_ask_ontology_summary_no_prompt_leakage():
    """NL ontology summary for B must never contain A-only markers."""
    from infona_client.nlp.pipeline import NLQueryPipeline, _ontology_cache

    _ontology_cache.clear()
    neptune = _seed_adversarial()
    pipeline = NLQueryPipeline(neptune, "fake-key")

    stack_a = LayerStack(GRAPH_A, entitled=False)
    stack_b = LayerStack(GRAPH_B, entitled=False)

    summary_a = await pipeline._fetch_ontology(
        GRAPH_A,
        instance_graph=KG_A,
        layer_graph_uris=stack_a.visible_graph_uris(),
    )
    summary_b = await pipeline._fetch_ontology(
        GRAPH_B,
        instance_graph=KG_B,
        layer_graph_uris=stack_b.visible_graph_uris(),
    )

    assert TYPE_NAME in summary_a and TYPE_NAME in summary_b
    _assert_own_markers_present(summary_a, owner="A", required=(A_ATTR_PRIVATE, A_FUNC_NAME))
    _assert_own_markers_present(summary_b, owner="B", required=(B_ATTR_PRIVATE, B_FUNC_NAME))
    # Descriptions land via typeComment only in detail query; full ontology
    # query projects labels/attrs/funcs. Private attrs + funcs are the hard pin.
    assert A_ATTR_PRIVATE not in summary_b
    assert B_ATTR_PRIVATE not in summary_a
    assert A_FUNC_NAME not in summary_b
    assert B_FUNC_NAME not in summary_a
    _assert_no_peer_markers(summary_a, peer="B")
    _assert_no_peer_markers(summary_b, peer="A")


# ===========================================================================
# 5. Cache poisoning — _summary_cache + _ontology_cache
# ===========================================================================


def test_summary_cache_keyed_by_tenant_no_poisoning():
    """A then B back-to-back: B never receives A's cached summary."""
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    explore_routes._summary_cache.clear()

    client_a = _explore_client(neptune, TENANT_A)
    client_b = _explore_client(neptune, TENANT_B)

    r_a = client_a.get(
        f"/graphs/{TENANT_A}/explore/kgs/{KG_NAME}/types/{TYPE_NAME}/summary"
    )
    assert r_a.status_code == 200
    assert r_a.json().get("description") == A_TYPE_DESC

    # Cache must now hold an entry under (TENANT_A, kg, type).
    a_keys = [k for k in explore_routes._summary_cache if k[0] == TENANT_A]
    assert a_keys, "expected _summary_cache to store tenant A entry"
    for k in a_keys:
        assert k[0] != TENANT_B

    r_b = client_b.get(
        f"/graphs/{TENANT_B}/explore/kgs/{KG_NAME}/types/{TYPE_NAME}/summary"
    )
    assert r_b.status_code == 200
    body_b = r_b.json()
    assert body_b.get("description") == B_TYPE_DESC
    _assert_no_peer_markers(str(body_b), peer="A")

    # Structural pin: every live key includes tenant_id as element 0.
    for key in explore_routes._summary_cache:
        assert isinstance(key, tuple) and len(key) >= 3
        assert key[0] in (TENANT_A, TENANT_B)


def test_summary_cache_planted_cross_tenant_key_is_not_hit():
    """Planted violation: if cache were keyed only by (kg, type), B would get A.

    We plant A's payload under a hypothetical shared key and under B's real key,
    then prove a clean B request after clearing wrong keys returns B's content —
    and that the production key includes tenant_id so A/B cannot collide.
    """
    explore_routes._summary_cache.clear()
    # Hypothetical broken key (kg, type only) — production must NOT use this.
    broken = (KG_NAME, TYPE_NAME)
    explore_routes._summary_cache[broken] = (  # type: ignore[index]
        time.monotonic(),
        {"name": TYPE_NAME, "description": A_TYPE_DESC, "attributes": []},
    )
    # Production keys are 3-tuples with tenant first. GraphStore path (P-A1a).
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    r_b = _explore_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/explore/kgs/{KG_NAME}/types/{TYPE_NAME}/summary"
    )
    assert r_b.status_code == 200
    body = r_b.json()
    # B must not serve the planted broken-key payload.
    assert body.get("description") == B_TYPE_DESC
    assert A_TYPE_DESC not in str(body)
    # And the live cache entry for this request uses tenant_id.
    live = [
        k for k in explore_routes._summary_cache
        if isinstance(k, tuple) and len(k) == 3 and k[0] == TENANT_B
    ]
    assert live, "production cache key must be (tenant_id, kg_name, type_name)"


@pytest.mark.asyncio
async def test_ontology_cache_keyed_by_graph_uri_no_poisoning():
    """pipeline._ontology_cache keys by graph_uri (+ layers); A then B is safe."""
    from infona_client.nlp.pipeline import NLQueryPipeline, _ontology_cache

    _ontology_cache.clear()
    neptune = _seed_adversarial()
    pipeline = NLQueryPipeline(neptune, "fake-key")
    stack_a = LayerStack(GRAPH_A, entitled=False)
    stack_b = LayerStack(GRAPH_B, entitled=False)

    s_a = await pipeline._fetch_ontology(
        GRAPH_A, layer_graph_uris=stack_a.visible_graph_uris()
    )
    s_b = await pipeline._fetch_ontology(
        GRAPH_B, layer_graph_uris=stack_b.visible_graph_uris()
    )
    assert A_ATTR_PRIVATE in s_a or TYPE_NAME in s_a
    assert B_ATTR_PRIVATE in s_b or TYPE_NAME in s_b
    assert A_ATTR_PRIVATE not in s_b
    assert B_ATTR_PRIVATE not in s_a

    # Cache keys must include the tenant graph URI so A and B cannot collide.
    a_keys = [k for k in _ontology_cache if GRAPH_A in k]
    b_keys = [k for k in _ontology_cache if GRAPH_B in k]
    assert a_keys, "expected ontology cache entry for tenant A graph"
    assert b_keys, "expected ontology cache entry for tenant B graph"
    assert set(a_keys).isdisjoint(set(b_keys))


# ===========================================================================
# 6. Operator global browser — zero tenant-layer content
# ===========================================================================


@pytest.mark.asyncio
async def test_operator_global_shows_zero_tenant_content():
    neptune = _seed_adversarial()
    await _seed_skills_async()

    body = await fetch_global_ontology(neptune)
    dump = body.model_dump()
    assert "tenant_id" not in dump
    # Tenant-private type description must not appear.
    for m in A_MARKERS + B_MARKERS:
        assert m not in str(dump), f"tenant marker {m!r} leaked into operator global"
    # Tenant Hotel is private; Public Hotel may appear under public layer.
    tenant_hotels = [
        t for t in body.types
        if t.name == TYPE_NAME and t.layer == "tenant"
    ]
    assert tenant_hotels == []
    # No tenant-layer types at all.
    assert all(t.layer != "tenant" for t in body.types)
    # Skills on global must not carry tenant markers.
    for t in body.types:
        for s in t.skills:
            assert s.layer != "tenant"
            assert A_SKILL_BODY not in (s.body or "")
            assert B_SKILL_BODY not in (s.body or "")


def test_operator_http_route_zero_tenant_markers():
    neptune = _seed_adversarial()
    r = _operator_client(neptune).get("/operator/ontology/global")
    assert r.status_code == 200
    dump = str(r.json())
    for m in A_MARKERS + B_MARKERS:
        assert m not in dump
    assert "tenant_id" not in r.json()
    layer_names = {L["layer"] for L in r.json()["layers"]}
    assert layer_names == {"public", "enhanced"}
    assert "tenant" not in layer_names


# ===========================================================================
# 7. Consent re-assert (402a already covered; pin refuse here too)
# ===========================================================================


@pytest.mark.asyncio
async def test_consent_refuses_without_record_zero_writes():
    """Without consent, promotion write is refused (ONTA-402a re-assert)."""
    register_promotion_consent_provider(None)  # DenyAll default
    with pytest.raises(PromotionConsentError, match="no recorded consent"):
        await require_promotion_consent(TENANT_A, target_layer="public")

    from infona_client.resolver.governance import (
        GovernanceDecision,
        GovernanceEngine,
        JudgeVerdict,
        TypeProposal,
    )
    from datetime import datetime, timezone

    mock = AsyncMock()
    mock.update = AsyncMock()
    engine = GovernanceEngine(mock)
    proposal = TypeProposal(
        type_name="LoyaltyTier",
        parent_chain=["Tier"],
        tenant_id=TENANT_A,
        reasoning="generic",
        proposer_model="test",
    )
    decision = GovernanceDecision(
        target_layer="public",
        votes=[JudgeVerdict(approve=True, reasoning="ok"),
               JudgeVerdict(approve=True, reasoning="ok")],
        approved=True,
    )
    with pytest.raises(PromotionConsentError):
        await engine.write_governed_type(
            proposal, decision, timestamp=datetime(2026, 6, 9, tzinfo=timezone.utc),
        )
    assert mock.update.call_count == 0


# ===========================================================================
# 8. Planted-violation self-tests (assertions catch real leaks)
# ===========================================================================


@pytest.mark.asyncio
async def test_planted_graph_swap_is_caught_by_isolation_assertions():
    """If B's graph were answered with A's triples, our assertions go red."""
    neptune = _seed_adversarial(plant_a_into_b=True)
    body_b = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_B, entitled=False).layer_pairs(),
        entitled=False,
        tenant_id=TENANT_B,
        apply_shadowing=True,
    )
    dump = str(body_b.model_dump())
    # Under the planted leak, B sees A's type description.
    assert A_TYPE_DESC in dump
    # The suite's own cross-tenant assertion MUST fire.
    with pytest.raises(AssertionError, match="cross-tenant leak"):
        _assert_no_peer_markers(dump, peer="A")


def test_planted_summary_cache_collision_is_caught():
    """If production omitted tenant_id from the cache key, A→B would poison."""
    explore_routes._summary_cache.clear()
    # Simulate a broken cache that keys only by (kg, type).
    broken_key = (KG_NAME, TYPE_NAME)
    explore_routes._summary_cache[broken_key] = (  # type: ignore[index]
        time.monotonic(),
        {"name": TYPE_NAME, "description": A_TYPE_DESC, "attributes": [
            {"name": A_ATTR_PRIVATE}
        ]},
    )
    # Production key shape pin: real keys are (tenant, kg, type).
    production_key = (TENANT_A, KG_NAME, TYPE_NAME)
    assert len(production_key) == 3
    assert production_key[0] == TENANT_A
    # The broken 2-tuple must never be what get_type_summary looks up —
    # which is (tenant.tenant_id, kg_name, type_name). Prove via source.
    explore_dir = Path(explore_routes.__file__).resolve().parent
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(explore_dir.glob("explore*.py"))
    )
    assert "cache_key = (tenant.tenant_id, kg_name, type_name)" in src
    # Looking up with the production key does NOT hit the broken key.
    assert explore_routes._summary_cache.get(production_key) is None


# ===========================================================================
# 9. MCP / CLI surface — pin the server routes the clients hit
# ===========================================================================


def test_mcp_view_ontology_targets_canonical_types_route():
    """MCP ``view_ontology`` → SDK ``ontologyTypes()`` → GET /ontology/types.

    Full MCP TS is out of process; we pin the Python route the SDK hits and
    that the MCP tool source references the SDK method (contract style).
    """
    neptune = _seed_adversarial()
    _seed_adversarial_store()
    r = _ontology_client(neptune, TENANT_A).get(
        f"/graphs/{TENANT_A}/ontology/types"
    )
    assert r.status_code == 200
    _assert_own_markers_present(str(r.json()), owner="A", required=(A_TYPE_DESC,))
    _assert_no_peer_markers(str(r.json()), peer="B")

    mcp_src = (
        Path(__file__).resolve().parent.parent
        / "packages"
        / "mcp"
        / "src"
    )
    src = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(mcp_src.glob("*.ts"))
    )
    assert "view_ontology" in src
    assert "ontologyTypes" in src


def test_cli_ontology_types_targets_canonical_route():
    """CLI ``ontology types`` uses the same GET /ontology/types the suite pins."""
    cli_src = (
        Path(__file__).resolve().parent.parent
        / "packages"
        / "cli"
        / "src"
    )
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(cli_src.glob("client*.ts"))
    )
    assert "ontologyTypes" in src
    assert "/ontology/types" in src

    neptune = _seed_adversarial()
    _seed_adversarial_store()
    r = _ontology_client(neptune, TENANT_B).get(
        f"/graphs/{TENANT_B}/ontology/types"
    )
    assert r.status_code == 200
    _assert_own_markers_present(str(r.json()), owner="B", required=(B_TYPE_DESC,))
    _assert_no_peer_markers(str(r.json()), peer="A")


# ===========================================================================
# 10. Marker uniqueness sanity (suite self-check)
# ===========================================================================


def test_markers_are_pairwise_unique_and_non_substring():
    """Guard the suite itself: markers must not substring-match each other."""
    all_m = list(A_MARKERS) + list(B_MARKERS) + [PUBLIC_DESC]
    assert len(all_m) == len(set(all_m)), "duplicate markers in suite fixture"
    for i, a in enumerate(all_m):
        for j, b in enumerate(all_m):
            if i == j:
                continue
            assert a not in b, f"marker {a!r} is a substring of {b!r}"
