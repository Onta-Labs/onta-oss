"""Drift guard (ONTA-340): OSS keyed registry sources are **bring-your-own-key**.

Policy (ONTA-340, boundary doc §26): any API-registry entry (or enrichment /
retrieval adapter) that OSS ships and that requires authentication MUST resolve
its credential from the **user's own** environment (``auth.key_env``) or the
user's own per-tenant secret (``auth.secret_ref``, tenant_custom only). It MUST
be **dormant until that user-supplied credential is present** — never a
platform/shared key baked into (or implied by) the public seed, never an
un-authenticated fallback. "Managed keys" (the platform provisions, holds,
meters, and bills the credential) are a *premium* value-add and live only in the
proprietary overlay (`cograph/api_registry/`), never here.

Deny-by-default, modelled on ``test_write_path_convergence.py`` /
``test_retrieval_path_convergence.py`` / ``test_api_registry_secret_leak_guard.py``:

* **Seed-level** — every OSS ``global_public`` seed (`api_registry/data/*.json`)
  that declares auth references a credential by NAME (`key_env`), never a value;
  a global seed never uses a per-tenant `secret_ref`; no seed embeds a
  credential-shaped literal or a URL with embedded credentials anywhere.
* **Doc-parity** — every keyed seed's ``key_env`` is documented in the repo
  ``.env.example`` so a self-hoster knows they must register for + supply their
  own key (a new keyed seed can't ship silently).
* **Code-level** — no ``api_registry/`` module hardcodes a provider credential
  (a secret-shaped literal, or a hardcoded provider key-env read): the ONLY
  sanctioned credential origins are the generic ``os.environ[auth.key_env]``
  read in the executor and the injected per-tenant ``secret_resolver``.
* **Behavioral** — a keyed spec with the user's key absent goes dormant and
  issues **zero** network requests (no platform fallback, no un-authenticated
  call). This + the seed-level reference check are the primary teeth; the
  secret-shape scanners are a defense-in-depth backstop (they intentionally
  target obvious credential shapes, not every conceivable smuggled value).

Every checker (the seed-level auth-reference / embedded-credential branches, the
doc-parity check, and each secret-shape detector) has a planted-violation
self-test so the guard can't silently rot into a no-op.
"""

from __future__ import annotations

import json
import pathlib
import re

import httpx
import pytest

import cograph_client.api_registry as api_registry_pkg
from cograph_client.api_registry.executor import RegistryApiSource
from cograph_client.api_registry.spec import ApiSourceSpec, AuthMode, validate_spec

_PKG_ROOT = pathlib.Path(api_registry_pkg.__file__).parent
_DATA_DIR = _PKG_ROOT / "data"
# api_registry/__init__.py → api_registry → cograph_client → repo root.
_REPO_ROOT = _PKG_ROOT.parent.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"


# --------------------------------------------------------------------------- #
# Secret-shape detection (tight — anchored + realistic key length so it does NOT
# fire on real seed content like the FRED series id "APU0000712311", host names,
# hyphenated prose ("risk-based", "fc-barcelona"), or descriptive text).
# --------------------------------------------------------------------------- #
# Unambiguous provider credential prefixes at a word boundary, each requiring a
# realistic secret-length tail.
_SECRET_PREFIX = re.compile(
    r"\b(?:"
    r"sk-(?:or-|ant-|proj-)?(?:v\d+-)?[A-Za-z0-9]{16,}"
    r"|fc-[A-Za-z0-9]{16,}"
    r"|csk-[A-Za-z0-9]{16,}"
    r"|gh[posru]_[A-Za-z0-9]{16,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r")"
)
# A long, high-entropy, single-token value (letters AND digits, no spaces/URL).
_HI_ENTROPY = re.compile(r"^[A-Za-z0-9+/_\-=]{40,}$")
# Credentials embedded directly in a URL.
_URL_USERINFO = re.compile(r"://[^/\s@]+:[^/\s@]+@")
_URL_CRED_PARAM = re.compile(
    r"[?&](?:api[_-]?key|apikey|token|access[_-]?token|secret|password|passwd|username|key)="
    r"[^&\s]+",
    re.IGNORECASE,
)


def _looks_like_secret(value: str) -> bool:
    """True when ``value`` looks like a baked-in credential (not seed content)."""
    v = value.strip()
    if _SECRET_PREFIX.search(v):
        return True
    if " " in v or "://" in v:
        return False
    if _HI_ENTROPY.match(v) and any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
        return True
    return False


def _url_embeds_credentials(value: str) -> bool:
    if "://" not in value:
        return False
    return bool(_URL_USERINFO.search(value) or _URL_CRED_PARAM.search(value))


def _walk_strings(node):
    """Yield (dotted-path, string-value) for every string leaf in a JSON tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            for path, s in _walk_strings(v):
                yield (f"{k}.{path}" if path else str(k)), s
    elif isinstance(node, list):
        for i, v in enumerate(node):
            for path, s in _walk_strings(v):
                yield (f"[{i}].{path}" if path else f"[{i}]"), s
    elif isinstance(node, str):
        yield "", node


def _seed_files() -> list[pathlib.Path]:
    files = sorted(_DATA_DIR.glob("*.json"))
    assert files, f"no OSS seed catalog files found under {_DATA_DIR}"
    return files


# --------------------------------------------------------------------------- #
# Per-seed checkers (pure — take a raw seed dict, return a list of violations).
# Kept as functions so the file-iterating tests AND the planted-violation
# self-tests exercise the exact same logic.
# --------------------------------------------------------------------------- #
def _auth_reference_violations(name: str, raw: dict) -> list[str]:
    """A keyed OSS seed authenticates via ``key_env`` (the user's OWN env var),
    never a per-tenant ``secret_ref`` (tenant_custom only) and never a literal
    value. This is the structural definition of bring-your-own-key."""
    spec = ApiSourceSpec.from_dict(raw)
    errs = validate_spec(spec)
    if errs:
        return [f"{name}: invalid spec: {errs}"]
    out: list[str] = []
    if spec.auth.mode is AuthMode.none:
        return out
    if not spec.auth.key_env:
        out.append(
            f"{name}: auth.mode={spec.auth.mode.value} but no auth.key_env — an OSS "
            f"seed must reference the user's OWN env var (bring-your-own-key)"
        )
    if spec.auth.secret_ref:
        out.append(
            f"{name}: auth.secret_ref={spec.auth.secret_ref!r} — a global_public seed "
            f"must not resolve a per-tenant/managed secret; secret_ref is "
            f"tenant_custom only"
        )
    return out


def _embedded_credential_violations(name: str, raw: dict) -> list[str]:
    """No OSS seed may bake in a credential — a secret-shaped literal, a URL with
    embedded credentials, or a static query param that occupies the auth slot
    (which would hardcode the key instead of resolving the user's)."""
    out: list[str] = []
    for field_path, value in _walk_strings(raw):
        if _looks_like_secret(value):
            out.append(f"{name}: {field_path} = {value!r} looks like a baked-in credential")
        if _url_embeds_credentials(value):
            out.append(f"{name}: {field_path} = {value!r} embeds credentials in a URL")
    auth = raw.get("auth") or {}
    query_key = str(auth.get("query_key") or "")
    if query_key:
        for i, ep in enumerate(raw.get("endpoints") or []):
            if query_key in (ep.get("query") or {}):
                out.append(
                    f"{name}: endpoints[{i}].query hardcodes the auth slot {query_key!r} "
                    f"(the credential must be resolved, not baked into the spec)"
                )
    return out


def _undocumented_keyed_seeds(env_text: str, seeds: list[tuple[str, dict]]) -> list[str]:
    """Keyed seeds whose ``key_env`` is not documented in ``.env.example``."""
    missing: list[str] = []
    for name, raw in seeds:
        spec = ApiSourceSpec.from_dict(raw)
        if spec.auth.mode is AuthMode.none or not spec.auth.key_env:
            continue
        if spec.auth.key_env not in env_text:
            missing.append(f"{name}: {spec.auth.key_env}")
    return missing


# --------------------------------------------------------------------------- #
# Seed-level tests over the real OSS seed catalog.
# --------------------------------------------------------------------------- #
def test_every_keyed_seed_references_a_user_env_key():
    violations: list[str] = []
    for path in _seed_files():
        violations += _auth_reference_violations(path.name, json.loads(path.read_text()))
    assert not violations, (
        "OSS keyed seed(s) violate the bring-your-own-key rule (ONTA-340):\n  "
        + "\n  ".join(violations)
    )


def test_no_seed_embeds_a_credential_value():
    violations: list[str] = []
    for path in _seed_files():
        violations += _embedded_credential_violations(path.name, json.loads(path.read_text()))
    assert not violations, (
        "OSS seed(s) embed a credential (ONTA-340):\n  " + "\n  ".join(violations)
    )


def test_every_keyed_seed_is_documented_in_env_example():
    """Doc-parity: a self-hoster learns they must bring their own key from
    ``.env.example``. Every keyed seed's ``key_env`` must appear there so a new
    keyed seed cannot ship without telling self-hosters to register for a key."""
    if not _ENV_EXAMPLE.exists():
        pytest.skip(f"{_ENV_EXAMPLE} not present (installed package, not source tree)")
    seeds = [(p.name, json.loads(p.read_text())) for p in _seed_files()]
    missing = _undocumented_keyed_seeds(_ENV_EXAMPLE.read_text(), seeds)
    assert not missing, (
        "Keyed OSS seed(s) not documented in .env.example (ONTA-340) — a self-hoster "
        "must be told to bring their own key:\n  " + "\n  ".join(missing)
    )


# --------------------------------------------------------------------------- #
# Code-level: the only credential origins are os.environ[auth.key_env] (generic,
# in the executor) and the injected secret_resolver — no hardcoded provider key.
# --------------------------------------------------------------------------- #
# os.environ.get("X") / os.getenv("X") / os.environ["X"] with a LITERAL name.
_ENV_LITERAL_READ = re.compile(
    r"""(?:environ\.get|getenv)\(\s*["']([A-Za-z0-9_]+)["']"""
    r"""|environ\[\s*["']([A-Za-z0-9_]+)["']\s*\]"""
)
# An env name that names a provider credential (as opposed to a config knob).
_CRED_ENV_NAME = re.compile(
    r"(?:API_?KEY|ACCESS_?TOKEN|_TOKEN|_SECRET|_USERNAME|_PASSWORD|_PASSWD)$",
    re.IGNORECASE,
)
# Assigning a long secret-shaped literal to a credential-named variable.
_CODE_HARDCODED_SECRET = re.compile(
    r"""(?ix)\b(?:api_?key|secret|token|password|credential|bearer)\w*\s*=\s*"""
    r"""["']([A-Za-z0-9+/_\-=]{20,})["']"""
)
# The local secrets-encryption key is the user's OWN cipher key, not a provider
# credential — it is the one sanctioned literal env read in api_registry code.
_ENV_ALLOW = {"OMNIX_SECRETS_KEY"}


def _api_registry_py() -> list[pathlib.Path]:
    return sorted(_PKG_ROOT.rglob("*.py"))


def test_no_api_registry_module_hardcodes_a_provider_credential():
    violations: list[str] = []
    for path in _api_registry_py():
        code = path.read_text()
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if _SECRET_PREFIX.search(code):
            violations.append(f"{rel}: contains a provider-key-shaped literal")
        m = _CODE_HARDCODED_SECRET.search(code)
        if m:
            violations.append(f"{rel}: hardcodes a credential-shaped value ({m.group(1)[:8]}…)")
        for a, b in _ENV_LITERAL_READ.findall(code):
            name = a or b
            if name in _ENV_ALLOW:
                continue
            if _CRED_ENV_NAME.search(name):
                violations.append(
                    f"{rel}: reads a hardcoded provider credential env {name!r} — "
                    f"resolution must be generic (auth.key_env), never provider-specific"
                )
    assert not violations, (
        "api_registry code bakes in / hardcodes a credential (ONTA-340). The only "
        "sanctioned origins are os.environ[auth.key_env] (executor) and the injected "
        "secret_resolver.\n  " + "\n  ".join(violations)
    )


# --------------------------------------------------------------------------- #
# Behavioral: user key absent ⇒ dormant, zero network requests (no fallback).
# --------------------------------------------------------------------------- #
def _keyed_spec(*, mode: str, key_env: str = "", secret_ref: str = "") -> ApiSourceSpec:
    auth: dict[str, str] = {"mode": mode}
    if key_env:
        auth["key_env"] = key_env
    if secret_ref:
        auth["secret_ref"] = secret_ref
    if mode == "api_key_query":
        auth["query_key"] = "token"
    elif mode == "api_key_header":
        auth["header_name"] = "X-Api-Key"
    return ApiSourceSpec.from_dict({
        "slug": "byoktest",
        "title": "BYOK Test",
        "base_url": "https://api.byok.example",
        "auth": auth,
        "endpoints": [{
            "name": "default", "method": "GET", "path": "/search",
            "params": [{"name": "q", "location": "query"}],
            "result_path": "results",
            "field_mappings": {"name": "name"},
        }],
    })


@pytest.mark.asyncio
async def test_env_keyed_source_dormant_and_makes_no_request_without_user_key(monkeypatch):
    """With the user's env var UNSET and no resolver, the source is dormant and
    issues no HTTP request — no platform key, no un-authenticated fallback."""
    monkeypatch.delenv("BYOK_UNSET_ENV", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"dormant source must not fetch (called {request.url})")

    ex = RegistryApiSource(transport=httpx.MockTransport(handler))
    spec = _keyed_spec(mode="api_key_query", key_env="BYOK_UNSET_ENV")
    res = await ex.execute(spec, {"q": "x"})

    assert res.dormant is True
    assert res.pages_fetched == 0
    assert res.rows == []
    assert res.error and "dormant" in res.error


@pytest.mark.asyncio
async def test_secret_ref_source_dormant_without_resolver():
    """A ``secret_ref`` source with no injected resolver is dormant (never a
    plaintext fallback, never an un-authenticated call)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"dormant source must not fetch (called {request.url})")

    ex = RegistryApiSource(transport=httpx.MockTransport(handler))
    spec = _keyed_spec(mode="api_key_header", secret_ref="my_secret")
    res = await ex.execute(spec, {"q": "x"}, secret_resolver=None)

    assert res.dormant is True
    assert res.pages_fetched == 0
    assert res.error and "dormant" in res.error


# --------------------------------------------------------------------------- #
# Self-tests: every checker actually fires on a planted violation (and clears a
# clean input) — so a logic inversion in any branch is caught.
# --------------------------------------------------------------------------- #
def _minimal_seed(auth: dict, *, base_url: str = "https://api.example", endpoints=None) -> dict:
    return {
        "slug": "planted", "title": "Planted", "base_url": base_url, "auth": auth,
        "endpoints": endpoints or [{
            "name": "d", "method": "GET", "path": "/s",
            "result_path": "r", "field_mappings": {"n": "n"},
        }],
    }


def test_auth_reference_checker_fires_on_planted_violations():
    # Clean: env-var reference.
    assert not _auth_reference_violations(
        "ok", _minimal_seed({"mode": "api_key_query", "key_env": "X_API_KEY", "query_key": "t"})
    )
    # A keyed seed with no key_env (validate_spec also rejects, so a violation surfaces).
    assert _auth_reference_violations(
        "no_key", _minimal_seed({"mode": "api_key_query", "query_key": "t"})
    )
    # secret_ref on a global seed — validate_spec permits it, so the guard adds value.
    v = _auth_reference_violations(
        "ref", _minimal_seed({"mode": "api_key_header", "secret_ref": "k", "header_name": "X-Api-Key"})
    )
    assert any("secret_ref" in x for x in v)


def test_embed_checker_fires_on_planted_violations():
    # Clean seed embeds nothing.
    assert not _embedded_credential_violations(
        "ok", _minimal_seed({"mode": "api_key_query", "key_env": "X_API_KEY", "query_key": "t"})
    )
    # A static query param that fills the auth slot.
    slot = _minimal_seed(
        {"mode": "api_key_query", "key_env": "X_API_KEY", "query_key": "token"},
        endpoints=[{"name": "d", "method": "GET", "path": "/s", "query": {"token": "demo"},
                    "result_path": "r", "field_mappings": {"n": "n"}}],
    )
    assert any("auth slot" in x for x in _embedded_credential_violations("slot", slot))
    # A URL with embedded credentials.
    assert _embedded_credential_violations(
        "url", _minimal_seed({"mode": "none"}, base_url="https://user:pass@api.example")
    )
    # A baked-in secret literal anywhere.
    lit = _minimal_seed({"mode": "none"})
    lit["tos_note"] = "key sk-or-v1-abcdef0123456789deadbeef"
    assert _embedded_credential_violations("lit", lit)


def test_doc_parity_checker_fires_when_env_example_missing_a_key():
    seeds = [("fred.json", _minimal_seed(
        {"mode": "api_key_query", "key_env": "FRED_API_KEY", "query_key": "api_key"}))]
    assert _undocumented_keyed_seeds("", seeds) == ["fred.json: FRED_API_KEY"]
    assert not _undocumented_keyed_seeds("FRED_API_KEY=...", seeds)
    # An unkeyed seed is never required in .env.example.
    assert not _undocumented_keyed_seeds("", [("x.json", _minimal_seed({"mode": "none"}))])


def test_secret_shape_detector_fires_on_planted_secrets():
    assert _looks_like_secret("sk-or-v1-9f3a2b7c8d9e0f1a2b3c4d5e6f7a8b9c")
    assert _looks_like_secret("fc-1234567890abcdef1234567890abcdef")
    assert _looks_like_secret("AKIAIOSFODNN7EXAMPLE")
    assert _looks_like_secret("9f3a2b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c")  # hi-entropy
    # Must NOT fire on real seed content or innocuous hyphenated prose:
    assert not _looks_like_secret("APU0000712311")           # FRED series id
    assert not _looks_like_secret("api.stlouisfed.org")
    assert not _looks_like_secret("Requires a free GeoNames username.")
    assert not _looks_like_secret("https://fred.stlouisfed.org/docs/api")
    assert not _looks_like_secret("risk-based")
    assert not _looks_like_secret("task-force")
    assert not _looks_like_secret("fc-barcelona")


def test_url_credential_detector_fires_on_planted_urls():
    assert _url_embeds_credentials("https://user:pass@api.example.com/v1")
    assert _url_embeds_credentials("https://api.example.com/v1?api_key=SECRETVALUE")
    assert not _url_embeds_credentials("https://secure.geonames.org")
    assert not _url_embeds_credentials("https://api.stlouisfed.org")


def test_code_credential_scanners_fire_on_planted_code():
    assert _SECRET_PREFIX.search('KEY = "sk-or-v1-abcdef0123456789"')
    assert _CODE_HARDCODED_SECRET.search('api_key = "abcd1234efgh5678ijkl9012"')
    reads = _ENV_LITERAL_READ.findall('os.environ.get("EXA_API_KEY")')
    assert any(_CRED_ENV_NAME.search(a or b) for a, b in reads)
    # The cipher key is allowlisted (user's own encryption key, not a credential).
    assert "OMNIX_SECRETS_KEY" in _ENV_ALLOW
