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
    apply_registry_selection,
    register_registry_enrichment,
    reset_registry_enrichment,
)
from .matching import covers, fillable_column, has_enrich_params, type_matches
from .registry_selection import (
    SelectionNeed,
    arbitrate,
    clear_selection_cache,
    register_source_success_rate_provider,
    reset_source_success_rate_provider,
    select_registry_slugs,
    selection_enabled,
    selection_top_k,
    structured_prefilter,
)
from .crypto import (
    LocalAesGcmCipher,
    SecretCipher,
    SecretCipherError,
    get_secret_cipher,
    register_secret_cipher,
    reset_secret_cipher,
)
from .executor import ApiCallResult, RegistryApiSource, SecretResolver
from .secret_store import (
    InMemoryTenantSecretStore,
    PostgresTenantSecretStore,
    TenantApiSecret,
    TenantSecretStore,
    make_secret_resolver,
    make_tenant_secret_store,
    reset_tenant_secret_store,
    resolve_secret,
    store_secret,
)
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
    # secret cipher + encrypted secret store (ONTA-2xx Child 2)
    "SecretCipher",
    "SecretCipherError",
    "LocalAesGcmCipher",
    "register_secret_cipher",
    "get_secret_cipher",
    "reset_secret_cipher",
    "TenantApiSecret",
    "TenantSecretStore",
    "InMemoryTenantSecretStore",
    "PostgresTenantSecretStore",
    "make_tenant_secret_store",
    "reset_tenant_secret_store",
    "store_secret",
    "resolve_secret",
    "make_secret_resolver",
    # executor
    "RegistryApiSource",
    "ApiCallResult",
    "SecretResolver",
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
    # scalable discovery + selection (ONTA-341)
    "SelectionNeed",
    "apply_registry_selection",
    "select_registry_slugs",
    "structured_prefilter",
    "arbitrate",
    "selection_enabled",
    "selection_top_k",
    "clear_selection_cache",
    "register_source_success_rate_provider",
    "reset_source_success_rate_provider",
    "covers",
    "fillable_column",
    "type_matches",
    "has_enrich_params",
    # catalog freshness audit (phase 4)
    "audit_catalog",
    "format_markdown",
]
