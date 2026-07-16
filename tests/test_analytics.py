"""Tests for the OSS product-analytics seam (ONTA-323).

The seam mirrors the established plugin pattern (register_delivery_sink, …): a
process-wide sink registry with a NO-OP default, plus a fire-and-forget
``emit()`` that MUST never raise and MUST be a no-op unless a real sink is
registered. The proprietary PostHog sink registers over this seam via the
``OMNIX_ANALYTICS_PLUGIN`` hook at app boot; OSS itself never imports posthog.
"""

import pytest

from cograph_client.analytics import (
    NoOpSink,
    distinct_id_for,
    emit,
    flush_analytics,
    get_analytics_sink,
    register_analytics_sink,
    reset_analytics_sink,
)


class RecordingSink:
    """Captures every event + flush so a test can assert what the sink saw."""

    name = "recording"

    def __init__(self):
        self.captured: list[dict] = []
        self.flushes = 0

    def capture(self, *, event, distinct_id, properties, exc_info=None):
        self.captured.append(
            {
                "event": event,
                "distinct_id": distinct_id,
                "properties": dict(properties),
                "exc_info": exc_info,
            }
        )

    def flush(self):
        self.flushes += 1


class ThrowingSink:
    """A hostile sink whose every method raises — emit/flush must swallow it."""

    name = "throwing"

    def capture(self, *, event, distinct_id, properties, exc_info=None):
        raise RuntimeError("sink boom")

    def flush(self):
        raise RuntimeError("flush boom")


@pytest.fixture(autouse=True)
def _reset_sink():
    """Every test starts + ends with the OSS no-op default (no leakage)."""
    reset_analytics_sink()
    yield
    reset_analytics_sink()


def test_default_sink_is_noop():
    """With nothing registered the active sink is the OSS no-op default."""
    assert isinstance(get_analytics_sink(), NoOpSink)


def test_emit_is_noop_without_a_registered_sink():
    """emit() with no sink reaches no recording sink and never raises.

    Register a spy, then clear it → the active sink is the no-op default, so the
    spy must never observe the event emitted afterwards.
    """
    spy = RecordingSink()
    register_analytics_sink(spy)
    reset_analytics_sink()  # back to the no-op default

    emit("kg_created", distinct_id="user_1", tenant="t1", kg="g1")

    assert spy.captured == []
    assert isinstance(get_analytics_sink(), NoOpSink)


def test_registered_sink_receives_event_distinct_id_and_properties():
    """A registered sink gets exactly the event + distinct_id + properties."""
    sink = RecordingSink()
    register_analytics_sink(sink)

    emit(
        "ingestion_completed",
        distinct_id="user_42",
        tenant="acme",
        kg="people",
        rows=10,
        entities=8,
    )

    assert len(sink.captured) == 1
    call = sink.captured[0]
    assert call["event"] == "ingestion_completed"
    assert call["distinct_id"] == "user_42"
    assert call["properties"] == {
        "tenant": "acme",
        "kg": "people",
        "rows": 10,
        "entities": 8,
    }


def test_emit_never_raises_even_if_sink_throws():
    """A sink that raises must not surface — analytics never breaks a request."""
    register_analytics_sink(ThrowingSink())
    # Must not raise.
    emit("backend_request_error", distinct_id=None, path="/x", status=500)


# --- ONTA-358: optional generic exception carrier through the seam ----------- #


def test_exc_info_defaults_to_none_and_reaches_the_sink():
    """No exc_info given → the sink still receives the keyword, valued None."""
    sink = RecordingSink()
    register_analytics_sink(sink)

    emit("kg_created", distinct_id="u", tenant="t", kg="g")

    assert sink.captured[0]["exc_info"] is None


def test_exc_info_exception_instance_passed_through_unchanged():
    """A raw exception is handed to the sink AS-IS — the seam never inspects it."""
    sink = RecordingSink()
    register_analytics_sink(sink)
    boom = ValueError("kaboom")

    emit("backend_request_error", distinct_id=None, path="/x", exc_info=boom)

    assert sink.captured[0]["exc_info"] is boom
    # The carrier is out-of-band — it must NOT leak into the flat properties.
    assert "exc_info" not in sink.captured[0]["properties"]


def test_exc_info_tuple_passed_through_unchanged():
    """A sys.exc_info()-style (type, value, tb) tuple passes through verbatim."""
    import sys

    sink = RecordingSink()
    register_analytics_sink(sink)

    try:
        raise RuntimeError("tuple boom")
    except RuntimeError:
        info = sys.exc_info()
        emit("backend_request_error", exc_info=info)

    assert sink.captured[0]["exc_info"] is info


def test_emit_with_exc_info_never_raises_when_sink_throws():
    """Passing exc_info to a hostile sink is still swallowed — never raises."""
    register_analytics_sink(ThrowingSink())
    # Must not raise even though the sink blows up on capture.
    emit("backend_request_error", exc_info=ValueError("x"), path="/x")


def test_emit_with_exc_info_is_noop_without_a_sink():
    """exc_info with the no-op default is dropped and never raises."""
    emit("backend_request_error", exc_info=ValueError("x"), path="/x")  # must not raise


def test_emit_distinct_id_defaults_to_none():
    """distinct_id is optional and defaults to None (anonymous/unattributed)."""
    sink = RecordingSink()
    register_analytics_sink(sink)

    emit("backend_request_error", path="/boom", status=500)

    assert sink.captured[0]["distinct_id"] is None


def test_register_none_resets_to_noop():
    """register_analytics_sink(None) restores the no-op default."""
    sink = RecordingSink()
    register_analytics_sink(sink)
    assert get_analytics_sink() is sink

    register_analytics_sink(None)
    assert isinstance(get_analytics_sink(), NoOpSink)

    # And emitting afterwards no longer reaches the old sink.
    emit("kg_created", distinct_id="u", tenant="t", kg="g")
    assert sink.captured == []


def test_flush_delegates_to_sink():
    """flush_analytics drains the registered sink."""
    sink = RecordingSink()
    register_analytics_sink(sink)

    flush_analytics()

    assert sink.flushes == 1


def test_flush_never_raises_even_if_sink_throws():
    """A hostile flush must be swallowed (shutdown best-effort)."""
    register_analytics_sink(ThrowingSink())
    # Must not raise.
    flush_analytics()


def test_flush_is_noop_without_a_sink():
    """flush_analytics with the no-op default does nothing and never raises."""
    flush_analytics()  # must not raise


def test_distinct_id_prefers_subject_then_system_then_none():
    """Identity resolution: subject wins; else system:<tenant>; else None."""
    assert distinct_id_for("user_7", "acme") == "user_7"
    assert distinct_id_for(None, "acme") == "system:acme"
    assert distinct_id_for("", "acme") == "system:acme"
    assert distinct_id_for(None, None) is None
    assert distinct_id_for("", "") is None


def test_no_posthog_import_in_oss_analytics():
    """The OSS seam pulls in zero third-party analytics dependency."""
    import sys

    import cograph_client.analytics  # noqa: F401

    assert "posthog" not in sys.modules


# --- ONTA-355: query_executed result-quality metadata ---------------------- #


def _tenant(subject="user_9", tenant_id="acme"):
    from cograph_client.auth.api_keys import TenantContext

    return TenantContext(tenant_id=tenant_id, api_key="k", subject=subject)


def test_query_executed_emits_result_count_and_returned_rows():
    """The /ask emit carries cheap result-quality signal + the NL mode tag."""
    import time

    from cograph_client.api.routes.ask import _emit_query_executed
    from cograph_client.models.query import NLResult

    sink = RecordingSink()
    register_analytics_sink(sink)

    result = NLResult(answer="3 rows", sparql="SELECT ...", explanation="", timing={"rows": 3})
    _emit_query_executed(_tenant(), "people", time.monotonic(), result, ok=True)

    call = sink.captured[0]
    assert call["event"] == "query_executed"
    assert call["distinct_id"] == "user_9"
    props = call["properties"]
    assert props["result_count"] == 3
    assert props["returned_rows"] is True
    assert props["ok"] is True
    assert props["mode"] == "nl"
    assert props["kg"] == "people"
    assert props["tenant"] == "acme"
    assert "latency_ms" in props
    # Counts/booleans only — no row data, no answer text, no PII.
    assert "answer" not in props
    assert "sparql" not in props


def test_query_executed_zero_rows_reports_returned_rows_false():
    """A query that returned nothing → result_count 0, returned_rows False."""
    import time

    from cograph_client.api.routes.ask import _emit_query_executed
    from cograph_client.models.query import NLResult

    sink = RecordingSink()
    register_analytics_sink(sink)

    result = NLResult(answer="no results", sparql="SELECT ...", explanation="", timing={"rows": 0})
    _emit_query_executed(_tenant(), None, time.monotonic(), result, ok=True)

    props = sink.captured[0]["properties"]
    assert props["result_count"] == 0
    assert props["returned_rows"] is False
    assert props["kg"] == ""


def test_query_executed_degraded_result_without_rows_metadata_is_zero():
    """The graceful-degrade NLResult (empty timing) reports 0 rows, not a crash."""
    import time

    from cograph_client.api.routes.ask import _emit_query_executed
    from cograph_client.models.query import NLResult

    sink = RecordingSink()
    register_analytics_sink(sink)

    degraded = NLResult(answer="internal error", sparql="", explanation="")
    _emit_query_executed(_tenant(), "people", time.monotonic(), degraded, ok=False)

    props = sink.captured[0]["properties"]
    assert props["result_count"] == 0
    assert props["returned_rows"] is False
    assert props["ok"] is False


# --- OMNIX_ANALYTICS_PLUGIN loader (mirrors the router/enrichment loaders) --- #


def test_analytics_plugin_loaded_at_startup(monkeypatch):
    """register() runs during create_app() and installs the sink over the seam."""
    from cograph_client.api import app as app_module
    from cograph_client.config import settings

    monkeypatch.setattr(
        settings, "analytics_plugin", "tests.fake_analytics_plugin:register"
    )
    try:
        app_module.create_app()

        from tests import fake_analytics_plugin

        assert fake_analytics_plugin.LOADED is True
        # The sink the plugin registered is now the active process sink.
        assert getattr(get_analytics_sink(), "name", None) == "fake-analytics"
    finally:
        from tests import fake_analytics_plugin

        fake_analytics_plugin.LOADED = False
        fake_analytics_plugin.CAPTURED.clear()
        reset_analytics_sink()


def test_analytics_plugin_invalid_format_logged(monkeypatch):
    """Malformed spec is logged but does not raise (app still starts)."""
    from cograph_client.api import app as app_module
    from cograph_client.config import settings

    monkeypatch.setattr(settings, "analytics_plugin", "no_colon_here")
    # Must not raise.
    app_module.create_app()


def test_analytics_plugin_import_failure_does_not_crash(monkeypatch):
    """A plugin that can't be imported is logged; the app still starts."""
    from cograph_client.api import app as app_module
    from cograph_client.config import settings

    monkeypatch.setattr(settings, "analytics_plugin", "tests.does_not_exist:register")
    # Must not raise.
    app_module.create_app()
