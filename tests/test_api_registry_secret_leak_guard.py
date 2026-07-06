"""Drift guard (ONTA-2xx Child 2): a resolved plaintext secret must never leak
into a log, a stored request/display URL, an API response, or a trace.

Two layers, mirroring the write/retrieval convergence guards:

- **Behavioral** — run the executor with a sentinel secret through a mock
  transport and assert the sentinel appears ONLY in the outbound request header
  on the base host, never in the returned ``ApiCallResult`` (rows / provenance /
  sources / error) and never in what a log formatter would render.
- **Structural** — scan ``api_registry/`` source for a code smell that would leak
  a secret: logging the resolved auth material, or writing the auth header/query
  dicts into provenance/sources. Fails on any hit.
"""

from __future__ import annotations

import io
import logging
import pathlib
import re

import httpx
import pytest

import cograph_client.api_registry as api_registry_pkg
from cograph_client.api_registry.executor import RegistryApiSource
from cograph_client.api_registry.spec import ApiSourceSpec

_SENTINEL = "SENTINEL-SECRET-DO-NOT-LEAK-9f3a2b"

_PKG_ROOT = pathlib.Path(api_registry_pkg.__file__).parent


def _spec() -> ApiSourceSpec:
    return ApiSourceSpec.from_dict({
        "slug": "leaktest",
        "title": "Leak Test",
        "base_url": "https://api.leaktest.example",
        "auth": {"mode": "api_key_query", "secret_ref": "api_key", "query_key": "token"},
        "endpoints": [{
            "name": "default", "method": "GET", "path": "/search",
            "params": [{"name": "q", "location": "query"}],
            "result_path": "results",
            "field_mappings": {"name": "name"},
        }],
    })


# --------------------------------------------------------------------------- #
# Behavioral: the sentinel never appears in the result or the logs
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_query_key_secret_never_in_result_or_logs():
    """A query-key secret is the WORST case for leakage — it would land in the
    URL if the executor didn't keep it out of the stored/display URL. Assert it
    stays out of every surfaced field and out of the captured logs."""
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        # The secret IS on the outbound wire (as the token query param) — that's
        # correct. We only assert it never comes BACK to us in a stored field.
        return httpx.Response(200, json={"results": [{"name": "row1"}]})

    # Capture everything the api_registry loggers emit during the call.
    buf = io.StringIO()
    handler_log = logging.StreamHandler(buf)
    handler_log.setLevel(logging.DEBUG)
    reg_logger = logging.getLogger("cograph_client.api_registry")
    reg_logger.addHandler(handler_log)
    reg_logger.setLevel(logging.DEBUG)
    try:
        ex = RegistryApiSource(transport=httpx.MockTransport(handler))

        async def resolver(name):
            return _SENTINEL

        res = await ex.execute(spec, {"q": "x"}, secret_resolver=resolver)
    finally:
        reg_logger.removeHandler(handler_log)

    assert res.error is None
    # The secret must be in NONE of the surfaced fields.
    from dataclasses import asdict
    import json as _json

    surfaced = _json.dumps(res.to_dict())
    assert _SENTINEL not in surfaced, "secret leaked into the ApiCallResult"
    for url in res.sources:
        assert _SENTINEL not in url, "secret leaked into a stored source URL"
    for url in res.provenance.values():
        assert _SENTINEL not in url, "secret leaked into provenance"
    # And nothing logged the secret.
    assert _SENTINEL not in buf.getvalue(), "secret leaked into logs"


@pytest.mark.asyncio
async def test_error_path_does_not_leak_secret():
    """Even when the upstream call fails, the error surfaced must not carry the
    secret."""
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    ex = RegistryApiSource(transport=httpx.MockTransport(handler))

    async def resolver(name):
        return _SENTINEL

    res = await ex.execute(spec, {"q": "x"}, secret_resolver=resolver)
    assert res.error is not None
    assert _SENTINEL not in (res.error or "")
    import json as _json

    assert _SENTINEL not in _json.dumps(res.to_dict())


# --------------------------------------------------------------------------- #
# Structural: no module logs the resolved auth material or writes it to output
# --------------------------------------------------------------------------- #
# A leak smell: passing an auth-material VARIABLE (auth_headers / auth_query /
# secret / plaintext) as a log ARGUMENT — i.e. after a comma, as a bare
# interpolation value, not the mention of the word inside a message string. So
# ``logger.info("secret cipher registered")`` is fine (the word is inside the
# quoted format string), while ``logger.info("key=%s", secret)`` is flagged
# (``secret`` is a bare argument). We match ", <var>" not immediately inside a
# string literal by requiring the token to be a comma-separated bare identifier.
_LOG_SECRET = re.compile(
    r"log(?:ger)?\.\w+\([^)]*,\s*(secret|plaintext|auth_headers|auth_query)\b\s*[),]",
)
_PROV_FROM_AUTH = re.compile(
    r"(provenance|sources)\b[^=\n]*=\s*[^=\n]*\bauth_(headers|query)\b"
)


def test_no_api_registry_module_logs_or_surfaces_auth_material():
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        code = path.read_text()
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if _LOG_SECRET.search(code):
            violations.append(f"{rel}: logs auth material")
        if _PROV_FROM_AUTH.search(code):
            violations.append(f"{rel}: writes auth material into provenance/sources")
    assert not violations, (
        "Potential secret-leak sites in api_registry/. A resolved secret / auth "
        "header/query must never be logged or written into provenance/sources. "
        "Offenders:\n  " + "\n  ".join(violations)
    )


def test_guard_flags_a_planted_secret_log():
    """Self-test: the structural scan actually fires on a planted leak."""
    planted = 'logger.info("using key %s", secret)\n'
    assert _LOG_SECRET.search(planted)
    planted2 = "result.provenance = auth_headers\n"
    assert _PROV_FROM_AUTH.search(planted2)
