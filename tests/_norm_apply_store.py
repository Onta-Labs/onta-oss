"""Shared fixtures + helpers for the ``apply_rule``-on-the-store tests.

Split out of ``test_normalization_apply_store*.py`` only to keep both test
modules under the file-size budget; import the fixtures by name so pytest
collects them (``from tests._norm_apply_store import store  # noqa: F401``).

Everything seeds through the converged ``insert_facts`` path and reads the
``MemoryGraphStore`` back directly, so a test asserts on the SAME store the
code under test reads and writes.
"""

from __future__ import annotations

import pytest

from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests
from infona_client.normalization.rules import NormalizationRule, make_rule_id

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
ENT = "https://graph.infona.ai/entities/"
TYPES = "https://graph.infona.ai/types/"
ONTO = "https://graph.infona.ai/onto/"

TENANT = "t1"
KG = "june-16"


@pytest.fixture(autouse=True)
def _no_background_recompute(monkeypatch):
    """apply_rule fires a fire-and-forget Explorer type-stats recompute."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


@pytest.fixture
def store():
    reset_graph_store_for_tests()
    s = MemoryGraphStore()
    configure_graph_store(s)
    yield s
    reset_graph_store_for_tests()


# --------------------------------------------------------------------------- #
# Helpers — seed through the converged write path, read the store back.
# --------------------------------------------------------------------------- #
async def _seed(store, triples, tenant=TENANT, kg=KG):
    await insert_facts(None, kg_graph_uri(tenant, kg), list(triples), store=store)


def _props(store, entity_id, tenant=TENANT, kg=KG):
    row = store._entities.get((tenant, kg, entity_id))
    return None if row is None else dict(row.props)


def _values(store, entity_id, leaf, tenant=TENANT, kg=KG):
    """The leaf's value(s) as a list, whatever the store's scalar/list shape."""
    props = _props(store, entity_id, tenant, kg) or {}
    raw = props.get(leaf)
    if raw is None:
        return []
    return list(raw) if isinstance(raw, list) else [raw]


def _edges(store, tenant=TENANT, kg=KG):
    return {
        (r.start_id, r.attr, r.end_id)
        for r in store._rels.values()
        if r.tenant_id == tenant and r.kg == kg
    }


def _entity_ids(store, tenant=TENANT, kg=KG):
    return {eid for (t, k, eid) in store._entities if t == tenant and k == kg}


def _object_assertions(store, tenant=TENANT, kg=KG):
    return {
        (a.subject_id, a.property_id, a.object_id)
        for a in store._assertions.values()
        if a.tenant_id == tenant and a.kg == kg and a.object_id
    }


def _rule(type_name, predicate, rule_type, target_kind="attribute", **params):
    return NormalizationRule(
        id=make_rule_id(KG, type_name, predicate, rule_type),
        kg_name=KG,
        type_name=type_name,
        predicate=predicate,
        target_kind=target_kind,
        rule_type=rule_type,
        params=params,
        confidence=0.9,
        status="confirmed",
    )


def _mentor(idx, skills, type_name="Mentor"):
    uri = f"{ENT}{type_name}/{idx}"
    return uri, [
        (uri, RDF_TYPE, f"{TYPES}{type_name}"),
        (uri, f"{TYPES}{type_name}/attrs/skills", skills),
    ]
