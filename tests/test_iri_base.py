"""Configurable IRI base (ONTA brand default + env override)."""

from cograph_client.graph.iri import (
    DEFAULT_IRI_BASE,
    ENTITY_URI_PREFIX,
    GRAPH_URI_PREFIX,
    IRI_BASE,
    LEGACY_IRI_BASES,
    TYPE_URI_PREFIX,
)
from cograph_client.graph.ontology_queries import entity_uri, type_uri
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.nlp.validator import normalize_sparql


def test_default_base_is_onta_branded():
    assert IRI_BASE == DEFAULT_IRI_BASE == "https://graph.onta.sh"
    assert not IRI_BASE.endswith("/")
    assert "cograph.tech" not in IRI_BASE


def test_derived_prefixes_share_base():
    assert TYPE_URI_PREFIX == f"{IRI_BASE}/types/"
    assert ENTITY_URI_PREFIX == f"{IRI_BASE}/entities/"
    assert GRAPH_URI_PREFIX == f"{IRI_BASE}/graphs/"


def test_minters_use_live_base():
    assert entity_uri("City", "San Francisco") == f"{IRI_BASE}/entities/City/San_Francisco"
    assert type_uri("Person") == f"{IRI_BASE}/types/Person"
    assert tenant_graph_uri("demo") == f"{IRI_BASE}/graphs/demo"
    assert kg_graph_uri("demo", "kg1") == f"{IRI_BASE}/graphs/demo/kg/kg1"


def test_normalize_sparql_rewrites_legacy_hosts():
    for legacy in LEGACY_IRI_BASES:
        sparql = f"SELECT ?s WHERE {{ ?s a <{legacy}/types/Film> }}"
        out = normalize_sparql(sparql)
        assert f"<{IRI_BASE}/types/Film>" in out
        assert legacy not in out


def test_legacy_bases_documented():
    assert "https://cograph.tech" in LEGACY_IRI_BASES
    assert "https://omnix.dev" in LEGACY_IRI_BASES
