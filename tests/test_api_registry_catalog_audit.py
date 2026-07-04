"""Catalog freshness + live-smoke audit (ONTA-194, phase 4).

Covers the offline freshness gate (UNVERIFIED / STALE from ``verified_at``), the
opt-in live smoke (OK / EMPTY / UNREACHABLE / DORMANT / NO_SMOKE via a mocked
executor), the enabled-only exit gate, the Markdown report, and the spec-level
validation of the two new fields (``verified_at`` date, ``smoke_bindings`` keys).
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from cograph_client.api_registry import (
    ApiSourceSpec,
    RegistryApiSource,
    audit_catalog,
    format_markdown,
    make_api_source_catalog,
    validate_spec,
)
from cograph_client.api_registry.catalog import (
    ApiSourceCatalog,
    register_api_source_layer,
    reset_api_source_catalog,
    reset_api_source_layers,
)
from cograph_client.api_registry.catalog_audit import _needs_review, main
from cograph_client.retrieval import safety as safety_mod

TODAY = date(2026, 7, 4)


@pytest.fixture(autouse=True)
def _no_overlays():
    reset_api_source_layers()
    reset_api_source_catalog()
    yield
    reset_api_source_layers()
    reset_api_source_catalog()


@pytest.fixture
def _pub_dns(monkeypatch):
    # Make the executor's DNS-level SSRF guard resolve any host to a public IP.
    monkeypatch.setattr(safety_mod, "_resolve_ips", lambda host: ["93.184.216.34"])


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _entry_dict(**over) -> dict:
    d = {
        "slug": "example_ok",
        "title": "Example",
        "base_url": "https://api.example.com",
        "auth": {"mode": "none"},
        "endpoints": [
            {
                "name": "search",
                "path": "/v1/search",
                "result_path": "results",
                "field_mappings": {"id": "id"},
                "params": [{"name": "q", "target": "q"}],
                "pagination": {"style": "none"},
            }
        ],
    }
    d.update(over)
    return d


def _mk(slug: str, *, verified_at: str = "", enabled: bool = True, smoke=None, auth=None) -> ApiSourceSpec:
    d = _entry_dict(slug=slug, title=slug.title(), enabled=enabled, verified_at=verified_at)
    if auth is not None:
        d["auth"] = auth
    if smoke is not None:
        d["endpoints"][0]["smoke_bindings"] = smoke
    return ApiSourceSpec.from_dict(d)


def _cat(*specs: ApiSourceSpec) -> ApiSourceCatalog:
    return ApiSourceCatalog(entries={s.slug: s for s in specs})


# --------------------------------------------------------------------------- #
# Offline freshness
# --------------------------------------------------------------------------- #
async def test_fresh_entry_has_no_flags():
    [f] = await audit_catalog(today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01")))
    assert f["flags"] == []
    assert f["unverified"] is False and f["stale"] is False
    assert f["age_days"] == 3


async def test_unverified_entry_flagged():
    [f] = await audit_catalog(today=TODAY, catalog=_cat(_mk("a", verified_at="")))
    assert f["unverified"] is True
    assert f["age_days"] is None
    assert "UNVERIFIED" in f["flags"]


async def test_unparseable_verified_at_is_unverified():
    # from_dict keeps the raw string; the audit treats an unparseable date as
    # unverified rather than crashing.
    spec = _mk("a", verified_at="2026-07-01")
    spec.verified_at = "nonsense"
    [f] = await audit_catalog(today=TODAY, catalog=_cat(spec))
    assert f["unverified"] is True and "UNVERIFIED" in f["flags"]


async def test_stale_entry_flagged():
    [f] = await audit_catalog(today=TODAY, max_age_days=180, catalog=_cat(_mk("a", verified_at="2020-01-01")))
    assert f["stale"] is True and "STALE" in f["flags"]
    assert f["age_days"] > 180


async def test_boundary_exactly_at_threshold_is_not_stale():
    # age == max_age_days is fresh; age == max_age_days + 1 is stale.
    on = await audit_catalog(today=TODAY, max_age_days=10, catalog=_cat(_mk("a", verified_at="2026-06-24")))
    assert on[0]["age_days"] == 10 and on[0]["stale"] is False
    over = await audit_catalog(today=TODAY, max_age_days=10, catalog=_cat(_mk("a", verified_at="2026-06-23")))
    assert over[0]["age_days"] == 11 and over[0]["stale"] is True


async def test_future_dated_verified_at_is_flagged_and_gates():
    # A future stamp (a typo) must not read as eternally-fresh.
    [f] = await audit_catalog(today=TODAY, catalog=_cat(_mk("a", verified_at="2027-01-01")))
    assert f["future"] is True and "FUTURE" in f["flags"]
    assert f["stale"] is False  # future is distinct from stale
    assert f["age_days"] < 0
    assert _needs_review([f]) is True


# --------------------------------------------------------------------------- #
# The enabled-only exit gate
# --------------------------------------------------------------------------- #
async def test_needs_review_true_for_enabled_unverified():
    findings = await audit_catalog(today=TODAY, catalog=_cat(_mk("a", verified_at="")))
    assert _needs_review(findings) is True


async def test_disabled_unverified_does_not_gate():
    findings = await audit_catalog(today=TODAY, catalog=_cat(_mk("a", verified_at="", enabled=False)))
    assert findings[0]["unverified"] is True
    assert _needs_review(findings) is False


async def test_all_fresh_does_not_gate():
    findings = await audit_catalog(
        today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01"), _mk("b", verified_at="2026-06-30"))
    )
    assert _needs_review(findings) is False


# --------------------------------------------------------------------------- #
# Live smoke (mocked executor)
# --------------------------------------------------------------------------- #
def _executor(handler) -> RegistryApiSource:
    return RegistryApiSource(transport=httpx.MockTransport(handler))


async def test_live_smoke_ok(_pub_dns):
    ex = _executor(lambda r: httpx.Response(200, json={"results": [{"id": "1"}]}))
    [f] = await audit_catalog(
        today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01", smoke={"q": "x"})),
        live_smoke=True, executor=ex,
    )
    assert f["smoke"] == "OK"
    assert f["flags"] == []


async def test_live_smoke_empty_flags(_pub_dns):
    ex = _executor(lambda r: httpx.Response(200, json={"results": []}))
    [f] = await audit_catalog(
        today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01", smoke={"q": "x"})),
        live_smoke=True, executor=ex,
    )
    assert f["smoke"] == "EMPTY" and "EMPTY" in f["flags"]


async def test_live_smoke_unreachable_flags(_pub_dns):
    ex = _executor(lambda r: httpx.Response(500, text="boom"))
    [f] = await audit_catalog(
        today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01", smoke={"q": "x"})),
        live_smoke=True, executor=ex,
    )
    assert f["smoke"] == "UNREACHABLE" and "UNREACHABLE" in f["flags"]


async def test_live_smoke_dormant_is_not_a_flag(_pub_dns, monkeypatch):
    monkeypatch.delenv("SMOKE_MISSING_KEY", raising=False)
    spec = _mk(
        "a", verified_at="2026-07-01", smoke={"q": "x"},
        auth={"mode": "api_key_query", "key_env": "SMOKE_MISSING_KEY", "query_key": "token"},
    )
    ex = _executor(lambda r: httpx.Response(200, json={"results": [{"id": "1"}]}))
    [f] = await audit_catalog(today=TODAY, catalog=_cat(spec), live_smoke=True, executor=ex)
    assert f["smoke"] == "DORMANT"
    assert f["flags"] == []  # a missing key is expected, not a failure


async def test_live_smoke_no_bindings_is_no_smoke(_pub_dns):
    ex = _executor(lambda r: httpx.Response(200, json={"results": [{"id": "1"}]}))
    [f] = await audit_catalog(
        today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01")),  # no smoke_bindings
        live_smoke=True, executor=ex,
    )
    assert f["smoke"] == "NO_SMOKE"
    assert f["flags"] == []


async def test_offline_audit_never_touches_the_network():
    # live_smoke=False must not smoke even if an entry has smoke_bindings.
    [f] = await audit_catalog(
        today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01", smoke={"q": "x"}))
    )
    assert "smoke" not in f


async def test_disabled_entry_is_not_smoke_tested(_pub_dns):
    # A disabled entry short-circuits the executor to error="disabled"; it must
    # NOT be run under live smoke (which would read as a spurious UNREACHABLE).
    ex = _executor(lambda r: httpx.Response(200, json={"results": [{"id": "1"}]}))
    [f] = await audit_catalog(
        today=TODAY,
        catalog=_cat(_mk("a", verified_at="2026-07-01", smoke={"q": "x"}, enabled=False)),
        live_smoke=True, executor=ex,
    )
    assert "smoke" not in f
    assert "UNREACHABLE" not in f["flags"]


def test_live_smoke_flags_never_flip_the_gate():
    # A fresh entry that live-smokes EMPTY/UNREACHABLE must not fail the gate —
    # the exit gate is offline (freshness) only.
    fresh_but_empty = {"enabled": True, "unverified": False, "stale": False,
                       "future": False, "flags": ["EMPTY"]}
    fresh_but_unreachable = {"enabled": True, "unverified": False, "stale": False,
                             "future": False, "flags": ["UNREACHABLE"]}
    assert _needs_review([fresh_but_empty, fresh_but_unreachable]) is False


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
async def test_format_markdown_summarizes_flags():
    findings = await audit_catalog(
        today=TODAY, catalog=_cat(_mk("fresh_one", verified_at="2026-07-01"), _mk("stale_one", verified_at="2020-01-01"))
    )
    md = format_markdown(findings, max_age_days=180)
    assert "API source catalog freshness report" in md
    assert "stale_one" in md and "fresh_one" in md
    assert "`STALE`" in md
    assert "UNVERIFIED" in md  # the summary line is always present


async def test_format_markdown_clean_report():
    findings = await audit_catalog(today=TODAY, catalog=_cat(_mk("a", verified_at="2026-07-01")))
    md = format_markdown(findings)
    assert "All entries are fresh and verified" in md


# --------------------------------------------------------------------------- #
# Seed catalog is fresh + smoke-configured
# --------------------------------------------------------------------------- #
async def test_seed_catalog_is_fresh_and_passes_gate():
    findings = await audit_catalog(today=TODAY, catalog=make_api_source_catalog())
    assert findings, "seed catalog is empty"
    for f in findings:
        assert f["unverified"] is False, f"{f['slug']} has no verified_at"
    assert _needs_review(findings) is False


def test_all_seed_entries_declare_verified_at_and_smoke():
    for spec in make_api_source_catalog().all():
        assert spec.verified_at, f"{spec.slug} missing verified_at"
        ep = spec.endpoint()
        assert ep is not None and ep.smoke_bindings, f"{spec.slug} missing smoke_bindings"


# --------------------------------------------------------------------------- #
# CLI exit code
# --------------------------------------------------------------------------- #
def test_main_exits_zero_when_all_fresh(capsys):
    # A very large max-age means the shipped (verified) seeds never go stale, so
    # the only way to fail is an unverified entry — of which the seed has none.
    code = main(["--max-age-days", "100000"])
    out = capsys.readouterr().out
    assert code == 0
    assert "API source catalog freshness report" in out


def test_main_exits_one_when_an_enabled_entry_is_unverified(capsys):
    register_api_source_layer("global_enhanced", [_mk("test_unverified", verified_at="")])
    code = main(["--max-age-days", "100000"])
    assert code == 1
    assert "test_unverified" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Spec-level validation of the two new fields
# --------------------------------------------------------------------------- #
def test_invalid_verified_at_is_rejected():
    d = _entry_dict(verified_at="not-a-date")
    assert any("verified_at" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_valid_verified_at_is_accepted():
    d = _entry_dict(verified_at="2026-07-04")
    assert validate_spec(ApiSourceSpec.from_dict(d)) == []


def test_smoke_bindings_unknown_param_is_rejected():
    d = _entry_dict()
    d["endpoints"][0]["smoke_bindings"] = {"nope": "x"}
    assert any("smoke_bindings" in e for e in validate_spec(ApiSourceSpec.from_dict(d)))


def test_smoke_bindings_declared_param_is_accepted():
    d = _entry_dict()
    d["endpoints"][0]["smoke_bindings"] = {"q": "x"}
    assert validate_spec(ApiSourceSpec.from_dict(d)) == []


def test_smoke_bindings_roundtrip_through_dict():
    d = _entry_dict()
    d["endpoints"][0]["smoke_bindings"] = {"q": "x"}
    spec = ApiSourceSpec.from_dict(d)
    rebuilt = ApiSourceSpec.from_dict(spec.to_dict())
    assert rebuilt.endpoint().smoke_bindings == {"q": "x"}
    assert rebuilt.verified_at == spec.verified_at
