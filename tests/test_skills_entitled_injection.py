"""Enhanced-layer skills reach Cypher / list_type_schema / extractors IFF entitled.

Hermetic. SynthWidget only. Fake entitlement checker — never a real paid bit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from infona_client.agent.registry import AgentContext
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.entitlement import register_entitlement_checker
from infona_client.graph.layers import Layer
from infona_client.normalization.inference import list_type_schema
from infona_client.skills import (
    TypeSkill,
    register_skill_layer,
    reset_global_type_skill_store,
    reset_skill_layers,
    reset_type_skill_store,
)

WIDGET = "SynthWidget"
TENANT = "t1"
KG = "k1"
ONTOLOGY = "Type: SynthWidget\n  - code (string)"
ENHANCED_BODY = "Enhanced SynthWidget guidance for entitled workspaces only."
SKILL_MARKERS = ("TYPE SKILLS", "Skill:")


@pytest.fixture(autouse=True)
def _clean_skill_state():
    reset_skill_layers()
    reset_type_skill_store()
    reset_global_type_skill_store()
    register_entitlement_checker(None)
    yield
    reset_skill_layers()
    reset_type_skill_store()
    reset_global_type_skill_store()
    register_entitlement_checker(None)


class _FakeNeptune:
    async def query(self, q):
        return {"head": {"vars": []}, "results": {"bindings": []}}


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"cypher":"RETURN 1","sparql":"SELECT 1",'
                            '"explanation":"x","functions_needed":[]}'
                        )
                    }
                }
            ],
            "usage": {},
            "model": "google/gemini-2.5-flash",
        }


def _patch_httpx(captured: dict):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _Resp()

    return patch("httpx.AsyncClient", _Client)


def _user_from_captured(captured: dict) -> str:
    msgs = (captured.get("json") or {}).get("messages") or []
    for m in msgs:
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _ctx(**kw):
    return AgentContext(
        tenant_id=TENANT,
        kg_name=KG,
        neptune=_FakeNeptune(),
        type_name=kw.pop("type_name", WIDGET),
        openrouter_key="fake-key",
        **kw,
    )


def _entitle() -> None:
    register_entitlement_checker(
        lambda t: bool(getattr(t, "enhanced_entitled", False))
    )


def _tenant(*, entitled: bool) -> TenantContext:
    return TenantContext(tenant_id=TENANT, api_key="k", enhanced_entitled=entitled)


def _seed_enhanced() -> None:
    register_skill_layer(
        Layer.ENHANCED,
        [
            TypeSkill(
                slug="enhanced-meaning",
                type_name=WIDGET,
                body=ENHANCED_BODY,
                title="Enhanced meaning",
                layer=Layer.ENHANCED,
            )
        ],
    )


def _assert_gated(text: str, entitled: bool) -> None:
    if entitled:
        assert ENHANCED_BODY in text
        assert "TYPE SKILLS" in text
    else:
        assert ENHANCED_BODY not in text
        for marker in SKILL_MARKERS:
            assert marker not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("entitled", [True, False])
async def test_enhanced_skill_in_list_type_schema_iff_entitled(entitled):
    _seed_enhanced()
    _entitle()
    schema = await list_type_schema(
        _FakeNeptune(), TENANT, WIDGET, tenant=_tenant(entitled=entitled)
    )
    _assert_gated(schema.get("skills") or "", entitled)


@pytest.mark.asyncio
@pytest.mark.parametrize("entitled", [True, False])
async def test_enhanced_skill_in_cypher_ontology_iff_entitled(entitled):
    from infona_client.agent.capabilities.query import QueryCapability

    _seed_enhanced()
    _entitle()
    tenant = _tenant(entitled=entitled)
    pipe = QueryCapability()._build_pipeline(_ctx(extras={"tenant": tenant}))
    assert pipe._tenant_ctx is tenant
    pipe._openrouter_key = "sk-or-test"
    pipe._cerebras_key = ""
    pipe._query_provider = "openrouter"
    pipe._query_model = "google/gemini-2.5-flash"
    pipe.anthropic = None
    captured: dict = {}
    with _patch_httpx(captured):
        out = await pipe._try_llm_cypher(
            "how many?", ONTOLOGY, tenant_id=TENANT, kg_name=KG
        )
    assert out is not None
    _assert_gated(_user_from_captured(captured), entitled)


@pytest.mark.asyncio
@pytest.mark.parametrize("entitled", [True, False])
async def test_enhanced_skill_in_extractor_prompt_iff_entitled(monkeypatch, entitled):
    from infona_client.agent.capabilities.enrich_cap import EnrichCapability
    from infona_client.agent.capabilities.normalize_cap import NormalizeCapability
    from infona_client.agent.capabilities.ontology_cap import OntologyCapability

    _seed_enhanced()
    _entitle()
    ctx = _ctx(extras={"tenant": _tenant(entitled=entitled)})
    captured: dict = {}

    async def fake_chat(*args, **kwargs):
        captured["user"] = args[2] if len(args) > 2 else kwargs.get("user", "")
        return (
            '{"attributes": ["code"], "scope": null, "tier": "lite",'
            ' "confidence_min": 0.85, "rule_type": null, "predicate": null,'
            ' "op": "inspect", "confidence": 0.9}'
        )

    async def fake_types(_ctx):
        return [WIDGET]

    monkeypatch.setattr(
        "infona_client.agent.capabilities.enrich_cap.openrouter_chat", fake_chat
    )
    monkeypatch.setattr(
        "infona_client.agent.capabilities.enrich_cap._list_types", fake_types
    )
    await EnrichCapability().plan(ctx, "fill in the code")
    _assert_gated(captured["user"], entitled)

    captured.clear()
    monkeypatch.setattr(
        "infona_client.agent.capabilities.normalize_cap.openrouter_chat", fake_chat
    )
    await NormalizeCapability().plan(ctx, "clean the code")
    _assert_gated(captured["user"], entitled)

    captured.clear()
    monkeypatch.setattr(
        "infona_client.agent.capabilities.ontology_cap.openrouter_chat", fake_chat
    )
    await OntologyCapability()._extract_directive(ctx, "show the schema")
    _assert_gated(captured["user"], entitled)


@pytest.mark.asyncio
async def test_enrich_execute_pipeline_threads_tenant_ctx(monkeypatch):
    from infona_client.agent.capabilities.enrich_execute import EnrichExecuteMixin

    seen: list = []

    class _FakePipe:
        def __init__(self, *a, **k):
            self._tenant_ctx = "unset"

        async def select_entity_uris(self, *a, **k):
            seen.append(self._tenant_ctx)
            return []

    monkeypatch.setattr("infona_client.nlp.pipeline.NLQueryPipeline", _FakePipe)
    tenant = _tenant(entitled=True)
    await EnrichExecuteMixin()._resolve_subset_uris(
        _ctx(extras={"tenant": tenant}),
        WIDGET,
        {"description": "top one", "limit": 1},
    )
    assert seen == [tenant]
