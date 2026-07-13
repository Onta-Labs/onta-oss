"""ONTA-281 acceptance: an A10 user correction persists with TOP provenance rank,
and a later lower-authority refresh can NEVER clobber it.

Layers:

1. Pure unit tests (no store) — the top ``user_assertion`` authority outranks
   ``source_of_truth`` on the ONE shared scale; the :class:`UserAssertion` model
   resolves subject/type; the provenance is stamped at the top authority; the
   literal-attribute predicate is the ``attrs/`` form.
2. THE acceptance bar over a PRE-POPULATED in-process pyoxigraph store: seed
   ``Company/acme phone "111"`` (authority=source_of_truth); apply a user
   correction ``phone "222"``; prove:
     (a) current-facts = ONLY "222" (the correction superseded the old value);
     (b) the corrected fact's provenance carries the TOP ``user_assertion``
         authority;
     (c) PRECEDENCE PROOF (load-bearing): a subsequent conflicting write at a
         LOWER authority — simulating a P8 refresh/scrape (``phone "333"``,
         authority=source_of_truth) via ONTA-276's
         ``write_with_conflict_resolution`` — does NOT clobber the user fix:
         current-facts still = "222", and the scrape "333" is the deprecated
         loser;
     (d) an A6 ``GraphDelta`` receipt was emitted for the correction.
3. The canonical route (POST /graphs/{tenant}/corrections) mints the literal
   predicate, stamps the authenticated actor, and calls the shared writer.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cograph_client.api_registry.spec import (
    AUTHORITY_CONFIDENCE,
    AUTHORITY_RANK,
    AuthorityLevel,
)
from cograph_client.graph.kg_writer import GraphDelta, insert_facts
from cograph_client.graph.provenance import PROV_AUTHORITY, fetch_provenance
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.validity import current_objects_query, fetch_history
from cograph_client.pipeline.corrections import (
    USER_ASSERTION_AUTHORITY,
    UserAssertion,
    UserAssertionError,
    apply_user_assertion,
    build_user_assertion_provenance,
    literal_attribute_predicate,
)
from cograph_client.pipeline.mutations import write_with_conflict_resolution

TENANT, KG = "onta281", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
ACME = "https://cograph.tech/entities/Company/acme"
PHONE = "https://cograph.tech/types/Company/attrs/phone"

P111, P222, P333 = "111", "222", "333"


# --------------------------------------------------------------------------- #
# 1. Pure unit tests (no store)
# --------------------------------------------------------------------------- #
def test_user_assertion_is_the_top_authority_rank():
    """The whole precedence story in one assertion: ``user_assertion`` outranks
    every machine source on the ONE shared scale (LOWER rank == STRONGER)."""
    ua = AUTHORITY_RANK[AuthorityLevel.user_assertion]
    assert ua < AUTHORITY_RANK[AuthorityLevel.source_of_truth]
    assert ua < AUTHORITY_RANK[AuthorityLevel.authoritative]
    assert ua < AUTHORITY_RANK[AuthorityLevel.supplementary]
    assert ua == min(AUTHORITY_RANK.values()), "user_assertion must be the TOP slot"
    # And a calibrated confidence above source_of_truth.
    assert (
        AUTHORITY_CONFIDENCE[AuthorityLevel.user_assertion]
        > AUTHORITY_CONFIDENCE[AuthorityLevel.source_of_truth]
    )
    # Relative order of the pre-existing machine levels is unchanged (additive).
    assert (
        AUTHORITY_RANK[AuthorityLevel.source_of_truth]
        < AUTHORITY_RANK[AuthorityLevel.authoritative]
        < AUTHORITY_RANK[AuthorityLevel.supplementary]
    )


def test_user_assertion_resolves_subject_and_type_from_iri():
    """A subject IRI is enough — the type is parsed from ``…/entities/<Type>/…``."""
    a = UserAssertion(predicate=PHONE, value=P222, subject=ACME)
    assert a.resolved_subject() == ACME
    assert a.resolved_type() == "Company"


def test_user_assertion_mints_subject_from_type_and_id():
    """Given type+id (no subject IRI), it mints the SAME canonical IRI via the
    shared entity_uri minter."""
    a = UserAssertion(predicate=PHONE, value=P222, type_name="Company", entity_id="acme")
    assert a.resolved_subject() == ACME
    assert a.resolved_type() == "Company"


def test_user_assertion_requires_a_subject_or_type_id():
    a = UserAssertion(predicate=PHONE, value=P222)
    with pytest.raises(UserAssertionError):
        a.resolved_subject()


def test_literal_attribute_predicate_is_the_attrs_form():
    """A literal attribute correction targets ``types/<Type>/attrs/<leaf>`` (the
    literal-value predicate), via the shared attr_uri builder."""
    assert literal_attribute_predicate("Company", "phone") == PHONE


def test_build_user_assertion_provenance_stamps_top_authority():
    """The provenance the writer stamps carries authority=user_assertion + the
    calibrated top confidence."""
    triples = build_user_assertion_provenance(
        ACME, PHONE, P222,
        actor="user-42",
        observed_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        instance_graph=INSTANCE_GRAPH,
    )
    authority_objs = [o for _s, p, o in triples if p == PROV_AUTHORITY]
    assert authority_objs == [AuthorityLevel.user_assertion.value]


# --------------------------------------------------------------------------- #
# 2. Real end-to-end over a pyoxigraph store (the acceptance bar)
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence the shared refresh_after_write internals (cache-invalidate / embed /
    stats recompute) so the end-to-end tests isolate the correction mechanism — as
    tests/test_supersession.py + tests/test_conflict_policy.py do. The writer STILL
    calls refresh_after_write."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


async def _current(n: PyoxiNeptune, subject: str, predicate: str) -> set[str]:
    """The "current facts" projection — objects with no CLOSED validity interval."""
    raw = await n.query(current_objects_query(INSTANCE_GRAPH, subject, predicate))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


async def _seed_scraped_fact(
    n: PyoxiNeptune, subject: str, predicate: str, value: str, *, authority: AuthorityLevel
) -> None:
    """Seed an initial current fact WITH its authority persisted in provenance —
    via the SAME conflict-resolving op an upstream A4 machine fact lands through
    (no existing value, so it is a plain current write). This is what makes the
    seeded fact's authority readable when the correction (then the refresh)
    arrives."""
    await insert_facts(
        n,
        INSTANCE_GRAPH,
        [(subject, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
          "https://cograph.tech/types/Company")],
    )
    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=subject, predicate=predicate, type_name="Company", value=value,
        authority=authority, confidence=0.9, source="scraper", run_id="seed",
    )


@pytest.mark.asyncio
async def test_user_correction_persists_with_top_rank_and_survives_a_refresh():
    """THE acceptance bar (done-when + precedence proof).

    Pre-populated store: acme phone "111" from a scraper (source_of_truth). A user
    corrects it to "222". Then a P8-style refresh re-scrapes "333"
    (source_of_truth). Prove the user fix persists at the top rank AND the refresh
    cannot clobber it."""
    n = PyoxiNeptune()
    await _seed_scraped_fact(n, ACME, PHONE, P111, authority=AuthorityLevel.source_of_truth)
    assert await _current(n, ACME, PHONE) == {P111}

    # --- Apply the user correction "111" -> "222" (A10) ---
    receipt = await apply_user_assertion(
        n, INSTANCE_GRAPH,
        UserAssertion(predicate=PHONE, value=P222, subject=ACME, actor="user-42"),
        run_id="run-281",
    )

    # (a) The correction superseded the old value: current = ONLY "222".
    assert await _current(n, ACME, PHONE) == {P222}
    # The old "111" stays queryable as history, closed (not deleted).
    hist = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, PHONE)}
    assert set(hist) == {P111, P222}
    assert not hist[P111].is_current and hist[P111].valid_to, "old value must be CLOSED"
    assert hist[P222].is_current, "the correction must be the current fact"

    # (b) The corrected fact's provenance carries the TOP user_assertion authority.
    prov = {p.obj: p for p in await fetch_provenance(n, INSTANCE_GRAPH, ACME, PHONE)}
    assert prov[P222].authority == USER_ASSERTION_AUTHORITY.value == "user_assertion"

    # (d) An A6 GraphDelta receipt was emitted for the correction.
    assert isinstance(receipt.graph_delta, GraphDelta)
    assert receipt.graph_delta.run_id == "run-281"
    delta_spo = {(s, p, o) for _fid, s, p, o in receipt.graph_delta.facts}
    assert (ACME, PHONE, P222) in delta_spo, "the A6 delta must record the correction"
    assert receipt.superseded == ((ACME, PHONE, P111),)

    # (c) PRECEDENCE PROOF — a later P8 refresh re-scrapes "333" (source_of_truth),
    #     the SAME machinery ONTA-276 uses at write time. The user fix must win.
    refresh = await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=PHONE, type_name="Company", value=P333,
        authority=AuthorityLevel.source_of_truth, confidence=0.9, source="refresh_scraper",
        run_id="run-p8-refresh",
    )

    # The refresh did NOT clobber the user fix: current-facts still = "222".
    assert await _current(n, ACME, PHONE) == {P222}, (
        "a refresh must NEVER clobber a user correction"
    )
    # The scrape "333" is the deprecated loser (present-but-not-current), and the
    # conflict was decided on the AUTHORITY axis (user_assertion > source_of_truth).
    assert refresh.conflict is True and refresh.reason == "authority"
    assert refresh.winner == (ACME, PHONE, P222)
    assert refresh.loser == (ACME, PHONE, P333)
    hist2 = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, PHONE)}
    assert hist2[P222].is_current, "the user fix stays current after the refresh"
    assert not hist2[P333].is_current, "the re-scraped value is the deprecated loser"


@pytest.mark.asyncio
async def test_control_scrape_without_user_fix_wins_normally():
    """LOAD-BEARING control: with NO user correction in between, a fresh
    source_of_truth scrape "333" DOES supersede the earlier source_of_truth "111"
    (recency at equal authority) — proving the acceptance test's persistence is
    the user_assertion rank at work, not the write being inert."""
    n = PyoxiNeptune()
    await _seed_scraped_fact(n, ACME, PHONE, P111, authority=AuthorityLevel.source_of_truth)

    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=PHONE, type_name="Company", value=P333,
        authority=AuthorityLevel.source_of_truth, confidence=0.9, source="refresh_scraper",
        run_id="run-control",
    )
    # Equal authority → recency wins → the new scrape supersedes the old value.
    assert await _current(n, ACME, PHONE) == {P333}


# --------------------------------------------------------------------------- #
# 3. The canonical route
# --------------------------------------------------------------------------- #
def test_correction_route_mints_predicate_and_calls_writer(client, auth_headers, monkeypatch):
    """POST /graphs/{tenant}/corrections derives the type from the subject IRI,
    mints the literal predicate, stamps the authenticated actor, and calls the
    shared writer — returning the correction receipt."""
    from unittest.mock import AsyncMock

    import cograph_client.api.routes.corrections as route_mod
    from cograph_client.graph.kg_writer import build_graph_delta
    from cograph_client.pipeline.mutations import MutationReceipt

    captured = {}

    async def _fake_apply(neptune, instance_graph, assertion, **kwargs):
        captured["instance_graph"] = instance_graph
        captured["assertion"] = assertion
        captured["kwargs"] = kwargs
        return MutationReceipt(
            op="supersede",
            graph_delta=build_graph_delta(instance_graph, [(ACME, PHONE, P222)], run_id=kwargs.get("run_id")),
            inserted=((ACME, PHONE, P222),),
            superseded=((ACME, PHONE, P111),),
        )

    monkeypatch.setattr(route_mod, "apply_user_assertion", AsyncMock(side_effect=_fake_apply))

    resp = client.post(
        "/graphs/test-tenant/corrections",
        json={"kg_name": "corp", "subject": ACME, "attribute": "phone", "value": P222},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["authority"] == "user_assertion"
    assert body["predicate"] == PHONE
    assert body["value"] == P222
    assert body["superseded"] == [P111]

    a = captured["assertion"]
    assert a.subject == ACME
    assert a.type_name == "Company"
    assert a.predicate == PHONE  # the attrs/ literal predicate, minted server-side
    assert captured["kwargs"]["tenant_id"] == "test-tenant"
    assert captured["kwargs"]["kg_name"] == "corp"


def test_correction_route_rejects_missing_fields(client, auth_headers):
    """Empty subject/attribute/value are 422 — a malformed correction never reaches
    the writer."""
    resp = client.post(
        "/graphs/test-tenant/corrections",
        json={"kg_name": "corp", "subject": "", "attribute": "phone", "value": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
