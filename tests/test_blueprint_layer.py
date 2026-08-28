"""INF-578 — private overlay survives a non-clobbering upstream update."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from infona_client.blueprint import (
    extend_blueprint,
    fork_blueprint,
    inspect_blueprint,
    install_blueprint,
    uninstall_blueprint,
    update_blueprint,
)
from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.install import BlueprintNotInstalled, manifest_content_hash
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.graph.store import get_graph_store
from infona_client.blueprint.models import parse_blueprint
from infona_client.blueprint.overlay import (
    BlueprintIdMismatch,
    OverlayDocument,
    detect_conflicts,
    reset_blueprint_overlay_store,
)
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.graph.ontology_catalog import list_attributes, list_types
from infona_client.skills.store import make_type_skill_store, reset_type_skill_store

TENANT = "bp-layer-tenant"
PEER = "bp-layer-peer"
KG = "layer-kg"
MIN = Path(__file__).resolve().parents[1] / "infona_client/blueprint/data/clinical_trials_min.json"

EXTEND_TRIAL = {
    "concepts": [
        {
            "name": "ClinicalTrial",
            "attributes": [
                {
                    "name": "internal_priority",
                    "kind": "literal",
                    "datatype": "string",
                    "optional": True,
                }
            ],
        },
        {
            "name": "SiteMonitor",
            "label": "Site monitor",
            "identity": ["monitor_id"],
            "attributes": [
                {"name": "monitor_id", "kind": "literal", "datatype": "string"},
                {
                    "name": "display_name",
                    "kind": "literal",
                    "datatype": "string",
                    "optional": True,
                },
            ],
        },
    ],
    "sources": [{"id": "ctgov", "declared_cadence": "daily"}],
    "skills": [
        {
            "slug": "internal-priority",
            "type_name": "ClinicalTrial",
            "body": "Prefer the workspace internal_priority when ranking.",
            "title": "Internal priority",
        }
    ],
}


@pytest.fixture(autouse=True)
def _reset():
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()
    yield
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()


def _min(**patch) -> dict:
    data = json.loads(MIN.read_text(encoding="utf-8"))
    data.update(patch)
    return data


def _drop_organization(data: dict) -> dict:
    out = copy.deepcopy(data)
    out["concepts"] = [c for c in out["concepts"] if c["name"] != "Organization"]
    out["relationships"] = []
    trial = next(c for c in out["concepts"] if c["name"] == "ClinicalTrial")
    trial["attributes"] = [
        a for a in trial["attributes"] if a["name"] != "lead_sponsor"
    ]
    out["freshness"]["er"] = [
        e for e in out["freshness"]["er"] if e["type_name"] != "Organization"
    ]
    for source in out["sources"]:
        source["mappings"] = [
            m
            for m in source["mappings"]
            if "lead_sponsor" not in m.get("lands_on", "")
        ]
    return out


async def _type_names(tenant_id: str = TENANT) -> set[str]:
    return {t.name for t in await list_types(tenant_id=tenant_id)}


async def _attr_names(type_name: str, tenant_id: str = TENANT) -> set[str]:
    return {
        a.name
        for a in await list_attributes(tenant_id=tenant_id, type_name=type_name)
    }


@pytest.mark.asyncio
async def test_private_extension_survives_additive_upstream_update():
    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    extended = await extend_blueprint(TENANT, base["id"], EXTEND_TRIAL)
    assert extended["status"] == "extended"
    assert "SiteMonitor" in await _type_names()
    assert "internal_priority" in await _attr_names("ClinicalTrial")

    nxt = _min(version="0.2.0")
    org = next(c for c in nxt["concepts"] if c["name"] == "Organization")
    org["attributes"].append(
        {
            "name": "duns",
            "kind": "literal",
            "datatype": "string",
            "optional": True,
        }
    )
    got = await update_blueprint(
        nxt, tenant_id=TENANT, blueprint_id=base["id"], include_sample=False
    )
    assert got["status"] == "updated"
    assert got["conflicts"] == []
    assert "SiteMonitor" in await _type_names()
    assert "internal_priority" in await _attr_names("ClinicalTrial")
    assert "duns" in await _attr_names("Organization")
    card = await inspect_blueprint(TENANT, base["id"])
    assert card["version"] == "0.2.0"
    assert card["overlay"]["sources"][0]["declared_cadence"] == "daily"
    assert card["conflicts"] == []
    slugs = {s.slug for s in await make_type_skill_store().list_for_tenant(TENANT)}
    assert "internal-priority" in slugs
    assert "cite-nct" in slugs


@pytest.mark.asyncio
async def test_removed_extended_type_is_reported_not_clobbered():
    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    await extend_blueprint(
        TENANT,
        base["id"],
        {
            "concepts": [
                {
                    "name": "Organization",
                    "attributes": [
                        {
                            "name": "internal_tier",
                            "kind": "literal",
                            "datatype": "string",
                            "optional": True,
                        }
                    ],
                }
            ]
        },
    )
    assert "internal_tier" in await _attr_names("Organization")

    nxt = _drop_organization(_min(version="0.2.0"))
    got = await update_blueprint(
        nxt, tenant_id=TENANT, blueprint_id=base["id"], include_sample=False
    )
    assert got["status"] == "updated_with_conflicts"
    kinds = {c["kind"] for c in got["conflicts"]}
    assert "removed_extended_type" in kinds
    assert "Organization" in await _type_names()
    assert "internal_tier" in await _attr_names("Organization")
    card = await inspect_blueprint(TENANT, base["id"])
    assert any(c["kind"] == "removed_extended_type" for c in card["conflicts"])


@pytest.mark.asyncio
async def test_narrowed_range_is_reported():
    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    await extend_blueprint(TENANT, base["id"], EXTEND_TRIAL)
    nxt = _min(version="0.2.0")
    trial = next(c for c in nxt["concepts"] if c["name"] == "ClinicalTrial")
    lead = next(a for a in trial["attributes"] if a["name"] == "lead_sponsor")
    lead["range_type"] = "ClinicalTrial"
    rel = next(r for r in nxt["relationships"] if r["name"] == "lead_sponsor")
    rel["target"] = "ClinicalTrial"
    got = await update_blueprint(
        nxt, tenant_id=TENANT, blueprint_id=base["id"], include_sample=False
    )
    assert any(c["kind"] == "narrowed_range" for c in got["conflicts"])
    assert "internal_priority" in await _attr_names("ClinicalTrial")
    assert "SiteMonitor" in await _type_names()


@pytest.mark.asyncio
async def test_source_override_conflict_keeps_the_overlay():
    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    await extend_blueprint(
        TENANT, base["id"], {"sources": [{"id": "ctgov", "declared_cadence": "daily"}]}
    )
    nxt = _min(version="0.2.0")
    nxt["sources"][0]["declared_cadence"] = "monthly"
    got = await update_blueprint(
        nxt, tenant_id=TENANT, blueprint_id=base["id"], include_sample=False
    )
    assert any(c["kind"] == "source_changed" for c in got["conflicts"])
    card = await inspect_blueprint(TENANT, base["id"])
    assert card["overlay"]["sources"][0]["declared_cadence"] == "daily"


@pytest.mark.asyncio
async def test_reinstall_of_new_version_also_preserves_overlay():
    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    await extend_blueprint(TENANT, base["id"], EXTEND_TRIAL)
    nxt = _min(version="0.2.0")
    again = await install_blueprint(nxt, tenant_id=TENANT, kg=KG, include_sample=False)
    assert again.status == "updated"
    assert "SiteMonitor" in await _type_names()
    card = await inspect_blueprint(TENANT, base["id"])
    assert card["overlay"] is not None


@pytest.mark.asyncio
async def test_overlay_survives_store_wrapper_reload():
    """extend + bounce + inspect still shows the private layer."""
    from infona_client.blueprint.overlay import (
        GraphStoreBlueprintOverlayStore,
        make_blueprint_overlay_store,
    )

    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    await extend_blueprint(TENANT, base["id"], EXTEND_TRIAL)
    assert isinstance(make_blueprint_overlay_store(), GraphStoreBlueprintOverlayStore)

    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    card = await inspect_blueprint(TENANT, base["id"])
    assert card["overlay"] is not None
    assert card["overlay"]["sources"][0]["declared_cadence"] == "daily"
    names = {c["name"] for c in card["overlay"]["concepts"]}
    assert "SiteMonitor" in names
    raw = await get_graph_store().blueprint_overlay_get(TENANT, base["id"])
    assert raw is not None
    assert raw["base_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_uninstall_removes_overlay_from_graph_store():
    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    await extend_blueprint(TENANT, base["id"], EXTEND_TRIAL)
    await uninstall_blueprint(TENANT, base["id"])
    reset_blueprint_overlay_store()
    assert await get_graph_store().blueprint_overlay_get(TENANT, base["id"]) is None
    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(TENANT, base["id"])


@pytest.mark.asyncio
async def test_overlay_is_tenant_confined_and_absent_from_fork():
    base = _min()
    await install_blueprint(base, tenant_id=TENANT, kg=KG, include_sample=False)
    await extend_blueprint(TENANT, base["id"], EXTEND_TRIAL)
    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(PEER, base["id"])
    forked = await fork_blueprint(TENANT, base["id"], as_id="acme/layer-demo")
    assert forked.manifest["id"] == "acme/layer-demo"
    assert "SiteMonitor" not in [c["name"] for c in forked.manifest["concepts"]]
    assert forked.manifest["sources"][0]["declared_cadence"] != "daily"


@pytest.mark.asyncio
async def test_update_refuses_id_mismatch_and_missing_pin():
    with pytest.raises(BlueprintNotInstalled):
        await update_blueprint(_min(), tenant_id=TENANT, blueprint_id="infona/clinical-trials")
    await install_blueprint(_min(), tenant_id=TENANT, kg=KG, include_sample=False)
    other = _min(id="acme/other", namespace="acme", name="Other")
    with pytest.raises(BlueprintIdMismatch):
        await update_blueprint(
            other, tenant_id=TENANT, blueprint_id="infona/clinical-trials"
        )


@pytest.mark.asyncio
async def test_seed_install_and_fork_still_pass():
    first = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg="ct")
    assert first.status == "installed"
    forked = await fork_blueprint(TENANT, "infona/clinical-trials", as_id="acme/ct")
    assert forked.parent_id == "infona/clinical-trials"
    assert manifest_content_hash(parse_blueprint(forked.manifest))


def test_detect_conflicts_is_a_pure_three_way():
    old = parse_blueprint(_min())
    new = parse_blueprint(_drop_organization(_min(version="0.2.0")))
    overlay = OverlayDocument.model_validate(
        {
            "concepts": [
                {
                    "name": "Organization",
                    "attributes": [
                        {
                            "name": "internal_tier",
                            "kind": "literal",
                            "datatype": "string",
                            "optional": True,
                        }
                    ],
                }
            ]
        }
    )
    kinds = {c.kind for c in detect_conflicts(old, new, overlay)}
    assert kinds == {"removed_extended_type"}
