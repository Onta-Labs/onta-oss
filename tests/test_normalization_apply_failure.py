"""A normalization apply that RAISES must leave a durable, visible failure.

Before this landed, both background-apply sites swallowed the exception:

    except Exception:
        logger.error("normalize_apply_failed", rule_id=rule.id, exc_info=True)

The route had already returned 202, the rule stayed ``confirmed`` forever, and
the user got no error, no state change and no retry signal — they believed their
rule was live when it was not, and never would be. Today's concrete cause is
``normalization/execute.py``'s reads still being on the retired SPARQL client
(``SparqlClientRetired`` on the first read for ``strip_emoji`` /
``promote_to_node`` / both ``list_explode`` shapes), but NOTHING here is
SPARQL-specific: these tests raise a plain exception, so they keep asserting the
right thing after that port lands.

Covered:

* both ``_apply_and_mark`` sites — ``api/routes/normalize.py`` (HTTP) and
  ``agent/capabilities/normalize_cap.py`` (agent) — record ``failed`` +
  ``last_error`` + ``failed_at`` rather than leaving the rule ``confirmed``;
* neither site raises out of its detached task;
* the 202/async shape is unchanged, and the failure is readable through the
  ordinary ``GET /normalize/rules`` surface (including ``?status=failed``);
* the retry story: a ``failed`` rule is accepted by apply again (no 409) and a
  subsequent success clears the error instead of leaving a stale one.
"""

from __future__ import annotations

import pytest

from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests
from infona_client.normalization.rules import (
    MAX_LAST_ERROR_CHARS,
    NormalizationRule,
    NormalizationRuleStore,
    make_rule_id,
)

TENANT = "t1"
KG = "june-16"

BOOM = "reads are retired"


class FakeNeptune:
    """The rule store's ``neptune`` arg is vestigial (ONTA-529); apply is stubbed."""


def _rule(status: str = "confirmed") -> NormalizationRule:
    return NormalizationRule(
        id=make_rule_id(KG, "Mentor", "skills", "strip_emoji"),
        kg_name=KG,
        type_name="Mentor",
        predicate="skills",
        target_kind="attribute",
        rule_type="strip_emoji",
        params={},
        confidence=0.9,
        rationale="emoji junk",
        status=status,  # type: ignore[arg-type]
    )


@pytest.fixture
def memory_store():
    """Hermetic MemoryGraphStore for the rule store's catalog writes/reads."""
    reset_graph_store_for_tests()
    store = MemoryGraphStore()
    configure_graph_store(store)
    yield store
    reset_graph_store_for_tests()


async def _boom(*_a, **_k):
    raise RuntimeError(BOOM)


# --------------------------------------------------------------------------- #
# Site 1 — the HTTP route's background task.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_route_apply_failure_is_recorded_on_the_rule(memory_store, monkeypatch):
    from infona_client.api.routes import normalize as route_mod
    from infona_client.normalization import apply_job

    monkeypatch.setattr(apply_job, "apply_rule", _boom)
    store = NormalizationRuleStore(FakeNeptune())
    rule = _rule()
    await store.save(TENANT, rule)

    # Detached task semantics: must not raise, whatever apply_rule did.
    await route_mod._apply_and_mark(FakeNeptune(), TENANT, rule)

    got = await store.get(TENANT, rule.id)
    assert got is not None
    # THE regression: on main this is still "confirmed" — the user's rule looks
    # live and is not.
    assert got.status == "failed"
    assert got.last_error and BOOM in got.last_error
    assert "RuntimeError" in got.last_error
    assert got.failed_at


# --------------------------------------------------------------------------- #
# Site 2 — the agent capability's background task.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_agent_apply_failure_is_recorded_on_the_rule(memory_store, monkeypatch):
    from infona_client.agent.capabilities import normalize_cap
    from infona_client.normalization import apply_job

    monkeypatch.setattr(apply_job, "apply_rule", _boom)
    store = NormalizationRuleStore(FakeNeptune())
    rule = _rule()
    await store.save(TENANT, rule)

    await normalize_cap._apply_and_mark(FakeNeptune(), TENANT, rule)

    got = await store.get(TENANT, rule.id)
    assert got is not None
    assert got.status == "failed"
    assert got.last_error and BOOM in got.last_error
    assert got.failed_at


# --------------------------------------------------------------------------- #
# Success still works, and CLEARS a prior failure (no stale error on an
# ``applied`` rule) — this is the retry payoff.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_successful_reapply_clears_the_recorded_failure(
    memory_store, monkeypatch
):
    from infona_client.api.routes import normalize as route_mod
    from infona_client.normalization import apply_job

    store = NormalizationRuleStore(FakeNeptune())
    rule = _rule()
    await store.save(TENANT, rule)

    monkeypatch.setattr(apply_job, "apply_rule", _boom)
    await route_mod._apply_and_mark(FakeNeptune(), TENANT, rule)
    failed = await store.get(TENANT, rule.id)
    assert failed is not None and failed.status == "failed" and failed.last_error

    # Underlying cause fixed → re-apply the SAME rule row.
    async def _ok(*_a, **_k):
        return {"literals_rewritten": 3}

    monkeypatch.setattr(apply_job, "apply_rule", _ok)
    await route_mod._apply_and_mark(FakeNeptune(), TENANT, failed)

    got = await store.get(TENANT, rule.id)
    assert got is not None
    assert got.status == "applied"
    assert got.applied_at
    assert got.last_error is None
    assert got.failed_at is None


@pytest.mark.asyncio
async def test_failure_does_not_erase_a_prior_successful_applied_at(
    memory_store, monkeypatch
):
    """``applied_at`` records the last apply that LANDED — a later failure must
    not rewrite history, or an operator loses the only evidence the rule ever
    took effect on the graph."""
    from infona_client.api.routes import normalize as route_mod
    from infona_client.normalization import apply_job

    store = NormalizationRuleStore(FakeNeptune())
    rule = _rule()
    await store.save(TENANT, rule)

    async def _ok(*_a, **_k):
        return {}

    monkeypatch.setattr(apply_job, "apply_rule", _ok)
    await route_mod._apply_and_mark(FakeNeptune(), TENANT, rule)
    applied = await store.get(TENANT, rule.id)
    assert applied is not None and applied.applied_at
    stamp = applied.applied_at

    monkeypatch.setattr(apply_job, "apply_rule", _boom)
    await route_mod._apply_and_mark(FakeNeptune(), TENANT, applied)

    got = await store.get(TENANT, rule.id)
    assert got is not None
    assert got.status == "failed"
    assert got.applied_at == stamp


@pytest.mark.asyncio
async def test_persisted_error_is_capped(memory_store, monkeypatch):
    """The message is a literal in the tenant ontology graph — cap it."""
    from infona_client.normalization import apply_job

    async def _huge(*_a, **_k):
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(apply_job, "apply_rule", _huge)
    store = NormalizationRuleStore(FakeNeptune())
    rule = _rule()
    await store.save(TENANT, rule)

    outcome = await apply_job.apply_and_record(FakeNeptune(), TENANT, rule)
    assert outcome.ok is False
    got = await store.get(TENANT, rule.id)
    assert got is not None and got.last_error is not None
    assert len(got.last_error) == MAX_LAST_ERROR_CHARS


@pytest.mark.asyncio
async def test_apply_and_record_never_raises_even_if_the_store_is_down(
    memory_store, monkeypatch
):
    """Last-ditch: if we cannot even RECORD the failure, still don't blow up the
    detached task (that would be an unhandled-task warning and nothing else)."""
    from infona_client.normalization import apply_job

    monkeypatch.setattr(apply_job, "apply_rule", _boom)

    async def _store_down(*_a, **_k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(NormalizationRuleStore, "update_status", _store_down)
    outcome = await apply_job.apply_and_record(FakeNeptune(), TENANT, _rule())
    assert outcome.ok is False and BOOM in outcome.error


# --------------------------------------------------------------------------- #
# End-to-end over the real route: 202 preserved, failure visible via GET, and a
# failed rule is RETRYABLE (not stranded behind the 409 confirm gate).
# --------------------------------------------------------------------------- #
@pytest.fixture
def route_client(monkeypatch):
    import os

    os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
    os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"
    from fastapi.testclient import TestClient

    from infona_client.api.app import create_app

    reset_graph_store_for_tests()
    configure_graph_store(MemoryGraphStore())
    app = create_app()
    app.state.neptune_client = FakeNeptune()
    try:
        yield TestClient(app)
    finally:
        reset_graph_store_for_tests()


def test_route_202_then_failure_is_visible_and_retryable(route_client, monkeypatch):
    import asyncio

    from infona_client.api.routes import normalize as route_mod
    from infona_client.normalization import apply_job

    client = route_client
    h = {"X-API-Key": "test-key"}

    # Capture the detached coroutine instead of letting it float: the point is
    # that the ROUTE still returns 202 and the WORK still happens out of band.
    spawned: list = []
    monkeypatch.setattr(route_mod, "_spawn", spawned.append)
    monkeypatch.setattr(apply_job, "apply_rule", _boom)

    created = client.post(
        "/graphs/test-tenant/normalize/rules",
        json={
            "kg_name": KG,
            "type_name": "Mentor",
            "predicate": "skills",
            "target_kind": "attribute",
            "rule_type": "strip_emoji",
            "params": {},
            "status": "confirmed",
        },
        headers=h,
    )
    assert created.status_code == 200
    rule_id = created.json()["id"]

    accepted = client.post(
        f"/graphs/test-tenant/normalize/rules/{rule_id}/apply", headers=h
    )
    assert accepted.status_code == 202  # async shape preserved

    assert len(spawned) == 1
    asyncio.run(spawned[0])

    listed = client.get("/graphs/test-tenant/normalize/rules", headers=h).json()
    assert [r["status"] for r in listed] == ["failed"]
    assert BOOM in listed[0]["last_error"]
    assert listed[0]["failed_at"]

    # The failure is findable by the status filter every client already has.
    only_failed = client.get(
        "/graphs/test-tenant/normalize/rules?status=failed", headers=h
    ).json()
    assert [r["id"] for r in only_failed] == [rule_id]

    # RETRY: a failed rule is applyable again — no 409, no manual store surgery.
    spawned.clear()

    async def _ok(*_a, **_k):
        return {"literals_rewritten": 1}

    monkeypatch.setattr(apply_job, "apply_rule", _ok)
    retried = client.post(
        f"/graphs/test-tenant/normalize/rules/{rule_id}/apply", headers=h
    )
    assert retried.status_code == 202
    assert len(spawned) == 1
    asyncio.run(spawned[0])

    after = client.get("/graphs/test-tenant/normalize/rules", headers=h).json()
    assert after[0]["status"] == "applied"
    assert after[0]["last_error"] is None
    assert after[0]["failed_at"] is None


def test_route_still_refuses_apply_on_a_suggested_rule(route_client):
    """The new ``failed`` entry to apply must not open the confirm gate itself."""
    client = route_client
    h = {"X-API-Key": "test-key"}
    created = client.post(
        "/graphs/test-tenant/normalize/rules",
        json={
            "kg_name": KG,
            "type_name": "Mentor",
            "predicate": "skills",
            "target_kind": "attribute",
            "rule_type": "strip_emoji",
            "params": {},
        },
        headers=h,
    )
    assert created.status_code == 200 and created.json()["status"] == "suggested"
    blocked = client.post(
        f"/graphs/test-tenant/normalize/rules/{created.json()['id']}/apply", headers=h
    )
    assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_failed_status_round_trips_through_the_store(memory_store):
    """A ``failed`` rule must survive persist → read (the store's status field is
    a pydantic Literal; an unknown value would blow up the read, not just the
    write)."""
    store = NormalizationRuleStore(FakeNeptune())
    rule = _rule()
    await store.save(TENANT, rule)
    await store.update_status(
        TENANT, rule.id, "failed", failed_at="2026-08-21T00:00:00+00:00",
        last_error="RuntimeError: nope",
    )
    got = await store.get(TENANT, rule.id)
    assert got is not None and got.status == "failed"
    assert got.last_error == "RuntimeError: nope"
    assert got.failed_at == "2026-08-21T00:00:00+00:00"
    assert [r.id for r in await store.list(TENANT, status="failed")] == [rule.id]
    assert await store.list(TENANT, status="confirmed") == []
