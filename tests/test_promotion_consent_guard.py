"""Drift guard (ONTA-402a): tenant→global ontology writes require promotion consent.

Product rule: only the governed promotion path may write tenant-originated
shape/type content into Public (A) or Enhanced (B), and only with a recorded
per-workspace consent. Default is no consent.

Two layers, modelled on ``test_layer_content_guard.py`` /
``test_write_path_convergence.py``:

* **Structural** — production modules that perform global ontology writes must
  call ``require_promotion_consent`` (or the async seam) so a NEW writer cannot
  land ungated.
* **Behavioral** — drive ``write_governed_type`` with and without consent and
  assert refuse / proceed. Prove the write path was reached and blocked, not
  skipped.

Planted-violation self-tests prove every violation class the guard claims to
catch actually trips the scan / refusal.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone

import pytest

import infona_client
from infona_client.graph.layers import Layer, layer_type_uri, public_graph_uri
from infona_client.resolver.governance import (
    GovernanceDecision,
    GovernanceEngine,
    JudgeVerdict,
    TypeProposal,
)
from infona_client.resolver.promotion_consent import (
    DenyAllPromotionConsent,
    PromotionConsentError,
    has_promotion_consent,
    register_promotion_consent_provider,
    require_promotion_consent,
)

_PKG_ROOT = pathlib.Path(infona_client.__file__).parent
_CONSENT_HOME = "resolver/promotion_consent.py"
_GOVERNANCE_HOME = "resolver/governance.py"

FIXED_TS = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

# --------------------------------------------------------------------------- #
# Markers (structural)
# --------------------------------------------------------------------------- #
# M1 — a second definition of the refuse seam outside the home module.
_M_REQUIRE_DEF = re.compile(
    r"\basync\s+def\s+require_promotion_consent\s*\("
)
_M_HAS_DEF = re.compile(r"\basync\s+def\s+has_promotion_consent\s*\(")

# M2 — production writes into the Public / Enhanced named graphs via
#     insert_triples(...). Combined with absence of require_promotion_consent
#     in the same file = violation (unless allowlisted).
_M_INSERT_PUBLIC = re.compile(
    r"insert_triples\s*\(\s*public_graph_uri\s*\(\s*\)"
)
_M_INSERT_ENHANCED = re.compile(
    r"insert_triples\s*\(\s*enhanced_graph_uri\s*\(\s*\)"
)
# Premium writer uses global_graph_uri / self._graph_uri; catch the public call
# site that is still in OSS: write_governed_type.
_M_WRITE_GOVERNED = re.compile(r"\basync\s+def\s+write_governed_type\s*\(")

_M_REQUIRE_CALL = re.compile(r"\brequire_promotion_consent\s*\(")


# Allowlist: modules permitted to mention the markers for a documented reason.
_ALLOWLIST: dict[str, str] = {
    "resolver/promotion_consent.py": (
        "single definition site for require_promotion_consent / "
        "has_promotion_consent / PromotionConsentError (ONTA-402a)."
    ),
    "resolver/governance.py": (
        "write_governed_type is the OSS governed promotion write; it MUST call "
        "require_promotion_consent before insert_triples(public_graph_uri()). "
        "Allowlisted as a known write site the behavioral tests pin."
    ),
}


def _iter_py_files():
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            continue
        yield rel, path


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _markers(src: str) -> list[str]:
    marks: list[str] = []
    if _M_REQUIRE_DEF.search(src):
        marks.append("require_promotion_consent definition")
    if _M_HAS_DEF.search(src):
        marks.append("has_promotion_consent definition")
    if _M_INSERT_PUBLIC.search(src) or _M_INSERT_ENHANCED.search(src):
        if not _M_REQUIRE_CALL.search(src):
            marks.append("global insert_triples without require_promotion_consent")
    if _M_WRITE_GOVERNED.search(src) and not _M_REQUIRE_CALL.search(src):
        marks.append("write_governed_type without require_promotion_consent")
    return marks


# --------------------------------------------------------------------------- #
# Structural: single definition site + known write sites are gated
# --------------------------------------------------------------------------- #


def test_require_promotion_consent_defined_only_in_home_module():
    """M1: the refuse seam has one definition site."""
    offenders: list[str] = []
    for rel, path in _iter_py_files():
        if rel == _CONSENT_HOME:
            continue
        src = _read(path)
        if _M_REQUIRE_DEF.search(src) or _M_HAS_DEF.search(src):
            offenders.append(rel)
    assert offenders == [], (
        "require_promotion_consent / has_promotion_consent re-defined outside "
        f"{_CONSENT_HOME}: {offenders}. Import the OSS seam; do not fork it."
    )


def test_write_governed_type_calls_require_promotion_consent():
    """The OSS global write must call the refuse seam before inserting."""
    src = _read(_PKG_ROOT / _GOVERNANCE_HOME)
    assert _M_WRITE_GOVERNED.search(src)
    assert _M_REQUIRE_CALL.search(src), (
        f"{_GOVERNANCE_HOME} defines write_governed_type but never calls "
        "require_promotion_consent — ONTA-402a hard gate is missing."
    )
    # Ordering: require appears before the first public_graph insert.
    require_at = src.index("require_promotion_consent")
    insert_at = src.index("insert_triples(public_graph_uri()")
    assert require_at < insert_at, (
        "require_promotion_consent must run BEFORE insert_triples into Public"
    )


def test_no_ungated_global_insert_outside_allowlist():
    """Deny-by-default: any file that inserts into a global graph without
    calling require_promotion_consent is a violation unless allowlisted with
    a one-line justification."""
    offenders: list[tuple[str, list[str]]] = []
    for rel, path in _iter_py_files():
        if rel in _ALLOWLIST:
            continue
        marks = _markers(_read(path))
        # Only the global-write markers are violations outside the home module.
        bad = [
            m for m in marks
            if m.startswith("global insert") or m.startswith("write_governed")
        ]
        if bad:
            offenders.append((rel, bad))
    assert offenders == [], (
        "Ungated global ontology write(s) (ONTA-402a). Add "
        "require_promotion_consent before the insert, or allowlist with a "
        f"one-line justification: {offenders}"
    )


def test_allowlist_entries_exist_and_are_justified():
    for rel, why in _ALLOWLIST.items():
        assert (_PKG_ROOT / rel).is_file(), f"allowlist path missing: {rel}"
        assert len(why) > 20, f"allowlist justification too thin: {rel}"


# --------------------------------------------------------------------------- #
# Behavioral: write_governed_type refuse / proceed
# --------------------------------------------------------------------------- #


def _proposal(**overrides) -> TypeProposal:
    fields = dict(
        type_name="LoyaltyTier",
        parent_chain=["Tier"],
        tenant_id="acme",
        reasoning="Generic hospitality vocabulary",
        proposer_model="test-model",
    )
    fields.update(overrides)
    return TypeProposal(**fields)


def _decision(*approvals: bool) -> GovernanceDecision:
    votes = [JudgeVerdict(approve=a, reasoning=f"vote-{i}") for i, a in enumerate(approvals)]
    return GovernanceDecision(
        target_layer="public",
        votes=votes,
        approved=bool(votes) and sum(v.approve for v in votes) * 2 > len(votes),
    )


class _AllowTenant:
    def __init__(self, *tenant_ids: str):
        self._ok = frozenset(tenant_ids)

    async def has_consent(self, tenant_id: str, *, target_layer: str = "public") -> bool:
        return tenant_id in self._ok


@pytest.fixture
def mock_neptune():
    from unittest.mock import AsyncMock

    from infona_client.graph.client import NeptuneClient

    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    client.ask.return_value = True
    return client


@pytest.fixture(autouse=True)
def _reset_consent_provider():
    register_promotion_consent_provider(None)
    yield
    register_promotion_consent_provider(None)


@pytest.mark.asyncio
async def test_write_governed_type_refuses_without_consent_zero_writes(mock_neptune):
    """ACCEPTANCE: write path reached, refused, nothing written."""
    engine = GovernanceEngine(mock_neptune)
    # Default deny-all is registered (None → DenyAll).
    with pytest.raises(PromotionConsentError, match="no recorded consent"):
        await engine.write_governed_type(
            _proposal(), _decision(True, True), timestamp=FIXED_TS,
        )
    assert mock_neptune.update.call_count == 0
    assert mock_neptune.ask.await_count == 0


@pytest.mark.asyncio
async def test_write_governed_type_proceeds_with_consent(mock_neptune):
    register_promotion_consent_provider(_AllowTenant("acme"))
    engine = GovernanceEngine(mock_neptune)
    pub_uri = await engine.write_governed_type(
        _proposal(), _decision(True, True), timestamp=FIXED_TS,
    )
    assert pub_uri == layer_type_uri(Layer.PUBLIC, "LoyaltyTier")
    assert mock_neptune.update.call_count >= 3
    assert any(
        f"GRAPH <{public_graph_uri()}>" in c.args[0]
        for c in mock_neptune.update.call_args_list
    )


@pytest.mark.asyncio
async def test_consent_is_per_tenant(mock_neptune):
    register_promotion_consent_provider(_AllowTenant("workspace-a"))
    engine = GovernanceEngine(mock_neptune)

    await engine.write_governed_type(
        _proposal(tenant_id="workspace-a"), _decision(True, True), timestamp=FIXED_TS,
    )
    assert mock_neptune.update.call_count >= 1

    mock_neptune.reset_mock()
    with pytest.raises(PromotionConsentError, match="workspace-b"):
        await engine.write_governed_type(
            _proposal(tenant_id="workspace-b"),
            _decision(True, True),
            timestamp=FIXED_TS,
        )
    assert mock_neptune.update.call_count == 0


@pytest.mark.asyncio
async def test_deny_all_provider_and_default_has_consent():
    assert isinstance(
        # after reset fixture, get default via has_promotion_consent
        await has_promotion_consent("anyone"),
        bool,
    )
    assert await has_promotion_consent("anyone") is False
    register_promotion_consent_provider(DenyAllPromotionConsent())
    with pytest.raises(PromotionConsentError):
        await require_promotion_consent("x", what="planted")


# --------------------------------------------------------------------------- #
# Planted-violation self-tests
# --------------------------------------------------------------------------- #


def test_planted_require_def_is_detected():
    planted = "async def require_promotion_consent(tenant_id, *, target_layer='public', what=''):\n    pass\n"
    assert "require_promotion_consent definition" in _markers(planted)


def test_planted_ungated_insert_is_detected():
    planted = (
        "await self._neptune.update(insert_triples(public_graph_uri(), triples))\n"
    )
    assert "global insert_triples without require_promotion_consent" in _markers(planted)


def test_planted_gated_insert_is_clean():
    clean = (
        "await require_promotion_consent(tenant_id, target_layer='public')\n"
        "await self._neptune.update(insert_triples(public_graph_uri(), triples))\n"
    )
    assert _markers(clean) == []


def test_planted_write_governed_without_require_is_detected():
    planted = "async def write_governed_type(self, proposal, decision):\n    pass\n"
    assert "write_governed_type without require_promotion_consent" in _markers(planted)


def test_home_module_exports_error_and_default_deny():
    from infona_client.resolver import promotion_consent as pc

    assert issubclass(pc.PromotionConsentError, PermissionError)
    assert pc.get_promotion_consent_provider() is not None
