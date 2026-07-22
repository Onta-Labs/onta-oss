"""Optional HTTP Basic auth on NeptuneClient.

Neptune authorizes via IAM/network, so auth defaults OFF. But an auth-protected
SPARQL endpoint — e.g. the QC scenario fuzzer's disposable Fuseki sidecar, whose
update endpoint is guarded by an admin password — needs a credential, or its
writes (DROP GRAPH / INSERT) 401. NeptuneClient now accepts an optional
(user, password) that httpx sends as an `Authorization: Basic` header.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from cograph_client.graph.client import NeptuneClient


@pytest.mark.asyncio
async def test_constructor_wires_auth_and_defaults_off():
    with_auth = NeptuneClient("http://store.local", backend="fuseki", auth=("admin", "admin"))
    without = NeptuneClient("http://neptune.local")
    try:
        assert with_auth._client.auth is not None  # BasicAuth wired onto the httpx client
        assert without._client.auth is None  # default: Neptune needs no auth
    finally:
        await with_auth.close()
        await without.close()


@pytest.mark.asyncio
async def test_update_carries_basic_auth_header():
    """A write (the op that 401'd against the guarded Fuseki store) carries the
    Basic credential the constructor wired."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authz"] = request.headers.get("Authorization")
        return httpx.Response(200)

    c = NeptuneClient("http://store.local", backend="fuseki", auth=("admin", "admin"))
    # Swap only the transport, REUSING the auth the constructor produced, so this
    # exercises the constructor wiring rather than re-declaring credentials.
    c._client = httpx.AsyncClient(
        base_url="http://store.local",
        transport=httpx.MockTransport(handler),
        auth=c._client.auth,
    )
    try:
        await c.update("DROP SILENT GRAPH <urn:x>")
    finally:
        await c.close()

    assert seen["authz"] is not None and seen["authz"].startswith("Basic ")
    assert base64.b64decode(seen["authz"].split()[1]).decode() == "admin:admin"


@pytest.mark.asyncio
async def test_no_auth_sends_no_authorization_header():
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authz"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": []}})

    c = NeptuneClient("http://neptune.local")
    c._client = httpx.AsyncClient(
        base_url="http://neptune.local",
        transport=httpx.MockTransport(handler),
        auth=c._client.auth,  # None
    )
    try:
        await c.query("SELECT ?x WHERE { ?x ?p ?o }")
    finally:
        await c.close()

    assert seen["authz"] is None
