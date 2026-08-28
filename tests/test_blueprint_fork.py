"""INF-579 — fork copies the package, records lineage, does not clobber."""

from __future__ import annotations

import pytest

from infona_client.blueprint import (
    fork_blueprint,
    inspect_blueprint,
    install_blueprint,
    load_blueprint_package,
)
from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.overlay import reset_blueprint_overlay_store
from infona_client.blueprint.fork import copy_as_fork, default_fork_id
from infona_client.blueprint.install import manifest_content_hash
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.blueprint.plan import (
    BlueprintForkConflict,
    BlueprintNotFound,
    BlueprintNotInstalled,
)
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.graph.ontology_catalog import list_types
from infona_client.skills.store import reset_type_skill_store

TENANT = "bp-fork-tenant"
PEER = "bp-fork-peer"
SEED_ID = "infona/clinical-trials"


@pytest.fixture(autouse=True)
def _reset_blueprint_state():
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()
    yield
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()


@pytest.mark.asyncio
async def test_fork_of_clinical_trials_seed_is_inspectable_with_lineage():
    result = await fork_blueprint(
        TENANT, SEED_ID, as_id="acme/clinical-trials"
    )
    assert result.status == "forked"
    assert result.blueprint_id == "acme/clinical-trials"
    assert result.parent_id == SEED_ID
    assert result.parent_version == "0.1.0"
    assert result.lineage["parent"] == {"id": SEED_ID, "version": "0.1.0"}
    assert result.lineage["chain"][0] == result.lineage["parent"]
    assert "Infona" in result.attribution
    assert result.manifest["id"] == "acme/clinical-trials"
    assert result.manifest["concepts"][0]["name"] == "ClinicalTrial"
    assert result.content_hash != manifest_content_hash(
        load_blueprint_package(CLINICAL_TRIALS)
    )

    card = await inspect_blueprint(TENANT, "acme/clinical-trials")
    assert card["blueprint_id"] == "acme/clinical-trials"
    assert card["lineage"]["parent"]["id"] == SEED_ID
    assert card["attribution"] == result.attribution
    assert card["sample_is_current"] is False
    assert card.get("installed") is False

    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(PEER, "acme/clinical-trials")


@pytest.mark.asyncio
async def test_fork_does_not_write_instance_or_schema():
    before = {t.name for t in await list_types(tenant_id=TENANT)}
    await fork_blueprint(TENANT, SEED_ID, as_id="acme/clinical-trials")
    after = {t.name for t in await list_types(tenant_id=TENANT)}
    assert after == before
    assert "ClinicalTrial" not in after


@pytest.mark.asyncio
async def test_fork_does_not_clobber_source_or_reuse_its_pin():
    parent = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg="ct")
    forked = await fork_blueprint(TENANT, SEED_ID, as_id="acme/clinical-trials")
    child = await install_blueprint(
        forked.manifest, tenant_id=TENANT, kg="fork-kg"
    )
    assert child.status == "installed"
    assert child.blueprint_id == "acme/clinical-trials"
    assert child.blueprint_id != parent.blueprint_id
    assert child.content_hash != parent.content_hash

    again = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg="ct")
    assert again.status == "already_installed"
    assert again.blueprint_id == SEED_ID

    parent_card = await inspect_blueprint(TENANT, SEED_ID)
    assert parent_card["blueprint_id"] == SEED_ID
    assert parent_card.get("lineage", {}).get("parent") in (None, {})

    with pytest.raises(BlueprintForkConflict):
        await fork_blueprint(TENANT, SEED_ID, as_id="acme/clinical-trials")


@pytest.mark.asyncio
async def test_fork_of_fork_keeps_the_chain():
    first = await fork_blueprint(TENANT, SEED_ID, as_id="acme/clinical-trials")
    second = await fork_blueprint(
        TENANT, first.blueprint_id, as_id="beta/clinical-trials"
    )
    assert second.lineage["parent"] == {
        "id": "acme/clinical-trials",
        "version": "0.1.0",
    }
    assert [e["id"] for e in second.lineage["chain"]] == [
        "acme/clinical-trials",
        SEED_ID,
    ]


@pytest.mark.asyncio
async def test_unknown_parent_is_404():
    await fork_blueprint(TENANT, SEED_ID, as_id="acme/clinical-trials")
    with pytest.raises(BlueprintNotFound):
        await fork_blueprint(TENANT, "no-such/package")
    with pytest.raises(BlueprintNotFound):
        await fork_blueprint(PEER, "acme/clinical-trials")


def test_default_fork_id_uses_tenant_namespace():
    assert default_fork_id("acme-tenant", SEED_ID) == "acme-tenant/clinical-trials"
    assert default_fork_id("infona", SEED_ID) == "infona/clinical-trials-fork"


def test_copy_keeps_attribution_and_concepts():
    from datetime import date

    parent = load_blueprint_package(CLINICAL_TRIALS)
    child = copy_as_fork(parent, "acme/clinical-trials", forked_at=date(2026, 8, 28))
    assert child.attribution == parent.attribution
    assert [c.name for c in child.concepts] == [c.name for c in parent.concepts]
    assert child.lineage.parent is not None
    assert child.lineage.parent.id == SEED_ID
    assert child.lineage.forked_at == date(2026, 8, 28)


def test_cli_fork_prints_lineage(capsys):
    from infona_client.blueprint.__main__ import main

    rc = main(
        ["fork", SEED_ID, "--tenant", TENANT, "--as", "acme/clinical-trials"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "acme/clinical-trials" in out
    assert SEED_ID in out
    assert "forked" in out
