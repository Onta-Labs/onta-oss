"""A ``reader`` member may not write through ANY route (ONTA-451).

Infona membership v1 (infona-oss#257) defines the ``reader`` role and enforces it
with ``require_tenant_write``, but four mutating surfaces were never wired to
that guard and took a plain ``Depends(get_tenant)``, so a read-only member could
still mutate the workspace:

* ``POST /graphs/{t}/agent`` — the unified agent turn, which dispatches to
  web_ingest / enrich / normalize / dedup / ontology,
* ``POST /graphs/{t}/explore/kgs/{kg}/er-rebuild`` and ``…/recompute-stats``,
* ``POST/PATCH/DELETE /graphs/{t}/skills…``,
* ``POST/DELETE /graphs/{t}/functions``.

These tests pin the fix at the ROUTE level — a reader gets 403 from each — plus
the two properties that make ``/agent`` different from the rest:

* it is the one READ/WRITE MIXED route, so the gate is at CAPABILITY DISPATCH
  rather than a blanket route dependency: a reader keeps question / research /
  clarify turns and loses only the mutations, and
* a denied ``confirm`` must not consume the plan's one-shot execution claim.

Everything is stubbed (fake verifier, in-memory workspace + plan stores, fake
capabilities, mocked Neptune) so the suite stays offline and deterministic.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from infona_client.agent import planner as planner_mod
from infona_client.agent.planner import (
    make_plan_store,
    register_default_capabilities,
    reset_plan_store,
)
from infona_client.agent.registry import (
    PlanStep,
    register_capability,
    reset_capabilities,
)
from infona_client.api.app import create_app
from infona_client.auth.api_keys import AuthVerdict, register_external_verifier
from infona_client.auth.workspace_store import (
    make_workspace_store,
    reset_workspace_store,
)
from infona_client.graph.client import NeptuneClient

_run = asyncio.run

TENANT = "gate-ws"
KG = "kg1"

# Neither key is in the static ``INFONA_API_KEYS`` map (conftest maps only
# "test-key"), so both fall through to the fake verifier below and therefore
# carry a SUBJECT — which is what makes membership-role resolution kick in.
# A static/subject-less key resolves to ``writer`` by design (back-compat).
READER_KEY = {"X-API-Key": "key-reader"}
OWNER_KEY = {"X-API-Key": "key-owner"}

_SUBJECTS = {"key-reader": "user_reader", "key-owner": "user_owner"}


def _fake_verifier(key):
    """Both users are granted the tenant; only their ROLE differs."""
    subject = _SUBJECTS.get(key)
    if subject is None:
        return None
    return AuthVerdict(tenants=[TENANT], subject=subject)


@pytest.fixture(autouse=True)
def _membership():
    """``user_owner`` owns the workspace; ``user_reader`` is a read-only member."""
    reset_workspace_store()
    reset_capabilities()
    reset_plan_store()
    register_external_verifier(_fake_verifier)
    store = make_workspace_store()
    _run(store.claim_workspace(TENANT, "user_owner", "Gate WS"))
    _run(store.add_member(TENANT, "user_reader", "reader"))
    register_default_capabilities()
    yield
    register_external_verifier(None)
    reset_workspace_store()
    reset_capabilities()
    reset_plan_store()


@pytest.fixture
def gate_client(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    app = create_app()
    neptune = AsyncMock(spec=NeptuneClient)
    neptune.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    neptune.update.return_value = None
    # Fail-open kg probe so SCOPE_REQUIRE does not clarify missing before the
    # write gate under test (ONTA-534 GraphStore-first would MISSING an empty store).
    async def _ask_ok(sparql: str) -> bool:
        return True

    neptune.ask.side_effect = _ask_ok
    app.state.neptune_client = neptune
    tc = TestClient(app)
    app.state.neptune_client = neptune  # re-inject after lifespan
    return tc


def _assert_read_only_403(response) -> None:
    assert response.status_code == 403, response.text
    assert "read-only" in response.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Fake capabilities — a mutating one and a read-only one, so the /agent tests
# assert the GATE rather than the behavior of the real engines.
# --------------------------------------------------------------------------- #
class _FakeMutatingCap:
    """Stands in for normalize/enrich/dedup/ontology/web_ingest.

    Declares no ``writes`` attribute on purpose: the classifier is
    deny-by-default, so "didn't say" must be treated as "mutates".
    """

    name = "normalize"

    def describe(self) -> str:
        return "fake mutating capability"

    async def plan(self, ctx, instruction):
        return [PlanStep(capability=self.name, action="apply_rule", params={})]

    async def execute(self, ctx, step):
        return {"capability": self.name, "ran": True}


class _FakeReadOnlyCap:
    """Stands in for web_research: plans and executes without writing."""

    name = "web_research"
    writes = False

    def describe(self) -> str:
        return "fake read-only capability"

    async def plan(self, ctx, instruction):
        return [PlanStep(capability=self.name, action="research", params={})]

    async def execute(self, ctx, step):
        return {"capability": self.name, "ran": True}


def _stub_classifier(monkeypatch, intent: str):
    async def fake_chat(*a, **k):
        return json.dumps({"intent": intent, "clarify": ""})

    monkeypatch.setattr(planner_mod, "openrouter_chat", fake_chat)


def _agent_body(message: str = "", *, confirm_plan_id: str | None = None) -> dict:
    body: dict = {"message": message, "context": {"kg_name": KG}}
    if confirm_plan_id is not None:
        body["confirm"] = {"plan_id": confirm_plan_id}
    return body


def _post_agent(client, headers, **kw):
    return client.post(f"/graphs/{TENANT}/agent", json=_agent_body(**kw), headers=headers)


# --------------------------------------------------------------------------- #
# 1. POST /agent — the read/write MIXED route.
# --------------------------------------------------------------------------- #
def test_agent_mutating_turn_denied_for_reader(gate_client, monkeypatch):
    """A turn that would commit a mutating plan is 403 for a reader."""
    register_capability(_FakeMutatingCap())
    _stub_classifier(monkeypatch, "clean")

    r = _post_agent(gate_client, READER_KEY, message="clean up the names")
    _assert_read_only_403(r)


def test_agent_mutating_turn_persists_no_plan_for_reader(gate_client, monkeypatch):
    """The denial happens BEFORE the plan is persisted — nothing to confirm later."""
    register_capability(_FakeMutatingCap())
    _stub_classifier(monkeypatch, "clean")

    _post_agent(gate_client, READER_KEY, message="clean up the names")
    assert _run(make_plan_store().list_for_tenant(TENANT)) == []


def test_agent_mutating_turn_allowed_for_owner(gate_client, monkeypatch):
    """Positive control: the SAME turn from a writer still plans (not a blanket 403)."""
    register_capability(_FakeMutatingCap())
    _stub_classifier(monkeypatch, "clean")

    r = _post_agent(gate_client, OWNER_KEY, message="clean up the names")
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "plan"


def test_agent_question_turn_still_allowed_for_reader(gate_client, monkeypatch):
    """A reader keeps read-only turns — the whole reason the gate isn't on the route."""
    from infona_client.agent.capabilities.query import QueryCapability

    async def fake_answer(self, ctx, q):
        return {"answer": "42", "sparql": "SELECT ...", "rows": [], "narrative": ""}

    monkeypatch.setattr(QueryCapability, "answer", fake_answer)
    _stub_classifier(monkeypatch, "question")

    r = _post_agent(gate_client, READER_KEY, message="how many mentors are there?")
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "42"


def test_agent_read_only_capability_plan_allowed_for_reader(gate_client, monkeypatch):
    """The gate is per-CAPABILITY: a ``writes = False`` capability still plans+runs."""
    register_capability(_FakeReadOnlyCap())
    _stub_classifier(monkeypatch, "research")

    r = _post_agent(gate_client, READER_KEY, message="research pricing on the web")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["kind"] == "plan"

    confirmed = _post_agent(gate_client, READER_KEY, confirm_plan_id=plan["plan_id"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["kind"] == "result"


def test_agent_ontology_inspection_still_allowed_for_reader(gate_client, monkeypatch):
    """A read-only ANSWER from a write-classified capability is not a mutation.

    ``ontology`` mutates on ``declare_*`` but its ``inspect`` op returns an
    ``action="answer"`` step that the planner surfaces directly, never persisting
    or executing anything. The gate sits after that short-circuit precisely so a
    reader keeps schema inspection.
    """

    class _InspectOnlyOntologyCap(_FakeMutatingCap):
        name = "ontology"

        async def plan(self, ctx, instruction):
            return [
                PlanStep(
                    capability=self.name,
                    action="answer",
                    params={"answer_payload": {"types": ["Mentor"]}},
                )
            ]

    register_capability(_InspectOnlyOntologyCap())
    _stub_classifier(monkeypatch, "ontology")

    r = _post_agent(gate_client, READER_KEY, message="what types are in the schema?")
    assert r.status_code == 200, r.text
    assert r.json() == {"kind": "answer", "types": ["Mentor"]}


def test_agent_confirm_of_mutating_plan_denied_for_reader(gate_client, monkeypatch):
    """A reader cannot execute a mutating plan a writer proposed."""
    register_capability(_FakeMutatingCap())
    _stub_classifier(monkeypatch, "clean")

    planned = _post_agent(gate_client, OWNER_KEY, message="clean up the names")
    assert planned.status_code == 200, planned.text
    plan_id = planned.json()["plan_id"]

    denied = _post_agent(gate_client, READER_KEY, confirm_plan_id=plan_id)
    _assert_read_only_403(denied)


def test_agent_confirm_of_unknown_plan_fails_closed_for_reader(gate_client):
    """A plan the gate cannot READ is one it cannot prove read-only → refuse.

    A writer still gets the ordinary ``plan not found`` response, so this is a
    property of the read-only path, not a new error for everyone.
    """
    _assert_read_only_403(_post_agent(gate_client, READER_KEY, confirm_plan_id="nope"))

    writer = _post_agent(gate_client, OWNER_KEY, confirm_plan_id="nope")
    assert writer.status_code == 200, writer.text
    assert writer.json()["error"] == "plan not found"


def test_agent_denied_confirm_does_not_burn_the_one_shot_claim(gate_client, monkeypatch):
    """A reader's refused confirm must leave the plan runnable by a writer.

    The gate runs BEFORE ``claim_for_execution`` precisely so a denial cannot
    move the plan to ``executing`` and strand it (the one-shot guard would then
    refuse the legitimate confirm too).
    """
    register_capability(_FakeMutatingCap())
    _stub_classifier(monkeypatch, "clean")

    plan_id = _post_agent(gate_client, OWNER_KEY, message="clean up the names").json()[
        "plan_id"
    ]
    _assert_read_only_403(_post_agent(gate_client, READER_KEY, confirm_plan_id=plan_id))

    assert _run(make_plan_store().get(plan_id, TENANT)).status == "proposed"

    ran = _post_agent(gate_client, OWNER_KEY, confirm_plan_id=plan_id)
    assert ran.status_code == 200, ran.text
    assert ran.json()["kind"] == "result"
    assert ran.json()["steps"][0]["status"] == "ok"


# --------------------------------------------------------------------------- #
# 2. POST /explore/kgs/{kg}/er-rebuild + /recompute-stats
# --------------------------------------------------------------------------- #
def test_er_rebuild_denied_for_reader(gate_client):
    _assert_read_only_403(
        gate_client.post(
            f"/graphs/{TENANT}/explore/kgs/{KG}/er-rebuild", headers=READER_KEY
        )
    )


def test_recompute_stats_denied_for_reader(gate_client):
    _assert_read_only_403(
        gate_client.post(
            f"/graphs/{TENANT}/explore/kgs/{KG}/recompute-stats", headers=READER_KEY
        )
    )


def test_recompute_stats_allowed_for_owner(gate_client):
    r = gate_client.post(
        f"/graphs/{TENANT}/explore/kgs/{KG}/recompute-stats", headers=OWNER_KEY
    )
    assert r.status_code == 200, r.text


def test_explore_reads_still_allowed_for_reader(gate_client):
    """Only the two mutating explore routes moved — reads are untouched."""
    r = gate_client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/schema", headers=READER_KEY
    )
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 3. Skills CRUD
# --------------------------------------------------------------------------- #
def _skill_body() -> dict:
    return {
        "type_name": "Mentor",
        "slug": "greeting",
        "title": "Greeting",
        "body": "Say hello before answering.",
    }


def test_create_skill_denied_for_reader(gate_client):
    _assert_read_only_403(
        gate_client.post(
            f"/graphs/{TENANT}/skills", json=_skill_body(), headers=READER_KEY
        )
    )


def test_update_skill_denied_for_reader(gate_client):
    created = gate_client.post(
        f"/graphs/{TENANT}/skills", json=_skill_body(), headers=OWNER_KEY
    )
    assert created.status_code == 201, created.text

    _assert_read_only_403(
        gate_client.patch(
            f"/graphs/{TENANT}/skills/Mentor/greeting",
            json={"title": "Hijacked"},
            headers=READER_KEY,
        )
    )


def test_delete_skill_denied_for_reader(gate_client):
    gate_client.post(f"/graphs/{TENANT}/skills", json=_skill_body(), headers=OWNER_KEY)

    _assert_read_only_403(
        gate_client.delete(
            f"/graphs/{TENANT}/skills/Mentor/greeting", headers=READER_KEY
        )
    )


def test_skill_reads_and_validate_still_allowed_for_reader(gate_client):
    """``validate`` writes nothing, so it stays on plain ``get_tenant``."""
    assert gate_client.get(f"/graphs/{TENANT}/skills", headers=READER_KEY).status_code == 200
    validated = gate_client.post(
        f"/graphs/{TENANT}/skills/validate", json=_skill_body(), headers=READER_KEY
    )
    assert validated.status_code == 200, validated.text


# --------------------------------------------------------------------------- #
# 4. POST /functions
# --------------------------------------------------------------------------- #
def _function_body() -> dict:
    return {
        "entity_type": "Mentor",
        "name": "score",
        "endpoint_url": "https://example.test/score",
    }


def test_register_function_denied_for_reader(gate_client):
    _assert_read_only_403(
        gate_client.post(
            f"/graphs/{TENANT}/functions", json=_function_body(), headers=READER_KEY
        )
    )


def test_register_function_allowed_for_owner(gate_client):
    r = gate_client.post(
        f"/graphs/{TENANT}/functions", json=_function_body(), headers=OWNER_KEY
    )
    assert r.status_code == 201, r.text


def test_list_functions_still_allowed_for_reader(gate_client):
    r = gate_client.get(f"/graphs/{TENANT}/functions", headers=READER_KEY)
    assert r.status_code == 200, r.text


def test_delete_function_denied_for_reader(gate_client):
    _assert_read_only_403(
        gate_client.delete(
            f"/graphs/{TENANT}/functions/score",
            params={"entity_type": "Mentor"},
            headers=READER_KEY,
        )
    )


def test_delete_function_allowed_for_owner(gate_client):
    r = gate_client.delete(
        f"/graphs/{TENANT}/functions/score",
        params={"entity_type": "Mentor"},
        headers=OWNER_KEY,
    )
    # Writer is not 403: 200 if a prior test registered it, 404 if not.
    assert r.status_code in (200, 404), r.text
