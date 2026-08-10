"""Wave 0 contract freeze for the layered-ontology epic (ONTA-396).

Pins types and signatures only — no production behavior beyond the existing
OSS default that Enhanced is invisible. Every frozen item has a test that
fails if its shape drifts. See docs/layered-ontology-execution-plan.md §7.
"""

from __future__ import annotations

import inspect
from enum import Enum
from typing import get_type_hints

import pytest

from infona_client.auth.api_keys import TenantContext
from infona_client.graph.entitlement import (
    EntitlementChecker,
    get_entitlement_checker,
    is_entitled,
    register_entitlement_checker,
)
from infona_client.graph.global_ontology import fetch_global_ontology, fetch_ontology
from infona_client.graph.layer_content import (
    LAYER_A_CONTENT_ENFORCEMENT,
    LAYER_CONTENT_MATRIX,
    ContentKind,
    forbidden_kinds,
    permits,
)
from infona_client.graph.layers import Layer
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    GlobalOntologyResponse,
    OntologyCommitResult,
    OntologyMutation,
    OntologyOpKind,
    WorkspaceOntologyLayer,
    WorkspaceOntologyResponse,
    WorkspaceOntologyType,
)


def _hints(fn):
    """Resolved annotations — works under ``from __future__ import annotations``."""
    return get_type_hints(fn)


# ---------------------------------------------------------------------------
# 1. Layer content matrix
# ---------------------------------------------------------------------------


def test_layer_content_matrix_covers_every_layer_exactly_once():
    assert set(LAYER_CONTENT_MATRIX.keys()) == set(Layer)


def test_public_layer_permits_only_attributes_and_relationships():
    assert LAYER_CONTENT_MATRIX[Layer.PUBLIC] == frozenset(
        {ContentKind.ATTRIBUTES, ContentKind.RELATIONSHIPS}
    )
    for kind in (ContentKind.SKILLS, ContentKind.FUNCTIONS, ContentKind.SOURCES):
        assert not permits(Layer.PUBLIC, kind)
        assert kind in forbidden_kinds(Layer.PUBLIC)


def test_enhanced_and_tenant_permit_all_content_kinds():
    full = frozenset(ContentKind)
    assert LAYER_CONTENT_MATRIX[Layer.ENHANCED] == full
    assert LAYER_CONTENT_MATRIX[Layer.TENANT] == full
    for kind in ContentKind:
        assert permits(Layer.ENHANCED, kind)
        assert permits(Layer.TENANT, kind)


def test_a_restriction_is_hard_invariant_not_policy():
    """Conservative Wave 0 decision (plan §11 item 5): ONTA-400 ships a guard."""
    assert LAYER_A_CONTENT_ENFORCEMENT == "invariant"
    assert LAYER_A_CONTENT_ENFORCEMENT != "policy"


def test_content_kind_vocabulary_is_stable():
    assert {k.value for k in ContentKind} == {
        "attributes",
        "relationships",
        "skills",
        "functions",
        "sources",
    }


# ---------------------------------------------------------------------------
# 2. Entitlement signature + location
# ---------------------------------------------------------------------------


def test_is_entitled_signature_is_tenant_context_to_bool():
    sig = inspect.signature(is_entitled)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "tenant"
    hints = _hints(is_entitled)
    assert hints["tenant"] is TenantContext
    assert hints["return"] is bool


def test_register_entitlement_checker_signature():
    sig = inspect.signature(register_entitlement_checker)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "checker"
    hints = _hints(register_entitlement_checker)
    assert hints["return"] is type(None)
    # checker is Optional[EntitlementChecker]
    assert params[0].annotation is not inspect.Parameter.empty


def test_oss_default_is_not_entitled():
    # Ensure no leftover checker from another test pollutes this pin.
    register_entitlement_checker(None)
    tenant = TenantContext(tenant_id="acme", api_key="k")
    assert is_entitled(tenant) is False
    assert get_entitlement_checker() is None


def test_registered_checker_is_authoritative_and_clearable():
    register_entitlement_checker(None)
    tenant = TenantContext(tenant_id="paid", api_key="k")
    assert is_entitled(tenant) is False

    def _yes(t: TenantContext) -> bool:
        return t.tenant_id == "paid"

    register_entitlement_checker(_yes)
    try:
        assert is_entitled(tenant) is True
        assert is_entitled(TenantContext(tenant_id="free", api_key="k")) is False
        assert get_entitlement_checker() is _yes
    finally:
        register_entitlement_checker(None)
    assert is_entitled(tenant) is False


def test_buggy_checker_fails_closed():
    def _boom(_t: TenantContext) -> bool:
        raise RuntimeError("provider down")

    register_entitlement_checker(_boom)
    try:
        assert is_entitled(TenantContext(tenant_id="x", api_key="k")) is False
    finally:
        register_entitlement_checker(None)


def test_entitlement_checker_type_alias_is_callable():
    # Pin the alias exists and is the expected Callable form for premium plug-in.
    assert EntitlementChecker is not None
    # A real function satisfies the alias at runtime for mypy/pyright; here we
    # just confirm the name is importable and assignable.
    checker: EntitlementChecker = lambda t: False  # noqa: E731
    assert callable(checker)


def test_skills_route_entitled_delegates_to_seam():
    """The route-local ``_entitled`` must not re-hardcode False independently."""
    from infona_client.api.routes import skills as skills_routes

    register_entitlement_checker(None)
    tenant = TenantContext(tenant_id="acme", api_key="k")
    assert skills_routes._entitled(tenant) is False

    register_entitlement_checker(lambda t: t.tenant_id == "acme")
    try:
        assert skills_routes._entitled(tenant) is True
    finally:
        register_entitlement_checker(None)


def test_layer_stack_for_helper_uses_is_entitled():
    """ONTA-398: layered reads construct stacks via the single helper."""
    from infona_client.graph.entitlement import layer_stack_for
    from infona_client.graph.layers import Layer

    register_entitlement_checker(None)
    free = TenantContext(tenant_id="free", api_key="k")
    assert layer_stack_for(free).layers == (Layer.TENANT, Layer.PUBLIC)

    register_entitlement_checker(lambda t: t.tenant_id == "paid")
    try:
        paid = TenantContext(tenant_id="paid", api_key="k")
        assert layer_stack_for(paid).layers == (
            Layer.TENANT,
            Layer.ENHANCED,
            Layer.PUBLIC,
        )
    finally:
        register_entitlement_checker(None)


# ---------------------------------------------------------------------------
# 3. Generalized reader signature
# ---------------------------------------------------------------------------


def test_fetch_ontology_signature_is_frozen():
    sig = inspect.signature(fetch_ontology)
    params = sig.parameters
    assert list(params) == [
        "neptune",
        "layers",
        "catalog",
        "today",
        "entitled",
        "tenant_id",
        "apply_shadowing",
    ]
    assert params["layers"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["catalog"].default is None
    assert params["today"].default is None
    assert params["entitled"].default is False
    assert params["tenant_id"].default == ""
    assert params["apply_shadowing"].default is True
    assert _hints(fetch_ontology)["return"] is WorkspaceOntologyResponse


def test_fetch_global_ontology_signature_unchanged():
    """Operator two-layer call stays put — Wave 0 must not rewrite it."""
    sig = inspect.signature(fetch_global_ontology)
    params = sig.parameters
    assert list(params) == ["neptune", "catalog", "today"]
    assert params["catalog"].default is None
    assert params["today"].default is None
    assert _hints(fetch_global_ontology)["return"] is GlobalOntologyResponse


@pytest.mark.asyncio
async def test_fetch_ontology_empty_layers_returns_empty_response():
    """ONTA-397 landed the body: empty layer list is a normal empty payload."""
    body = await fetch_ontology(neptune=None, layers=(), tenant_id="acme")
    assert isinstance(body, WorkspaceOntologyResponse)
    assert body.types == []
    assert body.layers == []
    assert body.tenant_id == "acme"


# ---------------------------------------------------------------------------
# 4. ChangeRecord / diff type
# ---------------------------------------------------------------------------


REQUIRED_CHANGE_KINDS = {
    "add_type",
    "remove_type",
    "add_attribute",
    "remove_attribute",
    "add_relationship",
    "remove_relationship",
    "add_subclass",
    "remove_subclass",
    "change_comment",
    "change_range",
    "change_core_slot",
    "change_text_kind",
    "deprecate",
    "rename_with_alias",
}


def test_change_kind_vocabulary_covers_plan_surface():
    assert {k.value for k in ChangeKind} == REQUIRED_CHANGE_KINDS
    assert issubclass(ChangeKind, Enum)


def test_change_record_fields_are_stable():
    fields = set(ChangeRecord.model_fields)
    assert fields == {
        "kind",
        "type_name",
        "slot_name",
        "parent_type",
        "old_value",
        "new_value",
        "from_name",
        "to_name",
        "superseded_by",
    }
    rec = ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person")
    assert rec.model_dump()["kind"] == "add_type"
    # Round-trip every kind so a rename breaks loudly.
    for kind in ChangeKind:
        assert ChangeRecord(kind=kind).kind is kind


# ---------------------------------------------------------------------------
# 5. commit_ontology signature
# ---------------------------------------------------------------------------


def test_commit_ontology_signature_is_frozen():
    sig = inspect.signature(commit_ontology)
    params = sig.parameters
    assert list(params) == [
        "neptune",
        "graph_uri",
        "mutations",
        "expected_version",
        "actor",
        "message",
    ]
    assert params["expected_version"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["expected_version"].default is None
    assert params["actor"].default is None
    assert params["message"].default is None
    assert _hints(commit_ontology)["return"] is OntologyCommitResult


@pytest.mark.asyncio
async def test_commit_ontology_empty_mutations_is_noop_returning_fingerprint():
    """ONTA-403 landed the body: empty mutations still return a fingerprint.

    Wave 0 pinned NotImplementedError; the pin now asserts the live contract
    (empty commit is a version read, no raise).
    """
    class _N:
        async def query(self, sparql: str):
            return {"head": {"vars": []}, "results": {"bindings": []}}

        async def update(self, sparql: str):
            raise AssertionError("empty commit must not write")

    result = await commit_ontology(
        neptune=_N(),
        graph_uri="https://graph.onta.sh/graphs/acme",
        mutations=[],
    )
    assert result.graph_uri == "https://graph.onta.sh/graphs/acme"
    assert result.version_before == result.version_after
    assert result.applied == []
    assert result.change_records == []


def test_ontology_mutation_and_op_kind_vocabulary():
    assert {o.value for o in OntologyOpKind} == {
        "upsert_type",
        "upsert_attribute",
        "upsert_relationship",
        "set_subclass",
        "delete_type",
        "delete_attribute",
        "set_core_slot",
        "set_text_kind",
        "set_comment",
        "register_alias",
        "rename_attribute",
        "retire_alias",
        "deprecate",
    }
    m = OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person")
    assert m.type_name == "Person"
    result = OntologyCommitResult(
        graph_uri="https://graph.onta.sh/graphs/acme",
        version_after="deadbeef",
        applied=[m],
        change_records=[ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person")],
    )
    assert result.version_after == "deadbeef"
    assert len(result.change_records) == 1


# ---------------------------------------------------------------------------
# 6. Workspace ontology response model (new, not a GlobalOntology* mutation)
# ---------------------------------------------------------------------------


def test_workspace_ontology_response_is_distinct_from_global():
    assert WorkspaceOntologyResponse is not GlobalOntologyResponse
    ws_fields = set(WorkspaceOntologyResponse.model_fields)
    global_fields = set(GlobalOntologyResponse.model_fields)
    # Workspace carries tenant + entitlement; global does not.
    assert "tenant_id" in ws_fields
    assert "entitled" in ws_fields
    assert "tenant_id" not in global_fields
    assert "entitled" not in global_fields
    # Both carry layers + types, but the type element models differ.
    assert "layers" in ws_fields and "types" in ws_fields
    assert WorkspaceOntologyType is not None
    assert WorkspaceOntologyLayer is not None


def test_workspace_ontology_response_constructs_empty():
    body = WorkspaceOntologyResponse(tenant_id="acme", entitled=False)
    assert body.types == []
    assert body.layers == []
    assert body.model_dump()["tenant_id"] == "acme"


def test_global_ontology_response_still_constructs_empty():
    """Wave 0 must not break the operator payload model."""
    body = GlobalOntologyResponse()
    assert body.types == []
    assert body.layers == []
