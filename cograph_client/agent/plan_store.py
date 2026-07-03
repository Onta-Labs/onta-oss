"""Plan stores for the unified Ask-AI agent (COG-124).

A user asks the agent to do something → the planner proposes a plan (a list of
:class:`~cograph_client.agent.registry.PlanStep`) and persists it keyed by
``plan_id``; the user then confirms and the planner executes it. For that
confirm→execute to survive a process restart — or, on multi-task deployments
(e.g. ECS Fargate), to hit a different task than the one that planned — the
proposed plan must live in a durable, shared store, not in process memory.

The store is also the substrate of the ONE-SHOT execution guard: a confirm
claims the plan via an atomic ``claim_for_execution`` status transition
(``proposed`` → ``executing``; a stale ``executing`` claim is recoverable), and
the finished plan persists its ``result`` payload so a duplicate confirm — a
client retry after a gateway timeout, the Explorer auto-confirm double-firing —
replays the SAME acks/job ids instead of re-running (and re-billing /
re-ingesting) the steps. See :func:`cograph_client.agent.planner.execute_plan`.

This mirrors :mod:`cograph_client.enrichment.job_store` exactly:

- ``PlanStore`` — an async Protocol so the backend is swappable.
- ``InMemoryPlanStore`` — the zero-config default; non-durable, per-process.
- ``PostgresPlanStore`` — a durable, shared-across-tasks backend over a generic
  Postgres DSN (``settings.database_url``). Deliberately vendor-neutral: it reads
  a plain DSN, contains no cloud-provider identifiers, and works against any
  Postgres (local, Aurora, Neon, Supabase, ...).
- ``make_plan_store()`` — selects Postgres when ``settings.database_url`` is set,
  else in-memory.

The full plan is serialized to a ``payload`` jsonb column; the columns the agent
scopes/expires on (tenant, session, created_at) are mirrored alongside it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from cograph_client.agent.registry import PlanStep
from cograph_client.config import settings


@dataclass
class StoredPlan:
    """A persisted, tenant-scoped plan awaiting confirmation/execution.

    Carries the tenant + KG scope it was proposed in, the originating
    ``session_id`` (when the caller supplied one), and a ``created_at`` so a
    durable store can scope listing and expire stale plans.

    ``status`` is the one-shot execution guard's state machine: a plan is born
    ``proposed``, is atomically claimed to ``executing`` by exactly ONE confirm
    (see :meth:`InMemoryPlanStore.claim_for_execution`), and ends ``done`` (with
    the returned ``result`` payload persisted for idempotent replay) or
    ``failed``. ``executed_at`` stamps when the claim was taken so a claim
    orphaned by a mid-run crash can be detected as stale and re-claimed.
    """

    plan_id: str
    tenant_id: str
    kg_name: str
    type_name: str | None
    message: str
    steps: list[PlanStep]
    status: str = "proposed"  # proposed | executing | done | failed
    session_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # When the executing claim was taken (None until first confirm) — the
    # staleness reference for crash recovery.
    executed_at: datetime | None = None
    # The exact {kind:"result", steps:[...]} payload returned to the confirmer,
    # persisted on completion so a duplicate confirm replays the SAME acks/job
    # ids instead of re-running (and re-billing) the steps.
    result: dict | None = None

    def to_json(self) -> str:
        """Serialize to a JSON string for the jsonb payload column.

        ``PlanStep`` is a dataclass (not pydantic), so we round-trip it through
        its own ``to_dict``/``from_dict`` rather than ``model_dump_json``.
        ``default=str`` because ``result`` embeds capability acks verbatim — a
        downstream capability that slips a datetime (or similar) into its ack
        must not make the post-execution save throw and strand the plan
        ``executing``.
        """
        return json.dumps(
            {
                "plan_id": self.plan_id,
                "tenant_id": self.tenant_id,
                "kg_name": self.kg_name,
                "type_name": self.type_name,
                "message": self.message,
                "steps": [s.to_dict() for s in self.steps],
                "status": self.status,
                "session_id": self.session_id,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "executed_at": (
                    self.executed_at.isoformat() if self.executed_at else None
                ),
                "result": self.result,
            },
            default=str,
        )

    @classmethod
    def from_payload(cls, payload: Any) -> "StoredPlan":
        """Rebuild a :class:`StoredPlan` from a jsonb payload (str or dict)."""
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode()
        if isinstance(payload, str):
            data = json.loads(payload)
        else:
            data = payload
        created_raw = data.get("created_at")
        created = (
            datetime.fromisoformat(created_raw)
            if created_raw
            else datetime.now(timezone.utc)
        )
        executed_raw = data.get("executed_at")
        return cls(
            plan_id=data["plan_id"],
            tenant_id=data["tenant_id"],
            kg_name=data.get("kg_name", ""),
            type_name=data.get("type_name"),
            message=data.get("message", ""),
            steps=[PlanStep.from_dict(s) for s in data.get("steps", [])],
            status=data.get("status", "proposed"),
            session_id=data.get("session_id"),
            created_at=created,
            executed_at=(
                datetime.fromisoformat(executed_raw) if executed_raw else None
            ),
            result=data.get("result"),
        )


class PlanStore(Protocol):
    async def save(self, plan: StoredPlan) -> None: ...
    async def get(self, plan_id: str, tenant_id: str) -> Optional[StoredPlan]: ...
    async def delete(self, plan_id: str, tenant_id: str) -> None: ...
    async def list_for_tenant(self, tenant_id: str) -> list[StoredPlan]: ...
    async def list_for_session(self, session_id: str) -> list[StoredPlan]: ...
    async def claim_for_execution(
        self,
        plan_id: str,
        tenant_id: str,
        *,
        stale_before: Optional[datetime] = None,
    ) -> tuple[Optional[StoredPlan], bool]: ...


def _claim_allowed(plan: StoredPlan, stale_before: Optional[datetime]) -> bool:
    """Whether a claim-for-execution may take this plan.

    ``proposed`` is always claimable — the normal first confirm. ``executing``
    is claimable ONLY when its claim is older than ``stale_before`` (the prior
    executor is presumed dead — crashed / redeployed mid-run — so the plan is
    not stranded un-runnable forever). Terminal states (``done`` / ``failed``)
    are never claimable: that is the one-shot guard.
    """
    if plan.status == "proposed":
        return True
    if plan.status == "executing" and stale_before is not None:
        started = plan.executed_at or plan.created_at
        # No usable timestamp at all (malformed row) → can't be proven fresh.
        return started is None or started < stale_before
    return False


class InMemoryPlanStore:
    """Tenant-scoped in-memory plan store — the zero-config default.

    Mirrors :class:`~cograph_client.enrichment.job_store.InMemoryJobStore`:
    an ``asyncio.Lock`` guards the dict and reads/writes deep-copy so a caller
    can't mutate stored state by reference. Plans do not survive a process
    restart; use :class:`PostgresPlanStore` for durability.
    """

    def __init__(self) -> None:
        self._plans: dict[str, StoredPlan] = {}
        self._lock = asyncio.Lock()

    async def save(self, plan: StoredPlan) -> None:
        async with self._lock:
            self._plans[plan.plan_id] = _copy_plan(plan)

    async def get(self, plan_id: str, tenant_id: str) -> Optional[StoredPlan]:
        async with self._lock:
            p = self._plans.get(plan_id)
            if p is None or p.tenant_id != tenant_id:
                return None
            return _copy_plan(p)

    async def delete(self, plan_id: str, tenant_id: str) -> None:
        async with self._lock:
            p = self._plans.get(plan_id)
            if p is not None and p.tenant_id == tenant_id:
                self._plans.pop(plan_id, None)

    async def list_for_tenant(self, tenant_id: str) -> list[StoredPlan]:
        async with self._lock:
            plans = [p for p in self._plans.values() if p.tenant_id == tenant_id]
        return _sorted_newest_first([_copy_plan(p) for p in plans])

    async def list_for_session(self, session_id: str) -> list[StoredPlan]:
        async with self._lock:
            plans = [p for p in self._plans.values() if p.session_id == session_id]
        return _sorted_newest_first([_copy_plan(p) for p in plans])

    async def claim_for_execution(
        self,
        plan_id: str,
        tenant_id: str,
        *,
        stale_before: Optional[datetime] = None,
    ) -> tuple[Optional[StoredPlan], bool]:
        """Atomically transition a claimable plan to ``executing``.

        Returns ``(plan, claimed)``: the stored plan (a copy, post-transition
        when claimed) and whether THIS caller won the transition; ``(None,
        False)`` when no such plan exists for the tenant. Check-and-set runs
        under the store lock with no interleaved await, so two concurrent
        confirms of the same plan (the Explorer auto-confirm double-firing, a
        retried request racing the original) can never both claim it.
        """
        async with self._lock:
            p = self._plans.get(plan_id)
            if p is None or p.tenant_id != tenant_id:
                return None, False
            if not _claim_allowed(p, stale_before):
                return _copy_plan(p), False
            p.status = "executing"
            p.executed_at = datetime.now(timezone.utc)
            return _copy_plan(p), True


class PostgresPlanStore:
    """Durable ``PlanStore`` backed by a generic Postgres DSN via asyncpg.

    The full :class:`StoredPlan` is serialized to a ``payload`` jsonb column; the
    columns the agent scopes/expires on (tenant, session, status, created_at) are
    mirrored alongside it so common queries don't have to parse jsonb.

    The connection pool and table are created lazily on first use so importing
    this module (and constructing the store) never touches the network — the
    table DDL is idempotent (``CREATE TABLE IF NOT EXISTS``).

    Vendor-neutral by construction: the only configuration is a plain DSN. No
    cloud-provider ARNs, account IDs, or hostnames live here.
    """

    _TABLE = "cograph_plans"

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn if dsn is not None else settings.database_url
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:
        """Lazily create the asyncpg pool + table on first use."""
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            import asyncpg  # imported lazily so the dependency is optional

            pool = await asyncpg.create_pool(dsn=self._dsn)
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._TABLE} (
                        plan_id text PRIMARY KEY,
                        tenant_id text NOT NULL,
                        session_id text,
                        status text,
                        created_at timestamptz,
                        updated_at timestamptz,
                        payload jsonb NOT NULL
                    )
                    """
                )
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {self._TABLE}_tenant_idx "
                    f"ON {self._TABLE} (tenant_id)"
                )
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {self._TABLE}_session_idx "
                    f"ON {self._TABLE} (session_id)"
                )
            self._pool = pool
            return self._pool

    @staticmethod
    def _columns(plan: StoredPlan) -> tuple:
        """Mirror queryable columns from a plan (payload stored separately)."""
        now = datetime.now(timezone.utc)
        return (
            plan.plan_id,
            plan.tenant_id,
            plan.session_id,
            plan.status,
            plan.created_at,
            now,
            plan.to_json(),
        )

    async def save(self, plan: StoredPlan) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._TABLE}
                    (plan_id, tenant_id, session_id, status,
                     created_at, updated_at, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                ON CONFLICT (plan_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    session_id = EXCLUDED.session_id,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at,
                    payload = EXCLUDED.payload
                """,
                *self._columns(plan),
            )

    async def get(self, plan_id: str, tenant_id: str) -> Optional[StoredPlan]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT payload FROM {self._TABLE} "
                f"WHERE plan_id = $1 AND tenant_id = $2",
                plan_id,
                tenant_id,
            )
        if row is None:
            return None
        return StoredPlan.from_payload(row["payload"])

    async def delete(self, plan_id: str, tenant_id: str) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {self._TABLE} WHERE plan_id = $1 AND tenant_id = $2",
                plan_id,
                tenant_id,
            )

    async def list_for_tenant(self, tenant_id: str) -> list[StoredPlan]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT payload FROM {self._TABLE} WHERE tenant_id = $1 "
                f"ORDER BY created_at DESC",
                tenant_id,
            )
        return [StoredPlan.from_payload(r["payload"]) for r in rows]

    async def list_for_session(self, session_id: str) -> list[StoredPlan]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT payload FROM {self._TABLE} WHERE session_id = $1 "
                f"ORDER BY created_at DESC",
                session_id,
            )
        return [StoredPlan.from_payload(r["payload"]) for r in rows]

    async def claim_for_execution(
        self,
        plan_id: str,
        tenant_id: str,
        *,
        stale_before: Optional[datetime] = None,
    ) -> tuple[Optional[StoredPlan], bool]:
        """Atomically transition a claimable plan to ``executing``.

        Same contract as :meth:`InMemoryPlanStore.claim_for_execution`, made
        atomic across processes with ``SELECT … FOR UPDATE``: two confirms
        racing from different ECS tasks serialize on the row, so exactly one
        sees a claimable status and flips it — the loser observes the
        post-claim row and backs off.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"SELECT payload FROM {self._TABLE} "
                    f"WHERE plan_id = $1 AND tenant_id = $2 FOR UPDATE",
                    plan_id,
                    tenant_id,
                )
                if row is None:
                    return None, False
                plan = StoredPlan.from_payload(row["payload"])
                if not _claim_allowed(plan, stale_before):
                    return plan, False
                plan.status = "executing"
                plan.executed_at = datetime.now(timezone.utc)
                await conn.execute(
                    f"UPDATE {self._TABLE} SET status = $3, updated_at = $4, "
                    f"payload = $5::jsonb "
                    f"WHERE plan_id = $1 AND tenant_id = $2",
                    plan_id,
                    tenant_id,
                    plan.status,
                    datetime.now(timezone.utc),
                    plan.to_json(),
                )
                return plan, True


async def claim_plan_for_execution(
    store: PlanStore,
    plan_id: str,
    tenant_id: str,
    *,
    stale_before: Optional[datetime] = None,
) -> tuple[Optional[StoredPlan], bool]:
    """Claim ``plan_id`` for execution through whatever store is configured.

    Dispatches to the store's atomic ``claim_for_execution`` (both bundled
    backends implement it). A third-party ``PlanStore`` that predates the
    method degrades to a non-atomic get→check→save — still a correct one-shot
    guard against the sequential duplicate confirm (the retry-after-timeout
    case), just not race-proof against two truly concurrent confirms.
    """
    native = getattr(store, "claim_for_execution", None)
    if native is not None:
        return await native(plan_id, tenant_id, stale_before=stale_before)
    plan = await store.get(plan_id, tenant_id)
    if plan is None:
        return None, False
    if not _claim_allowed(plan, stale_before):
        return plan, False
    plan.status = "executing"
    plan.executed_at = datetime.now(timezone.utc)
    await store.save(plan)
    return plan, True


def _copy_plan(plan: StoredPlan) -> StoredPlan:
    """Deep-ish copy so in-memory callers can't mutate stored state by ref."""
    return StoredPlan.from_payload(plan.to_json())


def _sorted_newest_first(plans: list[StoredPlan]) -> list[StoredPlan]:
    _OLDEST = datetime.min.replace(tzinfo=timezone.utc)
    plans.sort(key=lambda p: p.created_at or _OLDEST, reverse=True)
    return plans


_store: Optional[InMemoryPlanStore] = None
_durable_store: Optional[PostgresPlanStore] = None


def get_plan_store() -> InMemoryPlanStore:
    global _store
    if _store is None:
        _store = InMemoryPlanStore()
    return _store


def make_plan_store() -> PlanStore:
    """Select the plan-store backend from configuration.

    Returns a :class:`PostgresPlanStore` when ``settings.database_url`` is set
    (durable, shared across ECS tasks), else an :class:`InMemoryPlanStore`
    (zero-config default). Both backends are process-level singletons, so each
    call returns the SAME instance — the durable store owns one asyncpg pool per
    process instead of building (and never closing) a fresh pool on every agent
    turn. The Postgres store creates its pool/table lazily, so calling this never
    touches the network.
    """
    global _durable_store
    if settings.database_url:
        if _durable_store is None:
            _durable_store = PostgresPlanStore()
        return _durable_store
    return get_plan_store()


def reset_plan_store() -> None:
    """Test helper — clear both singletons."""
    global _store, _durable_store
    _store = None
    _durable_store = None


__all__ = [
    "InMemoryPlanStore",
    "PlanStore",
    "PostgresPlanStore",
    "StoredPlan",
    "claim_plan_for_execution",
    "get_plan_store",
    "make_plan_store",
    "reset_plan_store",
]
