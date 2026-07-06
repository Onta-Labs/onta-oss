"""ONTA-2xx Child 2 — encrypted credential storage for tenant-custom sources.

Covers: cipher encrypt/decrypt round-trip + AAD binding + integrity failure;
the pluggable-cipher seam (register override + env default + fail-closed None);
the encrypted secret store (ciphertext-only at rest, tenant-scoped, per-slot AAD);
the executor's tenant_secret resolution (host-scoping preserved, dormant without
a resolver/secret, secret never in the result/provenance); and a structural
leak assertion that a plaintext secret never appears in a serialized spec.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cograph_client.api_registry.crypto import (
    LocalAesGcmCipher,
    SecretCipherError,
    ciphertext_scheme,
    get_secret_cipher,
    register_secret_cipher,
    reset_secret_cipher,
)
from cograph_client.api_registry.executor import RegistryApiSource
from cograph_client.api_registry.secret_store import (
    InMemoryTenantSecretStore,
    make_secret_resolver,
    make_tenant_secret_store,
    reset_tenant_secret_store,
    resolve_secret,
    secret_aad,
    store_secret,
)
from cograph_client.api_registry.spec import ApiSourceSpec, validate_spec

_SECRET = "sk-super-secret-value-12345"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_secret_cipher()
    reset_tenant_secret_store()
    monkeypatch.delenv("OMNIX_SECRETS_KEY", raising=False)
    yield
    reset_secret_cipher()
    reset_tenant_secret_store()


# --------------------------------------------------------------------------- #
# Cipher round-trip + integrity
# --------------------------------------------------------------------------- #
def test_cipher_roundtrip_and_format():
    c = LocalAesGcmCipher.from_env("a-local-passphrase")
    tok = c.encrypt(_SECRET, aad="t/slug/api_key")
    assert tok.startswith("v1.aesgcm.")
    assert ciphertext_scheme(tok) == "aesgcm"
    assert _SECRET not in tok  # ciphertext must not contain the plaintext
    assert c.decrypt(tok, aad="t/slug/api_key") == _SECRET


def test_cipher_nonce_is_random_per_encryption():
    c = LocalAesGcmCipher.from_env("k")
    a = c.encrypt(_SECRET, aad="x")
    b = c.encrypt(_SECRET, aad="x")
    assert a != b  # distinct nonces => distinct ciphertexts for the same plaintext


def test_cipher_aad_binding_rejects_moved_ciphertext():
    c = LocalAesGcmCipher.from_env("k")
    tok = c.encrypt(_SECRET, aad="tenant-a/slug/api_key")
    # Same key, different slot (aad) => authentication fails.
    with pytest.raises(SecretCipherError):
        c.decrypt(tok, aad="tenant-b/slug/api_key")


def test_cipher_wrong_key_fails_closed():
    tok = LocalAesGcmCipher.from_env("key-one").encrypt(_SECRET, aad="x")
    with pytest.raises(SecretCipherError):
        LocalAesGcmCipher.from_env("key-two").decrypt(tok, aad="x")


def test_cipher_tampered_ciphertext_fails():
    c = LocalAesGcmCipher.from_env("k")
    tok = c.encrypt(_SECRET, aad="x")
    # Flip a payload char (keep the v1.aesgcm. prefix).
    prefix, b64 = tok.rsplit(".", 1)
    tampered = prefix + "." + ("A" if b64[0] != "A" else "B") + b64[1:]
    with pytest.raises(SecretCipherError):
        c.decrypt(tampered, aad="x")


def test_cipher_error_message_never_leaks_secret_or_key():
    tok = LocalAesGcmCipher.from_env("key-one").encrypt(_SECRET, aad="x")
    try:
        LocalAesGcmCipher.from_env("key-two").decrypt(tok, aad="x")
    except SecretCipherError as exc:
        msg = str(exc)
        assert _SECRET not in msg
        assert "key-two" not in msg and "key-one" not in msg


# --------------------------------------------------------------------------- #
# Cipher seam (register / env default / fail-closed)
# --------------------------------------------------------------------------- #
def test_no_cipher_configured_returns_none():
    # No OMNIX_SECRETS_KEY and no registered cipher => None (fail closed).
    assert get_secret_cipher() is None


def test_env_key_builds_default_cipher(monkeypatch):
    monkeypatch.setenv("OMNIX_SECRETS_KEY", "env-provided-key")
    reset_secret_cipher()
    cipher = get_secret_cipher()
    assert cipher is not None and cipher.scheme == "aesgcm"


def test_registered_cipher_overrides_env(monkeypatch):
    monkeypatch.setenv("OMNIX_SECRETS_KEY", "env-key")
    reset_secret_cipher()

    class _FakeKms:
        scheme = "kms"

        def encrypt(self, plaintext, *, aad=""):
            return f"v1.kms.{plaintext[::-1]}"

        def decrypt(self, token, *, aad=""):
            return token.split(".", 2)[2][::-1]

    register_secret_cipher(_FakeKms())
    assert get_secret_cipher().scheme == "kms"


# --------------------------------------------------------------------------- #
# Encrypted secret store
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_store_holds_ciphertext_only_and_roundtrips():
    cipher = LocalAesGcmCipher.from_env("k")
    store = InMemoryTenantSecretStore()
    await store_secret(
        store, cipher, tenant_id="t1", slug="acme", logical_name="api_key",
        plaintext=_SECRET,
    )
    row = await store.get("t1", "acme", "api_key")
    assert row is not None
    # At rest: ciphertext only — the plaintext is nowhere in the stored row.
    assert _SECRET not in row.ciphertext
    assert row.scheme == "aesgcm"
    # Resolve decrypts back to the original.
    got = await resolve_secret(
        store, cipher, tenant_id="t1", slug="acme", logical_name="api_key"
    )
    assert got == _SECRET


@pytest.mark.asyncio
async def test_secret_store_is_tenant_and_slug_scoped():
    cipher = LocalAesGcmCipher.from_env("k")
    store = InMemoryTenantSecretStore()
    await store_secret(store, cipher, tenant_id="t1", slug="acme", logical_name="api_key", plaintext="A")
    await store_secret(store, cipher, tenant_id="t2", slug="acme", logical_name="api_key", plaintext="B")

    # Another tenant cannot read t1's secret (and vice versa).
    assert await store.get("t2", "acme", "api_key") is not None
    a = await resolve_secret(store, cipher, tenant_id="t1", slug="acme", logical_name="api_key")
    b = await resolve_secret(store, cipher, tenant_id="t2", slug="acme", logical_name="api_key")
    assert a == "A" and b == "B"
    # Missing slot => None, never another slot's value.
    assert await resolve_secret(store, cipher, tenant_id="t1", slug="other", logical_name="api_key") is None


@pytest.mark.asyncio
async def test_delete_for_source_removes_all_slots():
    cipher = LocalAesGcmCipher.from_env("k")
    store = InMemoryTenantSecretStore()
    await store_secret(store, cipher, tenant_id="t1", slug="acme", logical_name="api_key", plaintext="A")
    await store_secret(store, cipher, tenant_id="t1", slug="acme", logical_name="bearer", plaintext="B")
    assert await store.list_names("t1", "acme") == ["api_key", "bearer"]
    await store.delete_for_source("t1", "acme")
    assert await store.list_names("t1", "acme") == []


def test_secret_aad_is_slot_specific():
    assert secret_aad("t", "s", "n") == "t/s/n"
    assert secret_aad("t", "s", "n") != secret_aad("t", "s", "other")


# --------------------------------------------------------------------------- #
# Executor: tenant_secret resolution + host-scoping preserved
# --------------------------------------------------------------------------- #
def _secret_ref_spec() -> ApiSourceSpec:
    return ApiSourceSpec.from_dict({
        "slug": "acme_private",
        "title": "Acme Private API",
        "base_url": "https://api.acme.example",
        "auth": {"mode": "api_key_header", "secret_ref": "api_key", "header_name": "X-Acme-Key"},
        "endpoints": [{
            "name": "default", "method": "GET", "path": "/search",
            "params": [{"name": "q", "location": "query"}],
            "result_path": "results",
            "field_mappings": {"name": "name"},
        }],
    })


@pytest.mark.asyncio
async def test_executor_resolves_tenant_secret_and_scopes_to_host():
    spec = _secret_ref_spec()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["auth_header"] = request.headers.get("X-Acme-Key")
        return httpx.Response(200, json={"results": [{"name": "row1"}]})

    ex = RegistryApiSource(transport=httpx.MockTransport(handler))

    async def resolver(name):
        assert name == "api_key"
        return _SECRET

    res = await ex.execute(spec, {"q": "x"}, secret_resolver=resolver)
    assert res.error is None
    assert seen["auth_header"] == _SECRET  # decrypted secret reached the request
    # Host-scoping preserved: the secret must never appear in the stored/display
    # URL, provenance, or sources (only in the request header, on the base host).
    assert seen["host"] == "api.acme.example"
    for url in res.sources:
        assert _SECRET not in url
    for url in res.provenance.values():
        assert _SECRET not in url
    assert _SECRET not in json.dumps(res.to_dict())


@pytest.mark.asyncio
async def test_executor_dormant_without_resolver():
    spec = _secret_ref_spec()
    ex = RegistryApiSource(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    res = await ex.execute(spec, {"q": "x"}, secret_resolver=None)
    assert res.dormant is True and res.error and "secret resolver" in res.error


@pytest.mark.asyncio
async def test_executor_dormant_when_secret_absent():
    spec = _secret_ref_spec()
    ex = RegistryApiSource(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    async def empty_resolver(name):
        return None

    res = await ex.execute(spec, {"q": "x"}, secret_resolver=empty_resolver)
    assert res.dormant is True and "not set" in (res.error or "")


@pytest.mark.asyncio
async def test_executor_secret_not_attached_on_redirect_to_other_host():
    spec = _secret_ref_spec()
    hosts_with_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Acme-Key"):
            hosts_with_auth.append(request.url.host)
        if request.url.host == "api.acme.example":
            # Redirect off-host — the secret must NOT follow.
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, json={"results": []})

    ex = RegistryApiSource(transport=httpx.MockTransport(handler))

    async def resolver(name):
        return _SECRET

    await ex.execute(spec, {"q": "x"}, secret_resolver=resolver)
    # The auth header was only ever sent to the registered base host.
    assert hosts_with_auth == ["api.acme.example"]
    assert "evil.example" not in hosts_with_auth


# --------------------------------------------------------------------------- #
# make_secret_resolver wiring
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_make_secret_resolver_none_without_cipher():
    # No cipher configured => no resolver (executor then treats as dormant).
    assert make_secret_resolver("t1", "acme") is None


@pytest.mark.asyncio
async def test_make_secret_resolver_roundtrips_via_process_store(monkeypatch):
    monkeypatch.setenv("OMNIX_SECRETS_KEY", "proc-key")
    reset_secret_cipher()
    reset_tenant_secret_store()
    cipher = get_secret_cipher()
    store = make_tenant_secret_store()
    await store_secret(store, cipher, tenant_id="t1", slug="acme", logical_name="api_key", plaintext=_SECRET)

    resolver = make_secret_resolver("t1", "acme")
    assert resolver is not None
    assert await resolver("api_key") == _SECRET
    # A different tenant's resolver cannot read t1's secret.
    other = make_secret_resolver("t2", "acme")
    assert await other("api_key") is None


# --------------------------------------------------------------------------- #
# Structural: a plaintext secret never appears in a serialized spec
# --------------------------------------------------------------------------- #
def test_spec_carries_secret_ref_not_secret_value():
    spec = _secret_ref_spec()
    dumped = json.dumps(spec.to_dict())
    assert "secret_ref" in dumped
    assert "api_key" in dumped  # the logical NAME is fine to serialize
    # The spec must never carry a secret value field.
    d = spec.to_dict()
    assert "secrets" not in d
    assert "secret" not in d.get("auth", {})
    assert not any("value" in k.lower() for k in d.get("auth", {}))


def test_validate_rejects_both_key_env_and_secret_ref():
    spec = ApiSourceSpec.from_dict({
        "slug": "x", "title": "X", "base_url": "https://api.example.com",
        "auth": {"mode": "bearer", "key_env": "X_KEY", "secret_ref": "api_key"},
        "endpoints": [{"name": "default", "method": "GET", "path": "/", "field_mappings": {"a": "a"}}],
    })
    errs = validate_spec(spec)
    assert any("only one of" in e for e in errs)


def test_validate_accepts_secret_ref_alone():
    errs = validate_spec(_secret_ref_spec())
    assert errs == []


def test_validate_rejects_secret_ref_with_mode_none():
    spec = ApiSourceSpec.from_dict({
        "slug": "x", "title": "X", "base_url": "https://api.example.com",
        "auth": {"mode": "none", "secret_ref": "api_key"},
        "endpoints": [{"name": "default", "method": "GET", "path": "/", "field_mappings": {"a": "a"}}],
    })
    errs = validate_spec(spec)
    assert any("mode=none" in e for e in errs)
