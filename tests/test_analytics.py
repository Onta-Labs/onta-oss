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

    def capture(self, *, event, distinct_id, properties):
        self.captured.append(
            {"event": event, "distinct_id": distinct_id, "properties": dict(properties)}
        )

    def flush(self):
        self.flushes += 1


class ThrowingSink:
    """A hostile sink whose every method raises — emit/flush must swallow it."""

    name = "throwing"

    def capture(self, *, event, distinct_id, properties):
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
