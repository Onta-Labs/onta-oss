"""KG registration is part of the shared write path (ONTA-153).

The bug: the record that ``list_kgs`` reads to populate the Explorer dropdown
was written in exactly ONE place — ``create_kg``, the Explorer's "New KG" button.
Any non-UI writer (agent web-discovery, CLI, MCP) that ingested into a brand-new
``kg_name`` wrote the instance data + ontology but the KG never appeared in the
dropdown (``list_kgs`` returned ``[]``).

Fix: ``refresh_after_write`` — the shared post-write housekeeping every writer
already calls — registers the KG idempotently.

**Ported by ONTA-527.** Registration used to be a guarded SPARQL
``INSERT … WHERE { FILTER NOT EXISTS { … } }`` writing
``<kg_uri> <onto/kg_name> "name"`` into the tenant metadata graph, and this file
asserted that statement's TEXT: the NOT-EXISTS guard, escaped literals, balanced
quotes, no ``kg_triple_count 0``. Registration is a ``:KnowledgeGraph`` node now
(``graph/kg_registry.py``), reached from ``refresh_after_write`` via
``ensure_kg_registered_store``, so every one of those assertions described a
statement nobody emits.

Two consequences worth stating plainly:

* ``kg_writer.ensure_kg_registered`` (the SPARQL helper) has **no product
  callers left** — the tests that drove it with an ``AsyncMock`` were the only
  ones. Rather than keep exercising a dead helper, each behaviour it guaranteed
  is re-pinned here against the live path: idempotent, non-clobbering, refuses a
  name that could not be created through the UI, never fails the write, and
  round-trips into ``GET /kgs``.
* SPARQL-literal escaping is no longer a thing that can go wrong: names and
  descriptions are Cypher PARAMETERS. The injection cases are ported to the
  observable property that mattered — hostile text round-trips as DATA, and
  changes nothing else in the workspace.
"""

import asyncio

import pytest

from infona_client.graph.kg_registry import (
    ensure_kg_registered_store,
    list_registered_kgs,
    upsert_registered_kg,
)
from infona_client.graph.kg_writer import refresh_after_write

TENANT = "test-tenant"


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _names(tenant_id: str = TENANT) -> list[str]:
    return [e["name"] for e in _run(list_registered_kgs(tenant_id))]


def _row(name: str, tenant_id: str = TENANT) -> dict:
    for e in _run(list_registered_kgs(tenant_id)):
        if e["name"] == name:
            return e
    return {}


# --- ensure_kg_registered_store: shape, idempotency, validation ---------------


def test_ensure_kg_registered_creates_exactly_one_registration():
    _run(ensure_kg_registered_store(TENANT, "fresh-kg"))
    assert _names() == ["fresh-kg"]
    # P2: no stale-on-arrival count is claimed for a KG that already has data.
    assert _row("fresh-kg")["triple_count"] == 0


def test_ensure_kg_registered_is_idempotent_and_non_clobbering():
    """Calling twice never duplicates, and never overwrites a description.

    This is what the ``FILTER NOT EXISTS`` guard bought on the SPARQL path; the
    registry buys it with ``only_if_absent=True`` on the MERGE.
    """
    _run(upsert_registered_kg(TENANT, "k", description="the real description"))
    _run(ensure_kg_registered_store(TENANT, "k"))
    _run(ensure_kg_registered_store(TENANT, "k"))

    assert _names() == ["k"]
    assert _row("k")["description"] == "the real description"


@pytest.mark.parametrize(
    "bad", ['evil" .', "back\\slash", "line\nbreak", "has>angle", "with space", "../x"]
)
def test_ensure_kg_registered_refuses_a_name_the_ui_could_not_create(bad):
    """A name outside the KG charset is skipped, not registered.

    On SPARQL this guard kept a ``>`` out of an interpolated IRI. The property
    graph has no IRI to corrupt, but the guard is still load-bearing: such a
    name cannot be addressed by the KG-scoped routes (``kg_graph_uri`` raises),
    so registering one would put an unusable row in the Explorer dropdown.
    """
    _run(ensure_kg_registered_store(TENANT, bad))
    assert _names() == []


def test_ensure_kg_registered_best_effort_on_failure(monkeypatch):
    """A registration failure must never propagate out of the write path."""
    import infona_client.graph.kg_registry as registry_mod

    async def boom(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(registry_mod, "upsert_registered_kg", boom)
    _run(ensure_kg_registered_store(TENANT, "k"))  # must not raise


def test_ensure_kg_registered_noop_without_name():
    _run(ensure_kg_registered_store(TENANT, ""))
    assert _names() == []


def test_registration_is_tenant_scoped():
    """A peer workspace never sees this registration."""
    _run(ensure_kg_registered_store(TENANT, "mine"))
    assert _names("other-tenant") == []


# --- refresh_after_write hook: registers fresh KG, never duplicates -----------


def test_refresh_after_write_registers_fresh_kg(monkeypatch):
    """A writer producing facts into a fresh kg_name → refresh_after_write must
    register it (the part that was missing for non-UI writers). A SECOND refresh
    must NOT duplicate it, nor clobber a description set in between."""
    import infona_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(
        pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None
    )
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)

    # ``neptune`` is vestigial on this path (ONTA-527) — None is what a writer
    # with no legacy client passes, and the count/stats steps skip themselves.
    _run(refresh_after_write(None, tenant_id=TENANT, kg_name="brand-new"))
    assert _names() == ["brand-new"]

    _run(upsert_registered_kg(TENANT, "brand-new", description="set by the user"))
    _run(refresh_after_write(None, tenant_id=TENANT, kg_name="brand-new"))

    assert _names() == ["brand-new"]
    assert _row("brand-new")["description"] == "set by the user"


def test_refresh_after_write_skips_registration_without_kg(monkeypatch):
    """A tenant-graph-only write (no kg_name) registers nothing."""
    import infona_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(
        pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None
    )
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)

    _run(refresh_after_write(None, tenant_id=TENANT, kg_name=None))
    assert _names() == []


# --- create_kg route: hostile text is data (P0) + truthful re-create (P1) -----


def test_create_kg_treats_a_hostile_description_as_data(
    client, mock_neptune, auth_headers
):
    """A description carrying quotes / braces / a statement separator changes
    nothing but its own row, and comes back byte-identical.

    The SPARQL version of this asserted ``\\"`` appeared in the emitted UPDATE
    and that its quotes balanced. There is no statement to inspect now, so the
    property is asserted where it actually matters: the text survives a
    round-trip as a VALUE, and the peer KG registered alongside it is untouched
    (a real breakout would have run its ``DROP``-shaped payload).
    """
    _run(upsert_registered_kg(TENANT, "bystander", description="untouched"))
    hostile = 'evil" } } ; DROP ALL ; #\\'

    resp = client.post(
        f"/graphs/{TENANT}/kgs",
        json={"name": "k", "description": hostile},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["description"] == hostile

    listed = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers).json()
    by_name = {row["name"]: row for row in listed}
    assert by_name["k"]["description"] == hostile
    assert by_name["bystander"]["description"] == "untouched"
    mock_neptune.update.assert_not_called()


def test_create_kg_recreate_keeps_the_existing_description(
    client, mock_neptune, auth_headers
):
    """Re-POSTing an existing KG with an empty description must not blank it."""
    _run(upsert_registered_kg(TENANT, "k", description="the real description"))

    resp = client.post(
        f"/graphs/{TENANT}/kgs",
        json={"name": "k", "description": ""},  # caller sends empty on re-create
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["description"] == "the real description"
    assert _row("k")["description"] == "the real description"


@pytest.mark.xfail(
    reason=(
        "BUG (introduced by the Neo4j cutover, surfaced by ONTA-527): "
        "api/routes/knowledge_graphs.py::create_kg calls upsert_registered_kg "
        "with triple_count=0 unconditionally, and graph/kg_registry.py's MERGE "
        "overwrites the stored count on ON MATCH whenever $triple_count is not "
        "NULL. So re-POSTing an existing KG — an idempotent create the Explorer "
        "and CLI both do — resets its triple_count to 0 and the 201 body "
        "reports 0 for a KG that may hold real data. The SPARQL version could "
        "not do this: its INSERT was NOT-EXISTS-guarded and it read the "
        "registration back before answering. Masked in practice by the deeper "
        "gap in test_kg_list_counts.py (nothing ever writes a non-zero "
        "triple_count on this path), which is why the fix belongs with that "
        "one: pass triple_count=None on create so an existing count is kept."
    ),
    strict=True,
)
def test_create_kg_recreate_does_not_reset_the_triple_count(client, auth_headers):
    _run(upsert_registered_kg(TENANT, "k", description="real", triple_count=50000))

    resp = client.post(
        f"/graphs/{TENANT}/kgs",
        json={"name": "k", "description": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["triple_count"] == 50000
    assert _row("k")["triple_count"] == 50000


def test_create_kg_new_path_contract_unchanged(client, auth_headers):
    """The create-new path returns the values just written (description as
    given, count 0)."""
    resp = client.post(
        f"/graphs/{TENANT}/kgs",
        json={"name": "k", "description": "brand new kg"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "k"
    assert body["description"] == "brand new kg"
    assert body["triple_count"] == 0


# --- Round-trip: register, then GET /kgs returns the KG ------------------------


def test_register_then_list_roundtrip(client, mock_neptune, auth_headers):
    """ensure_kg_registered_store writes a record ``list_kgs`` reads back —
    pinning the shared registry contract end-to-end, with no SPARQL involved."""
    _run(ensure_kg_registered_store(TENANT, "my-kg"))

    resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)
    assert resp.status_code == 200
    assert [row["name"] for row in resp.json()] == ["my-kg"]
    mock_neptune.query.assert_not_called()
