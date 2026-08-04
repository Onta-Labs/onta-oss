"""Drift guard: every MUTATING surface enforces the workspace WRITE capability.

ONTA-451 (onta-oss#288) fixed the four routes that were unguarded when it was
written, and ``tests/test_reader_write_gate_routes.py`` pins those four by name.
This file exists because naming them is exactly how the gap happened:
``require_tenant_write`` was wired into a *list* of route modules, so every
mutating route added afterwards silently defaulted to unguarded, and a reader
who could not call ``POST /ingest`` directly could ask ``POST /agent`` to run
the same ingest. So the two files do different jobs, and this one is the
DENY-BY-DEFAULT half: it fails on the NEXT unguarded route, which nobody has
written yet.

Shape (modelled on ``test_write_path_convergence.py`` /
``test_entity_uri_convergence.py``): **scans**, not enumeration.

- **Guard 1 (HTTP surface)**: AST-scan EVERY route module for a
  POST/PUT/PATCH/DELETE handler and fail unless it (transitively) depends on
  ``require_tenant_write``, or sits in a small allowlist with a written
  justification.
- **Guard 2 (agent capability surface)**: the ``/agent`` route is mixed (a
  question is a legitimate read), so its check lives one layer down, at
  capability dispatch in the planner. Every REGISTERED capability must be
  refused to a reader unless it declares ``writes = False`` AND is justified in
  the read-only allowlist here. A capability a downstream deployment registers
  is refused by default (``capability_writes`` reads
  ``getattr(cap, "writes", True)``).

Both carry planted-violation self-tests, so a scanner that silently stops
scanning cannot read as "all clear".

KNOWN LIMIT, and it is a real one: Guard 1 keys on the HTTP VERB, not on the
effect. A **GET that writes as a side effect** (lazy materialization, a
backfill, scheduling a background recompute) is invisible to it, and there is no
mechanical scan for that short of whole-program effect analysis. Two such paths
existed, both reachable by a reader, and both are fixed by threading the
caller's capability into the WRITE rather than gating the read: ``GET /kgs``
(``knowledge_graphs.list_kgs``, which persisted triple counts, backfilled the
stats store, kicked off the billed summary sweep and scheduled the very
recompute ``POST /recompute-stats`` refuses a reader) and the ontology reads
that ensure/auto-upgrade the workspace base pin.
``test_read_only_get_paths_do_not_write`` pins both. When you add a lazy write
behind a GET, gate the WRITE and pin it by hand: this file's scanner will not
catch it for you.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import cograph_client
from cograph_client.agent.plan_store import StoredPlan, make_plan_store
from cograph_client.agent.registry import (
    PlanStep,
    capability_writes,
    get_capabilities,
)
from cograph_client.auth import api_keys
from cograph_client.auth.api_keys import TenantContext
from cograph_client.auth.workspace_store import make_workspace_store

_ROUTES_DIR = pathlib.Path(cograph_client.__file__).parent / "api" / "routes"
_MUTATING_METHODS = {"post", "put", "patch", "delete"}

#: The ONE dependency that enforces the workspace write capability.
_WRITE_DEP = "require_tenant_write"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Guard 1: deny-by-default scan of the mutating HTTP surface
# --------------------------------------------------------------------------

# Mutating routes permitted to NOT depend on ``require_tenant_write``, each with
# the reason it is not a capability violation. A new POST/PUT/PATCH/DELETE lands
# OUTSIDE this map and fails by default, that is the whole point: ONTA-452
# happened because the guard was a list of modules somebody remembered to edit.
#
# Keys are "<module>.py::<handler>".
_ALLOWLIST: dict[str, str] = {
    # --- Mixed read/write: enforced one layer down, see Guard 2 ---------------
    "agent.py::agent_turn": (
        "MIXED route (ONTA-451/452): a question turn is a legitimate READ a "
        "reader must keep. The membership capability is resolved on the route "
        "(get_tenant_with_capability -> AgentContext.capability) and enforced at "
        "capability DISPATCH in the planner, both where a mutating plan is "
        "persisted and in execute_plan. Guard 2 below pins that enforcement for "
        "every registered capability, deny-by-default."
    ),
    # --- Read-only POSTs (POST because the body is a payload, not a mutation) --
    "ask.py::ask_question": "read-only: NL question -> SPARQL SELECT, answers only.",
    "query.py::execute_query": "read-only: SPARQL SELECT/ASK passthrough; the UPDATE twin is a separate route.",
    "grep.py::grep_graph": "read-only: literal substring scan over one KG's triples, returns matches.",
    "search.py::semantic_search": "read-only: hybrid semantic search over the tenant's index, returns matches.",
    "skills.py::validate_skill_route": "read-only authoring pre-flight: validates a skill body and returns errors, writes nothing.",
    # --- Identity / membership admin: a DIFFERENT gate, not tenant write ------
    "tenants.py::add_tenant": "identity-scoped: creates a workspace on the CALLER's own profile; there is no membership on it to have write on yet.",
    "tenants.py::remove_tenant": "identity-scoped: removes a workspace from the CALLER's own profile; ownership-gated by the tenant provider.",
    "workspace_invites.py::create_invite": "membership admin: owner-only via _require_owner (can_admin_members), strictly stronger than write.",
    "workspace_invites.py::revoke_invite": "membership admin: owner-only via _require_owner.",
    "workspace_invites.py::remove_member": "membership admin: owner-only via _require_owner.",
    "workspace_invites.py::accept_invite": "invitee-scoped: the RECIPIENT accepts their own invite; requiring write would make a reader invite un-acceptable.",
    "workspace_invites.py::decline_invite": "invitee-scoped: the RECIPIENT declines their own invite.",
    "workspace_invites.py::accept_invite_by_token": "invitee-scoped: token-bearing accept of one's own invite.",
}


def _dep_names(fn: ast.AST) -> set[str]:
    """Names passed to ``Depends(...)`` anywhere in a function signature.

    Covers both spellings so a route cannot look unguarded merely because it
    used the newer one: a default (``t: T = Depends(dep)``) and an annotation
    (``t: Annotated[T, Depends(dep)]``).
    """
    names: set[str] = set()
    args = getattr(fn, "args", None)
    if args is None:
        return names
    nodes: list[ast.AST] = list(args.defaults) + [d for d in args.kw_defaults if d]
    for arg in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
        if arg.annotation is not None:
            nodes.append(arg.annotation)
    for node in nodes:
        for call in [node, *ast.walk(node)]:
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in ("Depends", "Security")
                and call.args
            ):
                continue
            target = call.args[0]
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _module_functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _enforces_write(fn: ast.AST, functions: dict[str, ast.AST]) -> bool:
    """True if ``fn`` depends on ``require_tenant_write`` directly OR through a
    same-module wrapper dependency (e.g. ``require_raw_update_access``)."""
    seen: set[str] = set()
    frontier = list(_dep_names(fn))
    while frontier:
        dep = frontier.pop()
        if dep == _WRITE_DEP:
            return True
        if dep in seen:
            continue
        seen.add(dep)
        inner = functions.get(dep)
        if inner is not None:
            frontier.extend(_dep_names(inner))
    return False


def _declares_mutating_method(dec: ast.AST) -> bool:
    """``@router.post(...)`` etc, or ``@router.api_route(..., methods=[...])``."""
    f = dec.func if isinstance(dec, ast.Call) else dec
    if not isinstance(f, ast.Attribute):
        return False
    if f.attr in _MUTATING_METHODS:
        return True
    if f.attr != "api_route" or not isinstance(dec, ast.Call):
        return False
    for kw in dec.keywords:
        if kw.arg != "methods" or not isinstance(kw.value, (ast.List, ast.Tuple)):
            continue
        for el in kw.value.elts:
            if isinstance(el, ast.Constant) and str(el.value).lower() in _MUTATING_METHODS:
                return True
    return False


def _mutating_handlers(tree: ast.AST) -> list[ast.AST]:
    """Handlers decorated with a mutating HTTP method."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_declares_mutating_method(d) for d in node.decorator_list):
            out.append(node)
    return out


def _scan_routes(root: pathlib.Path) -> list[str]:
    """Every mutating handler that does NOT enforce the write capability."""
    unguarded: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        functions = _module_functions(tree)
        for fn in _mutating_handlers(tree):
            if not _enforces_write(fn, functions):
                unguarded.append(f"{path.name}::{fn.name}")
    return unguarded


def test_every_mutating_route_enforces_write_capability():
    """Deny-by-default: a mutating route either depends on require_tenant_write
    or carries a written justification here."""
    offenders = [k for k in _scan_routes(_ROUTES_DIR) if k not in _ALLOWLIST]
    assert not offenders, (
        "These mutating routes do not enforce the workspace write capability "
        "(a reader-role member can call them). Add "
        "`Depends(require_tenant_write)`, or add a justified entry to "
        f"_ALLOWLIST in this file if the route genuinely is not a write: {offenders}"
    )


def test_allowlist_entries_are_live():
    """A stale allowlist entry is a lie about the code, drop it when the route
    is fixed, renamed, or removed."""
    unguarded = set(_scan_routes(_ROUTES_DIR))
    stale = sorted(k for k in _ALLOWLIST if k not in unguarded)
    assert not stale, (
        "These _ALLOWLIST entries no longer match an unguarded mutating route "
        f"(route fixed/renamed/deleted?), remove them: {stale}"
    )


def test_allowlist_entries_carry_a_justification():
    thin = sorted(k for k, v in _ALLOWLIST.items() if len(v.strip()) < 30)
    assert not thin, f"_ALLOWLIST entries need a real justification: {thin}"


def test_route_scanner_catches_a_planted_violation(tmp_path):
    """Self-test: the scanner must actually fail on a new unguarded mutating
    route (otherwise a silently-broken scanner reads as 'all clear')."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from fastapi import APIRouter, Depends\n"
        "from cograph_client.auth.api_keys import TenantContext, get_tenant\n"
        "router = APIRouter()\n"
        "@router.post('/danger')\n"
        "async def danger(tenant: TenantContext = Depends(get_tenant)):\n"
        "    return {}\n"
        "@router.api_route('/danger2', methods=['PUT'])\n"
        "async def danger2(tenant: TenantContext = Depends(get_tenant)):\n"
        "    return {}\n"
    )
    found = _scan_routes(tmp_path)
    # Both spellings of "this is a mutating route" are seen, so a new route
    # cannot slip past by using the less common decorator.
    assert "planted.py::danger" in found
    assert "planted.py::danger2" in found


def test_route_scanner_accepts_direct_and_wrapped_write_deps(tmp_path):
    """The scanner must NOT flag a guarded route: guarded directly, through a
    same-module wrapper dependency, or via an ``Annotated`` signature.

    The Annotated case matters because a false POSITIVE is not harmless here:
    the natural response to it is an allowlist entry, which would permanently
    exempt a route that was in fact guarded.
    """
    ok = tmp_path / "ok.py"
    ok.write_text(
        "from typing import Annotated\n"
        "from fastapi import APIRouter, Depends\n"
        "from cograph_client.auth.access import require_tenant_write\n"
        "from cograph_client.auth.api_keys import TenantContext\n"
        "router = APIRouter()\n"
        "@router.post('/direct')\n"
        "async def direct(t: TenantContext = Depends(require_tenant_write)):\n"
        "    return {}\n"
        "def wrapper(t: TenantContext = Depends(require_tenant_write)):\n"
        "    return t\n"
        "@router.delete('/wrapped')\n"
        "async def wrapped(t: TenantContext = Depends(wrapper)):\n"
        "    return {}\n"
        "@router.patch('/annotated')\n"
        "async def annotated(\n"
        "    t: Annotated[TenantContext, Depends(require_tenant_write)],\n"
        "):\n"
        "    return {}\n"
    )
    assert _scan_routes(tmp_path) == []


def test_no_route_module_registers_routes_imperatively():
    """``add_api_route`` would register a mutating route the AST scan cannot
    classify, so the scan-friendly decorator form is the only sanctioned one."""
    offenders = [
        p.name
        for p in sorted(_ROUTES_DIR.glob("*.py"))
        if "add_api_route" in p.read_text()
    ]
    assert not offenders, (
        "These route modules register routes imperatively, which is invisible "
        "to the write-capability scan. Use @router.<method>(...) instead: "
        f"{offenders}"
    )


# --------------------------------------------------------------------------
# Guard 2: deny-by-default over the agent capability surface
# --------------------------------------------------------------------------

#: Capabilities that genuinely write NOTHING a reader may not do, each with the
#: reason. Everything else, registered here or by a downstream deployment, is
#: refused to a reader by default.
_READ_ONLY_CAPABILITIES: dict[str, str] = {
    "query": "answers a question by generating a SPARQL SELECT; writes nothing.",
    "web_research": "reads the web and returns a cited answer/artifact (ADR 0006); writes nothing to the KG.",
}


def _registered_by_name() -> dict:
    from cograph_client.agent.planner import register_default_capabilities

    register_default_capabilities()
    return {c.name: c for c in get_capabilities()}


def test_every_agent_capability_is_mutating_unless_justified():
    """Deny-by-default: a capability counts as mutating unless it declares
    ``writes = False`` and is justified here."""
    caps = _registered_by_name()
    undeclared = sorted(
        name
        for name, cap in caps.items()
        if not capability_writes(cap) and name not in _READ_ONLY_CAPABILITIES
    )
    assert not undeclared, (
        "These capabilities declare writes=False but carry no justification. "
        "A reader can run them through POST /agent, add a justified entry to "
        f"_READ_ONLY_CAPABILITIES only if they truly write nothing: {undeclared}"
    )
    lying = sorted(
        name
        for name in _READ_ONLY_CAPABILITIES
        if name in caps and capability_writes(caps[name])
    )
    assert not lying, (
        f"_READ_ONLY_CAPABILITIES lists mutating capabilities: {lying}"
    )


def test_default_capability_is_treated_as_mutating():
    """Self-test of the deny-by-default rule: a capability that says nothing , 
    the shape any NEW capability starts as, is mutating."""

    class Silent:
        name = "silent"

    assert capability_writes(Silent()) is True
    assert capability_writes(None) is True

    class Declared:
        name = "declared"
        writes = False

    assert capability_writes(Declared()) is False


def test_mutating_capabilities_are_refused_to_a_reader():
    """Every registered mutating capability is denied at execute_plan for a
    read-only context, including any a downstream deployment registered."""
    from cograph_client.agent.planner import execute_plan
    from cograph_client.agent.registry import AgentContext, ReadOnlyMembershipError

    caps = _registered_by_name()
    store = make_plan_store()
    for name, cap in caps.items():
        if not capability_writes(cap):
            continue
        plan_id = f"reader-denied-{name}"
        _run(
            store.save(
                StoredPlan(
                    plan_id=plan_id,
                    tenant_id="guard-ws",
                    kg_name="kg",
                    type_name=None,
                    message="do it",
                    steps=[PlanStep(capability=name, action="run")],
                )
            )
        )
        ctx = AgentContext(
            tenant_id="guard-ws",
            kg_name="kg",
            neptune=AsyncMock(),
            # The read-only membership capability, as the canonical /agent
            # route resolves it onto the context.
            capability="read",
        )
        with pytest.raises(ReadOnlyMembershipError) as ei:
            _run(execute_plan(ctx, plan_id))
        assert name in str(ei.value), name
        # Denied BEFORE the one-shot claim: the plan is still runnable by a
        # writer afterwards (a refused confirm must not burn the plan).
        stored = _run(store.get(plan_id, "guard-ws"))
        assert stored is not None and stored.status == "proposed", name


# --------------------------------------------------------------------------
# Behavioral: the reported escalation, end to end through POST /agent.
#
# ``tests/test_reader_write_gate_routes.py`` (ONTA-451) covers the same route
# with FAKE capabilities registered for the test. These drive the REAL
# registered capability classes and assert the capability's ``execute`` is
# never reached, so a refactor that moved the gate to the wrong side of
# dispatch would fail here even if the fakes still 403'd.
# --------------------------------------------------------------------------

_READER_TENANT = "esc-ws"


@pytest.fixture
def reader_client(app):
    """A TestClient whose caller is a READER member of ``esc-ws``."""
    from fastapi.testclient import TestClient

    store = make_workspace_store()
    _run(store.claim_workspace(_READER_TENANT, "user_owner", "Escalation"))
    _run(store.add_member(_READER_TENANT, "user_reader", "reader"))
    app.dependency_overrides[api_keys.get_tenant] = lambda: TenantContext(
        tenant_id=_READER_TENANT, api_key="k", subject="user_reader"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(api_keys.get_tenant, None)


def _save_plan(plan_id: str, capability: str, action: str = "run") -> None:
    _run(
        make_plan_store().save(
            StoredPlan(
                plan_id=plan_id,
                tenant_id=_READER_TENANT,
                kg_name="kg",
                type_name="Company",
                message="merge the duplicates",
                steps=[PlanStep(capability=capability, action=action)],
            )
        )
    )


def test_reader_cannot_mutate_a_kg_through_the_agent(reader_client):
    """THE bug: a reader confirming a dedup plan reached DedupCapability.execute
    (a real KG mutation) through POST /agent, while POST /ingest 403s."""
    from cograph_client.agent.capabilities.dedup_cap import DedupCapability

    _save_plan("plan-reader-dedup", "dedup", "rebuild")
    spy = AsyncMock(return_value={"status": "queued"})
    with patch.object(DedupCapability, "execute", spy):
        resp = reader_client.post(
            f"/graphs/{_READER_TENANT}/agent",
            json={
                "message": "",
                "context": {"kg_name": "kg"},
                "confirm": {"plan_id": "plan-reader-dedup"},
            },
        )
    assert resp.status_code == 403, resp.text
    assert spy.await_count == 0, "the mutating capability ran for a reader"


def test_reader_cannot_ingest_through_the_agent(reader_client):
    """Same escalation for the web-ingest capability (mints new entities)."""
    from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability

    _save_plan("plan-reader-ingest", "web_ingest", "ingest")
    spy = AsyncMock(return_value={"status": "queued"})
    with patch.object(WebIngestCapability, "execute", spy):
        resp = reader_client.post(
            f"/graphs/{_READER_TENANT}/agent",
            json={
                "message": "",
                "context": {"kg_name": "kg"},
                "confirm": {"plan_id": "plan-reader-ingest"},
            },
        )
    assert resp.status_code == 403, resp.text
    assert spy.await_count == 0


def test_reader_is_refused_before_a_mutating_plan_is_even_persisted(reader_client):
    """Refuse at PLAN time too, not only at confirm.

    A reader who asks for an enrich should get a straight 403, not a plan they
    can never confirm (and not a row in the plan store). The confirm-time gate
    stays because a plan can sit un-confirmed while the caller's role changes.
    """
    from cograph_client.agent.capabilities.enrich_cap import EnrichCapability

    step = PlanStep(capability="enrich", action="enrich", params={})
    with patch(
        "cograph_client.agent.planner._classify",
        new=AsyncMock(return_value={"intents": ["enrich"]}),
    ), patch.object(
        EnrichCapability, "plan", AsyncMock(return_value=[step])
    ), patch(
        "cograph_client.agent.planner.check_kg_scope",
        new=AsyncMock(return_value=None),
    ), patch(
        "cograph_client.agent.planner.make_plan_store"
    ) as plan_store:
        saver = AsyncMock()
        plan_store.return_value.save = saver
        resp = reader_client.post(
            f"/graphs/{_READER_TENANT}/agent",
            json={
                "message": "enrich every company with its revenue",
                "context": {"kg_name": "kg", "type_name": "Company"},
            },
        )

    assert resp.status_code == 403, resp.text
    assert saver.await_count == 0, "a plan a reader can never run was persisted"


def test_reader_is_refused_at_the_front_door_too(reader_client):
    """Control: the direct write route was always guarded."""
    resp = reader_client.post(
        f"/graphs/{_READER_TENANT}/ingest",
        json={"content": "acme corp", "source": "t", "kg_name": "kg"},
    )
    assert resp.status_code == 403, resp.text


def test_reader_can_still_ask_a_question_through_the_agent(reader_client):
    """The other direction, which matters just as much: gating the mutating
    turns must NOT break a reader's read turn."""
    from cograph_client.agent.capabilities.query import QueryCapability

    answer = AsyncMock(return_value={"answer": "42", "sparql": "SELECT * {}", "rows": []})
    with patch.object(QueryCapability, "answer", answer):
        with patch(
            "cograph_client.agent.planner._classify",
            new=AsyncMock(return_value={"intents": ["question"]}),
        ):
            resp = reader_client.post(
                f"/graphs/{_READER_TENANT}/agent",
                json={"message": "how many companies?", "context": {"kg_name": "kg"}},
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "answer"
    assert answer.await_count == 1


def test_reader_can_still_run_a_read_only_capability_plan(reader_client):
    """A confirm whose steps are all read-only (web research) stays allowed.
    The gate is per-capability, not "any confirm"."""
    from cograph_client.agent.capabilities.web_research_cap import WebResearchCapability

    _save_plan("plan-reader-research", "web_research", "research")
    spy = AsyncMock(return_value={"kind": "research_result", "answer": "ok"})
    with patch.object(WebResearchCapability, "execute", spy):
        resp = reader_client.post(
            f"/graphs/{_READER_TENANT}/agent",
            json={
                "message": "",
                "context": {"kg_name": "kg"},
                "confirm": {"plan_id": "plan-reader-research"},
            },
        )
    assert resp.status_code == 200, resp.text
    assert spy.await_count == 1


def test_writer_can_still_execute_a_mutating_plan(app):
    """Regression floor: the fix must not lock out writers."""
    from fastapi.testclient import TestClient
    from cograph_client.agent.capabilities.dedup_cap import DedupCapability

    store = make_workspace_store()
    _run(store.claim_workspace("esc-ws-w", "user_owner2", "Escalation W"))
    _run(store.add_member("esc-ws-w", "user_writer", "writer"))
    app.dependency_overrides[api_keys.get_tenant] = lambda: TenantContext(
        tenant_id="esc-ws-w", api_key="k", subject="user_writer"
    )
    _run(
        make_plan_store().save(
            StoredPlan(
                plan_id="plan-writer-dedup",
                tenant_id="esc-ws-w",
                kg_name="kg",
                type_name="Company",
                message="merge the duplicates",
                steps=[PlanStep(capability="dedup", action="rebuild")],
            )
        )
    )
    try:
        spy = AsyncMock(return_value={"status": "queued"})
        with patch.object(DedupCapability, "execute", spy):
            resp = TestClient(app).post(
                "/graphs/esc-ws-w/agent",
                json={
                    "message": "",
                    "context": {"kg_name": "kg"},
                    "confirm": {"plan_id": "plan-writer-dedup"},
                },
            )
        assert resp.status_code == 200, resp.text
        assert spy.await_count == 1
    finally:
        app.dependency_overrides.pop(api_keys.get_tenant, None)


# --------------------------------------------------------------------------
# Behavioral: GETs that write. The scanner cannot see these (see module
# docstring), so they are pinned by hand.
# --------------------------------------------------------------------------


def test_read_only_get_paths_do_not_write(reader_client, mock_neptune):
    """A reader listing their graphs must not persist anything.

    ``GET /kgs`` used to be a full bypass of the write gate: it ran a raw
    SPARQL UPDATE to store triple counts, upserted rows into the durable stats
    store, kicked off the billed summary backfill, and scheduled the SAME
    whole-KG recompute that ``POST /recompute-stats`` refuses to a reader.
    """
    from cograph_client.api.routes import explore as explore_mod
    from cograph_client.api.routes import knowledge_graphs as kg_mod

    mock_neptune.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {"bindings": [{"name": {"value": "kg"}}]},
    }
    upsert = AsyncMock()
    with patch.object(kg_mod, "_store_triple_count", AsyncMock()) as store_count, patch(
        "cograph_client.graph.kg_stats_store.get_kg_stats_store"
    ) as stats_store, patch.object(
        explore_mod, "schedule_recompute"
    ) as sched, patch.object(
        explore_mod, "schedule_summary_backfill"
    ) as summary, patch.object(
        explore_mod, "read_kg_summary_from_stats", AsyncMock(return_value=(3, 1, {}))
    ):
        stats_store.return_value.list_for_tenant = AsyncMock(return_value=[])
        stats_store.return_value.upsert = upsert
        resp = reader_client.get(f"/graphs/{_READER_TENANT}/kgs")

    assert resp.status_code == 200, resp.text
    assert store_count.await_count == 0, "reader wrote the stored triple count"
    assert upsert.await_count == 0, "reader upserted a stats row"
    assert summary.call_count == 0, "reader kicked off the billed summary backfill"
    # The listing still WORKS and still carries the computed numbers.
    assert [row["name"] for row in resp.json()] == ["kg"]
    assert resp.json()[0]["entity_count"] == 3
    # ...and nothing had to be recomputed, because the stats graph was readable.
    assert sched.call_count == 0


def test_reader_stats_miss_still_schedules_the_recompute(reader_client, mock_neptune):
    """The one write a reader's listing MAY trigger, and must.

    When the stats graph was never materialized there is no number to read, so
    skipping the recompute would leave a reader looking at ``entity_count: 0``
    permanently, with nothing to distinguish "not computed yet" from "empty".
    A confident wrong number with no signal is the worse failure, so the
    recompute (idempotent, de-duplicated per KG, fires only on a miss) runs for
    readers too. The unbounded on-demand twin POST /recompute-stats stays gated.
    """
    from cograph_client.api.routes import explore as explore_mod

    mock_neptune.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {"bindings": [{"name": {"value": "kg"}}]},
    }
    with patch(
        "cograph_client.graph.kg_stats_store.get_kg_stats_store"
    ) as stats_store, patch.object(explore_mod, "schedule_recompute") as sched, patch.object(
        explore_mod, "read_kg_summary_from_stats", AsyncMock(return_value=None)
    ):
        stats_store.return_value.list_for_tenant = AsyncMock(return_value=[])
        stats_store.return_value.upsert = AsyncMock()
        resp = reader_client.get(f"/graphs/{_READER_TENANT}/kgs")

    assert resp.status_code == 200, resp.text
    assert sched.call_count == 1, "a reader's stats miss must still be recomputed"


def test_schedule_recompute_collapses_repeats_for_one_kg():
    """The coalescing that makes the reader-reachable recompute non-spammable.

    Requests that pile up on ONE in-flight scan collapse to a single follow-up
    scan, not N stacked whole-KG scans.
    """
    import asyncio as _asyncio

    from cograph_client.api.routes import explore as explore_mod

    started = _asyncio.Event()

    async def _drive():
        calls = []

        async def slow(client, tenant_id, kg_name):
            calls.append(kg_name)
            started.set()
            await _asyncio.sleep(0.05)

        with patch.object(explore_mod, "_safe_recompute", slow):
            explore_mod.schedule_recompute(None, "t", "kg")
            explore_mod.schedule_recompute(None, "t", "kg")  # coalesced
            explore_mod.schedule_recompute(None, "t", "other")  # different KG
            await started.wait()
            await _asyncio.sleep(0.3)
        return calls

    calls = _run(_drive())
    # Three requests, three scans would be the un-coalesced behaviour; two
    # "kg" requests arriving together produce the initial scan plus ONE
    # follow-up, and never more however many pile on.
    assert sorted(calls) == ["kg", "kg", "other"]
    # The in-flight marker is released once the scan finishes, and nothing is
    # left queued.
    assert not explore_mod._recompute_inflight
    assert not explore_mod._recompute_pending


def test_schedule_recompute_reruns_a_request_that_arrived_mid_scan():
    """A recompute requested DURING a scan must be deferred, never dropped.

    This is the ONTA-452 blocker: the whole-KG scan takes ~15s, and both the
    concurrent CSV batch path and the ``POST /recompute-stats`` the CLI fires
    right after the last batch land inside that window. A scan that started
    BEFORE a write reads pre-write state, so if the post-write request is
    discarded the stale numbers are persisted to the durable ``kg_stats_store``
    and nothing ever re-triggers (``_kg_stats_for`` only schedules on a store
    MISS, and a partial row is not a miss). The wrong ``entity_count`` then
    sits on the dashboard indefinitely.

    Modelled the way the reviewer reproduced it: the fake scan READS the
    mutable state, sleeps, then PERSISTS what it read. The write lands during
    the first scan, so only a genuine re-run persists the post-write value.
    """
    import asyncio as _asyncio

    from cograph_client.api.routes import explore as explore_mod

    state = {"actual": 1, "persisted": None}
    scans = []
    first_started = _asyncio.Event()

    async def _drive():
        async def read_sleep_persist(client, tenant_id, kg_name):
            scans.append(kg_name)
            seen = state["actual"]  # read BEFORE the write lands
            first_started.set()
            await _asyncio.sleep(0.05)
            state["persisted"] = seen

        with patch.object(explore_mod, "_safe_recompute", read_sleep_persist):
            explore_mod.schedule_recompute(None, "t", "kg")
            await first_started.wait()
            # A write lands mid-scan, then asks for a recompute. The running
            # scan already read the OLD value, so this request is the only
            # thing that can make the persisted number right.
            state["actual"] = 42
            explore_mod.schedule_recompute(None, "t", "kg")
            await _asyncio.sleep(0.3)

    _run(_drive())

    assert len(scans) == 2, (
        "the mid-scan request must produce EXACTLY one follow-up scan "
        f"(got {len(scans)}); dropping it persists pre-write numbers forever"
    )
    assert state["persisted"] == 42, (
        "stats persisted the pre-write value: the post-write recompute was "
        "dropped instead of deferred"
    )
    assert not explore_mod._recompute_inflight
    assert not explore_mod._recompute_pending


def test_reader_ontology_read_does_not_pin_the_workspace(reader_client):
    """Opening the ontology / version strip must not backfill or auto-upgrade
    the workspace base pin, which is a write to the pin graph."""
    from cograph_client.graph import ontology_base_pin as pin_mod
    from cograph_client.graph.ontology_base_pin import BasePin

    # set_base_pin returns a REAL BasePin, not a bare AsyncMock: when this test
    # fails it must fail on the await_count assertion below (the actual
    # regression), not on a pydantic ValidationError while serializing a
    # MagicMock into BasePinResponse, which would point at the wrong thing.
    written = BasePin(
        tenant_id=_READER_TENANT, base_layer="public", base_version=3,
        auto_upgrade=False, previous_version=None,
    )
    with patch.object(
        pin_mod, "set_base_pin", AsyncMock(return_value=written)
    ) as set_pin, patch.object(
        pin_mod, "get_base_pin", AsyncMock(return_value=None)
    ), patch.object(
        pin_mod, "latest_base_release_version", AsyncMock(return_value=3)
    ):
        resp = reader_client.get(f"/graphs/{_READER_TENANT}/ontology/base-pin")

    assert resp.status_code == 200, resp.text
    assert set_pin.await_count == 0, "reader pinned the workspace base layer"
    # The reader still sees the pin they WOULD have been given.
    assert resp.json()["base_version"] == 3
