"""Tenant ontology routes — layered READS, tenant-graph WRITES (ONTA-397).

Implementation lives in sibling ``ontology_*.py`` modules. Every previously
importable name is re-exported here. Route handlers are registered on
``router`` so FastAPI paths stay identical.

**Reads** go through the GraphStore catalog (ONTA-535). **Writes** (create
type, add attribute, aliases, resolve-apply) target the tenant layer;
schema mutations go through ``commit_ontology``.

Tests monkeypatch names on this module (``ensure_workspace_base_pin``,
``latest_base_release_version``, ``_current_revision_counter``,
``preview_base_upgrade``, ``upgrade_base_pin``, ``rollback_base_pin``,
``fetch_ontology_changelog``, ``get_base_pin``, ``diff_graphs``,
``_build_resolver``). Siblings look them up at call time via ``_host()``.
"""

from __future__ import annotations

from fastapi import APIRouter

from infona_client.graph.client import NeptuneClient  # noqa: F401 — residual allowlist
from infona_client.graph.aliases import (  # noqa: F401 — _host().backfill_aliases
    AliasStillReferencedError,
    backfill_aliases,
    fetch_alias_map,
)
from infona_client.graph.ontology_base_pin import (  # noqa: F401 — monkeypatch surface
    BasePin,
    BasePinReadError,
    ensure_workspace_base_pin,
    get_base_pin,
    latest_base_release_version,
    preview_base_upgrade,
    rollback_base_pin,
    upgrade_base_pin,
)
from infona_client.graph.ontology_changelog import (  # noqa: F401
    fetch_ontology_changelog,
    group_changelog_entries,
)
from infona_client.graph.ontology_commit import (  # noqa: F401 — schema write path
    commit_ontology,
    release_graph_uri,
    revision_graph_uri,
)
from infona_client.graph.ontology_compat import classify_diff  # noqa: F401
from infona_client.graph.ontology_snapshots import (  # noqa: F401
    _current_revision_counter,
    diff_graphs,
)
from infona_client.config import settings  # noqa: F401 — _host().settings
from infona_client.nlp.pipeline import get_embedding_service  # noqa: F401

from infona_client.api.routes.ontology_aliases import (  # noqa: F401
    backfill_attribute_aliases as _backfill_attribute_aliases,
    list_attribute_aliases as _list_attribute_aliases,
    register_attribute_alias as _register_attribute_alias,
    rename_attribute_with_alias as _rename_attribute_with_alias,
    retire_attribute_alias as _retire_attribute_alias,
)
from infona_client.api.routes.ontology_base import (
    get_workspace_base_pin as _get_workspace_base_pin,
    post_workspace_base_rollback as _post_workspace_base_rollback,
    post_workspace_base_upgrade as _post_workspace_base_upgrade,
    preview_workspace_base_upgrade as _preview_workspace_base_upgrade,
)
from infona_client.api.routes.ontology_changelog import (
    get_ontology_changelog as _get_ontology_changelog,
    get_ontology_history as _get_ontology_history,
)
from infona_client.api.routes.ontology_common import (  # noqa: F401
    _ABS_IRI_RE,
    _ACTION_RE,
    _VERDICT_CACHE_PATH,
    _base_pin_response,
    _changelog_entry_model,
    _host,
    _resolve_ontology_ref,
    _type_response,
)
from infona_client.api.routes.ontology_diff import get_ontology_diff as _get_ontology_diff
from infona_client.api.routes.ontology_resolve import (  # noqa: F401
    _apply_change,
    _build_resolver,
    apply_ontology_change as _apply_ontology_change,
    apply_ontology_changes as _apply_ontology_changes,
    change_label,
    resolve_ontology as _resolve_ontology,
)
from infona_client.api.routes.ontology_types import (
    add_attributes as _add_attributes,
    add_subtype as _add_subtype,
    create_type as _create_type,
    delete_attribute_route as _delete_attribute_route,
)
from infona_client.api.routes.ontology_workspace import (  # noqa: F401
    _workspace_catalog,
    _workspace_ontology,
    _workspace_ontology_store,
    get_full_schema as _get_full_schema,
    get_type as _get_type,
    get_workspace_ontology as _get_workspace_ontology,
    list_types as _list_types,
    workspace_type_counts as _workspace_type_counts,
)
from infona_client.models.ontology import (  # noqa: F401
    AliasMapResponse,
    ApplyBatchResult,
    BasePinResponse,
    OntologyChangelogResponse,
    OntologyDiffResponse,
    OntologyHistoryResponse,
    ResolutionResult,
    TypeResponse,
    UpgradePreviewResponse,
    WorkspaceOntologyResponse,
    WorkspaceTypeCountsResponse,
)

router = APIRouter(prefix="/graphs/{tenant}/ontology")

# Re-bind route handlers on this module (same paths as before the extract).
get_workspace_ontology = router.get("", response_model=WorkspaceOntologyResponse)(
    _get_workspace_ontology
)
workspace_type_counts = router.get(
    "/type-counts", response_model=WorkspaceTypeCountsResponse
)(_workspace_type_counts)
create_type = router.post("/types", status_code=201)(_create_type)
list_types = router.get("/types", response_model=list[TypeResponse])(_list_types)
get_type = router.get("/types/{type_name}", response_model=TypeResponse)(_get_type)
add_attributes = router.post("/types/{type_name}/attributes", status_code=201)(
    _add_attributes
)
delete_attribute_route = router.delete("/types/{type_name}/attributes/{attr_name}")(
    _delete_attribute_route
)
add_subtype = router.post("/types/{type_name}/subtypes", status_code=201)(_add_subtype)
get_full_schema = router.get("/schema")(_get_full_schema)
get_ontology_changelog = router.get(
    "/changelog", response_model=OntologyChangelogResponse
)(_get_ontology_changelog)
get_workspace_base_pin = router.get("/base-pin", response_model=BasePinResponse)(
    _get_workspace_base_pin
)
preview_workspace_base_upgrade = router.get(
    "/base-pin/preview", response_model=UpgradePreviewResponse
)(_preview_workspace_base_upgrade)
post_workspace_base_upgrade = router.post(
    "/base-pin/upgrade", response_model=BasePinResponse
)(_post_workspace_base_upgrade)
post_workspace_base_rollback = router.post(
    "/base-pin/rollback", response_model=BasePinResponse
)(_post_workspace_base_rollback)
get_ontology_history = router.get("/history", response_model=OntologyHistoryResponse)(
    _get_ontology_history
)
get_ontology_diff = router.get("/diff", response_model=OntologyDiffResponse)(
    _get_ontology_diff
)
register_attribute_alias = router.post("/aliases", status_code=201)(
    _register_attribute_alias
)
rename_attribute_with_alias = router.post("/aliases/rename", status_code=201)(
    _rename_attribute_with_alias
)
backfill_attribute_aliases = router.post("/aliases/backfill")(
    _backfill_attribute_aliases
)
retire_attribute_alias = router.delete("/aliases")(_retire_attribute_alias)
list_attribute_aliases = router.get("/aliases", response_model=AliasMapResponse)(
    _list_attribute_aliases
)
resolve_ontology = router.post("/resolve", response_model=ResolutionResult)(
    _resolve_ontology
)
apply_ontology_change = router.post("/apply")(_apply_ontology_change)
apply_ontology_changes = router.post("/apply/batch", response_model=ApplyBatchResult)(
    _apply_ontology_changes
)
