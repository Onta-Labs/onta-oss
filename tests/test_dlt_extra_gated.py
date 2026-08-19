"""Extra-gated dlt tests. Skip when ``infona-client[dlt]`` is not installed."""

from __future__ import annotations

import pytest

from infona_client.ingestion.dlt_source import dlt_available, require_dlt

pytestmark = pytest.mark.skipif(
    not dlt_available(), reason="infona-client[dlt] not installed"
)


def test_require_dlt_succeeds_when_extra_present():
    require_dlt()
    import dlt  # noqa: F401 — allowed here only because this file is tests/

    assert dlt is not None
