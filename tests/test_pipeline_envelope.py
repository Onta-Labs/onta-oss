"""Minimal construction test for ArtifactEnvelope (ONTA-265).

This is a schema/type-stub deliverable, not a pipeline integration — no stage
constructs one of these yet. The test just pins the contract: mandatory fields
are enforced, fact_id derivation is deterministic and lineage-sensitive, and
round-tripping through to_dict/from_dict is lossless enough to reconstruct an
equivalent envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cograph_client.pipeline import ArtifactEnvelope, derive_fact_id


def test_construct_root_envelope():
    root_id = derive_fact_id(run_id="run-1", stage="A1", local_key="source-bundle-0")
    env = ArtifactEnvelope(
        workspace_id="ws-acme",
        run_id="run-1",
        fact_id=root_id,
    )
    assert env.workspace_id == "ws-acme"
    assert env.run_id == "run-1"
    assert env.fact_id == root_id
    assert env.parent_fact_ids == ()
    assert env.spend_usd == 0.0
    assert env.ontology_version is None
    assert isinstance(env.observed_at, datetime)
    assert env.observed_at.tzinfo is not None


def test_mandatory_fields_enforced():
    with pytest.raises(ValueError):
        ArtifactEnvelope(workspace_id="", run_id="run-1", fact_id="fact-1")
    with pytest.raises(ValueError):
        ArtifactEnvelope(workspace_id="ws-acme", run_id="", fact_id="fact-1")
    with pytest.raises(ValueError):
        ArtifactEnvelope(workspace_id="ws-acme", run_id="run-1", fact_id="")


def test_child_propagates_and_derives_new_fact_id():
    root = ArtifactEnvelope(
        workspace_id="ws-acme",
        run_id="run-1",
        fact_id=derive_fact_id(run_id="run-1", stage="A1", local_key="row-0"),
        spend_usd=0.01,
    )
    child = root.child(stage="A2", local_key="row-0", spend_delta_usd=0.02)

    # workspace_id/run_id propagate verbatim; spend accumulates; a new fact_id
    # is minted with the parent as sole lineage entry.
    assert child.workspace_id == root.workspace_id
    assert child.run_id == root.run_id
    assert child.fact_id != root.fact_id
    assert child.parent_fact_ids == (root.fact_id,)
    assert child.spend_usd == pytest.approx(0.03)


def test_fact_id_derivation_is_deterministic_and_lineage_sensitive():
    a = derive_fact_id(run_id="run-1", stage="A2", parent_fact_ids=("p1",), local_key="k")
    b = derive_fact_id(run_id="run-1", stage="A2", parent_fact_ids=("p1",), local_key="k")
    assert a == b  # deterministic replay

    # Different local_key (a fan-out sibling) yields a different id.
    c = derive_fact_id(run_id="run-1", stage="A2", parent_fact_ids=("p1",), local_key="k2")
    assert c != a

    # Parent order doesn't matter for fan-in (sorted before hashing).
    d = derive_fact_id(run_id="run-1", stage="A6", parent_fact_ids=("p2", "p1"), local_key="")
    e = derive_fact_id(run_id="run-1", stage="A6", parent_fact_ids=("p1", "p2"), local_key="")
    assert d == e


def test_to_dict_from_dict_round_trip():
    env = ArtifactEnvelope(
        workspace_id="ws-acme",
        run_id="run-1",
        fact_id="fact-abc",
        parent_fact_ids=("fact-parent",),
        observed_at=datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone.utc),
        spend_usd=1.23,
        ontology_version="v7",
    )
    restored = ArtifactEnvelope.from_dict(env.to_dict())
    assert restored == env
