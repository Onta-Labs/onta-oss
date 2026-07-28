"""Type-attached SKILLS — model, store, layer resolution, injection seam, routes.

A skill is type-attached PROSE consumed by an LM agent (distinct from a
FUNCTION, which is type-attached compute).
"""

import asyncio
import re
from unittest.mock import AsyncMock, patch

import pytest

from cograph_client.graph.layers import Layer, layer_type_uri
from cograph_client.skills import (
    InMemoryTypeSkillStore,
    TypeSkill,
    global_skills_for_type,
    make_type_skill_store,
    merge_layers,
    register_skill_layer,
    render_skills_block,
    reset_skill_layers,
    reset_type_skill_store,
    resolve_skills,
    skills_prompt_block,
    validate_skill,
)
from cograph_client.skills.registry import (
    global_skills_by_layer,
    load_skill_dir,
    parse_skill_markdown,
)
from cograph_client.skills.resolve import DEFAULT_PROMPT_BUDGET
from cograph_client.skills.store import PostgresTypeSkillStore


@pytest.fixture(autouse=True)
def _clean_skill_state():
    """Skills keep two process-wide singletons (the memoized store and the
    registered global layers). Reset both around every test so ordering can
    never make an assertion pass for the wrong reason."""
    reset_skill_layers()
    reset_type_skill_store()
    yield
    reset_skill_layers()
    reset_type_skill_store()


def _skill(slug="notes", type_name="Person", layer=Layer.TENANT, **kw):
    return TypeSkill(
        slug=slug,
        type_name=type_name,
        body=kw.pop("body", "Some guidance about this type."),
        layer=layer,
        tenant_id=kw.pop("tenant_id", "t1" if layer is Layer.TENANT else None),
        **kw,
    )


def _stack(entitled=False):
    from cograph_client.graph.layers import LayerStack

    return LayerStack(tenant_graph_uri="https://cograph.tech/graphs/t1", entitled=entitled)


# --------------------------------------------------------------------------- #
# Model + validation
# --------------------------------------------------------------------------- #
def test_valid_skill_has_no_errors():
    assert validate_skill(_skill()) == []


@pytest.mark.parametrize(
    "mutate, expect_fragment",
    [
        (lambda s: setattr(s, "slug", "Not A Slug"), "slug"),
        (lambda s: setattr(s, "slug", ""), "slug"),
        (lambda s: setattr(s, "type_name", "not/a/type"), "type_name"),
        (lambda s: setattr(s, "body", "   "), "body must not be empty"),
        (lambda s: setattr(s, "body", "x" * 20_001), "max 20000"),
        (lambda s: setattr(s, "title", "t" * 201), "title exceeds"),
        (lambda s: setattr(s, "summary", "s" * 501), "summary exceeds"),
        (lambda s: setattr(s, "tenant_id", None), "must carry a tenant_id"),
    ],
)
def test_validation_rejects_malformed_skills(mutate, expect_fragment):
    s = _skill()
    mutate(s)
    errors = validate_skill(s)
    assert errors, f"expected an error mentioning {expect_fragment!r}"
    assert any(expect_fragment in e for e in errors), errors


def test_global_layer_skill_must_not_carry_a_tenant_id():
    """Shared canon leaking a tenant id would make a workspace's private prose
    look curated — fail it at the model boundary."""
    s = _skill(layer=Layer.PUBLIC, tenant_id=None)
    assert validate_skill(s) == []
    s.tenant_id = "t1"
    assert any("must not carry a tenant_id" in e for e in validate_skill(s))


def test_type_uri_is_layer_qualified():
    """A Public Person and a Tenant Person are DIFFERENT types; a skill must
    attach to the layer-qualified URI, not a bare tenant-namespace one."""
    tenant = _skill(layer=Layer.TENANT)
    public = _skill(layer=Layer.PUBLIC, tenant_id=None)
    assert tenant.type_uri == layer_type_uri(Layer.TENANT, "Person")
    assert public.type_uri == layer_type_uri(Layer.PUBLIC, "Person")
    assert tenant.type_uri != public.type_uri


def test_round_trip_through_dict():
    s = _skill(title="T", summary="S", metadata={"k": "v"})
    back = TypeSkill.from_dict(s.to_dict())
    assert (back.slug, back.type_name, back.body, back.layer) == (
        s.slug, s.type_name, s.body, s.layer,
    )
    assert back.metadata == {"k": "v"}


def test_from_dict_degrades_unknown_layer_to_tenant():
    assert TypeSkill.from_dict({"slug": "a", "type_name": "P", "body": "b",
                                "layer": "bogus"}).layer is Layer.TENANT


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def test_store_round_trip_and_version_bump():
    store = InMemoryTypeSkillStore()

    async def go():
        first = await store.upsert(_skill(body="v1"))
        assert first.version == 1
        second = await store.upsert(_skill(body="v2"))
        assert second.version == 2, "re-upsert must bump the revision"
        assert second.body == "v2"
        assert second.created_at == first.created_at, "created_at must survive"
        got = await store.get("t1", "Person", "notes")
        assert got.body == "v2"
        assert [s.slug for s in await store.list_for_tenant("t1")] == ["notes"]
        assert await store.delete("t1", "Person", "notes") is True
        assert await store.delete("t1", "Person", "notes") is False
        assert await store.get("t1", "Person", "notes") is None

    asyncio.run(go())


def test_store_type_lookup_is_case_tolerant_but_preserves_display_casing():
    store = InMemoryTypeSkillStore()

    async def go():
        await store.upsert(_skill(type_name="Person"))
        got = await store.get("t1", "pErSoN", "notes")
        assert got is not None, "lookups must not be casing-sensitive"
        assert got.type_name == "Person", "display casing must survive"
        assert len(await store.list_for_tenant("t1", "PERSON")) == 1

    asyncio.run(go())


def test_store_is_tenant_isolated():
    store = InMemoryTypeSkillStore()

    async def go():
        await store.upsert(_skill(tenant_id="t1", body="tenant one secret"))
        await store.upsert(_skill(tenant_id="t2", body="tenant two secret"))
        rows = await store.list_for_tenant("t1")
        assert [r.body for r in rows] == ["tenant one secret"]
        assert await store.get("t1", "Person", "notes") is not None
        other = await store.get("t2", "Person", "notes")
        assert other.body == "tenant two secret"

    asyncio.run(go())


def test_store_returns_copies_not_live_references():
    """A caller mutating a returned skill must not corrupt the store."""
    store = InMemoryTypeSkillStore()

    async def go():
        await store.upsert(_skill(body="original"))
        got = await store.get("t1", "Person", "notes")
        got.body = "mutated by caller"
        again = await store.get("t1", "Person", "notes")
        assert again.body == "original"

    asyncio.run(go())


def test_store_selection_follows_the_dsn(monkeypatch):
    from cograph_client.config import settings

    reset_type_skill_store()
    monkeypatch.setattr(settings, "database_url", None, raising=False)
    assert isinstance(make_type_skill_store(), InMemoryTypeSkillStore)

    reset_type_skill_store()
    monkeypatch.setattr(
        settings, "database_url", "postgresql://u:p@h/db", raising=False
    )
    store = make_type_skill_store()
    assert isinstance(store, PostgresTypeSkillStore)
    assert store._pool is None, "selection must not touch the network"


# --------------------------------------------------------------------------- #
# Registry — curated global layers
# --------------------------------------------------------------------------- #
def test_front_matter_is_parsed_and_stripped():
    meta, body = parse_skill_markdown(
        '---\ntitle: Naming\nsummary: "how names work"\nenabled: false\n---\nThe body.\n'
    )
    assert meta == {"title": "Naming", "summary": "how names work", "enabled": "false"}
    assert body.strip() == "The body."


def test_markdown_without_front_matter_is_all_body():
    text = "# Heading\n\nEverything here is body."
    meta, body = parse_skill_markdown(text)
    assert meta == {}
    assert body == text


def test_unterminated_front_matter_is_treated_as_body():
    """A malformed header must never eat the content."""
    text = "---\ntitle: oops\nno closing fence\nreal content"
    meta, body = parse_skill_markdown(text)
    assert meta == {}
    assert body == text


def test_load_skill_dir_takes_type_from_directory_and_slug_from_filename(tmp_path):
    d = tmp_path / "Organization"
    d.mkdir()
    (d / "naming-conventions.md").write_text(
        "---\ntitle: Naming\n---\nOrgs are named by legal entity.\n", encoding="utf-8"
    )
    (d / "broken.md").write_text("", encoding="utf-8")  # empty body -> invalid

    loaded = load_skill_dir(tmp_path, layer=Layer.PUBLIC)
    assert [s.slug for s in loaded] == ["naming-conventions"], "invalid file must be skipped"
    got = loaded[0]
    assert got.type_name == "Organization"
    assert got.layer is Layer.PUBLIC
    assert got.title == "Naming"
    assert "legal entity" in got.body


def test_load_skill_dir_on_missing_directory_is_empty_not_an_error(tmp_path):
    assert load_skill_dir(tmp_path / "nope", layer=Layer.PUBLIC) == []


def test_register_skill_layer_refuses_the_tenant_layer():
    """Tenant skills in a process-wide registry would leak across workspaces."""
    with pytest.raises(ValueError, match="GLOBAL layers only"):
        register_skill_layer(Layer.TENANT, [_skill()])


def test_register_skill_layer_refuses_nonempty_public_layer():
    """ONTA-400: Public is attrs+rels only — non-empty skill registration refuses."""
    from cograph_client.graph.layer_content import LayerContentError

    with pytest.raises(LayerContentError, match="may not carry skills"):
        register_skill_layer(Layer.PUBLIC, [_skill(slug="pub", tenant_id=None)])


def test_register_skill_layer_allows_empty_public_registration():
    """Reserved-empty seed path remains callable as a no-op."""
    register_skill_layer(Layer.PUBLIC, [])
    assert global_skills_by_layer().get(Layer.PUBLIC, []) == []


def test_register_skill_layer_normalizes_and_rejects_invalid_content():
    register_skill_layer(
        Layer.ENHANCED,
        [
            _skill(slug="curated", tenant_id="leaked"),  # tenant_id must be scrubbed
            _skill(slug="BAD SLUG", tenant_id=None),  # invalid -> dropped
        ],
    )
    entries = global_skills_by_layer()[Layer.ENHANCED]
    assert [e.slug for e in entries] == ["curated"]
    assert entries[0].layer is Layer.ENHANCED
    assert entries[0].tenant_id is None


def test_global_skills_for_type_is_the_operator_read_function():
    """The operator Global Ontology assembler's entry point: plain, importable,
    no tenant context. Skills live on Enhanced (Public carries none — ONTA-400)."""
    register_skill_layer(Layer.ENHANCED, [_skill(slug="enh", tenant_id=None)])
    # A second Enhanced registration appends (multiple contributors).
    register_skill_layer(Layer.ENHANCED, [_skill(slug="enh2", tenant_id=None)])

    got = global_skills_for_type("Person")
    assert [s.slug for s in got] == ["enh", "enh2"]
    assert [s.slug for s in global_skills_for_type("person")] == ["enh", "enh2"]
    assert global_skills_for_type("Person", layer=Layer.PUBLIC) == []
    assert [s.slug for s in global_skills_for_type("Person", layer=Layer.ENHANCED)] == [
        "enh",
        "enh2",
    ]
    assert global_skills_for_type("Unknown") == []


# --------------------------------------------------------------------------- #
# Layer resolution
# --------------------------------------------------------------------------- #
def test_layers_union_rather_than_shadow_wholesale():
    """A type has one DEFINITION but many SKILLS — they compose."""
    merged = merge_layers(
        {
            Layer.TENANT: [_skill(slug="local")],
            Layer.PUBLIC: [_skill(slug="universal", tenant_id=None, layer=Layer.PUBLIC)],
        },
        _stack(),
        type_name="Person",
    )
    assert [s.slug for s in merged] == ["local", "universal"], (
        "both layers' skills must survive, tenant first"
    )


def test_same_slug_in_a_higher_layer_shadows_the_lower_one():
    merged = merge_layers(
        {
            Layer.TENANT: [_skill(slug="naming", body="TENANT VERSION")],
            Layer.PUBLIC: [
                _skill(slug="naming", body="PUBLIC VERSION", tenant_id=None,
                       layer=Layer.PUBLIC)
            ],
        },
        _stack(),
        type_name="Person",
    )
    assert len(merged) == 1
    assert merged[0].body == "TENANT VERSION"


def test_enhanced_layer_is_invisible_without_entitlement():
    by_layer = {
        Layer.ENHANCED: [_skill(slug="premium", tenant_id=None, layer=Layer.ENHANCED)],
        Layer.PUBLIC: [_skill(slug="free", tenant_id=None, layer=Layer.PUBLIC)],
    }
    assert [s.slug for s in merge_layers(by_layer, _stack(entitled=False),
                                         type_name="Person")] == ["free"]
    assert [s.slug for s in merge_layers(by_layer, _stack(entitled=True),
                                         type_name="Person")] == ["premium", "free"]


def test_disabling_a_higher_layer_skill_suppresses_the_lower_one():
    """`enabled: false` on an override means 'this guidance does not apply here',
    not 'fall back to the curated version'."""
    merged = merge_layers(
        {
            Layer.TENANT: [_skill(slug="naming", enabled=False)],
            Layer.PUBLIC: [
                _skill(slug="naming", body="CURATED", tenant_id=None, layer=Layer.PUBLIC)
            ],
        },
        _stack(),
        type_name="Person",
    )
    assert merged == []


def test_merge_filters_by_type():
    merged = merge_layers(
        {Layer.TENANT: [_skill(type_name="Person"), _skill(type_name="Company")]},
        _stack(),
        type_name="company",
    )
    assert [s.type_name for s in merged] == ["Company"]


def test_resolve_skills_combines_store_and_registry():
    # Curated skills land on Enhanced (Public may not carry them — ONTA-400).
    register_skill_layer(Layer.ENHANCED, [_skill(slug="universal", tenant_id=None)])
    store = InMemoryTypeSkillStore()

    async def go():
        await store.upsert(_skill(slug="local"))
        # Non-entitled stack is Tenant > Public; Enhanced is invisible without
        # entitlement. Pass entitled=True via resolve_skills' stack... resolve
        # builds its own LayerStack from entitled flag.
        got = await resolve_skills(
            "Person", tenant_id="t1", store=store, entitled=True
        )
        assert [s.slug for s in got] == ["local", "universal"]

    asyncio.run(go())


def test_resolve_skills_degrades_when_the_tenant_store_is_broken():
    """A broken store must cost you the tenant layer, not the whole feature."""
    register_skill_layer(Layer.ENHANCED, [_skill(slug="universal", tenant_id=None)])
    broken = AsyncMock()
    broken.list_for_tenant.side_effect = RuntimeError("db down")

    got = asyncio.run(
        resolve_skills("Person", tenant_id="t1", store=broken, entitled=True)
    )
    assert [s.slug for s in got] == ["universal"]


# --------------------------------------------------------------------------- #
# The agent-injection seam
# --------------------------------------------------------------------------- #
def test_empty_resolution_renders_nothing():
    """'Empty means invisible' is what makes the seam safe on a hot prompt
    path — a workspace with no skills must get a byte-identical prompt."""
    assert render_skills_block([]) == ""
    assert asyncio.run(skills_prompt_block([], tenant_id="t1")) == ""


def test_rendered_block_carries_body_type_and_layer():
    block = render_skills_block(
        [_skill(slug="naming", title="Naming", body="Never merge two clinics.")]
    )
    assert "Never merge two clinics." in block
    assert "Person" in block
    assert "Naming" in block
    assert "tenant" in block, "the layer must be visible to the reader"


def test_rendered_block_respects_the_budget_and_announces_truncation():
    big = _skill(slug="big", body="B" * 5_000)
    other = _skill(slug="other", body="C" * 5_000)
    block = render_skills_block([big, other], max_chars=1_000)
    assert len(block) < 1_500, f"budget ignored: {len(block)}"
    assert "[truncated]" in block, "silent truncation is not allowed"
    assert "omitted" in block, "dropped skills must be reported, not vanish"


def test_prompt_block_dedupes_a_repeated_type():
    """A repeated type must be resolved ONCE — an agent that names the same type
    twice must not spend twice the prompt budget on it. Distinct types keep
    their own copy of a same-named skill: those are different skills."""
    register_skill_layer(
        Layer.ENHANCED,
        [
            _skill(slug="shared", type_name="Person", tenant_id=None),
            _skill(slug="shared", type_name="Company", tenant_id=None),
        ],
    )
    text = asyncio.run(
        skills_prompt_block(
            ["Person", "Person", "Company"], tenant_id="t1", entitled=True
        )
    )
    assert text.count("### Person") == 1
    assert text.count("### Company") == 1


def test_prompt_block_never_raises():
    """A broken skills feature must never take down a query."""
    with patch(
        "cograph_client.skills.resolve.resolve_skills",
        side_effect=RuntimeError("boom"),
    ):
        assert asyncio.run(skills_prompt_block(["Person"], tenant_id="t1")) == ""


def test_prompt_block_uses_the_documented_default_budget():
    """Asserted against a LITERAL, not against DEFAULT_PROMPT_BUDGET — comparing
    the output to the same symbol that produced it would pass for any value."""
    assert DEFAULT_PROMPT_BUDGET == 6_000
    register_skill_layer(
        Layer.ENHANCED, [_skill(slug="huge", body="X" * 19_000, tenant_id=None)]
    )
    text = asyncio.run(
        skills_prompt_block(["Person"], tenant_id="t1", entitled=True)
    )
    assert 0 < len(text) <= 6_200, f"default budget not applied: {len(text)}"


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #
_BASE = "/graphs/test-tenant/skills"


def _create(client, headers, **kw):
    body = {
        "slug": "naming",
        "type_name": "Person",
        "body": "A Person here is always a clinician.",
        "title": "Naming",
    }
    body.update(kw)
    return client.post(_BASE, json=body, headers=headers)


def test_create_then_list_then_read(client, auth_headers):
    resp = _create(client, auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["layer"] == "tenant"
    assert resp.json()["editable"] is True
    assert resp.json()["version"] == 1

    listed = client.get(_BASE, headers=auth_headers)
    assert listed.status_code == 200
    assert [s["slug"] for s in listed.json()] == ["naming"]
    assert listed.json()[0]["body_chars"] > 0

    one = client.get(f"{_BASE}/Person/naming", headers=auth_headers)
    assert one.status_code == 200
    assert one.json()["body"] == "A Person here is always a clinician."


def test_create_is_idempotent_on_slug_and_bumps_version(client, auth_headers):
    _create(client, auth_headers)
    again = _create(client, auth_headers, body="Revised guidance.")
    assert again.status_code == 201
    assert again.json()["version"] == 2
    assert len(client.get(_BASE, headers=auth_headers).json()) == 1


def test_create_rejects_an_invalid_skill(client, auth_headers):
    assert _create(client, auth_headers, body="   ").status_code == 422
    assert _create(client, auth_headers, slug="Not A Slug").status_code == 422


def test_validate_route_reports_errors_without_writing(client, auth_headers):
    resp = client.post(
        f"{_BASE}/validate",
        json={"slug": "ok", "type_name": "Person", "body": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False
    assert resp.json()["errors"]
    assert client.get(_BASE, headers=auth_headers).json() == []


def test_patch_updates_only_the_supplied_fields(client, auth_headers):
    _create(client, auth_headers)
    resp = client.patch(
        f"{_BASE}/Person/naming", json={"summary": "gist"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["summary"] == "gist"
    assert resp.json()["body"] == "A Person here is always a clinician."
    assert resp.json()["version"] == 2


def test_delete_removes_the_skill(client, auth_headers):
    _create(client, auth_headers)
    assert client.delete(f"{_BASE}/Person/naming", headers=auth_headers).status_code == 200
    assert client.get(_BASE, headers=auth_headers).json() == []
    assert client.delete(f"{_BASE}/Person/naming", headers=auth_headers).status_code == 404


def test_curated_global_skills_are_read_only_over_http(client, auth_headers):
    # Curated skills live on Enhanced (Public may not carry them — ONTA-400).
    # Entitle the caller so the Enhanced layer is visible on the list route.
    from cograph_client.graph.entitlement import register_entitlement_checker

    register_skill_layer(
        Layer.ENHANCED,
        [_skill(slug="curated", tenant_id=None, body="Curated guidance.")],
    )
    register_entitlement_checker(lambda _t: True)
    try:
        listed = client.get(_BASE, headers=auth_headers).json()
        assert [s["slug"] for s in listed] == ["curated"]
        assert listed[0]["editable"] is False
        assert listed[0]["layer"] == "enhanced"

        patched = client.patch(
            f"{_BASE}/Person/curated", json={"body": "hijacked"}, headers=auth_headers
        )
        assert patched.status_code == 403
        assert "read-only" in patched.json()["detail"]

        deleted = client.delete(f"{_BASE}/Person/curated", headers=auth_headers)
        assert deleted.status_code == 403

        # The sanctioned override: a tenant skill with the SAME slug shadows it.
        assert _create(client, auth_headers, slug="curated", body="Ours.").status_code == 201
        resolved = client.get(_BASE, headers=auth_headers).json()
        assert len(resolved) == 1
        assert resolved[0]["layer"] == "tenant"
        assert (
            client.get(f"{_BASE}/Person/curated", headers=auth_headers).json()["body"]
            == "Ours."
        )
    finally:
        register_entitlement_checker(None)


def test_unfiltered_list_reads_the_store_once(client, auth_headers):
    """No N+1: listing every type must not issue one store read per type."""
    store = make_type_skill_store()
    for t in ("Person", "Company", "Clinic", "Trial"):
        _create(client, auth_headers, type_name=t)

    calls = []
    original = store.list_for_tenant

    async def spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    store.list_for_tenant = spy
    try:
        resp = client.get(_BASE, headers=auth_headers)
    finally:
        store.list_for_tenant = original

    assert resp.status_code == 200
    assert len(resp.json()) == 4
    assert len(calls) == 1, f"expected 1 store read, got {len(calls)}"


def test_unknown_skill_is_404(client, auth_headers):
    assert client.get(f"{_BASE}/Person/nope", headers=auth_headers).status_code == 404
    assert client.patch(
        f"{_BASE}/Person/nope", json={"body": "x"}, headers=auth_headers
    ).status_code == 404


def test_prompt_block_route_returns_the_seam_text(client, auth_headers):
    _create(client, auth_headers)
    resp = client.get(
        f"{_BASE}/prompt-block", params={"type_name": ["Person"]}, headers=auth_headers
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["skill_count"] == 1
    assert "A Person here is always a clinician." in payload["text"]
    assert payload["chars"] == len(payload["text"])


def test_prompt_block_route_is_empty_without_types(client, auth_headers):
    """No requested type means NO context — the route must not helpfully dump
    every skill in the workspace into someone's prompt."""
    _create(client, auth_headers)
    resp = client.get(f"{_BASE}/prompt-block", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["text"] == ""
    assert resp.json()["skill_count"] == 0


def test_routes_require_authentication(client):
    assert client.get(_BASE).status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Boundary
# --------------------------------------------------------------------------- #
#: An actual import STATEMENT of the proprietary `cograph.` package — anchored
#: at line start so a prose mention of the rule inside a docstring (which every
#: module in this package carries) is not a false positive, and `cograph_client`
#: is excluded by the required dot.
_PROPRIETARY_IMPORT = re.compile(r"^\s*(?:from|import)\s+cograph\.", re.MULTILINE)


def test_skills_package_never_imports_the_proprietary_tree():
    import pathlib

    import cograph_client.skills as pkg
    import cograph_client.api.routes.skills as routes

    files = list(pathlib.Path(pkg.__file__).parent.rglob("*.py"))
    files.append(pathlib.Path(routes.__file__))
    offenders = [
        p.name for p in files if _PROPRIETARY_IMPORT.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"OSS must not import the premium tree: {offenders}"


def test_the_proprietary_import_guard_can_actually_fail():
    """Keep the guard above honest — a planted violation must trip it."""
    assert _PROPRIETARY_IMPORT.search("from cograph.enrichment import x")
    assert _PROPRIETARY_IMPORT.search("import cograph.qc")
    assert not _PROPRIETARY_IMPORT.search("from cograph_client.config import settings")
    assert not _PROPRIETARY_IMPORT.search("Boundary: OSS — no ``from cograph.*``.")
