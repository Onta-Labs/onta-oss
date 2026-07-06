"""Tests for the ``promote_to_node`` normalization rule (execute.py).

``promote_to_node`` is the "escape hatch" that makes a literal-by-default
modeling choice safe: a column first ingested as a plain literal
(``specialty = "Cardiology"``, ``rating = "4.6"``) can be turned into a
first-class entity NODE later, without re-ingesting. This suite exercises the
four behaviours that matter:

  1. **value-keyed categorical** — distinct values become SHARED nodes (free
     dedup); the same value across owners points at ONE node; the ontology range
     flips literal -> the target type.
  2. **owner-keyed measurement** — each owner gets its OWN node (two 4.6 ratings
     are NOT merged) and the original literal is PRESERVED losslessly as the
     node's ``value`` attribute.
  3. **idempotent re-run** — a second apply is a total no-op (the ``isLiteral``
     filter selects nothing once the object is a URI).
  4. **split** — a multi-valued literal ``"A, B"`` with ``split=True`` becomes
     two value-keyed nodes.

The fake Neptune from ``test_normalization`` is reused; a small subclass teaches
it the two SPARQL shapes this rule adds that the base fake doesn't already
evaluate: the promotion SELECT (all literal objects of the predicate) and the
``upsert_attribute`` ``rdfs:range`` DELETE/INSERT/WHERE flip.
"""

from __future__ import annotations

import re

import pytest

from cograph_client.normalization.execute import apply_rule
from cograph_client.normalization.rules import NormalizationRule, make_rule_id

from tests.test_normalization import (  # reuse the harness
    ENTITY,
    KG,
    RDF_TYPE,
    RDFS_LABEL,
    TENANT,
    TYPES,
    FakeNeptune,
    _split_ops,
    _stub_schedule_recompute,  # noqa: F401 — autouse fixture, imported to register
)

RDFS_RANGE = "http://www.w3.org/2000/01/rdf-schema#range"
KG_GRAPH = "https://cograph.tech/graphs/t1/kg/june-16"
ONTO_GRAPH = "https://cograph.tech/graphs/t1"


# --------------------------------------------------------------------------- #
# Fake extension: teach the base FakeNeptune the two shapes promote_to_node
# emits that it doesn't already handle.
#   - the promotion SELECT: SELECT ?s ?p ?o ... FILTER(?p = <prim> || ...) +
#     isLiteral(?o), with NO CONTAINS filter (promote applies to ALL literals).
#   - upsert_attribute's rdfs:range flip: DELETE { <a> <range> ?r } INSERT
#     { <a> <range> <new> } WHERE { OPTIONAL { <a> <range> ?r } }.
# --------------------------------------------------------------------------- #
class PromoteFakeNeptune(FakeNeptune):
    async def update(self, sparql: str) -> None:
        # Intercept the upsert_attribute single-valued-predicate replace form (a
        # DELETE/INSERT/WHERE with an OPTIONAL over rdfs:range or rdfs:comment)
        # BEFORE delegating — the base fake only understands VALUES /
        # single-subject / orphan DELETE shapes and would silently no-op this one.
        # Split into ops the SAME way the base does, apply the range/comment flip
        # ops ourselves, and delegate everything else to the base implementation.
        for op in _split_ops(sparql):
            op = op.strip()
            if not op:
                continue
            if self._apply_range_flip(op):
                continue
            await super().update(op)

    def _apply_range_flip(self, op: str) -> bool:
        """Apply an ``upsert_attribute``-style single-valued predicate replace.

        Matches ``DELETE { GRAPH <g> { <a> <PRED> ?x } } INSERT { GRAPH <g> {
        <a> <PRED> <new> } } WHERE { ... }`` and rewrites the object of
        ``(<a>, <PRED>)`` to ``<new>`` (delete-old-then-insert-new). Returns True
        iff it handled the op. Covers rdfs:range (the promotion's schema flip);
        the rdfs:comment block clears to empty (no INSERT) which is also handled.
        """
        if not op.startswith("DELETE") or "INSERT" not in op or "WHERE" not in op:
            return False
        del_m = re.search(
            r"DELETE \{ GRAPH <([^>]+)> \{ <([^>]+)> <([^>]+)> \?\w+ \} \}", op
        )
        if not del_m:
            return False
        graph, subj, pred = del_m.group(1), del_m.group(2), del_m.group(3)
        # Remove any existing (subj, pred, *) triples.
        for t in list(self._g(graph)):
            if t[0] == subj and t[1] == pred:
                self._g(graph).discard(t)
        # Insert the new object if the INSERT block carries a concrete one.
        ins_m = re.search(
            rf"INSERT \{{ GRAPH <{re.escape(graph)}> \{{ <{re.escape(subj)}> "
            rf"<{re.escape(pred)}> (<[^>]+>|\"(?:[^\"\\]|\\.)*\") \}} \}}",
            op,
        )
        if ins_m:
            raw = ins_m.group(1)
            obj = raw[1:-1] if raw.startswith("<") else raw.strip('"')
            self._g(graph).add((subj, pred, obj))
        return True

    def _eval_select(self, sparql: str, quads: set) -> list[dict]:
        # promote_to_node SELECT: our marker comment makes it unambiguous. Match
        # BEFORE the base (whose isLiteral branch would treat it as strip_emoji).
        if "# promote_to_node" in sparql:
            return self._eval_promote(sparql, quads)
        return super()._eval_select(sparql, quads)

    def _eval_promote(self, sparql: str, quads: set) -> list[dict]:
        # Predicate matches: primary/onto exact IRIs (?p = <...>) + the attrs
        # suffix (STRENDS). Objects must be literals (we model non-literals as
        # http(s) URIs, exactly like the base fake's _eval_explode_lit).
        exact = set(re.findall(r"\?p = <([^>]+)>", sparql))
        suffix_m = re.search(r'STRENDS\(STR\(\?p\), "([^"]+)"\)', sparql)
        suffix = suffix_m.group(1) if suffix_m else None
        out = []
        for (s, p, o) in quads:
            if not (p in exact or (suffix and p.endswith(suffix))):
                continue
            if o.startswith("http://") or o.startswith("https://"):
                continue  # URI object -> not a literal (already promoted)
            out.append({"s": s, "p": p, "o": o})
        return out


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #
def _seed_doctor_specialties(neptune: PromoteFakeNeptune) -> None:
    """3 Doctors: 2 with specialty 'Cardiology', 1 with 'Oncology'."""
    attr = TYPES + "Doctor/attrs/specialty"
    rows = [
        ("d1", "Cardiology"),
        ("d2", "Cardiology"),
        ("d3", "Oncology"),
    ]
    for local, val in rows:
        e = ENTITY + f"Doctor/{local}"
        neptune._g(KG_GRAPH).add((e, RDF_TYPE, TYPES + "Doctor"))
        neptune._g(KG_GRAPH).add((e, attr, val))
    # ontology declares specialty as a string attribute (range xsd:string).
    neptune._g(ONTO_GRAPH).update(
        {
            (attr, RDF_TYPE, "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"),
            (attr, "http://www.w3.org/2000/01/rdf-schema#domain", TYPES + "Doctor"),
            (attr, RDFS_RANGE, "http://www.w3.org/2001/XMLSchema#string"),
        }
    )


def _seed_shop_ratings(neptune: PromoteFakeNeptune) -> None:
    """2 CoffeeShops, both rating '4.6' (identical literal, different owners)."""
    attr = TYPES + "CoffeeShop/attrs/rating"
    for local in ("shop-1", "shop-2"):
        e = ENTITY + f"CoffeeShop/{local}"
        neptune._g(KG_GRAPH).add((e, RDF_TYPE, TYPES + "CoffeeShop"))
        neptune._g(KG_GRAPH).add((e, attr, "4.6"))
    neptune._g(ONTO_GRAPH).update(
        {
            (attr, RDF_TYPE, "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"),
            (attr, "http://www.w3.org/2000/01/rdf-schema#domain", TYPES + "CoffeeShop"),
            (attr, RDFS_RANGE, "http://www.w3.org/2001/XMLSchema#float"),
        }
    )


def _promote_rule(type_name, predicate, **params) -> NormalizationRule:
    return NormalizationRule(
        id=make_rule_id(KG, type_name, predicate, "promote_to_node"),
        kg_name=KG,
        type_name=type_name,
        predicate=predicate,
        target_kind="attribute",
        rule_type="promote_to_node",
        params=params,
        confidence=0.9,
        status="confirmed",
    )


def _range_of(neptune, attr_uri_str) -> str | None:
    for (s, p, o) in neptune._g(ONTO_GRAPH):
        if s == attr_uri_str and p == RDFS_RANGE:
            return o
    return None


# --------------------------------------------------------------------------- #
# 1. value-keyed categorical
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_promote_value_keyed_categorical_shares_nodes():
    neptune = PromoteFakeNeptune()
    _seed_doctor_specialties(neptune)
    rule = _promote_rule("Doctor", "specialty", target_type="Specialty", key_by="value")

    summary = await apply_rule(neptune, TENANT, rule)

    quads = neptune._g(KG_GRAPH)
    attr = TYPES + "Doctor/attrs/specialty"
    cardio = ENTITY + "Specialty/Cardiology"
    onco = ENTITY + "Specialty/Oncology"

    # 2 shared Specialty nodes (Cardiology reused across d1 + d2).
    assert (cardio, RDF_TYPE, TYPES + "Specialty") in quads
    assert (onco, RDF_TYPE, TYPES + "Specialty") in quads
    cardio_types = [t for t in quads if t[0] == cardio and t[1] == RDF_TYPE]
    assert len(cardio_types) == 1  # ONE node, not one-per-owner

    # rdfs:label + attrs/name set on the categorical node.
    assert (cardio, RDFS_LABEL, "Cardiology") in quads
    assert (cardio, TYPES + "Specialty/attrs/name", "Cardiology") in quads

    # edges rewired to the shared nodes.
    assert (ENTITY + "Doctor/d1", attr, cardio) in quads
    assert (ENTITY + "Doctor/d2", attr, cardio) in quads
    assert (ENTITY + "Doctor/d3", attr, onco) in quads

    # old literals gone.
    assert (ENTITY + "Doctor/d1", attr, "Cardiology") not in quads
    assert (ENTITY + "Doctor/d2", attr, "Cardiology") not in quads
    assert (ENTITY + "Doctor/d3", attr, "Oncology") not in quads

    # ontology range flipped string -> Specialty (a types/ URI).
    assert _range_of(neptune, attr) == TYPES + "Specialty"

    assert summary["nodes_created"] == 2
    assert summary["edges_added"] == 3
    assert summary["literals_promoted"] == 3


# --------------------------------------------------------------------------- #
# 2. owner-keyed measurement + value preserved
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_promote_owner_keyed_measurement_distinct_and_lossless():
    neptune = PromoteFakeNeptune()
    _seed_shop_ratings(neptune)
    rule = _promote_rule("CoffeeShop", "rating", target_type="Rating", key_by="owner")

    summary = await apply_rule(neptune, TENANT, rule)

    quads = neptune._g(KG_GRAPH)
    attr = TYPES + "CoffeeShop/attrs/rating"
    n1 = ENTITY + "Rating/shop-1-rating"
    n2 = ENTITY + "Rating/shop-2-rating"

    # 2 DISTINCT Rating nodes (NOT merged despite identical 4.6 value).
    assert n1 != n2
    assert (n1, RDF_TYPE, TYPES + "Rating") in quads
    assert (n2, RDF_TYPE, TYPES + "Rating") in quads

    # each preserves the original literal losslessly under attrs/value.
    assert (n1, TYPES + "Rating/attrs/value", "4.6") in quads
    assert (n2, TYPES + "Rating/attrs/value", "4.6") in quads
    # and carries the label.
    assert (n1, RDFS_LABEL, "4.6") in quads

    # edges rewired, literals gone.
    assert (ENTITY + "CoffeeShop/shop-1", attr, n1) in quads
    assert (ENTITY + "CoffeeShop/shop-2", attr, n2) in quads
    assert (ENTITY + "CoffeeShop/shop-1", attr, "4.6") not in quads
    assert (ENTITY + "CoffeeShop/shop-2", attr, "4.6") not in quads

    # ontology range flipped float -> Rating.
    assert _range_of(neptune, attr) == TYPES + "Rating"

    assert summary["nodes_created"] == 2  # distinct per owner
    assert summary["edges_added"] == 2
    assert summary["literals_promoted"] == 2


# --------------------------------------------------------------------------- #
# 3. idempotent re-run
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_promote_idempotent_second_run_is_noop():
    neptune = PromoteFakeNeptune()
    _seed_doctor_specialties(neptune)
    rule = _promote_rule("Doctor", "specialty", target_type="Specialty", key_by="value")

    first = await apply_rule(neptune, TENANT, rule)
    assert first["literals_promoted"] == 3

    before = set(neptune._g(KG_GRAPH))
    onto_before = set(neptune._g(ONTO_GRAPH))

    second = await apply_rule(neptune, TENANT, rule)

    # zero mutations: the objects are URIs now, so isLiteral selects nothing.
    assert second == {"nodes_created": 0, "edges_added": 0, "literals_promoted": 0}
    assert neptune._g(KG_GRAPH) == before
    assert neptune._g(ONTO_GRAPH) == onto_before


# --------------------------------------------------------------------------- #
# 4. split: "A, B" -> two value-keyed nodes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_promote_value_keyed_split_multivalue():
    neptune = PromoteFakeNeptune()
    attr = TYPES + "Doctor/attrs/specialty"
    e = ENTITY + "Doctor/d1"
    neptune._g(KG_GRAPH).add((e, RDF_TYPE, TYPES + "Doctor"))
    neptune._g(KG_GRAPH).add((e, attr, "Cardiology, Oncology"))
    neptune._g(ONTO_GRAPH).update(
        {
            (attr, RDF_TYPE, "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"),
            (attr, "http://www.w3.org/2000/01/rdf-schema#domain", TYPES + "Doctor"),
            (attr, RDFS_RANGE, "http://www.w3.org/2001/XMLSchema#string"),
        }
    )
    rule = _promote_rule(
        "Doctor", "specialty", target_type="Specialty", key_by="value", split=True
    )

    summary = await apply_rule(neptune, TENANT, rule)

    quads = neptune._g(KG_GRAPH)
    cardio = ENTITY + "Specialty/Cardiology"
    onco = ENTITY + "Specialty/Oncology"

    # the single "A, B" literal split into TWO value-keyed nodes + two edges.
    assert (cardio, RDF_TYPE, TYPES + "Specialty") in quads
    assert (onco, RDF_TYPE, TYPES + "Specialty") in quads
    assert (e, attr, cardio) in quads
    assert (e, attr, onco) in quads
    # packed literal gone.
    assert (e, attr, "Cardiology, Oncology") not in quads

    assert summary["nodes_created"] == 2
    assert summary["edges_added"] == 2
    assert summary["literals_promoted"] == 1  # one literal, two atoms


# --------------------------------------------------------------------------- #
# 5. the stubbed list_explode(attribute, target=entity) now routes to a
#    value-keyed split promotion instead of the old no-op.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_list_explode_attribute_target_entity_promotes():
    neptune = PromoteFakeNeptune()
    attr = TYPES + "Doctor/attrs/specialty"
    e = ENTITY + "Doctor/d1"
    neptune._g(KG_GRAPH).add((e, RDF_TYPE, TYPES + "Doctor"))
    neptune._g(KG_GRAPH).add((e, attr, "Cardiology; Oncology"))
    neptune._g(ONTO_GRAPH).update(
        {
            (attr, RDF_TYPE, "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"),
            (attr, "http://www.w3.org/2000/01/rdf-schema#domain", TYPES + "Doctor"),
            (attr, RDFS_RANGE, "http://www.w3.org/2001/XMLSchema#string"),
        }
    )
    rule = NormalizationRule(
        id=make_rule_id(KG, "Doctor", "specialty"),
        kg_name=KG,
        type_name="Doctor",
        predicate="specialty",
        target_kind="attribute",
        rule_type="list_explode",
        params={"delimiters": ["; "], "target": "entity"},
        confidence=0.9,
        status="confirmed",
    )

    summary = await apply_rule(neptune, TENANT, rule)

    quads = neptune._g(KG_GRAPH)
    # target_type derived from the predicate leaf -> "Specialty".
    cardio = ENTITY + "Specialty/Cardiology"
    onco = ENTITY + "Specialty/Oncology"
    assert (cardio, RDF_TYPE, TYPES + "Specialty") in quads
    assert (onco, RDF_TYPE, TYPES + "Specialty") in quads
    assert (e, attr, cardio) in quads
    assert (e, attr, onco) in quads
    assert (e, attr, "Cardiology; Oncology") not in quads
    # range upgraded to the derived Specialty type.
    assert _range_of(neptune, attr) == TYPES + "Specialty"

    assert summary["nodes_created"] == 2
    assert summary["literals_promoted"] == 1
