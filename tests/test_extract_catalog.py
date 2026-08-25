"""Connector catalog invariants (ONTA-555).

The catalog is prefill data for the SAME generic REST/SQL extract, so every
template must be constructible as a real :class:`DltSourceSpec` and must obey
the rules the extract route already enforces (https only, relative resource
paths, BYOK credentials). These tests are what makes "adding a connector is a
data-only change" true rather than aspirational.
"""

from __future__ import annotations

import pytest

from infona_client.ingestion.catalog import (
    get_connector,
    list_connectors,
    unresolved_placeholders,
)
from infona_client.ingestion.dlt_source import validate_extract_target
from infona_client.ingestion.models import DltAuthSpec, DltSourceSpec

CONNECTORS = list_connectors()


def test_catalog_is_not_empty_and_ids_are_unique():
    ids = [c.id for c in CONNECTORS]
    assert len(ids) > 10, "the gallery should read as a catalog, not a stub"
    assert len(ids) == len(set(ids)), f"duplicate connector ids: {ids}"
    assert {"custom_rest", "custom_sql"} <= set(ids), "the blank tiles must exist"


def test_every_template_carries_no_credential():
    """A template names the credential to paste — it never ships one."""
    for c in CONNECTORS:
        blob = c.model_dump_json()
        assert '"token"' not in blob, f"{c.id} looks like it carries a token field"
        for header in c.headers:
            assert header.lower() not in {
                "authorization",
                "cookie",
                "x-api-key",
                "x-auth-token",
            }, f"{c.id} pins a credential header"
        if c.auth.type == "api_key":
            assert c.auth.api_key_header, f"{c.id} is api_key auth with no header name"


@pytest.mark.parametrize("template", CONNECTORS, ids=lambda t: t.id)
def test_template_builds_a_valid_source_spec(template):
    """Substituting the example values yields a spec the extract route accepts."""
    base_url = template.example_base_url()
    resources = [r.path for r in template.resources] or ["placeholder_resource"]
    spec = DltSourceSpec(
        kind=template.kind,
        base_url=base_url or ("https://api.example.com" if template.kind == "rest_api" else None),
        dsn="postgresql://user@db.example.com/app" if template.kind == "sql" else None,
        auth=DltAuthSpec(
            type=template.auth.type,
            secret_ref="token",
            api_key_header=template.auth.api_key_header,
        ),
        resources=resources,
        headers=dict(template.headers),
    )
    # The same SSRF / relative-path checks the run path applies.
    validate_extract_target(spec)


@pytest.mark.parametrize("template", CONNECTORS, ids=lambda t: t.id)
def test_rest_templates_are_https_and_declare_their_placeholders(template):
    if template.kind != "rest_api" or not template.base_url:
        return
    assert template.base_url.startswith("https://"), f"{template.id} is not https"
    declared = {p.key for p in template.placeholders}
    used = set(unresolved_placeholders(template.base_url))
    assert used == declared, (
        f"{template.id}: base_url uses {sorted(used)} but declares {sorted(declared)}"
    )
    for p in template.placeholders:
        assert p.example, f"{template.id}: placeholder {p.key} has no example"


@pytest.mark.parametrize("template", CONNECTORS, ids=lambda t: t.id)
def test_resources_name_an_ontology_type(template):
    for r in template.resources:
        assert r.suggested_type and r.suggested_type[0].isupper(), (
            f"{template.id}/{r.path}: suggested_type should be an ontology type name"
        )
        assert r.id_field, f"{template.id}/{r.path}: id_field is required"
        assert "://" not in r.path, f"{template.id}/{r.path}: paths must be relative"


def test_get_connector_is_case_insensitive_and_misses_cleanly():
    assert get_connector("HubSpot") is not None
    assert get_connector("nope-not-real") is None


def test_unresolved_placeholders_reports_unfilled_slots():
    assert unresolved_placeholders("https://{store}.myshopify.com") == ["store"]
    assert unresolved_placeholders("https://acme.myshopify.com") == []
    assert unresolved_placeholders(None) == []


# --- route --------------------------------------------------------------------


def test_catalog_route_serves_the_same_list(client, auth_headers):
    """One canonical route — every client picks from the identical catalog."""
    resp = client.get("/graphs/test-tenant/extract-sources/catalog", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["id"] for c in body] == [c.id for c in CONNECTORS]
    assert "hubspot" in {c["id"] for c in body}


def test_catalog_is_not_shadowed_by_a_source_named_catalog(client, auth_headers):
    """``/catalog`` sits beside ``/{slug}``; the literal route must win."""
    from infona_client.ingestion.extract_source_store import reset_extract_source_store

    reset_extract_source_store()
    resp = client.post(
        "/graphs/test-tenant/extract-sources",
        json={
            "slug": "catalog",
            "source": {
                "kind": "rest_api",
                "base_url": "https://api.example.com",
                "resources": ["v1/things"],
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "reserved" in resp.text
    assert (
        client.get(
            "/graphs/test-tenant/extract-sources/catalog", headers=auth_headers
        ).status_code
        == 200
    )


def test_unfilled_placeholder_is_rejected_with_the_slot_name(client, auth_headers):
    """Saving https://{store}.myshopify.com would fail later as an opaque DNS error."""
    from infona_client.ingestion.extract_source_store import reset_extract_source_store

    reset_extract_source_store()
    resp = client.post(
        "/graphs/test-tenant/extract-sources",
        json={
            "slug": "shop",
            "source": {
                "kind": "rest_api",
                "base_url": "https://{store}.myshopify.com/admin/api/2024-10",
                "resources": ["orders.json"],
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "store" in resp.text
