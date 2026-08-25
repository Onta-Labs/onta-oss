"""ONTA-543: synthetic messy-suppliers fixture proves merge + conflict policy.

Hermetic MemoryGraphStore ingest of ``examples/suppliers-messy.csv`` (same
style as ``tests/resolver/er/test_rebuild.py``). Asserts:

* a merge happened (Acme 3→1, Globex 2→1)
* winner + reason + provenance of the surviving value are present
* one field conflict (credit_rating) stays unresolved / flagged

The CSV is synthetic. No real customer data. No spider-bench leakage.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.graph.iri import TYPE_URI_PREFIX
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.ontology_queries import attr_uri, entity_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.pipeline.conflict import REASON_AUTHORITY, REASON_VALUE, FactClaim, resolve
from infona_client.graph.validity import fetch_current_object_terms
from infona_client.pipeline.mutations import write_with_conflict_resolution
from infona_client.resolver.er.rebuild import rebuild_type
from infona_client.resolver.er.rebuild_explain import format_rebuild_report
from infona_client.resolver.er.types import DEFAULT_ORG_CONFIG

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "examples" / "suppliers-messy.csv"
FIXTURE_DOC = REPO / "examples" / "suppliers-messy.md"
FRAGMENT = REPO / "docs" / "_fragments" / "ONTA-543.md"
GOLDEN = REPO / "examples" / "suppliers-messy.er-rebuild.txt"

TENANT = "demo-tenant"
KG = "suppliers"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
SUPPLIER_TYPE_URI = f"{TYPE_URI_PREFIX}Supplier"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

ACME_WINNER = entity_uri("Supplier", "ERP-1001")
GLOBEX_WINNER = entity_uri("Supplier", "ERP-2001")
HQ_PRED = attr_uri("Supplier", "headquarters")
CREDIT_PRED = attr_uri("Supplier", "credit_rating")


def _rows() -> list[dict[str, str]]:
    assert FIXTURE.is_file(), f"missing fixture: {FIXTURE}"
    with FIXTURE.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_fixture_is_synthetic_and_has_the_intended_mess() -> None:
    rows = _rows()
    assert len(rows) == 6
    names = [r["supplier_name"] for r in rows]
    assert names.count("Acme Corp") == 1
    assert "ACME Corporation" in names
    assert "Acme" in names
    acme = [r for r in rows if "acme" in r["supplier_name"].lower()]
    assert len(acme) == 3
    assert {r["headquarters"] for r in acme} == {"Austin", "San Francisco"}
    assert {r["credit_rating"] for r in acme} == {"A", "BBB"}
    assert {r["record_source"] for r in acme} == {"erp", "crm", "directory"}
    assert {r["tax_id"] for r in acme} == {"12-3456789"}

    doc = FIXTURE_DOC.read_text(encoding="utf-8").lower()
    fragment = FRAGMENT.read_text(encoding="utf-8").lower()
    for text in (doc, fragment):
        assert "synthetic" in text
        assert "spider-bench" not in text or "no spider-bench" in text
        assert "customer" in text  # "no real customer data"
    # No live customer / eval-set identifiers in the CSV cells.
    blob = FIXTURE.read_text(encoding="utf-8").lower()
    assert "spider" not in blob
    assert "patient" not in blob


def test_fragment_is_the_third_quickstart_command() -> None:
    text = FRAGMENT.read_text(encoding="utf-8")
    assert "infona ingest examples/suppliers-messy.csv --kg suppliers" in text
    assert "infona er rebuild --kg suppliers" in text
    assert "unresolved  credit_rating" in text
    assert "winner:" in text
    assert "reason:" in text
    assert "provenance:" in text


async def _seed_row(row: dict[str, str]) -> str:
    from infona_client.resolver.er.blocking import SparqlBlocker, generate_block_keys
    from infona_client.resolver.er.normalize import DefaultNormalizer
    from infona_client.resolver.er.types import EntitySignals

    uri = entity_uri("Supplier", row["record_id"])
    normalized = DefaultNormalizer().normalize(
        EntitySignals(
            name=row["supplier_name"] or None,
            email=row["email"] or None,
            phone=row["phone"] or None,
        )
    )
    extras = [
        (uri, attr_uri("Supplier", key), row[col])
        for key, col in (
            ("headquarters", "headquarters"),
            ("credit_rating", "credit_rating"),
            ("record_source", "record_source"),
            ("authority", "authority"),
            ("observed_at", "observed_at"),
            ("tax_id", "tax_id"),
            ("record_id", "record_id"),
        )
        if row.get(col)
    ]
    triples = [
        (uri, RDF_TYPE, SUPPLIER_TYPE_URI),
        (uri, RDFS_LABEL, row["supplier_name"]),
        *extras,
        *SparqlBlocker.index_triples(uri, normalized, generate_block_keys(normalized)),
    ]
    await insert_facts(None, INSTANCE_GRAPH, triples)
    return uri


async def _supplier_ids() -> list[str]:
    from infona_client.graph.explore_store import list_entities_by_type

    page = await list_entities_by_type(
        tenant_id=TENANT, kg=KG, type_name="Supplier"
    )
    return sorted(e.id for e in page.entities)


@pytest.mark.asyncio
async def test_ingest_rebuild_merge_winner_and_unresolved_conflict() -> None:
    rows = _rows()
    for row in rows:
        await _seed_row(row)
    assert len(await _supplier_ids()) == 6

    report = await rebuild_type(
        None, INSTANCE_GRAPH, "Supplier", SUPPLIER_TYPE_URI, DEFAULT_ORG_CONFIG,
    )
    assert report["entities_before"] == 6
    assert report["entities_after"] == 3
    assert report["clusters_merged"] == 2
    assert report["fragments_absorbed"] == 3

    ids = await _supplier_ids()
    assert ACME_WINNER in ids
    assert GLOBEX_WINNER in ids
    assert entity_uri("Supplier", "ERP-3001") in ids
    assert entity_uri("Supplier", "CRM-4402") not in ids
    assert entity_uri("Supplier", "DIR-8891") not in ids
    assert entity_uri("Supplier", "CRM-5503") not in ids

    merges = {m["winner"]: m for m in report["merges"]}
    acme = merges[ACME_WINNER]
    assert set(acme["losers"]) == {
        entity_uri("Supplier", "CRM-4402"),
        entity_uri("Supplier", "DIR-8891"),
    }
    assert acme["reason"] == "signal-richest"
    assert acme["score"] >= DEFAULT_ORG_CONFIG.auto_merge_threshold
    prov = acme["provenance"]
    assert prov["source"] == "erp"
    assert prov["authority"] == "source_of_truth"
    assert prov["observed_at"].startswith("2026-03-01")

    hq = next(c for c in report["conflicts"] if c["field"] == "headquarters")
    assert hq["entity"] == ACME_WINNER
    assert hq["winner"]["value"] == "Austin"
    assert hq["winner"]["source"] == "erp"
    assert hq["reason"] == REASON_AUTHORITY
    assert hq["loser"]["value"] == "San Francisco"

    leftover = report["unresolved"]
    assert len(leftover) == 1
    credit = leftover[0]
    assert credit["field"] == "credit_rating"
    assert credit["entity"] == ACME_WINNER
    values = {v["value"] for v in credit["values"]}
    assert values == {"A", "BBB"}
    assert "not silently guessed" in credit["flagged"]

    from infona_client.graph.store import get_graph_store

    def _lits(pred: str) -> set:
        leaf = pred.rstrip("/").rsplit("/", 1)[-1]
        return {
            a.get("literal_value")
            for a in get_graph_store().snapshot_assertions()
            if a.get("subject_id") == ACME_WINNER
            and str(a.get("property_id") or "").endswith(leaf)
        }

    assert _lits(HQ_PRED) >= {"Austin", "San Francisco"}
    assert set(await fetch_current_object_terms(None, INSTANCE_GRAPH, ACME_WINNER, HQ_PRED)) == {
        "Austin"
    }
    assert _lits(CREDIT_PRED) >= {"A", "BBB"}
    assert set(
        await fetch_current_object_terms(None, INSTANCE_GRAPH, ACME_WINNER, CREDIT_PRED)
    ) == {"A", "BBB"}

    kg_report = {
        "types": [report],
        "fragments_absorbed_total": report["fragments_absorbed"],
        "merges": report["merges"],
        "conflicts": report["conflicts"],
        "unresolved": report["unresolved"],
    }
    rendered = format_rebuild_report(kg_report, KG)
    assert GOLDEN.is_file(), f"missing golden: {GOLDEN}"
    assert rendered == GOLDEN.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_headquarters_write_receipt_carries_winner_reason_provenance() -> None:
    """The same HQ disagreement, through the converged conflict writer."""
    rows = _rows()
    for row in rows:
        await _seed_row(row)
    await rebuild_type(
        None, INSTANCE_GRAPH, "Supplier", SUPPLIER_TYPE_URI, DEFAULT_ORG_CONFIG,
    )

    existing = FactClaim(
        value="Austin",
        authority=AuthorityLevel.source_of_truth,
        source="erp",
        observed_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    incoming = FactClaim(
        value="San Francisco",
        authority=AuthorityLevel.supplementary,
        source="directory",
        observed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    # Pure policy (no store): authority decides, value tiebreak is not used.
    decision = resolve(existing, incoming)
    assert decision.conflict is True
    assert decision.winner.value == "Austin"
    assert decision.reason == REASON_AUTHORITY

    receipt = await write_with_conflict_resolution(
        None,
        INSTANCE_GRAPH,
        subject=ACME_WINNER,
        predicate=HQ_PRED,
        type_name="Supplier",
        value=incoming.value,
        authority=incoming.authority,
        source=incoming.source,
        observed_at=incoming.observed_at,
        existing_claims=[existing],
        run_id="onta-543-hq",
    )
    assert receipt.conflict is True
    assert receipt.reason == REASON_AUTHORITY
    assert receipt.winner == (ACME_WINNER, HQ_PRED, "Austin")
    assert receipt.loser == (ACME_WINNER, HQ_PRED, "San Francisco")

    # Both instance values remain stored; only Austin is current.
    from infona_client.graph.store import get_graph_store

    hq_vals = {
        a.get("literal_value")
        for a in get_graph_store().snapshot_assertions()
        if a.get("subject_id") == ACME_WINNER
        and str(a.get("property_id") or "").endswith("headquarters")
    }
    assert hq_vals >= {"Austin", "San Francisco"}
    assert set(await fetch_current_object_terms(None, INSTANCE_GRAPH, ACME_WINNER, HQ_PRED)) == {
        "Austin"
    }


def test_credit_rating_would_be_a_lexical_guess() -> None:
    """Equal-trust A vs BBB: the leftover axis is value — we refuse that guess."""
    erp = FactClaim(
        "A",
        authority=AuthorityLevel.source_of_truth,
        source="erp",
        observed_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    crm = FactClaim(
        "BBB",
        authority=AuthorityLevel.source_of_truth,
        source="crm",
        observed_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    d = resolve(erp, crm)
    assert d.conflict is True
    assert d.reason == REASON_VALUE
    assert d.winner.value in {"A", "BBB"}
