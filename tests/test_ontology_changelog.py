"""ONTA-401 — ontology changelog delta payload + reader.

Covers:
- full ChangeRecord delta serialize/parse (entry describes change without live graph)
- query builder: companion graph scoping, since/subject/action, pagination, same-ms order
- append-only property of commit_ontology writers (one entry per commit, none removed)
- governance thin entries remain parseable (no delta required)

**Ported by ONTA-527.** The writer section used to capture the SPARQL strings
``commit_ontology`` sent (``_WriteCaptureNeptune``) and assert on the ``INSERT
DATA { GRAPH <…/changelog> … }`` text — one INSERT per commit, a fresh
``gov/log/<uuid>`` subject in each, the delta JSON inside the body. Production
is Neo4j-only and ``commit_ontology`` takes its GraphStore branch, which emits
no changelog write at all, so those assertions were about a string nobody
builds any more. They are re-expressed as what they were proxies for — *after N
commits the workspace changelog holds N entries describing them* — read back
through the shipped reader, and marked ``strict=True`` xfail while the write and
read halves are both still SPARQL-only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from infona_client.graph.ontology_changelog import (
    GOV_ACTION,
    GOV_NS,
    GOV_SUBJECT,
    GOV_TIMESTAMP,
    changelog_graph_uri_for,
    fetch_ontology_changelog,
    ontology_changelog_query,
    parse_change_records,
    serialize_change_records,
)
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    OntologyMutation,
    OntologyOpKind,
)
from infona_client.resolver.governance import changelog_triples


# ---------------------------------------------------------------------------
# Delta codec
# ---------------------------------------------------------------------------


def test_serialize_parse_roundtrip_full_change_record():
    """Delta carries every ChangeRecord field — enough to describe the change
    without consulting the live ontology graph (ONTA-401 acceptance)."""
    records = [
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person"),
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name="Person",
            slot_name="name",
            new_value="string",
        ),
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name="Guest",
            slot_name="phone_num",
            from_name="phone_num",
            to_name="phone",
            old_value="https://graph.infona.ai/types/Guest/attrs/phone_num",
            new_value="https://graph.infona.ai/types/Guest/attrs/phone",
        ),
        ChangeRecord(
            kind=ChangeKind.DEPRECATE,
            type_name="LegacyThing",
            superseded_by="Thing",
        ),
    ]
    raw = serialize_change_records(records)
    assert "from_name" in raw and "phone_num" in raw
    assert "superseded_by" in raw and "Thing" in raw
    back = parse_change_records(raw)
    assert len(back) == 4
    assert back[0].kind is ChangeKind.ADD_TYPE and back[0].type_name == "Person"
    assert back[2].from_name == "phone_num" and back[2].to_name == "phone"
    assert back[3].superseded_by == "Thing"


def test_parse_change_records_tolerates_missing_and_garbage():
    """Governance writers omit delta — empty/malformed must not raise."""
    assert parse_change_records(None) == []
    assert parse_change_records("") == []
    assert parse_change_records("not-json") == []
    assert parse_change_records("{}") == []  # object, not list
    assert parse_change_records('[{"no_kind": true}]') == []
    # One bad row does not drop siblings.
    raw = (
        '[{"kind":"add_type","type_name":"A"},'
        '{"kind":"not_a_real_kind"},'
        '{"kind":"remove_type","type_name":"B"}]'
    )
    out = parse_change_records(raw)
    assert [r.type_name for r in out] == ["A", "B"]


def test_serialize_excludes_none_fields():
    raw = serialize_change_records(
        [ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="X")]
    )
    assert "slot_name" not in raw
    assert "null" not in raw


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------


def test_changelog_graph_uri_is_companion():
    g = "https://graph.infona.ai/graphs/acme"
    assert changelog_graph_uri_for(g) == f"{g}/changelog"
    # Trailing slash stripped — no double slash.
    assert changelog_graph_uri_for(g + "/") == f"{g}/changelog"


def test_query_scopes_to_tenant_companion_only():
    """TENANT ISOLATION: FROM is exactly the caller's companion — never another
    tenant's graph and never a cross-tenant UNION."""
    g = "https://graph.infona.ai/graphs/tenant-a"
    q = ontology_changelog_query(g)
    assert f"FROM <{g}/changelog>" in q
    assert "tenant-b" not in q
    assert "UNION" not in q.upper()
    assert "https://graph.infona.ai/graphs/global/changelog" not in q


def test_query_filters_and_pagination():
    g = "https://graph.infona.ai/graphs/t"
    q = ontology_changelog_query(
        g,
        since="2026-07-01T00:00:00Z",
        subject="https://graph.infona.ai/graphs/t",
        action="commit_ontology",
        limit=25,
        offset=50,
    )
    assert 'FILTER(?timestamp > "2026-07-01T00:00:00Z"^^' in q
    assert f"<{GOV_SUBJECT}> <https://graph.infona.ai/graphs/t>" in q or (
        f"<{GOV_SUBJECT}>" in q and "https://graph.infona.ai/graphs/t" in q
    )
    assert 'FILTER(?action = "commit_ontology")' in q
    assert "LIMIT 25" in q
    assert "OFFSET 50" in q
    # Same-ms stable order: secondary key on entry URI.
    assert "ORDER BY DESC(?timestamp) DESC(?entry)" in q


def test_query_rejects_bad_limit_offset():
    with pytest.raises(ValueError):
        ontology_changelog_query("https://graph.infona.ai/graphs/t", limit=0)
    with pytest.raises(ValueError):
        ontology_changelog_query("https://graph.infona.ai/graphs/t", offset=-1)


def test_query_escapes_action_and_since_literals():
    """Crafted action/since cannot break out of the SPARQL string literal."""
    q = ontology_changelog_query(
        "https://graph.infona.ai/graphs/t",
        action='x" } FILTER(true) #',
        since='2020-01-01"^^<http://evil>',
    )
    # Quotes inside the payload are escaped — the SPARQL string does not close early.
    assert '\\"' in q
    assert 'FILTER(?action = "x\\"' in q
    # since payload is inside a typed-literal string, not a second datatype IRI.
    assert 'FILTER(?timestamp > "2020-01-01\\"^^<http://evil>"^^' in q


# ---------------------------------------------------------------------------
# fetch_ontology_changelog (mocked Neptune)
# ---------------------------------------------------------------------------


def _sparql_json(rows: list[dict[str, str]]) -> dict:
    vars_: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                vars_.append(k)
    return {
        "head": {"vars": vars_},
        "results": {
            "bindings": [
                {k: {"value": v} for k, v in row.items()} for row in rows
            ]
        },
    }


@pytest.mark.asyncio
async def test_fetch_reconstructs_entry_from_delta_alone():
    """Acceptance: entry describes the change without consulting the live graph."""
    delta = serialize_change_records(
        [
            ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Invoice"),
            ChangeRecord(
                kind=ChangeKind.ADD_ATTRIBUTE,
                type_name="Invoice",
                slot_name="amount",
                new_value="float",
            ),
        ]
    )
    neptune = AsyncMock()
    neptune.query.return_value = _sparql_json(
        [
            {
                "entry": f"{GOV_NS}log/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "action": "commit_ontology",
                "subject": "https://graph.infona.ai/graphs/acme",
                "timestamp": "2026-07-28T12:00:00Z",
                "tenant": "acme",
                "actor": "tester",
                "message": "add Invoice",
                "versionBefore": "aaa",
                "versionAfter": "bbb",
                "revision": "3",
                "delta": delta,
            }
        ]
    )
    entries = await fetch_ontology_changelog(
        neptune, "https://graph.infona.ai/graphs/acme"
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.action == "commit_ontology"
    assert e.subject == "https://graph.infona.ai/graphs/acme"
    assert e.revision == 3
    assert e.actor == "tester"
    assert [c.kind for c in e.changes] == [
        ChangeKind.ADD_TYPE,
        ChangeKind.ADD_ATTRIBUTE,
    ]
    assert e.changes[0].type_name == "Invoice"
    assert e.changes[1].slot_name == "amount"
    # The fetch only ever FROM-s the companion changelog graph.
    sent = neptune.query.await_args.args[0]
    assert "FROM <https://graph.infona.ai/graphs/acme/changelog>" in sent
    assert sent.count("FROM <") == 1
    assert "UNION" not in sent.upper()


@pytest.mark.asyncio
async def test_fetch_tolerates_governance_thin_entry():
    """Governance changelog_triples shape (no delta) still yields a valid entry."""
    # Mirror what changelog_triples writes.
    thin = changelog_triples(
        "add_type",
        "https://graph.infona.ai/types/public/Hotel",
        "acme",
        "2026-07-28T12:00:00+00:00",
    )
    by_pred = {p: o for (_s, p, o) in thin}
    neptune = AsyncMock()
    neptune.query.return_value = _sparql_json(
        [
            {
                "entry": thin[0][0],
                "action": by_pred[GOV_ACTION],
                "subject": by_pred[GOV_SUBJECT],
                "timestamp": by_pred[GOV_TIMESTAMP].split("^^")[0],
                "tenant": "acme",
            }
        ]
    )
    entries = await fetch_ontology_changelog(
        neptune, "https://graph.infona.ai/graphs/global/public"
    )
    assert len(entries) == 1
    assert entries[0].action == "add_type"
    assert entries[0].changes == []  # no delta — still valid
    assert entries[0].tenant_id == "acme"


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_neptune_error():
    neptune = AsyncMock()
    neptune.query.side_effect = RuntimeError("neptune down")
    out = await fetch_ontology_changelog(
        neptune, "https://graph.infona.ai/graphs/t"
    )
    assert out == []


# ---------------------------------------------------------------------------
# Append-only property of commit_ontology writers (ported — see module docstring)
# ---------------------------------------------------------------------------


class _DecommissionedSparql:
    """The SPARQL endpoint production no longer has.

    Amazon Neptune was decommissioned 2026-08-11; the routes that still take a
    ``NeptuneClient`` dependency hold a client whose every call fails. Using it
    here keeps these tests honest: nothing may pass because a hand-rolled triple
    store answered a query that cannot run in production.
    """

    async def query(self, sparql: str) -> dict:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")

    async def update(self, sparql: str) -> None:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")


CHANGELOG_GAP = (
    "BUG (ONTA-527 port gap): the workspace ontology changelog does not exist "
    "on Neo4j. graph/ontology_commit.py::_commit_ontology_graph_store — the "
    "branch commit_ontology takes whenever a GraphStore is configured, i.e. "
    "always in production — applies catalog writes and returns; it never calls "
    "_emit_changelog or _bump_revision, so no entry and no revision counter is "
    "written for any schema change. The read half is unported too: "
    "fetch_ontology_changelog builds ontology_changelog_query and runs it via "
    "neptune.query, with no GraphStore path, so GET /ontology/changelog answers "
    "[] for every workspace no matter what was committed."
)


@pytest.mark.xfail(reason=CHANGELOG_GAP, strict=True)
@pytest.mark.asyncio
async def test_commit_records_an_entry_carrying_the_full_delta():
    g = "https://graph.infona.ai/graphs/acme"
    await commit_ontology(
        None,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="full_name",
                datatype="string",
            ),
        ],
        actor="alice",
        message="seed",
    )
    entries = await fetch_ontology_changelog(_DecommissionedSparql(), g)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == "commit_ontology"
    assert entry.subject == g
    assert entry.actor == "alice"
    assert entry.message == "seed"
    # The delta describes the change without consulting the live ontology.
    assert [c.kind for c in entry.changes] == [
        ChangeKind.ADD_TYPE,
        ChangeKind.ADD_ATTRIBUTE,
    ]
    assert entry.changes[1].slot_name == "full_name"


@pytest.mark.xfail(reason=CHANGELOG_GAP, strict=True)
@pytest.mark.asyncio
async def test_append_only_n_commits_n_distinct_entries():
    """N commits → N entries, all distinct; earlier ones are never rewritten."""
    g = "https://graph.infona.ai/graphs/t-append"
    for i in range(5):
        await commit_ontology(
            None,
            g,
            [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name=f"T{i}")],
        )
    entries = await fetch_ontology_changelog(_DecommissionedSparql(), g, limit=50)
    assert len(entries) == 5
    assert len({e.entry_uri for e in entries}) == 5


@pytest.mark.xfail(reason=CHANGELOG_GAP, strict=True)
@pytest.mark.asyncio
async def test_same_ms_commits_still_yield_distinct_entries(monkeypatch):
    """Two commits forced to the same timestamp stay two entries.

    Entry identity is a uuid node, never the timestamp — the property that
    keeps a burst of same-millisecond schema writes from collapsing into one.
    """
    from datetime import datetime, timezone

    from infona_client.graph import ontology_commit as oc_mod

    class _FixedDT:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 7, 28, 0, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(oc_mod, "datetime", _FixedDT)

    g = "https://graph.infona.ai/graphs/t-samems"
    await commit_ontology(
        None, g, [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="A")]
    )
    await commit_ontology(
        None, g, [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="B")]
    )
    entries = await fetch_ontology_changelog(_DecommissionedSparql(), g)
    assert len(entries) == 2
    assert len({e.entry_uri for e in entries}) == 2
    assert {e.timestamp for e in entries} == {"2026-07-28T00:00:00Z"}


@pytest.mark.asyncio
async def test_empty_commit_writes_no_changelog_entry():
    """A no-op commit must not manufacture history.

    This holds vacuously today (the GraphStore branch writes no entries at all —
    see the xfails above); it stays here so the property is still pinned once
    the changelog is ported.
    """
    g = "https://graph.infona.ai/graphs/t-empty"
    await commit_ontology(None, g, [])
    assert await fetch_ontology_changelog(_DecommissionedSparql(), g) == []
