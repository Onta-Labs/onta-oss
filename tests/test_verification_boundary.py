"""Frozen CHARACTERIZATION of the epistemic A4 verified-fact artifact (ONTA-361).

This is the deterministic freeze/diff guard for the P4 Verify stage — the epistemic
sibling of ``qc/boundary.py``'s MECHANICAL a2/a3/a4/a5 tiers, kept DELIBERATELY
SEPARATE so the two "A4"s never collide:

  * ``qc/boundary.py`` freezes the mechanical A4 (``validate_triple`` output) under
    ``tests/fixtures/boundary/*.a4.json`` — those stay byte-identical (this file
    NEVER touches them, and asserts as much via ``boundary.check() == []``).
  * THIS file freezes the epistemic A4 (:class:`VerifiedFact`) — the DEFAULT OFFLINE
    verifier's verdict over each domain's canonical A3 clean facts — under a separate
    namespace, ``tests/fixtures/verification/*.a4verify.json``.

Determinism: the offline verifier is pure + network-free (every fact → UNVERIFIABLE,
no evidence), and the A3 corpus is re-derived from the same canonical decomp datasets
``qc/boundary.py`` uses. The envelope's wall-clock ``observed_at`` is the ONLY
non-deterministic field, so the frozen projection drops it; ``run_id`` / ``workspace_id``
are pinned constants, making every ``fact_id`` reproducible.

  # regenerate the frozen fixtures (offline, one command):
  PYTHONPATH="$PWD" python tests/test_verification_boundary.py --freeze
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cograph_client.qc import boundary as b
from cograph_client.resolver.models import CleanFact, CleanOutcome
from cograph_client.verification import (
    DefaultOfflineVerifier,
    TruthVerdict,
    get_fact_verifier,
    register_fact_verifier,
    verify_clean_facts,
)

DOMAINS = list(b.DEFAULT_DOMAINS)

# The epistemic-A4 tier label + its fixture namespace — distinct from qc/boundary's
# a2/a3/a4/a5 so the two artifacts can never be confused on disk.
TIER = "a4verify"

# Pinned envelope scope so every derived fact_id is reproducible in the frozen fixture.
_WORKSPACE_ID = "boundary"
_RUN_ID = "boundary-verify"

# A policy object that duck-types as ON, so the render actually CONSULTS the offline
# verifier (characterizing its real output) rather than the off/passthrough branch.
_ON_POLICY = type("_On", (), {"enabled": True})()


def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "verification"


def _fixture_path(out_dir: Path, domain: str) -> Path:
    return out_dir / f"{domain}.{TIER}.json"


def _clean_facts_for(domain: str) -> list[CleanFact]:
    """Reconstruct the domain's canonical A3 clean facts from qc/boundary's a3
    artifact — the SAME corpus, so the two harnesses stay in lock-step."""
    a3 = b.render_domain(domain).a3
    return [
        CleanFact(
            datatype=f["datatype"],
            raw_value=f["raw_value"],
            clean_value=f["clean_value"],
            outcome=CleanOutcome(f["outcome"]),
            reason=f.get("reason", ""),
            entity_id=f["entity_id"],
            attribute=f["attribute"],
        )
        for f in a3["clean_facts"]
    ]


def _stable_envelope(env_dict: dict) -> dict:
    """The envelope projection MINUS the wall-clock ``observed_at`` (the only
    non-deterministic field), so the frozen fixture is byte-stable."""
    return {k: v for k, v in env_dict.items() if k != "observed_at"}


def _stable_fact(vf) -> dict:
    d = vf.to_dict()
    d["envelope"] = _stable_envelope(d["envelope"])
    return d


def render_domain(domain: str) -> dict:
    """Render the epistemic-A4 artifact for one domain via the DEFAULT OFFLINE
    verifier, as a deterministic JSON-ready dict."""
    # Render explicitly against the offline default — never a verifier a test left
    # registered — so this characterization is stable regardless of global state.
    register_fact_verifier(None)
    facts = _clean_facts_for(domain)
    verified = verify_clean_facts(
        facts, _ON_POLICY, workspace_id=_WORKSPACE_ID, run_id=_RUN_ID,
        verifier=DefaultOfflineVerifier(),
    )
    rows = sorted(
        (_stable_fact(v) for v in verified),
        key=lambda d: (d["entity_id"], d["attribute"], d["value"] or "", d["datatype"]),
    )
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"verified_facts": rows, "verdict_counts": counts, "total": len(rows)}


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def freeze(out_dir: Path | None = None) -> list[Path]:
    out = out_dir or fixtures_dir()
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for domain in DOMAINS:
        path = _fixture_path(out, domain)
        path.write_text(_dumps(render_domain(domain)))
        written.append(path)
    return written


# --------------------------------------------------------------------------- #
# The diff guard: a re-render must byte-match the frozen fixture, per domain.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("domain", DOMAINS)
def test_frozen_epistemic_a4_matches_rerender(domain):
    path = _fixture_path(fixtures_dir(), domain)
    assert path.exists(), f"missing frozen fixture {path} — run `--freeze`"
    assert _dumps(render_domain(domain)) == path.read_text(), (
        f"{domain}.{TIER} drifted from its frozen fixture. If intentional, re-freeze "
        f"with `python tests/test_verification_boundary.py --freeze`."
    )


@pytest.mark.parametrize("domain", DOMAINS)
def test_offline_render_is_all_unverifiable_with_no_evidence(domain):
    """The offline default corroborates nothing: every canonical fact is UNVERIFIABLE
    with empty evidence — a fact is not 'verified' until an independent source says so."""
    payload = render_domain(domain)
    assert payload["total"] > 0
    assert set(payload["verdict_counts"]) == {TruthVerdict.UNVERIFIABLE.value}
    for row in payload["verified_facts"]:
        assert row["verdict"] == "unverifiable"
        assert row["evidence"] == []
        assert row["confidence"] == 0.0


def test_render_is_deterministic():
    for domain in DOMAINS:
        assert _dumps(render_domain(domain)) == _dumps(render_domain(domain))


# --------------------------------------------------------------------------- #
# The naming-collision guard: the mechanical boundary fixtures are UNTOUCHED.
# --------------------------------------------------------------------------- #
def test_mechanical_boundary_fixtures_stay_byte_stable():
    """This epistemic characterization must not perturb qc/boundary's a2/a3/a4/a5 —
    equivalent to `python -m cograph_client.qc.boundary --check` exiting 0."""
    assert b.check() == []
    assert b.TIERS == ("a2", "a3", "a4", "a5")  # our tier is NOT appended to theirs


def test_epistemic_tier_namespace_is_separate_from_mechanical():
    """Our fixtures live under a distinct name+dir so the two A4s never collide."""
    assert TIER == "a4verify" and TIER not in b.TIERS
    assert fixtures_dir().name == "verification"
    assert fixtures_dir() != b.default_fixtures_dir()


if __name__ == "__main__":
    if "--freeze" in sys.argv:
        for p in freeze():
            print(f"froze {p}")
    else:
        print("usage: python tests/test_verification_boundary.py --freeze")
