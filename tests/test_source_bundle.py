"""Unit tests for the A1 Source Bundle artifact (ONTA-346).

Discovery-run acceptance (``WebIngestCapability.execute``) lives in premium
``infona/web_ingest/tests``. This file pins the OSS builder: secret_refs-ONLY,
tier validation, and deterministic fact-id lineage.
"""

from __future__ import annotations

import json

import pytest

from infona_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id
from infona_client.pipeline.source_bundle import (
    KNOWN_TIERS,
    TIER_AUTHORITATIVE,
    TIER_WEB,
    SourceBundle,
    SourceRow,
    build_source_bundle,
    is_secret_ref,
)

# Distinctive sentinel so a leaked resolved credential is unmistakable.
PLAINTEXT_CREDENTIAL = "sk-live-PLAINTEXT-SHOULD-NEVER-LEAK-9f8e7d"


def test_builder_lineage_and_shape():
    rows = [
        {"name": "A", "source_url": "https://x.test/a"},
        {"name": "B", "source_url": "https://x.test/b"},
    ]
    bundle = build_source_bundle(
        rows,
        workspace_id="ws-1",
        run_id="run-1",
        provider="acme_api",
        tier=TIER_AUTHORITATIVE,
        secret_refs=["acme_secret"],
        key_attribute="name",
        bundle_key="acme_api:q",
    )
    assert isinstance(bundle.envelope, ArtifactEnvelope)
    assert bundle.workspace_id == "ws-1"
    assert bundle.run_id == "run-1"
    assert bundle.envelope.parent_fact_ids == ()
    assert len(bundle.rows) == 2
    assert bundle.rows[0].source_url == "https://x.test/a"
    assert all(r.tier == TIER_AUTHORITATIVE for r in bundle.rows)
    assert len(set(bundle.fact_ids)) == 2
    assert all(fid != bundle.envelope.fact_id for fid in bundle.fact_ids)
    rows[0]["name"] = "MUTATED"
    assert bundle.rows[0].data["name"] == "A"


def test_builder_is_deterministic():
    """Replaying the same run mints the same fact_ids (provenance-derived ids)."""
    rows = [{"name": "A"}, {"name": "B"}]
    kw = dict(
        workspace_id="ws", run_id="run", provider="p",
        tier=TIER_WEB, key_attribute="name", bundle_key="p:q",
    )
    b1 = build_source_bundle(rows, **kw)
    b2 = build_source_bundle(rows, **kw)
    assert b1.envelope.fact_id == b2.envelope.fact_id
    assert b1.fact_ids == b2.fact_ids


def test_secret_refs_only_rejects_resolved_credential():
    """The constructor rejects anything that isn't a well-formed logical secret
    reference — a resolved/decrypted credential can never be smuggled through."""
    env = ArtifactEnvelope(
        workspace_id="ws", run_id="run",
        fact_id=derive_fact_id(run_id="run", stage="A1", local_key="p"),
    )
    for bad in [
        PLAINTEXT_CREDENTIAL,
        "AKIAIOSFODNN7EXAMPLE",  # boundary-ok: AWS docs example, not a credential
        "Zm9vYmFy=",
        "a b c",
        "secret/with/slash",
        "x" * 65,
    ]:
        assert not is_secret_ref(bad)
        with pytest.raises(ValueError):
            SourceBundle(envelope=env, rows=(), secret_refs=(bad,))
    ok = SourceBundle(envelope=env, rows=(), secret_refs=("acme_secret", "acme_secret"))
    assert ok.secret_refs == ("acme_secret",)


def test_tier_validation():
    env = ArtifactEnvelope(
        workspace_id="ws", run_id="run",
        fact_id=derive_fact_id(run_id="run", stage="A1", local_key="p"),
    )
    assert TIER_AUTHORITATIVE in KNOWN_TIERS and TIER_WEB in KNOWN_TIERS
    with pytest.raises(ValueError):
        SourceRow(fact_id="f1", data={}, source_url=None, tier="bogus", provider="p")
    row = SourceRow(
        fact_id="f1", data={"name": "A"}, source_url=None, tier=TIER_WEB, provider="p"
    )
    SourceBundle(envelope=env, rows=(row,))


def test_authoritative_vs_web_contrast():
    """An authoritative bundle carries a secret_ref (no plaintext); a web bundle
    carries none."""
    auth = build_source_bundle(
        [{"name": "A"}], workspace_id="ws", run_id="r",
        provider="acme_api", tier=TIER_AUTHORITATIVE, secret_refs=["acme_secret"],
        key_attribute="name",
    )
    web = build_source_bundle(
        [{"name": "B"}], workspace_id="ws", run_id="r",
        provider="web_fake", tier=TIER_WEB,
        key_attribute="name",
    )
    assert auth.tiers == frozenset({TIER_AUTHORITATIVE})
    assert web.tiers == frozenset({TIER_WEB})
    assert auth.secret_refs == ("acme_secret",)
    assert web.secret_refs == ()
    assert PLAINTEXT_CREDENTIAL not in json.dumps(auth.to_dict())
