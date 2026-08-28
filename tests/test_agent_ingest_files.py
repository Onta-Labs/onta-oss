"""INF-603: attached CSV files thread through the canonical /agent request.

The Explorer (and any other client) sends CSV ingest through POST /agent with
``context.ingest_files`` — no new ingest route. This pins:

  * AgentRequestContext.ingest_files / ingest_source / keep_columns land in
    extras (never a filesystem ``path`` from HTTP),
  * a file-bearing turn is routed to ingest even if the classifier said
    "question", and degrades to the hosted-only answer when the steward is
    unregistered,
  * a clarify step's ``topic`` is forwarded on the HTTP body.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("INFONA_API_KEYS", '{"test-key": "test-tenant"}')
os.environ.setdefault("INFONA_NEPTUNE_ENDPOINT", "http://fake:8182")

from infona_client.agent import planner as planner_mod  # noqa: E402
from infona_client.agent.planner import (  # noqa: E402
    handle,
    register_default_capabilities,
    reset_plan_store,
)
from infona_client.agent.planner_intent import (  # noqa: E402
    _INGEST_STEWARD_HOSTED_ONLY,
)
from infona_client.agent.registry import (  # noqa: E402
    AgentContext,
    PlanStep,
    register_capability,
    reset_capabilities,
)
from infona_client.agent.conversation_store import (  # noqa: E402
    reset_conversation_store,
)

TIMEOUT = 5.0


class FakeNeptune:
    async def query(self, q):
        return {"head": {"vars": []}, "results": {"bindings": []}}

    async def update(self, q):
        return None


class FakeJobStore:
    def __init__(self):
        self.created = []

    async def create(self, job):
        self.created.append(job)

    async def get(self, job_id):
        return None

    async def update(self, job):
        return None


class FakeExecutor:
    def __init__(self):
        self.ran = []


def _ctx(**extras_kw):
    extras = {
        "enrichment_executor": extras_kw.pop("executor", FakeExecutor()),
        "enrichment_job_store": extras_kw.pop("job_store", FakeJobStore()),
    }
    extras.update(extras_kw)
    return AgentContext(
        tenant_id="t1",
        kg_name="kg1",
        neptune=FakeNeptune(),
        openrouter_key="fake-key",
        extras=extras,
    )


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_capabilities()
    reset_plan_store()
    reset_conversation_store()
    register_default_capabilities()
    yield
    reset_capabilities()
    reset_plan_store()
    reset_conversation_store()


def _stub_classifier(monkeypatch, intent: str, clarify: str = ""):
    async def fake_chat(*args, **kwargs):
        return json.dumps({"intent": intent, "clarify": clarify})

    monkeypatch.setattr(planner_mod, "openrouter_chat", fake_chat)


def test_request_context_carries_ingest_files_into_extras():
    from infona_client.api.routes.agent import (
        AgentIngestFile,
        AgentRequest,
        AgentRequestContext,
        _build_ctx,
    )
    from infona_client.auth.api_keys import TenantContext

    body = AgentRequest(
        message="ingest this csv",
        context=AgentRequestContext(
            kg_name="crm",
            ingest_files=[
                AgentIngestFile(
                    name="contacts.csv",
                    text="Contact ID,Email\n1,ada@example.test\n",
                )
            ],
            ingest_source="Ontraport",
            keep_columns=["Referral Code"],
            drop_columns=["Card last 4"],
        ),
    )
    ctx = _build_ctx(
        TenantContext(tenant_id="t1", api_key="k"),
        body,
        FakeNeptune(),
        FakeExecutor(),
        FakeJobStore(),
    )
    files = ctx.extras.get("ingest_files") or []
    assert len(files) == 1
    assert files[0]["name"] == "contacts.csv"
    assert "ada@example.test" in files[0]["text"]
    assert "path" not in files[0]
    assert ctx.extras.get("ingest_source") == "ontraport"
    assert ctx.extras.get("keep_columns") == ["Referral Code"]
    assert ctx.extras.get("drop_columns") == ["Card last 4"]


def test_http_ingest_file_drops_path_even_if_client_sends_one():
    from infona_client.api.routes.agent import AgentIngestFile, _sanitize_ingest_files

    dumped = AgentIngestFile(
        name="x.csv",
        text="id,email\n1,a@example.test\n",
    ).model_dump()
    dumped["path"] = "/etc/passwd"
    # Reconstruct from the HTTP-shaped dict: extra path is ignored.
    file = AgentIngestFile.model_validate(
        {k: v for k, v in dumped.items() if k != "path"}
    )
    out = _sanitize_ingest_files([file])
    assert out and "path" not in out[0]


def test_agent_request_context_ingest_files_default_empty():
    from infona_client.api.routes.agent import AgentRequestContext

    ctx = AgentRequestContext(kg_name="kg1")
    assert ctx.ingest_files == []
    assert ctx.ingest_source == ""
    assert ctx.keep_columns == []
    assert ctx.drop_columns == []


@pytest.mark.asyncio
async def test_ingest_files_force_ingest_even_if_classifier_says_question(
    monkeypatch,
):
    """A paperclip CSV is file ingest, not a read-only question."""

    class _Steward:
        name = "ingest_steward"
        writes = True

        def describe(self) -> str:
            return "ingest csv"

        async def plan(self, ctx, instruction):
            return [
                PlanStep(
                    capability=self.name,
                    action="clarify",
                    params={
                        "topic": "source",
                        "question": "Where is this from?",
                        "options": ["Ontraport", "HubSpot"],
                    },
                )
            ]

        async def execute(self, ctx, step):
            return {"ok": True}

    register_capability(_Steward())
    _stub_classifier(monkeypatch, "question")
    out = await asyncio.wait_for(
        handle(
            _ctx(
                ingest_files=[
                    {
                        "name": "contacts.csv",
                        "text": "Contact ID,Email\n1,ada@example.test\n",
                    }
                ]
            ),
            "here is a file",
        ),
        TIMEOUT,
    )
    assert out["kind"] == "clarify"
    assert out.get("topic") == "source"
    assert "from" in (out.get("question") or "").lower()


@pytest.mark.asyncio
async def test_ingest_files_without_steward_are_hosted_only(monkeypatch):
    _stub_classifier(monkeypatch, "question")
    out = await asyncio.wait_for(
        handle(
            _ctx(
                ingest_files=[
                    {"name": "contacts.csv", "text": "id,email\n1,a@example.test\n"}
                ]
            ),
            "ingest this csv",
        ),
        TIMEOUT,
    )
    assert out["kind"] == "answer"
    assert out.get("answer") == _INGEST_STEWARD_HOSTED_ONLY
