"""ONTA-2xx Child 1 — tenant_custom catalog layer + durable store.

Covers the three acceptance properties: layer precedence (tenant_custom shadows
global by slug), per-tenant isolation (a tenant only ever sees its own custom
entries), and cache invalidation (a write drops the cached merge so the next load
re-reads the store). Uses the InMemory store so no DB is required.
"""

from __future__ import annotations

import pytest

from cograph_client.api_registry import (
    LAYER_TENANT_CUSTOM,
    InMemoryTenantApiSourceStore,
    TenantApiSource,
    get_api_source_catalog,
    invalidate_tenant_catalog,
    load_tenant_custom_catalog,
    reset_api_source_catalog,
    reset_tenant_api_source_store,
    set_tenant_custom_specs,
)
from cograph_client.api_registry.catalog import _LAYER_RANK
from cograph_client.api_registry.spec import ApiSourceSpec


@pytest.fixture(autouse=True)
def _clean_catalog():
    reset_api_source_catalog()
    reset_tenant_api_source_store()
    yield
    reset_api_source_catalog()
    reset_tenant_api_source_store()


def _spec(slug: str, *, title: str = "", enabled: bool = True) -> ApiSourceSpec:
    return ApiSourceSpec.from_dict(
        {
            "slug": slug,
            "title": title or slug,
            "base_url": "https://api.example.com",
            "enabled": enabled,
            "endpoints": [
                {
                    "name": "default",
                    "method": "GET",
                    "path": "/search",
                    "params": [{"name": "q", "location": "query"}],
                    "result_path": "results",
                    "field_mappings": {"name": "name"},
                }
            ],
        }
    )


def _record(tenant: str, slug: str, *, enabled: bool = True, title: str = "") -> TenantApiSource:
    return TenantApiSource(
        tenant_id=tenant, slug=slug, spec=_spec(slug, title=title), enabled=enabled
    )


# --------------------------------------------------------------------------- #
# Layer rank / precedence
# --------------------------------------------------------------------------- #
def test_tenant_custom_has_highest_rank():
    assert _LAYER_RANK["tenant_custom"] == 20
    assert _LAYER_RANK["tenant_custom"] > _LAYER_RANK["global_enhanced"] > _LAYER_RANK["global_public"]


def test_tenant_custom_shadows_a_global_slug_for_that_tenant_only():
    # nppes is a global_public seed slug. A tenant that defines its own "nppes"
    # entry shadows the global one for itself.
    tenant_nppes = _spec("nppes", title="My Private NPPES Mirror")
    set_tenant_custom_specs("tenant-a", [tenant_nppes])

    cat_a = get_api_source_catalog("tenant-a")
    assert cat_a.get("nppes").title == "My Private NPPES Mirror"
    assert cat_a.get("nppes").layer == LAYER_TENANT_CUSTOM

    # A different tenant, and the global (tenant_id=None) view, still see the
    # curated global nppes.
    cat_b = get_api_source_catalog("tenant-b")
    cat_global = get_api_source_catalog()
    assert cat_b.get("nppes").layer == "global_public"
    assert cat_global.get("nppes").layer == "global_public"


def test_tenant_custom_adds_a_new_slug_visible_only_to_that_tenant():
    set_tenant_custom_specs("tenant-a", [_spec("acme_internal")])
    assert get_api_source_catalog("tenant-a").get("acme_internal") is not None
    # Isolation: not visible to another tenant nor to the global view.
    assert get_api_source_catalog("tenant-b").get("acme_internal") is None
    assert get_api_source_catalog().get("acme_internal") is None


def test_merged_catalog_does_not_mutate_the_global_singleton():
    before = set(get_api_source_catalog().slugs())
    set_tenant_custom_specs("tenant-a", [_spec("acme_internal")])
    _ = get_api_source_catalog("tenant-a")
    after = set(get_api_source_catalog().slugs())
    assert before == after, "the per-tenant merge must not leak into the global catalog"


# --------------------------------------------------------------------------- #
# Store round-trip + load
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_store_roundtrip_and_load_into_catalog():
    store = InMemoryTenantApiSourceStore()
    await store.upsert(_record("tenant-a", "acme_internal", title="Acme Internal API"))

    got = await store.get("tenant-a", "acme_internal")
    assert got is not None
    assert got.spec.title == "Acme Internal API"
    assert got.created_at is not None and got.updated_at is not None

    cat = await load_tenant_custom_catalog("tenant-a", store)
    entry = cat.get("acme_internal")
    assert entry is not None
    assert entry.layer == LAYER_TENANT_CUSTOM


@pytest.mark.asyncio
async def test_store_isolation_across_tenants():
    store = InMemoryTenantApiSourceStore()
    await store.upsert(_record("tenant-a", "a_only"))
    await store.upsert(_record("tenant-b", "b_only"))

    a_rows = await store.list_for_tenant("tenant-a")
    b_rows = await store.list_for_tenant("tenant-b")
    assert {r.slug for r in a_rows} == {"a_only"}
    assert {r.slug for r in b_rows} == {"b_only"}

    # get() is tenant-scoped: tenant-a cannot read tenant-b's slug.
    assert await store.get("tenant-a", "b_only") is None
    assert await store.get("tenant-b", "a_only") is None


@pytest.mark.asyncio
async def test_load_reflects_enabled_flag_on_the_row():
    store = InMemoryTenantApiSourceStore()
    # Row-level enabled=False overrides spec.enabled=True in the materialized spec.
    await store.upsert(_record("tenant-a", "paused", enabled=False))
    cat = await load_tenant_custom_catalog("tenant-a", store)
    entry = cat.get("paused")
    assert entry is not None
    assert entry.enabled is False
    assert entry not in cat.enabled()


@pytest.mark.asyncio
async def test_delete_returns_whether_a_row_existed():
    store = InMemoryTenantApiSourceStore()
    await store.upsert(_record("tenant-a", "gone"))
    assert await store.delete("tenant-a", "gone") is True
    assert await store.delete("tenant-a", "gone") is False
    # Deleting another tenant's slug is a no-op, not a cross-tenant delete.
    await store.upsert(_record("tenant-b", "keep"))
    assert await store.delete("tenant-a", "keep") is False
    assert await store.get("tenant-b", "keep") is not None


# --------------------------------------------------------------------------- #
# Cache invalidation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invalidation_forces_reload_after_a_write():
    store = InMemoryTenantApiSourceStore()
    await store.upsert(_record("tenant-a", "v1", title="V1"))
    cat1 = await load_tenant_custom_catalog("tenant-a", store)
    assert cat1.get("v1").title == "V1"

    # A stale cached merge would keep returning V1 after the store changes.
    await store.upsert(_record("tenant-a", "v1", title="V2"))
    # Without invalidation the sync catalog still shows the cached V1...
    assert get_api_source_catalog("tenant-a").get("v1").title == "V1"
    # ...invalidate + reload picks up V2.
    invalidate_tenant_catalog("tenant-a")
    cat2 = await load_tenant_custom_catalog("tenant-a", store)
    assert cat2.get("v1").title == "V2"


def test_invalidation_of_unknown_tenant_is_a_noop():
    # Should not raise even if the tenant was never loaded.
    invalidate_tenant_catalog("never-seen")


@pytest.mark.asyncio
async def test_empty_tenant_gets_the_plain_global_catalog():
    store = InMemoryTenantApiSourceStore()
    cat = await load_tenant_custom_catalog("tenant-empty", store)
    # Same slugs as the global catalog (no custom entries added).
    assert set(cat.slugs()) == set(get_api_source_catalog().slugs())


# --------------------------------------------------------------------------- #
# PostgresTenantApiSourceStore — no real DB, shared pool faked
# --------------------------------------------------------------------------- #
class _FakeConn:
    """Records SQL + params; returns canned rows for fetch/fetchrow."""

    def __init__(self, recorder: list) -> None:
        self._rec = recorder
        self.rows: list = []
        self.row = None
        self.execute_result = "DELETE 1"

    async def execute(self, sql, *params):
        self._rec.append(("execute", sql, params))
        return self.execute_result

    async def fetchrow(self, sql, *params):
        self._rec.append(("fetchrow", sql, params))
        return self.row

    async def fetch(self, sql, *params):
        self._rec.append(("fetch", sql, params))
        return self.rows


class _AcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


def _patch_pool(monkeypatch, conn: _FakeConn):
    import cograph_client.api_registry.store as store_mod

    async def fake_get_pg_pool(dsn):
        return _FakePool(conn)

    # The store imports get_pg_pool lazily inside _ensure_pool, so patch it on
    # the shared pool module where the name is looked up.
    import cograph_client.db.pool as pool_mod

    monkeypatch.setattr(pool_mod, "get_pg_pool", fake_get_pg_pool)
    return store_mod


@pytest.mark.asyncio
async def test_postgres_store_upsert_runs_ddl_and_returns_record(monkeypatch):
    from datetime import datetime, timezone

    from cograph_client.api_registry.store import PostgresTenantApiSourceStore

    rec: list = []
    conn = _FakeConn(rec)
    _patch_pool(monkeypatch, conn)

    now = datetime.now(timezone.utc)
    spec = _spec("acme_internal", title="Acme")
    conn.row = {
        "tenant_id": "tenant-a",
        "slug": "acme_internal",
        "spec_json": __import__("json").dumps(spec.to_dict()),
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }

    store = PostgresTenantApiSourceStore(dsn="postgresql://fake/db")
    out = await store.upsert(_record("tenant-a", "acme_internal", title="Acme"))
    assert out.tenant_id == "tenant-a" and out.slug == "acme_internal"

    ddl = [c for c in rec if c[0] == "execute" and "CREATE TABLE" in c[1]]
    assert ddl, "upsert must ensure the table (idempotent DDL)"
    assert "tenant_api_sources" in ddl[0][1]
    assert "PRIMARY KEY (tenant_id, slug)" in ddl[0][1]
    upserts = [c for c in rec if "INSERT INTO" in c[1]]
    assert upserts and "ON CONFLICT (tenant_id, slug)" in upserts[0][1]


@pytest.mark.asyncio
async def test_postgres_queries_are_tenant_scoped(monkeypatch):
    from cograph_client.api_registry.store import PostgresTenantApiSourceStore

    rec: list = []
    conn = _FakeConn(rec)
    _patch_pool(monkeypatch, conn)

    store = PostgresTenantApiSourceStore(dsn="postgresql://fake/db")
    conn.rows = []
    await store.list_for_tenant("tenant-a")
    conn.row = None
    await store.get("tenant-a", "slug-x")
    conn.execute_result = "DELETE 0"
    deleted = await store.delete("tenant-a", "slug-x")

    # Every read/delete SQL must filter by tenant_id ($1) — no cross-tenant path.
    reads = [c for c in rec if c[0] in ("fetch", "fetchrow") or "DELETE FROM" in c[1]]
    for _kind, sql, params in reads:
        assert "tenant_id = $1" in sql, f"query not tenant-scoped: {sql}"
        assert params[0] == "tenant-a"
    assert deleted is False  # "DELETE 0" => nothing removed


@pytest.mark.asyncio
async def test_postgres_smoke_real_db():
    """Optional real-DB smoke test; skipped unless OMNIX_DATABASE_URL is set."""
    import os

    dsn = os.environ.get("OMNIX_DATABASE_URL")
    if not dsn:
        pytest.skip("OMNIX_DATABASE_URL not set")
    from cograph_client.api_registry.store import PostgresTenantApiSourceStore

    store = PostgresTenantApiSourceStore(dsn=dsn)
    await store.upsert(_record("smoke-tenant", "smoke_slug", title="Smoke"))
    got = await store.get("smoke-tenant", "smoke_slug")
    assert got is not None and got.spec.title == "Smoke"
    assert await store.delete("smoke-tenant", "smoke_slug") is True
