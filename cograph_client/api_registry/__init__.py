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
    load_catalog_dir,
    make_api_source_catalog,
    register_api_source_layer,
    registered_layers,
    reset_api_source_layers,
)
from .executor import ApiCallResult, RegistryApiSource
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
    "register_api_source_layer",
    "registered_layers",
    "reset_api_source_layers",
    # executor
    "RegistryApiSource",
    "ApiCallResult",
]
