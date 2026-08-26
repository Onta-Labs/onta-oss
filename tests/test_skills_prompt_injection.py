"""Always-on skills injection into /ask generation, list_type_schema, classifier.

Hermetic. Uses SynthWidget only — never production type names.
Empty store must leave prompts byte-identical (no TYPE SKILLS / Skill: headers).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from infona_client.agent.planner_classify import _classify
from infona_client.agent.registry import AgentContext
from infona_client.graph.layers import Layer
from infona_client.nlp import pipeline as pipeline_mod
from infona_client.nlp.prompts import (
    build_cypher_generation_prompt,
    build_generation_prompt,
)
from infona_client.normalization.inference import list_type_schema
from infona_client.skills import (
    TypeSkill,
    make_type_skill_store,
    reset_global_type_skill_store,
    reset_skill_layers,
    reset_type_skill_store,
)

WIDGET = "SynthWidget"
TENANT = "t1"
KG = "k1"
WIDGET_BODY = "A SynthWidget is a synthetic fixture type used only in tests."
ONTOLOGY = "Type: SynthWidget\n  - code (string)"
SKILL_MARKERS = ("TYPE SKILLS", "Skill:")


@pytest.fixture(autouse=True)
def _clean_skill_state():
    reset_skill_layers()
    reset_type_skill_store()
    reset_global_type_skill_store()
    yield
    reset_skill_layers()
    reset_type_skill_store()
    reset_global_type_skill_store()


class _FakeNeptune:
    async def query(self, q):
        return {"head": {"vars": []}, "results": {"bindings": []}}


def _pipe():
    pipe = pipeline_mod.NLQueryPipeline.__new__(pipeline_mod.NLQueryPipeline)
    pipe._openrouter_key = "sk-or-test"
    pipe._cerebras_key = ""
    pipe._query_provider = "openrouter"
    pipe._query_model = "google/gemini-2.5-flash"
    pipe.anthropic = None
    return pipe


def _ctx(**kw):
    return AgentContext(
        tenant_id=TENANT,
        kg_name=KG,
        neptune=_FakeNeptune(),
        type_name=kw.pop("type_name", WIDGET),
        openrouter_key="fake-key",
        **kw,
    )


async def _seed_widget_skill(body: str = WIDGET_BODY) -> TypeSkill:
    return await make_type_skill_store().upsert(
        TypeSkill(
            slug="meaning",
            type_name=WIDGET,
            body=body,
            title="Meaning",
            layer=Layer.TENANT,
            tenant_id=TENANT,
        )
    )


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


# --------------------------------------------------------------------------- #
# Empty store → byte-identical prompts
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_store_cypher_prompt_unchanged():
    expected = build_cypher_generation_prompt(
        "how many?", ONTOLOGY, tenant_id=TENANT, kg_name=KG
    )
    captured: dict = {}
    with _patch_httpx(captured):
        out = await _pipe()._try_llm_cypher(
            "how many?", ONTOLOGY, tenant_id=TENANT, kg_name=KG
        )
    assert out is not None
    user = _user_from_captured(captured)
    assert user == expected
    for marker in SKILL_MARKERS:
        assert marker not in user


@pytest.mark.asyncio
async def test_empty_store_sparql_prompt_unchanged():
    graph = f"https://graph.infona.ai/graphs/{TENANT}/kg/{KG}"
    expected = build_generation_prompt(
        "how many?", ONTOLOGY, graph, kg_name=KG
    )
    captured: dict = {}

    async def fake_or(prompt):
        captured["prompt"] = prompt
        return {"sparql": "SELECT 1"}

    pipe = _pipe()
    pipe._generate_via_openrouter = fake_or  # type: ignore[method-assign]
    out = await pipe._generate_sparql("how many?", ONTOLOGY, graph)
    assert out is not None
    assert captured["prompt"] == expected
    for marker in SKILL_MARKERS:
        assert marker not in captured["prompt"]


@pytest.mark.asyncio
async def test_empty_store_list_type_schema_skills_empty():
    schema = await list_type_schema(_FakeNeptune(), TENANT, WIDGET)
    assert schema.get("skills") == ""
    assert "attributes" in schema
    assert "relationships" in schema
    for marker in SKILL_MARKERS:
        assert marker not in (schema.get("skills") or "")


@pytest.mark.asyncio
async def test_empty_store_classifier_prompt_unchanged(monkeypatch):
    captured: dict = {}

    async def fake_chat(key, system, user, **kw):
        captured["user"] = user
        return '{"intent": "question"}'

    monkeypatch.setattr(
        "infona_client.agent.planner.openrouter_chat", fake_chat
    )
    empty_user: dict = {}

    async def fake_chat_empty(key, system, user, **kw):
        empty_user["user"] = user
        return '{"intent": "question"}'

    # Snapshot with no type_name (injection skipped) vs type_name + empty store.
    monkeypatch.setattr(
        "infona_client.agent.planner.openrouter_chat", fake_chat_empty
    )
    await _classify(_ctx(type_name=None), "count them")
    baseline = empty_user["user"]

    monkeypatch.setattr(
        "infona_client.agent.planner.openrouter_chat", fake_chat
    )
    await _classify(_ctx(type_name=WIDGET), "count them")
    user = captured["user"]
    assert user == baseline
    for marker in SKILL_MARKERS:
        assert marker not in user
        assert marker not in baseline


# --------------------------------------------------------------------------- #
# One tenant skill on SynthWidget
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tenant_skill_lands_in_cypher_prompt():
    await _seed_widget_skill()
    captured: dict = {}
    with _patch_httpx(captured):
        out = await _pipe()._try_llm_cypher(
            "how many?", ONTOLOGY, tenant_id=TENANT, kg_name=KG
        )
    assert out is not None
    user = _user_from_captured(captured)
    assert WIDGET_BODY in user
    assert "TYPE SKILLS" in user
    assert WIDGET in user


@pytest.mark.asyncio
async def test_tenant_skill_lands_in_sparql_prompt():
    await _seed_widget_skill()
    graph = f"https://graph.infona.ai/graphs/{TENANT}/kg/{KG}"
    captured: dict = {}

    async def fake_or(prompt):
        captured["prompt"] = prompt
        return {"sparql": "SELECT 1"}

    pipe = _pipe()
    pipe._generate_via_openrouter = fake_or  # type: ignore[method-assign]
    await pipe._generate_sparql("how many?", ONTOLOGY, graph)
    assert WIDGET_BODY in captured["prompt"]
    assert "TYPE SKILLS" in captured["prompt"]


@pytest.mark.asyncio
async def test_tenant_skill_lands_in_list_type_schema():
    await _seed_widget_skill()
    schema = await list_type_schema(_FakeNeptune(), TENANT, WIDGET)
    assert WIDGET_BODY in (schema.get("skills") or "")
    assert "TYPE SKILLS" in schema["skills"]


@pytest.mark.asyncio
async def test_tenant_skill_lands_in_classifier_when_type_set(monkeypatch):
    await _seed_widget_skill()
    captured: dict = {}

    async def fake_chat(key, system, user, **kw):
        captured["user"] = user
        return '{"intent": "question"}'

    monkeypatch.setattr(
        "infona_client.agent.planner.openrouter_chat", fake_chat
    )
    await _classify(_ctx(type_name=WIDGET), "count them")
    assert WIDGET_BODY in captured["user"]
    assert "TYPE SKILLS" in captured["user"]


@pytest.mark.asyncio
async def test_classifier_skips_skills_when_type_name_unset(monkeypatch):
    await _seed_widget_skill()
    captured: dict = {}

    async def fake_chat(key, system, user, **kw):
        captured["user"] = user
        return '{"intent": "question"}'

    monkeypatch.setattr(
        "infona_client.agent.planner.openrouter_chat", fake_chat
    )
    await _classify(_ctx(type_name=None), "count them")
    assert WIDGET_BODY not in captured["user"]
    for marker in SKILL_MARKERS:
        assert marker not in captured["user"]


@pytest.mark.asyncio
async def test_enrich_extractor_includes_nonempty_skills(monkeypatch):
    from infona_client.agent.capabilities.enrich_extract import (
        _extract_enrich_request,
    )

    captured: dict = {}

    async def fake_chat(*args, **kwargs):
        captured["user"] = args[2] if len(args) > 2 else kwargs.get("user", "")
        return (
            '{"attributes": ["code"], "scope": null, "tier": "lite",'
            ' "confidence_min": 0.85}'
        )

    monkeypatch.setattr(
        "infona_client.agent.capabilities.enrich_cap.openrouter_chat", fake_chat
    )
    schema = {
        "attributes": ["code"],
        "relationships": [],
        "skills": f"## TYPE SKILLS\n{WIDGET_BODY}",
    }
    await _extract_enrich_request(
        _ctx(), "fill in the code", WIDGET, schema
    )
    assert WIDGET_BODY in captured["user"]
    assert "TYPE SKILLS" in captured["user"]


@pytest.mark.asyncio
async def test_enrich_extractor_omits_empty_skills(monkeypatch):
    from infona_client.agent.capabilities.enrich_extract import (
        _EXTRACT_USER_TEMPLATE,
        _extract_enrich_request,
    )

    captured: dict = {}

    async def fake_chat(*args, **kwargs):
        captured["user"] = args[2] if len(args) > 2 else kwargs.get("user", "")
        return (
            '{"attributes": ["code"], "scope": null, "tier": "lite",'
            ' "confidence_min": 0.85}'
        )

    monkeypatch.setattr(
        "infona_client.agent.capabilities.enrich_cap.openrouter_chat", fake_chat
    )
    schema = {"attributes": ["code"], "relationships": []}
    await _extract_enrich_request(
        _ctx(), "fill in the code", WIDGET, schema
    )
    expected = _EXTRACT_USER_TEMPLATE.format(
        type_name=WIDGET,
        attributes="code",
        relationships="(none)",
        skills="",
        instruction="fill in the code",
    )
    assert captured["user"] == expected
    for marker in SKILL_MARKERS:
        assert marker not in captured["user"]


# --------------------------------------------------------------------------- #
# Store failure → still returns a prompt (never raises)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_store_failure_cypher_still_returns_prompt():
    await _seed_widget_skill()
    captured: dict = {}
    with patch(
        "infona_client.skills.resolve.resolve_skills",
        side_effect=RuntimeError("boom"),
    ), _patch_httpx(captured):
        out = await _pipe()._try_llm_cypher(
            "how many?", ONTOLOGY, tenant_id=TENANT, kg_name=KG
        )
    assert out is not None
    user = _user_from_captured(captured)
    assert user
    assert WIDGET_BODY not in user
    for marker in SKILL_MARKERS:
        assert marker not in user


@pytest.mark.asyncio
async def test_store_failure_list_type_schema_still_returns():
    with patch(
        "infona_client.skills.resolve.resolve_skills",
        side_effect=RuntimeError("boom"),
    ):
        schema = await list_type_schema(_FakeNeptune(), TENANT, WIDGET)
    assert "attributes" in schema
    assert schema.get("skills") == ""


@pytest.mark.asyncio
async def test_store_failure_classifier_still_returns(monkeypatch):
    await _seed_widget_skill()
    captured: dict = {}

    async def fake_chat(key, system, user, **kw):
        captured["user"] = user
        return '{"intent": "question"}'

    monkeypatch.setattr(
        "infona_client.agent.planner.openrouter_chat", fake_chat
    )
    with patch(
        "infona_client.skills.resolve.resolve_skills",
        side_effect=RuntimeError("boom"),
    ):
        out = await _classify(_ctx(type_name=WIDGET), "count them")
    assert out["intents"]
    assert WIDGET_BODY not in captured["user"]
    for marker in SKILL_MARKERS:
        assert marker not in captured["user"]
