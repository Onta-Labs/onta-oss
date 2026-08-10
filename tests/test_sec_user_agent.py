"""The SEC EDGAR User-Agent must come from deployment config, never a person.

SEC's fair-access policy asks automated clients to declare a working contact.
A published OSS build that hardcoded one operator's mailbox would make every
self-hoster's EDGAR traffic attributable to that individual, so the contact is
configuration (``INFONA_SEC_USER_AGENT``) with an impersonal project fallback.
"""

import re

import pytest

from infona_client.api.routes import lambda_functions as lf


@pytest.fixture(autouse=True)
def _reset_warn_flag():
    """The 'unset' warning is once-per-process; isolate it between tests."""
    lf._sec_ua_warned = False
    yield
    lf._sec_ua_warned = False


def test_configured_contact_wins(monkeypatch):
    monkeypatch.setattr(lf.settings, "sec_user_agent", "Acme Corp ops@acme.com")
    assert lf.sec_user_agent() == "Acme Corp ops@acme.com"


def test_blank_configuration_falls_back(monkeypatch):
    """Whitespace-only config is not a contact — treat it as unset."""
    monkeypatch.setattr(lf.settings, "sec_user_agent", "   ")
    assert lf.sec_user_agent() == lf.DEFAULT_SEC_USER_AGENT


def test_unset_falls_back_to_impersonal_default(monkeypatch):
    monkeypatch.setattr(lf.settings, "sec_user_agent", "")
    assert lf.sec_user_agent() == lf.DEFAULT_SEC_USER_AGENT


def test_default_carries_no_personal_mailbox():
    """The regression this test exists for: a real mailbox in the fallback."""
    personal = re.compile(
        r"[A-Za-z0-9._%+-]+@(gmail|googlemail|yahoo|hotmail|outlook|live"
        r"|icloud|me|proton|protonmail|aol)\.[A-Za-z]{2,}"
    )
    assert not personal.search(lf.DEFAULT_SEC_USER_AGENT)


class RecordingLogger:
    """Stands in for the module's structlog logger.

    Swapping the MODULE attribute (rather than patching an attribute ON the
    structlog proxy, or using ``capture_logs``) keeps this test independent of
    both test ordering and structlog config. The app configures structlog with
    ``cache_logger_on_first_use=True`` (infona_client/logging.py:23), which
    freezes the proxy's bound logger on first use — so ``capture_logs`` cannot
    intercept it once any earlier test has logged, and patching the proxy
    itself leaves a concrete attribute behind that monkeypatch cannot undo.
    """

    def __init__(self):
        self.warnings = []

    def warning(self, event, **kw):
        self.warnings.append((event, kw))


def test_unset_warns_once(monkeypatch):
    monkeypatch.setattr(lf.settings, "sec_user_agent", "")
    recorder = RecordingLogger()
    monkeypatch.setattr(lf, "logger", recorder)

    lf.sec_user_agent()
    lf.sec_user_agent()
    lf.sec_user_agent()

    assert len(recorder.warnings) == 1
    event, kw = recorder.warnings[0]
    assert event == "sec_user_agent_unset"
    assert kw["fallback"] == lf.DEFAULT_SEC_USER_AGENT


def test_configured_contact_never_warns(monkeypatch):
    monkeypatch.setattr(lf.settings, "sec_user_agent", "Acme Corp ops@acme.com")
    recorder = RecordingLogger()
    monkeypatch.setattr(lf, "logger", recorder)

    lf.sec_user_agent()

    assert recorder.warnings == []
