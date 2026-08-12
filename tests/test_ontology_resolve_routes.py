"""Route tests for the NL ontology-evolution endpoints (COG-81; ported ONTA-527).

Deterministic and offline: ``OntologyResolver.resolve`` is monkeypatched to
return a fixed plan, so no LLM / embedding call is made.

**What changed.** These cases used to assert what SPARQL the route WROTE —
scraping ``mock_neptune.update`` for "age" / "Company" / "works_at" and
counting calls. ``/resolve`` and ``/apply`` still go through
``graph/ontology_commit.py::commit_ontology``, but that now dispatches to
``_commit_ontology_graph_store`` whenever a GraphStore is configured (always,
in production) and applies the mutation through
:mod:`infona_client.graph.ontology_catalog`. No SPARQL is emitted, so those
assertions were checking a builder that no longer runs — and, worse, the two
"writes nothing" cases passed VACUOUSLY (zero update calls is now true of every
request, applied or not).

Each case is re-pointed at the catalog: the confident change is readable back
as a type/attribute declaration, a proposal leaves the catalog untouched, and
``mock_neptune.update.assert_not_called()`` proves the store path ran.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import infona_client.api.routes.ontology as onto_routes
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import (
    list_attributes as cat_list_attributes,
    list_types as cat_list_types,
)
from infona_client.graph.store import configure_graph_store, get_graph_store
from infona_client.models.ontology import ResolutionResult, ResolvedChange

TENANT = "test-tenant"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _types(store=None) -> dict:
    store = store or get_graph_store()
    return {t.name: t for t in _run(cat_list_types(tenant_id=TENANT, store=store))}


def _attrs(store=None) -> dict:
    store = store or get_graph_store()
    return {
        (a.domain, a.name): a
        for a in _run(cat_list_attributes(tenant_id=TENANT, store=store))
    }


def _catalog_snapshot() -> tuple:
    """Comparable (types, attributes) state of the tenant catalog."""
    types = tuple(sorted((t.name, t.parent_type) for t in _types().values()))
    attrs = tuple(
        sorted(
            (a.domain, a.name, a.kind, a.datatype, a.range_type)
            for a in _attrs().values()
        )
    )
    return types, attrs


def _patch_resolver(monkeypatch, result: ResolutionResult):
    """Make the route build a resolver whose `.resolve` returns `result`."""
    fake = AsyncMock()
    fake.resolve = AsyncMock(return_value=result)
    monkeypatch.setattr(onto_routes, "_build_resolver", lambda graph_uri: fake)
    return fake


def test_resolve_auto_applies_confident_change(
    client, auth_headers, mock_neptune, monkeypatch
):
    applied = ResolvedChange(
        kind="attribute",
        subject_type="Person",
        name="age",
        datatype_or_target="integer",
        action="extend",
        confidence=0.95,
        reason="clear extend on existing Person",
    )
    _patch_resolver(monkeypatch, ResolutionResult(applied=[applied], proposals=[], summary="1 applied"))

    resp = client.post(
        f"/graphs/{TENANT}/ontology/resolve",
        headers=auth_headers,
        json={"ask": "track how old a person is"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["applied"]) == 1
    assert data["applied"][0]["name"] == "age"
    assert data["proposals"] == []
    # The commit landed in the tenant catalog (was: "age"/"integer" appear in
    # the emitted SPARQL).
    age = _attrs()[("Person", "age")]
    assert age.kind == "literal"
    assert age.datatype == "integer"
    assert age.layer == "tenant"
    mock_neptune.update.assert_not_called()


def test_resolve_with_only_proposals_writes_nothing(
    client, auth_headers, mock_neptune, monkeypatch
):
    proposal = ResolvedChange(
        kind="relationship",
        subject_type="Person",
        name="works_at",
        datatype_or_target="Company",
        action="create",
        confidence=0.4,
        reason="new target type Company",
    )
    _patch_resolver(monkeypatch, ResolutionResult(applied=[], proposals=[proposal], summary="1 proposal"))

    resp = client.post(
        f"/graphs/{TENANT}/ontology/resolve",
        headers=auth_headers,
        json={"ask": "track which company a person works for"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] == []
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["name"] == "works_at"
    # Proposals are NOT auto-applied — the catalog is untouched. (Asserting
    # "zero neptune updates" is now true of every request and proves nothing.)
    assert _catalog_snapshot() == ((), ())
    mock_neptune.update.assert_not_called()


def test_resolve_dry_run_returns_everything_as_proposals_and_writes_nothing(
    client, auth_headers, mock_neptune, monkeypatch
):
    """dry_run=True: the would-be-applied change AND the proposals all come back
    under `proposals`, `applied` is empty, `dry_run` is echoed, and NOTHING is
    written."""
    applied = ResolvedChange(
        kind="attribute",
        subject_type="Person",
        name="age",
        datatype_or_target="integer",
        action="extend",
        confidence=0.95,
        reason="clear extend on existing Person",
    )
    proposal = ResolvedChange(
        kind="relationship",
        subject_type="Person",
        name="works_at",
        datatype_or_target="Company",
        action="create",
        confidence=0.4,
        reason="new target type Company",
    )
    _patch_resolver(
        monkeypatch,
        ResolutionResult(applied=[applied], proposals=[proposal], summary="1 applied, 1 proposal"),
    )

    resp = client.post(
        f"/graphs/{TENANT}/ontology/resolve",
        headers=auth_headers,
        json={"ask": "track a person's age and employer", "dry_run": True},
    )

    assert resp.status_code == 200
    data = resp.json()
    # Plan-only: applied is empty, everything folded into proposals.
    assert data["applied"] == []
    assert data["dry_run"] is True
    names = {p["name"] for p in data["proposals"]}
    assert names == {"age", "works_at"}
    # Plan-only means the catalog is byte-for-byte untouched — including the
    # change that WOULD have auto-applied.
    assert _catalog_snapshot() == ((), ())
    mock_neptune.update.assert_not_called()


def test_resolve_default_omits_dry_run_and_still_auto_applies(
    client, auth_headers, mock_neptune, monkeypatch
):
    """Default (dry_run unset) is the prior behavior: the confident change
    auto-applies and `dry_run` defaults to False in the response."""
    applied = ResolvedChange(
        kind="attribute",
        subject_type="Person",
        name="age",
        datatype_or_target="integer",
        action="extend",
        confidence=0.95,
        reason="clear extend on existing Person",
    )
    _patch_resolver(monkeypatch, ResolutionResult(applied=[applied], proposals=[], summary="1 applied"))

    resp = client.post(
        f"/graphs/{TENANT}/ontology/resolve",
        headers=auth_headers,
        json={"ask": "track how old a person is"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["applied"]) == 1
    assert data["dry_run"] is False
    assert ("Person", "age") in _attrs()
    mock_neptune.update.assert_not_called()


def test_apply_create_relationship_mints_target_and_property(
    client, auth_headers, mock_neptune
):
    proposal = {
        "kind": "relationship",
        "subject_type": "Person",
        "name": "works_at",
        "datatype_or_target": "Company",
        "action": "create",
        "confidence": 0.9,
        "reason": "confirmed by agent",
    }

    resp = client.post(
        f"/graphs/{TENANT}/ontology/apply",
        headers=auth_headers,
        json=proposal,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"]["name"] == "works_at"
    # create relationship → mint the subject type, ensure the TARGET type, and
    # declare the property with its range (was: three tokens in the SPARQL).
    types = _types()
    assert "Person" in types and "Company" in types
    rel = _attrs()[("Person", "works_at")]
    assert rel.kind == "relationship"
    assert rel.range_type == "Company"
    mock_neptune.update.assert_not_called()


def test_apply_attribute_extend_writes_single_upsert(client, auth_headers, mock_neptune):
    proposal = {
        "kind": "attribute",
        "subject_type": "Person",
        "name": "email",
        "datatype_or_target": "string",
        "action": "extend",
        "confidence": 0.99,
        "reason": "confirmed",
    }

    resp = client.post(
        f"/graphs/{TENANT}/ontology/apply",
        headers=auth_headers,
        json=proposal,
    )

    assert resp.status_code == 200
    assert resp.json()["operations"] == 1
    email = _attrs()[("Person", "email")]
    assert email.kind == "literal" and email.datatype == "string"
    # An `extend` mints no new type beyond the attribute's own domain.
    assert set(_types()) == {"Person"}
    mock_neptune.update.assert_not_called()


# --- batch apply (persona-eval batch-ontology-apply bug) ---------------------


def _change(name, datatype, kind="attribute", action="extend", subject="Person"):
    return {
        "kind": kind,
        "subject_type": subject,
        "name": name,
        "datatype_or_target": datatype,
        "action": action,
        "confidence": 0.95,
        "reason": "confirmed",
    }


def test_apply_batch_applies_all_changes_in_one_call(client, auth_headers, mock_neptune):
    """N proposals in ONE batch call create all N attrs/relationships and is
    equivalent to N single calls: three changes => three declarations, all
    reported ok, no partial failure."""
    batch = {
        "changes": [
            _change("email", "string"),
            _change("age", "integer"),
            _change("works_at", "Company", kind="relationship", action="create"),
        ]
    }
    resp = client.post(
        f"/graphs/{TENANT}/ontology/apply/batch",
        headers=auth_headers,
        json=batch,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["applied_count"] == 3
    assert data["failed_count"] == 0
    assert len(data["results"]) == 3
    assert all(r["ok"] for r in data["results"])
    # Every change's declaration landed (was: every token present in the SPARQL).
    attrs = _attrs()
    assert attrs[("Person", "email")].datatype == "string"
    assert attrs[("Person", "age")].datatype == "integer"
    assert attrs[("Person", "works_at")].range_type == "Company"
    assert "Company" in _types()
    mock_neptune.update.assert_not_called()


def test_apply_batch_equivalent_to_n_single_calls(client, auth_headers, mock_neptune):
    """The batch route leaves the catalog in the same state as calling /apply
    once per change.

    (Was: compare the emitted schema-mutation SPARQL of both paths. Both lists
    are empty now, so that comparison passed vacuously — it would not have
    noticed the batch route writing nothing at all. Comparing catalog STATE
    cannot pass vacuously: the empty-vs-empty case is excluded below.)
    """
    changes = [
        _change("email", "string"),
        _change("age", "integer"),
        _change("works_at", "Company", kind="relationship", action="create"),
    ]

    # N single calls.
    for ch in changes:
        r = client.post(f"/graphs/{TENANT}/ontology/apply", headers=auth_headers, json=ch)
        assert r.status_code == 200
    single_state = _catalog_snapshot()
    assert single_state != ((), ()), "the single-apply path wrote nothing"

    # Same changes, fresh catalog, one batch call.
    configure_graph_store(MemoryGraphStore())
    assert _catalog_snapshot() == ((), ())
    r = client.post(
        f"/graphs/{TENANT}/ontology/apply/batch",
        headers=auth_headers,
        json={"changes": changes},
    )
    assert r.status_code == 200
    batch_state = _catalog_snapshot()

    assert batch_state == single_state
    mock_neptune.update.assert_not_called()


def test_apply_batch_partial_failure_is_well_defined(client, auth_headers, mock_neptune):
    """A change that raises is isolated: ok=False + error on that entry, the
    others still apply, and counts reflect the split (no all-or-nothing abort).

    The failing change is one the catalog genuinely refuses — an attribute leaf
    that collides with a reserved Entity property key (B2, graph/facts.py) — so
    the isolation is exercised against a real error rather than a stubbed
    transport failure (the old ``neptune.update`` side_effect can no longer fire:
    the write never reaches Neptune).
    """
    batch = {
        "changes": [
            _change("email", "string"),
            _change("name", "string"),  # reserved Entity property key → refused
            _change("age", "integer"),
        ]
    }
    resp = client.post(
        f"/graphs/{TENANT}/ontology/apply/batch",
        headers=auth_headers,
        json=batch,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["applied_count"] == 2
    assert data["failed_count"] == 1
    results = data["results"]
    assert results[0]["ok"] is True and results[0]["change"]["name"] == "email"
    assert results[1]["ok"] is False and results[1]["change"]["name"] == "name"
    assert "reserved" in results[1]["error"]
    assert results[2]["ok"] is True and results[2]["change"]["name"] == "age"
    # The isolation is real: the two good changes are in the catalog, the
    # refused one is not.
    attrs = _attrs()
    assert ("Person", "email") in attrs and ("Person", "age") in attrs
    assert ("Person", "name") not in attrs


def test_apply_batch_empty_list_is_422(client, auth_headers, mock_neptune):
    """An empty change list is a caller bug → 422 (min_length=1), not a silent
    no-op 200."""
    resp = client.post(
        f"/graphs/{TENANT}/ontology/apply/batch",
        headers=auth_headers,
        json={"changes": []},
    )
    assert resp.status_code == 422
