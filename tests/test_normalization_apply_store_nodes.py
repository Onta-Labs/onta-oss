"""``apply_rule``'s NODE-valued rule shapes, on the shipped store (ONTA-534).

The other half of ``test_normalization_apply_store.py`` (see its docstring for
why the pre-existing ``FakeNeptune`` harness cannot cover any of this): the
composite-relationship ``list_explode``, its orphan-composite sweep, and
``promote_to_node`` in both node-identity strategies. Every test here fails on
``origin/main`` with ``SparqlClientRetired``.
"""

from __future__ import annotations

import pytest

from infona_client.graph.ontology_catalog import list_attributes
from infona_client.normalization.execute import apply_rule
from tests._norm_apply_store import (  # noqa: F401 — pytest collects the fixtures
    ENT,
    KG,
    ONTO,
    RDF_TYPE,
    RDFS_LABEL,
    TENANT,
    TYPES,
    _edges,
    _entity_ids,
    _no_background_recompute,
    _object_assertions,
    _rule,
    _seed,
    _values,
    store,
)


# --------------------------------------------------------------------------- #
# list_explode — composite relationship + orphan sweep
# --------------------------------------------------------------------------- #
COMP_ER = f"{ENT}Language/English__Russian"
COMP_EP = f"{ENT}Language/English__Persian"
ENGLISH = f"{ENT}Language/English"
RUSSIAN = f"{ENT}Language/Russian"
PERSIAN = f"{ENT}Language/Persian"


def _composite(uri, label):
    return [(uri, RDF_TYPE, f"{TYPES}Language"), (uri, RDFS_LABEL, label)]


def _speaks_seed():
    return [
        (f"{ENT}Mentor/A", RDF_TYPE, f"{TYPES}Mentor"),
        (f"{ENT}Mentor/B", RDF_TYPE, f"{TYPES}Mentor"),
        (f"{ENT}Mentor/A", ONTO + "speaks", COMP_ER),
        (f"{ENT}Mentor/B", ONTO + "speaks", COMP_EP),
        *_composite(COMP_ER, "English, Russian"),
        *_composite(COMP_EP, "English, Persian"),
    ]


def _speaks_rule():
    return _rule(
        "Mentor",
        "speaks",
        "list_explode",
        target_kind="relationship",
        delimiters=[", ", "__"],
        target="entity",
    )


@pytest.mark.asyncio
async def test_list_explode_relationship_repoints_dedupes_and_sweeps(store):
    await _seed(store, _speaks_seed())

    summary = await apply_rule(None, TENANT, _speaks_rule())

    assert _edges(store) == {
        (f"{ENT}Mentor/A", "speaks", ENGLISH),
        (f"{ENT}Mentor/A", "speaks", RUSSIAN),
        (f"{ENT}Mentor/B", "speaks", ENGLISH),
        (f"{ENT}Mentor/B", "speaks", PERSIAN),
    }
    # "English" from BOTH composites is ONE canonical node (free dedup).
    ids = _entity_ids(store)
    assert ENGLISH in ids and RUSSIAN in ids and PERSIAN in ids
    # The composites are swept: no inbound speaks edge left.
    assert COMP_ER not in ids and COMP_EP not in ids
    assert summary == {
        "edges_rewritten": 2,
        "atomic_created": 3,
        "orphans_dropped": 2,
    }


@pytest.mark.asyncio
async def test_list_explode_relationship_rerun_is_a_no_op(store):
    await _seed(store, _speaks_seed())
    rule = _speaks_rule()
    await apply_rule(None, TENANT, rule)
    before = _edges(store), _entity_ids(store)

    summary = await apply_rule(None, TENANT, rule)

    assert (_edges(store), _entity_ids(store)) == before
    assert summary == {
        "edges_rewritten": 0,
        "atomic_created": 0,
        "orphans_dropped": 0,
    }


@pytest.mark.asyncio
async def test_orphan_sweep_keeps_atomic_and_still_referenced_nodes(store):
    spanish = f"{ENT}Language/Spanish"
    await _seed(
        store,
        [
            *_speaks_seed(),
            # C also speaks the SAME composite as A — still referenced mid-pass.
            (f"{ENT}Mentor/C", RDF_TYPE, f"{TYPES}Mentor"),
            (f"{ENT}Mentor/C", ONTO + "speaks", COMP_ER),
            # A pre-existing ATOMIC node nobody points at: no delimiter, must live.
            *_composite(spanish, "Spanish"),
        ],
    )

    summary = await apply_rule(None, TENANT, _speaks_rule())

    ids = _entity_ids(store)
    assert spanish in ids, "an atomic node is never an orphan-sweep candidate"
    assert COMP_ER not in ids and COMP_EP not in ids
    assert (f"{ENT}Mentor/C", "speaks", RUSSIAN) in _edges(store)
    assert summary["orphans_dropped"] == 2


@pytest.mark.asyncio
async def test_orphan_sweep_reruns_from_the_declared_range(store):
    """A leftover composite with NOTHING to rewrite is swept on a later apply.

    The sweep's target type comes from the ONTOLOGY (the predicate's declared
    range), so a pure re-run — ``edges_rewritten == 0``, no composite touched
    this pass — still resolves ``Language`` and cleans up what a buggy earlier
    run left behind (COG-118).
    """
    from infona_client.graph.ontology_catalog import upsert_attribute

    await upsert_attribute(
        store=store,
        type_name="Mentor",
        attr_name="speaks",
        description="languages",
        datatype="Language",
        layer="tenant",
        tenant_id=TENANT,
    )
    leftover = f"{ENT}Language/German__Polish"
    await _seed(store, _composite(leftover, "German, Polish"))

    summary = await apply_rule(None, TENANT, _speaks_rule())

    assert summary["edges_rewritten"] == 0, "nothing to rewrite — sweep-only pass"
    assert summary["orphans_dropped"] == 1
    assert leftover not in _entity_ids(store)


@pytest.mark.asyncio
async def test_orphan_sweep_cannot_reach_another_tenant_or_kg(store):
    """The sweep deletes strictly inside its own ``(tenant_id, kg)``.

    A same-named composite of the same type exists in a peer workspace AND in a
    sibling KG of the same workspace. Both must survive an apply that sweeps the
    local one — the read runs on a session pinned to the rule's scope.
    """
    await _seed(store, _speaks_seed())
    await _seed(store, _composite(COMP_ER, "English, Russian"), tenant="t2")
    await _seed(store, _composite(COMP_ER, "English, Russian"), kg="other-kg")

    await apply_rule(None, TENANT, _speaks_rule())

    assert COMP_ER not in _entity_ids(store)
    assert COMP_ER in _entity_ids(store, tenant="t2", kg=KG)
    assert COMP_ER in _entity_ids(store, kg="other-kg")


# --------------------------------------------------------------------------- #
# promote_to_node
# --------------------------------------------------------------------------- #
DOCTOR_1 = f"{ENT}Doctor/d1"
DOCTOR_2 = f"{ENT}Doctor/d2"
CARDIOLOGY = f"{ENT}Specialty/Cardiology"


def _doctors_seed():
    return [
        (DOCTOR_1, RDF_TYPE, f"{TYPES}Doctor"),
        (DOCTOR_1, f"{TYPES}Doctor/attrs/specialty", "Cardiology"),
        (DOCTOR_2, RDF_TYPE, f"{TYPES}Doctor"),
        (DOCTOR_2, f"{TYPES}Doctor/attrs/specialty", "Cardiology"),
    ]


def _promote_rule(key_by="value", target_type="Specialty", **extra):
    return _rule(
        "Doctor",
        "specialty",
        "promote_to_node",
        target_type=target_type,
        key_by=key_by,
        **extra,
    )


@pytest.mark.asyncio
async def test_promote_to_node_value_keyed_shares_one_node(store):
    await _seed(store, _doctors_seed())

    summary = await apply_rule(None, TENANT, _promote_rule())

    assert CARDIOLOGY in _entity_ids(store)
    assert _edges(store) == {
        (DOCTOR_1, "specialty", CARDIOLOGY),
        (DOCTOR_2, "specialty", CARDIOLOGY),
    }
    # The literal is gone from BOTH owners; the node carries the human value.
    assert _values(store, DOCTOR_1, "specialty") == []
    assert store._entities[(TENANT, KG, CARDIOLOGY)].name == "Cardiology"
    assert summary == {"nodes_created": 1, "edges_added": 2, "literals_promoted": 2}


@pytest.mark.asyncio
async def test_promote_to_node_flips_the_declared_range(store):
    await _seed(store, _doctors_seed())

    await apply_rule(None, TENANT, _promote_rule())

    attrs = await list_attributes(
        store=store, type_name="Doctor", layer="tenant", tenant_id=TENANT
    )
    declared = {a.name: (a.kind, a.range_type) for a in attrs}
    assert declared["specialty"] == ("relationship", "Specialty")


@pytest.mark.asyncio
async def test_promote_to_node_keeps_the_object_assertion(store):
    """The literal clear must not take the new edge's Assertion with it.

    ``object_property_iri(leaf) == datatype_property_iri(leaf)`` — one Property
    IRI per leaf whatever the range — so the predicate-scoped literal clear
    matches the OBJECT Assertion the promotion just wrote. The relationship
    projection survives either way, which is what makes the loss silent: the
    Explorer would still draw the edge while the Assertion-backed NL reads
    stopped seeing it.
    """
    await _seed(store, _doctors_seed())

    await apply_rule(None, TENANT, _promote_rule())

    prop = "https://graph.infona.ai/properties/specialty"
    assert (DOCTOR_1, prop, CARDIOLOGY) in _object_assertions(store)
    assert (DOCTOR_2, prop, CARDIOLOGY) in _object_assertions(store)


@pytest.mark.asyncio
async def test_promote_to_node_rerun_is_a_no_op(store):
    await _seed(store, _doctors_seed())
    rule = _promote_rule()
    await apply_rule(None, TENANT, rule)
    before = _edges(store), _entity_ids(store)

    summary = await apply_rule(None, TENANT, rule)

    assert (_edges(store), _entity_ids(store)) == before
    assert summary == {"nodes_created": 0, "edges_added": 0, "literals_promoted": 0}


@pytest.mark.asyncio
async def test_promote_to_node_owner_keyed_preserves_the_value(store):
    """A measurement lands on its own node in its NATIVE type.

    Seeded as the typed literal ingest really writes (``"4.6"^^xsd:float``,
    which the store holds as a real float), so "preserved losslessly" means the
    node's ``value`` is still a float — not a stringified copy of it.
    """
    shop = f"{ENT}Shop/s1"
    await _seed(
        store,
        [
            (shop, RDF_TYPE, f"{TYPES}Shop"),
            (
                shop,
                f"{TYPES}Shop/attrs/rating",
                "4.6^^http://www.w3.org/2001/XMLSchema#float",
            ),
        ],
    )
    assert _values(store, shop, "rating") == [4.6], "premise: a real float"
    rule = _rule(
        "Shop", "rating", "promote_to_node", target_type="Rating", key_by="owner"
    )

    summary = await apply_rule(None, TENANT, rule)

    node = f"{ENT}Rating/s1-rating"
    assert (shop, "rating", node) in _edges(store)
    assert _values(store, node, "value") == [4.6], "measurement kept losslessly"
    assert _values(store, shop, "rating") == []
    assert summary["nodes_created"] == 1


@pytest.mark.asyncio
async def test_list_explode_attribute_to_entity_promotes_each_atom(store):
    """The fourth shape: ``list_explode`` on an ATTRIBUTE with target=entity.

    Routed through ``promote_to_node`` as value-keyed + split, so ``"A, B"``
    becomes two SHARED categorical nodes rather than one node named ``"A, B"``.
    """
    doc = f"{ENT}Doctor/d1"
    await _seed(
        store,
        [
            (doc, RDF_TYPE, f"{TYPES}Doctor"),
            (doc, f"{TYPES}Doctor/attrs/specialty", "Cardiology, Oncology"),
        ],
    )

    summary = await apply_rule(
        None,
        TENANT,
        _rule(
            "Doctor",
            "specialty",
            "list_explode",
            delimiters=[", "],
            target="entity",
        ),
    )

    assert _edges(store) == {
        (doc, "specialty", CARDIOLOGY),
        (doc, "specialty", f"{ENT}Specialty/Oncology"),
    }
    assert _values(store, doc, "specialty") == []
    assert summary["nodes_created"] == 2


def test_extract_atom_bracket_id():
    from types import SimpleNamespace

    from infona_client.normalization.execute_helpers import (
        _delimiters,
        _extract_atom,
        _join_atom_key,
        _split,
    )

    assert _extract_atom("Ada Lovelace [1]", "bracket_id") == "1"
    assert _extract_atom("Ada Lovelace", "bracket_id") == "Ada Lovelace"
    key, joined = _join_atom_key("vip", None, {"VIP": "1"})
    assert (key, joined) == ("1", True)
    key, joined = _join_atom_key("Unknown", None, {"VIP": "1"})
    assert (key, joined) == ("Unknown", False)
    assert _split("A; B", ["; ", ";"]) == ["A", "B"]
    d = _delimiters(SimpleNamespace(params={"delimiters": ["; ", ";"]}))
    assert "; " in d and ", " not in d and " | " not in d


def test_promote_execute_modules_do_not_hardcode_fixture_tokens():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "infona_client" / "normalization"
    banned = ("VIP", "Portland", "Ada Lovelace")
    for name in ("execute_promote.py", "execute_helpers.py", "execute.py"):
        text = (root / name).read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{name} mentions {token!r}"


@pytest.mark.asyncio
async def test_promote_split_semicolon_list(store):
    doc = f"{ENT}Doctor/d1"
    await _seed(
        store,
        [
            (doc, RDF_TYPE, f"{TYPES}Doctor"),
            (doc, f"{TYPES}Doctor/attrs/specialty", "A; B"),
        ],
    )
    summary = await apply_rule(
        None,
        TENANT,
        _promote_rule(split=True, delimiters=["; ", ";"]),
    )
    assert (doc, "specialty", f"{ENT}Specialty/A") in _edges(store)
    assert (doc, "specialty", f"{ENT}Specialty/B") in _edges(store)
    assert _values(store, doc, "specialty") == []
    assert summary["nodes_created"] == 2
    assert summary["edges_added"] == 2


@pytest.mark.asyncio
async def test_promote_link_existing_does_not_clobber_label(store):
    from infona_client.graph.ontology_queries import entity_uri

    staff = entity_uri("Staff", "1")
    contact = f"{ENT}Contact/c1"
    await _seed(
        store,
        [
            (staff, RDF_TYPE, f"{TYPES}Staff"),
            (staff, RDFS_LABEL, "Ada Lovelace"),
            (staff, f"{TYPES}Staff/attrs/name", "Ada Lovelace"),
            (contact, RDF_TYPE, f"{TYPES}Contact"),
            (contact, f"{TYPES}Contact/attrs/owner", "Ada Lovelace [1]"),
        ],
    )
    summary = await apply_rule(
        None,
        TENANT,
        _rule(
            "Contact",
            "owner",
            "promote_to_node",
            target_type="Staff",
            key_by="value",
            extract="bracket_id",
            link_existing=True,
        ),
    )
    assert (contact, "owner", staff) in _edges(store)
    assert _values(store, contact, "owner") == []
    assert store._entities[(TENANT, KG, staff)].name == "Ada Lovelace"
    assert summary["nodes_created"] == 0
    assert summary["edges_added"] == 1


@pytest.mark.asyncio
async def test_promote_key_map_joins_existing_tag(store):
    from infona_client.graph.ontology_queries import entity_uri

    tag_a = entity_uri("Tag", "1")
    contact = f"{ENT}Contact/c1"
    await _seed(
        store,
        [
            (tag_a, RDF_TYPE, f"{TYPES}Tag"),
            (tag_a, RDFS_LABEL, "A"),
            (tag_a, f"{TYPES}Tag/attrs/name", "A"),
            (contact, RDF_TYPE, f"{TYPES}Contact"),
            (contact, f"{TYPES}Contact/attrs/contact_tags", "A; B"),
        ],
    )
    tag_b = entity_uri("Tag", "2")
    await _seed(
        store,
        [
            (tag_b, RDF_TYPE, f"{TYPES}Tag"),
            (tag_b, RDFS_LABEL, "B"),
            (tag_b, f"{TYPES}Tag/attrs/name", "B"),
        ],
    )
    await apply_rule(
        None,
        TENANT,
        _rule(
            "Contact",
            "contact_tags",
            "promote_to_node",
            target_type="Tag",
            key_by="value",
            split=True,
            delimiters=["; ", ";"],
            key_map={"A": "1", "B": "2"},
            link_existing=True,
        ),
    )
    edges = _edges(store)
    assert (contact, "contact_tags", tag_a) in edges
    assert (contact, "contact_tags", tag_b) in edges
    assert (contact, "contact_tags", entity_uri("Tag", "A")) not in edges
    assert _values(store, contact, "contact_tags") == []
    assert len([e for e in edges if e[0] == contact]) == 2


@pytest.mark.asyncio
async def test_promote_key_map_mixed_join_and_mint(store):
    """Case-mismatched known tag joins; unknown token mints a typed node."""
    from infona_client.graph.ontology_queries import entity_uri

    known = entity_uri("Tag", "1")
    contact = f"{ENT}Contact/c1"
    await _seed(
        store,
        [
            (known, RDF_TYPE, f"{TYPES}Tag"),
            (known, RDFS_LABEL, "VIP"),
            (contact, RDF_TYPE, f"{TYPES}Contact"),
            (contact, f"{TYPES}Contact/attrs/contact_tags", "vip; Unknown"),
        ],
    )
    summary = await apply_rule(
        None,
        TENANT,
        _rule(
            "Contact",
            "contact_tags",
            "promote_to_node",
            target_type="Tag",
            key_by="value",
            split=True,
            delimiters=["; ", ";"],
            key_map={"VIP": "1"},
            link_existing=True,
        ),
    )
    unknown = entity_uri("Tag", "Unknown")
    edges = _edges(store)
    assert (contact, "contact_tags", known) in edges
    assert (contact, "contact_tags", entity_uri("Tag", "VIP")) not in edges
    assert (contact, "contact_tags", unknown) in edges
    assert unknown in _entity_ids(store)
    assert store._entities[(TENANT, KG, unknown)].name == "Unknown"
    assert store._entities[(TENANT, KG, known)].name == "VIP"
    assert summary["nodes_created"] == 1
    assert summary["edges_added"] == 2


@pytest.mark.asyncio
async def test_promote_explicit_delimiters_do_not_split_on_comma(store):
    from infona_client.graph.ontology_queries import entity_uri as eu

    doc = f"{ENT}Doctor/d1"
    await _seed(
        store,
        [
            (doc, RDF_TYPE, f"{TYPES}Doctor"),
            (doc, f"{TYPES}Doctor/attrs/specialty", "Foo, Bar; Baz"),
        ],
    )
    await apply_rule(None, TENANT, _promote_rule(split=True, delimiters=["; ", ";"]))
    edges = _edges(store)
    assert (doc, "specialty", eu("Specialty", "Foo, Bar")) in edges
    assert (doc, "specialty", eu("Specialty", "Baz")) in edges
    assert (doc, "specialty", eu("Specialty", "Foo")) not in edges
