"""Golden-query harness for ADR 0013 RDF-semantic Neo4j model.

Validates by comparing **answer sets** from structured helper plans against
frozen gold — not SPARQL↔Cypher translation.

Run::

    pytest tests/test_golden_rdf_semantics.py -q
    python -m infona_client.graph.golden_neo4j
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infona_client.graph.assertion_model import type_membership_property_id
from infona_client.graph.golden_fixture import build_mini_people
from infona_client.graph.golden_neo4j import (
    DEFAULT_SUITE,
    CaseResult,
    compare_answers,
    execute_structured,
    format_report,
    load_suite,
    run_case,
    run_suite,
)
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph import rdfs_helpers as H


FIXTURE_DIR = DEFAULT_SUITE.parent


@pytest.fixture(scope="module")
def mini():
    return build_mini_people()


def test_suite_file_exists():
    assert DEFAULT_SUITE.is_file()
    suite = load_suite()
    assert suite["fixture"] == "mini_people"
    assert len(suite["queries"]) >= 8


def test_mini_people_counts(mini):
    tid, kg = mini.tenant_id, mini.kg
    exact = H.count_entities_of_type(
        mini.store, "Person", tenant_id=tid, kg=kg, include_subclasses=False
    )
    with_sub = H.count_entities_of_type(
        mini.store, "Person", tenant_id=tid, kg=kg, include_subclasses=True
    )
    assert exact == [{"count": 2}]
    assert with_sub == [{"count": 3}]


def test_mirror_entities_on_memory_store(mini):
    """Fixture dual-writes Entity MERGEs into MemoryGraphStore."""
    assert mini.mirror is not None
    assert mini.mirror.entity_count(tenant_id=mini.tenant_id, kg=mini.kg) >= 4


def test_type_membership_property_is_iri_base():
    pid = type_membership_property_id()
    assert pid.endswith("/properties/rdf_type")
    assert pid.startswith("http")


def test_entity_ids_use_shared_mint(mini):
    assert mini.ids.entities["Alice"] == entity_uri("Person", "Alice")
    assert mini.ids.entities["Dana"] == entity_uri("Employee", "Dana")


def test_compare_answers_set_equality():
    ok, msg = compare_answers(
        [{"a": 1}, {"a": 2}],
        [{"a": 2}, {"a": 1}],
        columns=["a"],
    )
    assert ok, msg
    ok2, _ = compare_answers([{"a": 1}], [{"a": 2}], columns=["a"])
    assert not ok2


def test_isolation_sibling_kg_empty(mini):
    rows = execute_structured(
        mini.store,
        mini.ids,
        {
            "helper": "assertions_for_subject",
            "subject_name": "Alice",
            "property": "birth_year",
            "project": "literal_value",
            "scope_kg": "$sibling_kg",
        },
    )
    assert rows == []


@pytest.mark.parametrize(
    "case",
    load_suite()["queries"],
    ids=lambda c: c["id"],
)
def test_golden_case(case, mini):
    result = run_case(case, mini, FIXTURE_DIR)
    assert result.status == "PASS", (
        f"{result.id}: {result.message}\n"
        f"  actual={result.actual!r}\n"
        f"  expected={result.expected!r}"
    )


def test_full_suite_all_pass():
    results = run_suite()
    report = format_report(results)
    fails = [r for r in results if r.status != "PASS"]
    assert not fails, report
    assert all(isinstance(r, CaseResult) for r in results)
    assert len(results) >= 8


def test_no_sparql_in_helpers_source():
    """Harness must not couple to SPARQL text (plan §2.2)."""
    helpers = Path(__file__).resolve().parents[1] / "infona_client" / "graph" / "rdfs_helpers.py"
    text = helpers.read_text(encoding="utf-8")
    # Executable SPARQL surface — not docstring mentions that forbid it.
    assert "SELECT ?" not in text
    assert "PREFIX " not in text
    assert "FROM <" not in text
    assert "INSERT DATA" not in text
