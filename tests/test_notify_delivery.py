"""Tests for the ONTA-235 notify / standing-alert delivery seam.

Covers the OSS mechanism end-to-end with INVENTED data (Widget/Sprocket types,
example.test URLs) — no persona tokens, so the mechanism is proven general:

* The best-effort HTTP-POST sink delivers a payload to an allowed URL and is
  BLOCKED by the shared SSRF guard for an internal/disallowed URL.
* ``register_delivery_sink`` swaps in a premium sink (a fake) and the dispatcher
  uses it.
* Delta detection: two fires with an unchanged value → sink NOT called; a changed
  value → sink called once with an old→new change payload; the first fire only
  establishes the baseline (no spurious "everything new" delivery).
* A delivery secret rides the ``secret_ref`` / SecretCipher seam, never raw.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from cograph_client.api.routes.actions import dispatch_scheduled_action
from cograph_client.scheduling.delivery import (
    DeliveryResult,
    DeliveryTarget,
    HttpPostSink,
    get_delivery_sink,
    register_delivery_sink,
    reset_delivery_sink,
)
from cograph_client.scheduling.models import Schedule
from cograph_client.scheduling.store import InMemoryScheduleStore
from cograph_client.scheduling.watch import (
    SNAPSHOT_KEY,
    diff_snapshots,
    snapshot_watch,
)


@pytest.fixture(autouse=True)
def _clean_sink():
    reset_delivery_sink()
    yield
    reset_delivery_sink()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNeptune:
    """Returns a scripted list of SPARQL result payloads, one per query() call."""

    def __init__(self, payloads: list[dict]):
        self._payloads = list(payloads)
        self.queries: list[str] = []

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        if self._payloads:
            return self._payloads.pop(0)
        return {"results": {"bindings": []}}


def _bindings(rows: list[tuple[str, str]]) -> dict:
    """Build a SPARQL ?key/?value result payload from (key, value) tuples."""
    return {
        "results": {
            "bindings": [
                {"key": {"value": k}, "value": {"value": v}} for k, v in rows
            ]
        }
    }


class _RecordingSink:
    """A fake premium sink: records deliveries, always succeeds."""

    name = "fake-premium"

    def __init__(self) -> None:
        self.deliveries: list[tuple[DeliveryTarget, dict]] = []

    async def deliver(self, target, payload) -> DeliveryResult:
        self.deliveries.append((target, payload))
        return DeliveryResult(ok=True, status_code=200)


def _notify_schedule(
    *,
    watch: dict,
    sink: dict | None,
    last_snapshot: dict | None = None,
    kg_name: str = "widgets-kg",
) -> Schedule:
    from cograph_client.enrichment.models import JobCategory

    params: dict = {"watch": watch}
    if sink is not None:
        params["sink"] = sink
    if last_snapshot is not None:
        params[SNAPSHOT_KEY] = last_snapshot
    return Schedule(
        id="notify-1",
        tenant_id="acme",
        kg_name=kg_name,
        category=JobCategory.enrichment,
        action="notify",
        params=params,
        interval_seconds=604_800,
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# HttpPostSink — SSRF guard + delivery
# ---------------------------------------------------------------------------


def test_http_sink_posts_to_allowed_url(monkeypatch):
    """An allowed public URL is POSTed with the payload; a 2xx → ok=True."""
    captured: dict = {}

    class _Resp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    import cograph_client.scheduling.delivery as delivery_mod

    monkeypatch.setattr(delivery_mod.httpx, "AsyncClient", _FakeClient)
    # Force the DNS re-check to allow (no real resolution in the test).
    monkeypatch.setattr(
        delivery_mod, "host_dns_blocked", lambda host: _false_coro()
    )

    sink = HttpPostSink()
    target = DeliveryTarget(url="https://example.test/hook")
    payload = {"changes": [{"key": "Widget-1", "old": "1", "new": "2"}]}

    result = asyncio.run(sink.deliver(target, payload))
    assert result.ok is True
    assert result.status_code == 200
    assert captured["url"] == "https://example.test/hook"
    assert captured["json"] == payload


async def _false_coro():
    return False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",  # loopback literal
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://localhost:8080/hook",  # localhost name
        "http://10.0.0.5/hook",  # private range
        "ftp://example.test/hook",  # non-http(s) scheme
    ],
)
def test_http_sink_blocks_internal_url(url):
    """The SSRF guard refuses internal / non-http(s) URLs BEFORE any socket opens.

    No httpx stub is installed — a URL that reached the network would raise; the
    guard must short-circuit with blocked=True first.
    """
    sink = HttpPostSink()
    result = asyncio.run(sink.deliver(DeliveryTarget(url=url), {"x": 1}))
    assert result.ok is False
    assert result.blocked is True


def test_http_sink_applies_secret_ref_as_bearer(monkeypatch):
    """A ``secret_ref`` is decrypted via the SecretCipher seam and applied as a
    bearer header — never carried raw on the schedule row."""
    captured: dict = {}

    class _Resp:
        status_code = 204

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, json, headers):
            captured["headers"] = headers
            return _Resp()

    import cograph_client.scheduling.delivery as delivery_mod

    monkeypatch.setattr(delivery_mod.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(
        delivery_mod, "host_dns_blocked", lambda host: _false_coro()
    )

    # Encrypt a token via the OSS cipher seam (local AES key from env).
    monkeypatch.setenv("OMNIX_SECRETS_KEY", "unit-test-secret-key-please")
    from cograph_client.api_registry.crypto import (
        get_secret_cipher,
        reset_secret_cipher,
    )

    reset_secret_cipher()
    cipher = get_secret_cipher()
    assert cipher is not None
    secret_ref = cipher.encrypt("hunter2-token")
    # The ciphertext must not be the plaintext (it's an opaque envelope).
    assert "hunter2-token" not in secret_ref

    sink = HttpPostSink()
    target = DeliveryTarget(url="https://example.test/hook", secret_ref=secret_ref)
    result = asyncio.run(sink.deliver(target, {"x": 1}))
    assert result.ok is True
    assert captured["headers"]["Authorization"] == "Bearer hunter2-token"
    reset_secret_cipher()


# ---------------------------------------------------------------------------
# watch — snapshot + diff
# ---------------------------------------------------------------------------


def test_snapshot_reads_cells_and_diff_detects_change():
    """snapshot_watch reduces bindings to a {key: value} map; diff_snapshots
    reports only the changed keys as old→new."""
    neptune = _FakeNeptune(
        [_bindings([("Widget-1", "10.00"), ("Widget-2", "20.00")])]
    )
    watch = {
        "cells": [
            {
                "key": "Widget-1",
                "subject": "https://ex/entities/Widget/1",
                "predicate": "https://ex/onto/price",
            }
        ]
    }
    snap = asyncio.run(snapshot_watch(neptune, watch, "https://ex/g"))
    assert snap == {"Widget-1": "10.00", "Widget-2": "20.00"}

    changed = diff_snapshots(
        {"Widget-1": "10.00", "Widget-2": "20.00"},
        {"Widget-1": "12.50", "Widget-2": "20.00"},
    )
    assert changed == [
        {"key": "Widget-1", "old": "10.00", "new": "12.50", "change": "changed"}
    ]


def test_diff_first_fire_is_baseline_only():
    """previous=None (never fired) yields NO changes — the first fire only
    establishes the baseline, it must not deliver a spurious "all new" alert."""
    assert diff_snapshots(None, {"Widget-1": "10.00"}) == []


def test_diff_empty_current_reports_no_removals():
    """An empty current snapshot (a read failure) reports NO changes — never a
    mass "removed" on a transient outage."""
    assert diff_snapshots({"Widget-1": "10.00"}, {}) == []


def test_diff_reports_added_key():
    changes = diff_snapshots({"Widget-1": "1"}, {"Widget-1": "1", "Sprocket-9": "7"})
    assert changes == [
        {"key": "Sprocket-9", "old": None, "new": "7", "change": "added"}
    ]


def test_snapshot_uses_raw_sparql_passthrough():
    """A watch carrying raw SPARQL is used verbatim — the general escape hatch."""
    neptune = _FakeNeptune([_bindings([("k", "v")])])
    watch = {"sparql": "SELECT ?key ?value WHERE { ?key ?p ?value }"}
    asyncio.run(snapshot_watch(neptune, watch, "https://ex/g"))
    assert neptune.queries == ["SELECT ?key ?value WHERE { ?key ?p ?value }"]


# ---------------------------------------------------------------------------
# dispatch_scheduled_action(notify) — delta gating + delivery
# ---------------------------------------------------------------------------


def test_notify_unchanged_value_does_not_deliver():
    """Two fires with an unchanged value → the sink is NOT called the 2nd time."""
    sink = _RecordingSink()
    register_delivery_sink(sink)
    store = InMemoryScheduleStore()

    async def run():
        # First fire seeds the baseline snapshot (no delivery even though there's
        # a value). Second fire reads the SAME value → no change → no delivery.
        neptune = _FakeNeptune(
            [
                _bindings([("Widget-1", "10.00")]),  # fire 1 (baseline)
                _bindings([("Widget-1", "10.00")]),  # fire 2 (unchanged)
            ]
        )
        sched = _notify_schedule(
            watch={
                "cells": [
                    {
                        "key": "Widget-1",
                        "subject": "https://ex/entities/Widget/1",
                        "predicate": "https://ex/onto/price",
                    }
                ]
            },
            sink={"url": "https://example.test/hook"},
        )
        await store.create(sched)

        # Fire 1 — baseline.
        await dispatch_scheduled_action(
            await store.get(sched.id),
            client=neptune,
            job_store=None,
            executor=None,
            schedule_store=store,
        )
        assert sink.deliveries == []  # baseline never delivers
        # Snapshot was persisted for the next fire's diff.
        assert (await store.get(sched.id)).params[SNAPSHOT_KEY] == {
            "Widget-1": "10.00"
        }

        # Fire 2 — unchanged → still no delivery.
        await dispatch_scheduled_action(
            await store.get(sched.id),
            client=neptune,
            job_store=None,
            executor=None,
            schedule_store=store,
        )
        assert sink.deliveries == []

    asyncio.run(run())


def test_notify_changed_value_delivers_once_with_old_new():
    """A changed value → the sink is called ONCE with an old→new change payload."""
    sink = _RecordingSink()
    register_delivery_sink(sink)
    store = InMemoryScheduleStore()

    async def run():
        neptune = _FakeNeptune([_bindings([("Widget-1", "12.50")])])
        # Pre-seed a baseline snapshot so this fire is a real change (10.00→12.50).
        sched = _notify_schedule(
            watch={
                "cells": [
                    {
                        "key": "Widget-1",
                        "subject": "https://ex/entities/Widget/1",
                        "predicate": "https://ex/onto/price",
                    }
                ]
            },
            sink={"url": "https://example.test/hook"},
            last_snapshot={"Widget-1": "10.00"},
        )
        await store.create(sched)

        await dispatch_scheduled_action(
            await store.get(sched.id),
            client=neptune,
            job_store=None,
            executor=None,
            schedule_store=store,
        )
        assert len(sink.deliveries) == 1
        target, payload = sink.deliveries[0]
        assert target.url == "https://example.test/hook"
        assert payload["schedule_id"] == sched.id
        assert payload["changes"] == [
            {"key": "Widget-1", "old": "10.00", "new": "12.50", "change": "changed"}
        ]
        # The fresh snapshot was written back for the next fire.
        assert (await store.get(sched.id)).params[SNAPSHOT_KEY] == {
            "Widget-1": "12.50"
        }

    asyncio.run(run())


def test_register_delivery_sink_swaps_in_premium_sink():
    """register_delivery_sink installs a premium sink and the dispatcher uses it
    (the OSS default HttpPostSink is superseded)."""
    assert isinstance(get_delivery_sink(), HttpPostSink)  # OSS default
    premium = _RecordingSink()
    register_delivery_sink(premium)
    assert get_delivery_sink() is premium  # premium wins

    store = InMemoryScheduleStore()

    async def run():
        neptune = _FakeNeptune([_bindings([("Sprocket-1", "hot")])])
        sched = _notify_schedule(
            watch={
                "cells": [
                    {
                        "key": "Sprocket-1",
                        "subject": "https://ex/entities/Sprocket/1",
                        "predicate": "https://ex/onto/state",
                    }
                ]
            },
            sink={"url": "https://example.test/hook"},
            last_snapshot={"Sprocket-1": "cold"},  # so this fire is a change
        )
        await store.create(sched)
        await dispatch_scheduled_action(
            await store.get(sched.id),
            client=neptune,
            job_store=None,
            executor=None,
            schedule_store=store,
        )
        # The PREMIUM sink received the delivery — proof the dispatcher routes
        # through the registered sink, not the OSS default.
        assert len(premium.deliveries) == 1
        assert premium.deliveries[0][1]["changes"][0]["new"] == "hot"

    asyncio.run(run())


def test_notify_change_but_no_sink_url_is_safe():
    """A change with no delivery URL configured is a no-op (logged), not a crash —
    the schedule is a valid standing trigger awaiting a delivery URL."""
    register_delivery_sink(_RecordingSink())
    store = InMemoryScheduleStore()

    async def run():
        neptune = _FakeNeptune([_bindings([("Widget-1", "99")])])
        sched = _notify_schedule(
            watch={
                "cells": [
                    {
                        "key": "Widget-1",
                        "subject": "https://ex/entities/Widget/1",
                        "predicate": "https://ex/onto/price",
                    }
                ]
            },
            sink=None,
            last_snapshot={"Widget-1": "1"},
        )
        await store.create(sched)
        # No raise even though there's a change but no sink.
        result = await dispatch_scheduled_action(
            await store.get(sched.id),
            client=neptune,
            job_store=None,
            executor=None,
            schedule_store=store,
        )
        assert result is None  # notify creates no job row

    asyncio.run(run())
