"""INF-565 — export a live KG to a schema-valid Blueprint.

Clinical Trials seed produces a document the INF-563 validator accepts.
A workspace deliberately seeded with all five INF-564 workspace-side
categories still exports, and none of those categories appear in the
package. Unclassifiable leaves fail closed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from infona_client.blueprint import (
    ExportRedactionError,
    export_blueprint,
    load_blueprint_package,
    validate_blueprint,
    write_blueprint_package,
)
from infona_client.blueprint.redact import (
    WORKSPACE_SIDE_CATEGORIES,
    scan_text_for_workspace_leak,
)
from infona_client.enrichment.models import JobCategory
from infona_client.functions.store import StoredFunction, make_function_store, reset_function_store
from infona_client.graph.iri import ATTR_META_NS, IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import get_graph_store
from infona_client.scheduling.models import Schedule
from infona_client.scheduling.store import make_schedule_store, reset_schedule_store
from infona_client.skills.models import TypeSkill
from infona_client.skills.store import make_type_skill_store, reset_type_skill_store

TENANT = "test-tenant"
KG = "clinical-trials"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Markers planted in the five workspace-side categories (INF-564).
SEEDED_RECORD_NCT = "NCT99999999"
SEEDED_SECRET = "sk-live-workspace-secret-001"
SEEDED_FN_SECRET = "sk-live-fn-not-for-export"
SEEDED_INTERNAL_URL = "https://internal.example/secret-doc"
SEEDED_LAST_RUN = "2026-01-15T12:00:00"
SEEDED_LAST_REFRESH = "2026-08-01T00:00:00Z"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def store():
    return get_graph_store()


@pytest.fixture(autouse=True)
def _reset_sidecar_stores():
    reset_type_skill_store()
    reset_function_store()
    reset_schedule_store()
    yield
    reset_type_skill_store()
    reset_function_store()
    reset_schedule_store()


def _seed_clinical_trials_ontology(store) -> None:
    _run(
        upsert_type(
            name="ClinicalTrial",
            description="A registered interventional or observational study.",
            tenant_id=TENANT,
            store=store,
        )
    )
    _run(
        upsert_type(
            name="Organization",
            description="A sponsor or collaborator.",
            tenant_id=TENANT,
            store=store,
        )
    )
    literals = [
        ("ClinicalTrial", "nct_id", "string", "ClinicalTrials.gov identifier"),
        ("ClinicalTrial", "official_title", "string", ""),
        ("ClinicalTrial", "overall_status", "string", ""),
        ("ClinicalTrial", "phase", "string", ""),
        ("ClinicalTrial", "enrollment", "integer", ""),
        ("Organization", "org_name", "string", ""),
        ("Organization", "org_class", "string", ""),
    ]
    for domain, name, datatype, desc in literals:
        _run(
            upsert_attribute(
                type_name=domain,
                attr_name=name,
                datatype=datatype,
                description=desc,
                tenant_id=TENANT,
                store=store,
                cardinality="1:1",
            )
        )
    _run(
        upsert_attribute(
            type_name="ClinicalTrial",
            attr_name="lead_sponsor",
            datatype="Organization",
            description="The owner you attribute by default.",
            tenant_id=TENANT,
            store=store,
            cardinality="N:1",
        )
    )


def _seed_workspace_side_categories(store) -> None:
    """Plant all five INF-564 workspace-side categories in the live KG."""
    trial = entity_uri("ClinicalTrial", SEEDED_RECORD_NCT)
    org = entity_uri("Organization", "LeakedOrg")
    graph = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
    _run(
        insert_facts(
            None,
            graph,
            [
                (trial, RDF_TYPE, f"{IRI_BASE}/types/ClinicalTrial"),
                (trial, LABEL, "Leaked Trial"),
                (trial, f"{IRI_BASE}/onto/nct_id", SEEDED_RECORD_NCT),
                (trial, f"{IRI_BASE}/onto/overall_status", "RECRUITING"),
                (
                    trial,
                    f"{ATTR_META_NS}ClinicalTrial/nct_id/source_url",
                    SEEDED_INTERNAL_URL,
                ),
                (
                    trial,
                    f"{IRI_BASE}/onto/nct_id_provenance",
                    "workspace-citation",
                ),
                (trial, f"{IRI_BASE}/onto/last_refresh", SEEDED_LAST_REFRESH),
                (org, RDF_TYPE, f"{IRI_BASE}/types/Organization"),
                (org, LABEL, "Leaked Org"),
                (org, f"{IRI_BASE}/onto/org_name", "Leaked Org"),
            ],
            store=store,
        )
    )
    _run(
        make_schedule_store().create(
            Schedule(
                id="sched-leaked",
                tenant_id=TENANT,
                kg_name=KG,
                category=JobCategory.enrichment,
                action="enrich",
                params={"secret_ref": SEEDED_SECRET},
                interval_seconds=7 * 24 * 3600,
                last_run=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
                next_run=datetime(2026, 1, 22, 12, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
    )
    _run(
        make_type_skill_store().upsert(
            TypeSkill(
                slug="cite-nct",
                type_name="ClinicalTrial",
                body="Always cite the NCT ID. Treat overall_status as time-sensitive.",
                title="Cite the NCT",
                summary="Always cite the NCT ID.",
                tenant_id=TENANT,
            )
        )
    )
    _run(
        make_function_store().upsert(
            StoredFunction(
                tenant_id=TENANT,
                name="is_recruiting",
                entity_type="ClinicalTrial",
                endpoint_url=f"https://internal.example/fn?api_key={SEEDED_FN_SECRET}",
                description="True when overall_status is a recruiting state.",
            )
        )
    )


def test_export_clinical_trials_is_schema_valid(store, tmp_path: Path):
    _seed_clinical_trials_ontology(store)
    _seed_workspace_side_categories(store)

    result = _run(export_blueprint(tenant_id=TENANT, kg=KG, store=store))
    errors = validate_blueprint(result.manifest)
    assert errors == [], errors
    assert result.manifest.id == "infona/clinical-trials"
    assert {c.name for c in result.manifest.concepts} == {
        "ClinicalTrial",
        "Organization",
    }
    assert any(s.id == "clinicaltrials_gov" for s in result.manifest.sources)
    assert result.manifest.sources[0].credential == "none"
    assert not result.manifest.sources[0].key_env
    assert any(r.name == "lead_sponsor" for r in result.manifest.relationships)
    assert any(t.id == "enrich" for t in result.manifest.tasks)
    assert "blueprint.yaml" in result.files

    dest = write_blueprint_package(result.manifest, tmp_path / "clinical-trials")
    loaded = load_blueprint_package(dest)
    assert loaded == result.manifest
    assert validate_blueprint(loaded) == []


def test_redaction_strips_all_five_workspace_side_categories(store):
    assert WORKSPACE_SIDE_CATEGORIES == {
        "records",
        "credentials",
        "scheduled_jobs",
        "citations_provenance",
        "freshness_status",
    }
    _seed_clinical_trials_ontology(store)
    _seed_workspace_side_categories(store)

    result = _run(export_blueprint(tenant_id=TENANT, kg=KG, store=store))
    dumped = result.files["blueprint.yaml"]
    payload = result.manifest.model_dump(mode="json", exclude_none=True)

    banned = [
        SEEDED_RECORD_NCT,
        SEEDED_SECRET,
        SEEDED_FN_SECRET,
        SEEDED_INTERNAL_URL,
        SEEDED_LAST_RUN,
        SEEDED_LAST_REFRESH,
        "workspace-citation",
        "Leaked Trial",
        "Leaked Org",
        "endpoint_url",
        "secret_ref",
        "last_run",
        "next_run",
        "attr_meta",
    ]
    leaks = scan_text_for_workspace_leak(dumped, banned_markers=banned)
    assert leaks == [], leaks
    for marker in banned:
        assert marker not in dumped, marker
        assert marker not in str(payload), marker

    # Cadence *policy* is Blueprint-side; the scheduled job is not.
    assert any(p.cadence == "weekly" for p in result.manifest.freshness.policies)
    assert "is_recruiting" in {f.name for f in result.manifest.functions}
    assert "cite-nct" in {s.slug for s in result.manifest.skills}


def test_unclassifiable_provenance_attribute_fails_closed(store):
    _seed_clinical_trials_ontology(store)
    _run(
        upsert_attribute(
            type_name="ClinicalTrial",
            attr_name="source_url",
            datatype="string",
            tenant_id=TENANT,
            store=store,
        )
    )
    with pytest.raises(ExportRedactionError, match="workspace-only"):
        _run(export_blueprint(tenant_id=TENANT, kg=KG, store=store))


def test_empty_catalog_fails_closed(store):
    with pytest.raises(ExportRedactionError, match="no ontology slice"):
        _run(export_blueprint(tenant_id=TENANT, kg=KG, store=store))


def test_export_route_returns_valid_package(store, client: TestClient, auth_headers):
    _seed_clinical_trials_ontology(store)
    _seed_workspace_side_categories(store)

    res = client.post(
        f"/graphs/{TENANT}/kgs/{KG}/blueprint/export",
        headers=auth_headers,
        json={},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kg"] == KG
    assert "blueprint.yaml" in body["files"]
    assert validate_blueprint(body["manifest"]) == []

    check = client.post(
        f"/graphs/{TENANT}/blueprint/validate",
        headers=auth_headers,
        json={"manifest": body["manifest"]},
    )
    assert check.status_code == 200, check.text
    assert check.json()["errors"] == []


def test_export_route_does_not_collide_with_instance_export():
    """Instance dump stays on GET …/export; Blueprint is a different route."""
    from infona_client.api.routes.export import router as instance_router
    from infona_client.api.routes.blueprint import export_router

    instance_paths = {getattr(r, "path", "") for r in instance_router.routes}
    blueprint_paths = {getattr(r, "path", "") for r in export_router.routes}
    assert "/{kg_name}/export" in instance_paths or any(
        "export" in p and "blueprint" not in p for p in instance_paths
    )
    assert any("blueprint/export" in p for p in blueprint_paths)
