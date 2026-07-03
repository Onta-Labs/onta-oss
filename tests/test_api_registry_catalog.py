"""CI validation for the API source registry catalog (ONTA-194, phase 1).

This is the schema + URL-lint gate that guarantees no malformed or unsafe
catalog entry can ship. It scans the OSS seed catalog (``global_public``) the
package actually ships and asserts every entry:

  * parses into an ``ApiSourceSpec`` and passes full structural validation,
  * has an https base_url / docs_url that resolves to a public host (URL lint —
    the static half of the executor's SSRF guard applied at CI time),
  * carries no secret-shaped value (credentials are env-var *names* only),
  * gates paid entries on an env key (the dormancy contract).

It also carries guard self-tests that plant known-bad entries to prove the
validator + URL lint actually reject them (modeled on the write-path drift
guard's self-tests).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cograph_client.api_registry import (
    ApiSourceSpec,
    Entitlement,
    make_api_source_catalog,
    url_lint_errors,
    validate_spec,
)
from cograph_client.api_registry.catalog import (
    _DATA_DIR,
    load_catalog_dir,
    reset_api_source_layers,
)

# Load the shipped OSS seed by parsing the RAW files directly (NOT through
# load_catalog_dir, which is tolerant and silently skips invalid entries). This
# makes the validation tests a real gate: a malformed/unsafe file that ships will
# fail here, instead of being silently dropped by the loader.
_SEED_FILES = sorted(_DATA_DIR.glob("*.json"))


def _load_raw(path) -> ApiSourceSpec:
    return ApiSourceSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))


_SEED = [_load_raw(p) for p in _SEED_FILES]
_SEED_IDS = [s.slug for s in _SEED]

# Secret-shaped strings must never appear in a catalog value (defense in depth;
# scripts/check_boundary.sh also scans these files).
_SECRET_RE = re.compile(
    r"sk-ant-[A-Za-z0-9]{16}|ak_[A-Z0-9]{24}|AIza[0-9A-Za-z_-]{30}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
)


@pytest.fixture(autouse=True)
def _no_overlays():
    """Keep tests hermetic — no premium overlay leaks in from another test."""
    reset_api_source_layers()
    yield
    reset_api_source_layers()


# --------------------------------------------------------------------------- #
# The seed exists and is well-formed
# --------------------------------------------------------------------------- #
def test_seed_catalog_is_non_empty():
    assert _SEED, "OSS seed catalog is empty"


def test_loader_drops_no_shipped_entry():
    # The tolerant loader silently skips invalid entries; if a shipped file were
    # invalid, the loader count would be < the raw file count. This asserts every
    # shipped file survives the loader (i.e. is valid), independent of the
    # parametrized validation below.
    loaded = load_catalog_dir(_DATA_DIR, layer="global_public")
    assert len(loaded) == len(_SEED_FILES), (
        "loader dropped a shipped seed entry (invalid?) — "
        f"{len(loaded)} loaded vs {len(_SEED_FILES)} files"
    )


def test_seed_contains_flagship_nppes():
    assert "nppes" in _SEED_IDS, "flagship NPPES entry is missing from the seed"


def test_make_catalog_loads_seed_by_slug():
    cat = make_api_source_catalog()
    assert set(cat.slugs()) >= set(_SEED_IDS)
    for slug in _SEED_IDS:
        assert cat.get(slug) is not None


def test_slugs_are_unique():
    assert len(_SEED_IDS) == len(set(_SEED_IDS))


@pytest.mark.parametrize("spec", _SEED, ids=_SEED_IDS)
def test_entry_is_schema_valid(spec: ApiSourceSpec):
    errors = validate_spec(spec)
    assert not errors, f"{spec.slug} failed validation: {errors}"


@pytest.mark.parametrize("spec", _SEED, ids=_SEED_IDS)
def test_entry_urls_pass_lint(spec: ApiSourceSpec):
    assert not url_lint_errors(spec.base_url, field_name="base_url")
    if spec.docs_url:
        assert not url_lint_errors(spec.docs_url, field_name="docs_url")


@pytest.mark.parametrize("spec", _SEED, ids=_SEED_IDS)
def test_entry_roundtrips_through_dict(spec: ApiSourceSpec):
    rebuilt = ApiSourceSpec.from_dict(spec.to_dict())
    assert rebuilt.slug == spec.slug
    assert rebuilt.to_dict() == spec.to_dict()


@pytest.mark.parametrize("spec", _SEED, ids=_SEED_IDS)
def test_paid_entries_gate_on_a_key(spec: ApiSourceSpec):
    if spec.entitlement is Entitlement.paid:
        assert spec.auth.key_env, f"{spec.slug} is paid but declares no auth.key_env"


def test_no_secret_shaped_values_in_data_files():
    for path in sorted(_DATA_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert not _SECRET_RE.search(text), f"secret-shaped string in {path.name}"


def test_key_env_holds_a_name_not_a_value():
    # Env-var names look like UPPER_SNAKE; a value would not.
    name_re = re.compile(r"^[A-Z][A-Z0-9_]*$")
    for spec in _SEED:
        if spec.auth.key_env:
            assert name_re.match(spec.auth.key_env), (
                f"{spec.slug}.auth.key_env={spec.auth.key_env!r} is not an env-var NAME"
            )


# --------------------------------------------------------------------------- #
# Guard self-tests — prove the validator + URL lint reject known-bad entries
# --------------------------------------------------------------------------- #
def _good_entry() -> dict:
    return {
        "slug": "example_ok",
        "title": "Example",
        "base_url": "https://api.example.com",
        "auth": {"mode": "none"},
        "endpoints": [
            {
                "name": "search",
                "path": "/v1/search",
                "field_mappings": {"id": "id"},
                "params": [{"name": "q", "target": "q"}],
                "pagination": {"style": "none"},
            }
        ],
    }


def test_guard_accepts_a_good_entry():
    assert validate_spec(ApiSourceSpec.from_dict(_good_entry())) == []


def test_loader_silently_skips_a_bad_file_but_raw_validation_catches_it(tmp_path):
    # Proves the gate has teeth: an unsafe entry (http base_url, private host, no
    # field_mappings) is SILENTLY dropped by the tolerant loader (why the
    # raw-file validation above is necessary), and raw validation flags it.
    bad = {
        "slug": "evilapi", "title": "Evil",
        "base_url": "http://169.254.169.254",
        "endpoints": [{"name": "s", "path": "/s", "field_mappings": {}}],
    }
    (tmp_path / "good.json").write_text(json.dumps(_good_entry()))
    (tmp_path / "evilapi.json").write_text(json.dumps(bad))

    loaded = load_catalog_dir(tmp_path, layer="global_public")
    assert {s.slug for s in loaded} == {"example_ok"}, "loader should drop evilapi silently"

    raw_errors = validate_spec(ApiSourceSpec.from_dict(bad))
    assert raw_errors, "raw validation must catch the bad entry the loader hid"


def test_guard_rejects_missing_slug():
    d = _good_entry()
    d["slug"] = ""
    assert any("slug" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_guard_rejects_http_base_url():
    d = _good_entry()
    d["base_url"] = "http://api.example.com"
    assert any("https" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_guard_rejects_private_host_base_url():
    for host in ("http://127.0.0.1", "https://10.0.0.5", "https://localhost", "https://foo.internal"):
        errs = url_lint_errors(host, field_name="base_url")
        assert errs, f"URL lint failed to flag {host}"


def test_guard_rejects_endpoint_without_field_mappings():
    d = _good_entry()
    d["endpoints"][0]["field_mappings"] = {}
    assert any("field_mappings" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_guard_rejects_offset_pagination_without_offset_param():
    d = _good_entry()
    d["endpoints"][0]["pagination"] = {"style": "offset", "limit_param": "limit", "page_size": 10}
    assert any("offset_param" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_guard_rejects_cursor_pagination_without_cursor_fields():
    d = _good_entry()
    d["endpoints"][0]["pagination"] = {"style": "cursor", "page_size": 10}
    assert any("cursor" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_guard_rejects_path_placeholder_without_param():
    d = _good_entry()
    d["endpoints"][0]["path"] = "/v1/product/{gtin}.json"
    # no path param declared for {gtin}
    assert any("placeholder" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_guard_rejects_paid_entry_without_auth():
    d = _good_entry()
    d["entitlement"] = "paid"
    assert any("paid" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_guard_rejects_api_key_query_without_query_key():
    d = _good_entry()
    d["auth"] = {"mode": "api_key_query", "key_env": "SOME_KEY"}
    assert any("query_key" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))
