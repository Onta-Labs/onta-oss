"""lead_sponsor (and other org leaves) must be Company relationships, not strings.

Regression: job c7c2c7d2 wrote Hoffmann-La Roche as a literal column. Explorer
showed a string attribute. The instance edge must be onto/lead_sponsor → a
Company node (same mint as ingest), and the catalog range must be types/Company.
"""

from infona_client.enrichment.executor import (
    EnrichmentExecutor,
    _infer_relationship_target,
)
from infona_client.graph.iri import IRI_BASE


def test_infer_lead_sponsor_mints_company_when_no_types():
    assert _infer_relationship_target("lead_sponsor") == "Company"
    assert _infer_relationship_target("lead_sponsor", []) == "Company"


def test_infer_lead_sponsor_prefers_existing_organization():
    assert (
        _infer_relationship_target("lead_sponsor", ["ClinicalTrial", "Organization"])
        == "Organization"
    )


def test_infer_does_not_touch_status_or_phase():
    types = ["ClinicalTrial", "Company"]
    assert _infer_relationship_target("status", types) is None
    assert _infer_relationship_target("phase", types) is None
    assert _infer_relationship_target("nct_id", types) is None


def test_infer_exact_type_name_match():
    assert _infer_relationship_target("company", ["Company"]) == "Company"
    assert _infer_relationship_target("Company", ["Company"]) == "Company"


def test_instance_triples_mint_company_node_and_onto_edge():
    triples = EnrichmentExecutor._instance_triples_for_value(
        f"{IRI_BASE}/entities/ClinicalTrial/NCT04660344",
        "ClinicalTrial",
        "lead_sponsor",
        "Hoffmann-La Roche",
        "Company",
    )
    preds = {p for _, p, _ in triples}
    assert f"{IRI_BASE}/onto/lead_sponsor" in preds
    assert any(
        o.endswith("/entities/Company/Hoffmann-La_Roche")
        or "/entities/Company/" in o
        for _, _, o in triples
    )
    # Must NOT write the literal on attrs/lead_sponsor.
    assert not any("/attrs/lead_sponsor" in p for _, p, _ in triples)
