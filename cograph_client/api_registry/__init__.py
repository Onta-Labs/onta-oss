"""API source registry (ONTA-194) — a curated directory of authoritative APIs
plus a generic declarative executor that runs any entry with zero per-API code.

Phase 1 (this package): the spec, the catalog loader + operator-curated layer
seam, the OSS ``global_public`` seed catalog, and the generic executor
(``RegistryApiSource``). Query-time routing onto the discovery / enrichment rails
is a later phase and is deliberately absent here.

Public surface::

    from cograph_client.api_registry import (
        make_api_source_catalog, RegistryApiSource, ApiSourceSpec,
        register_api_source_layer,
    )
"""

from __future__ import annotations

from .catalog import (
    ApiSourceCatalog,
    get_api_source_catalog,
    invalidate_tenant_catalog,
    load_catalog_dir,
    load_tenant_custom_catalog,
    make_api_source_catalog,
    register_api_source_layer,
    registered_layers,
    reset_api_source_catalog,
    reset_api_source_layers,
    set_tenant_custom_specs,
)
from .catalog_audit import audit_catalog, format_markdown
from .discovery import RegistryDiscoverySource, build_registry_sources
from .enrichment import (
    RegistrySourceAdapter,
    register_registry_enrichment,
    reset_registry_enrichment,
)
from .executor import ApiCallResult, RegistryApiSource
from .store import (
    InMemoryTenantApiSourceStore,
    LAYER_TENANT_CUSTOM,
    PostgresTenantApiSourceStore,
    TenantApiSource,
    TenantApiSourceStore,
    make_tenant_api_source_store,
    reset_tenant_api_source_store,
    validate_tenant_spec,
)
from .router import (
    MODE_API_ONLY,
    MODE_API_PLUS_WEB,
    MODE_WEB_ONLY,
    RoutingDecision,
    RoutingPick,
    route_query,
)
from .spec import (
    ApiSourceSpec,
    AuthMode,
    AuthorityLevel,
    Entitlement,
    PaginationStyle,
    SpecError,
    url_lint_errors,
    validate_spec,
)

__all__ = [
    # spec
    "ApiSourceSpec",
    "AuthMode",
    "AuthorityLevel",
    "Entitlement",
    "PaginationStyle",
    "SpecError",
    "validate_spec",
    "url_lint_errors",
    # catalog
    "ApiSourceCatalog",
    "load_catalog_dir",
    "make_api_source_catalog",
    "get_api_source_catalog",
    "load_tenant_custom_catalog",
    "set_tenant_custom_specs",
    "invalidate_tenant_catalog",
    "reset_api_source_catalog",
    "register_api_source_layer",
    "registered_layers",
    "reset_api_source_layers",
    # tenant-custom store (ONTA-2xx)
    "TenantApiSource",
    "TenantApiSourceStore",
    "InMemoryTenantApiSourceStore",
    "PostgresTenantApiSourceStore",
    "make_tenant_api_source_store",
    "reset_tenant_api_source_store",
    "validate_tenant_spec",
    "LAYER_TENANT_CUSTOM",
    # executor
    "RegistryApiSource",
    "ApiCallResult",
    # routing (phase 2)
    "route_query",
    "RoutingDecision",
    "RoutingPick",
    "MODE_API_ONLY",
    "MODE_API_PLUS_WEB",
    "MODE_WEB_ONLY",
    "RegistryDiscoverySource",
    "build_registry_sources",
    # enrichment rail (phase 3)
    "RegistrySourceAdapter",
    "register_registry_enrichment",
    "reset_registry_enrichment",
    # catalog freshness audit (phase 4)
    "audit_catalog",
    "format_markdown",
]
