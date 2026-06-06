"""COG-34: subclass-closure rewriter coverage for INDIRECT type-selection shapes.

ADR 0001 multi-typing rewrites type-assertion triples into a subclass-closure
property path so a query over a parent type returns subtype instances. The
DIRECT forms (`?x a <T>`, `?x <rdf:type> <T>`, `?x rdf:type <T>`) are covered by
tests in test_multityping_gaps.py.

This file covers the INDIRECT shapes the LLM sometimes emits, where the type is
bound to a VARIABLE in the rdf:type object position and constrained elsewhere:

  D) VALUES form:   `VALUES ?t { <types/T> } ... ?x <rdf:type> ?t`
  E) FILTER `=`:    `?x <rdf:type> ?t . FILTER(?t = <types/T>)`
  F) FILTER `IN`:   `?x <rdf:type> ?t . FILTER(?t IN (<types/T1>, <types/T2>))`

Plus: UNION-of-direct-forms (no new code needed), idempotency, and a negative
case (a non-type predicate whose object is a types URI must NOT be rewritten).

All deterministic string-level assertions — no live Neptune, no LLM.
"""

from __future__ import annotations

from cograph_client.graph.ontology_queries import rewrite_type_predicate_to_closure

CLOSURE = (
    "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
    "/<http://www.w3.org/2000/01/rdf-schema#subClassOf>*"
)
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
PERSON = "<https://cograph.tech/types/Person>"
ARTIST = "<https://cograph.tech/types/Artist>"


# ---------------------------------------------------------------------------
# Form D — VALUES binds the type variable
# ---------------------------------------------------------------------------


def test_values_form_rewritten():
    q = (
        "SELECT ?x WHERE { "
        f"VALUES ?t {{ {PERSON} }} "
        f"?x {RDF_TYPE} ?t "
        "}"
    )
    out = rewrite_type_predicate_to_closure(q)
    # The rdf:type predicate on the constrained variable is upgraded to closure...
    assert f"?x {CLOSURE} ?t" in out
    # ...and the VALUES binding itself is left intact.
    assert f"VALUES ?t {{ {PERSON} }}" in out
    # The original bare rdf:type triple no longer appears.
    assert f"?x {RDF_TYPE} ?t" not in out


# ---------------------------------------------------------------------------
# Form E — FILTER equality binds the type variable
# ---------------------------------------------------------------------------


def test_filter_eq_form_rewritten():
    q = (
        "SELECT ?x WHERE { "
        f"?x {RDF_TYPE} ?t . "
        f"FILTER(?t = {PERSON}) "
        "}"
    )
    out = rewrite_type_predicate_to_closure(q)
    assert f"?x {CLOSURE} ?t" in out
    assert f"FILTER(?t = {PERSON})" in out


# ---------------------------------------------------------------------------
# Form F — FILTER IN binds the type variable to one of several types
# ---------------------------------------------------------------------------


def test_filter_in_form_rewritten():
    q = (
        "SELECT ?x WHERE { "
        f"?x {RDF_TYPE} ?t . "
        f"FILTER(?t IN ({PERSON}, {ARTIST})) "
        "}"
    )
    out = rewrite_type_predicate_to_closure(q)
    assert f"?x {CLOSURE} ?t" in out
    assert f"FILTER(?t IN ({PERSON}, {ARTIST}))" in out


# ---------------------------------------------------------------------------
# UNION of direct forms — each branch already covered by Form A/B/C, no new code
# ---------------------------------------------------------------------------


def test_union_of_direct_forms_covered():
    q = (
        "SELECT ?x WHERE { "
        f"{{ ?x a {PERSON} }} UNION {{ ?x a {ARTIST} }} "
        "}"
    )
    out = rewrite_type_predicate_to_closure(q)
    assert f"?x {CLOSURE} {PERSON}" in out
    assert f"?x {CLOSURE} {ARTIST}" in out


# ---------------------------------------------------------------------------
# Idempotency — running twice equals running once
# ---------------------------------------------------------------------------


def test_indirect_rewrite_is_idempotent():
    q = (
        "SELECT ?x WHERE { "
        f"VALUES ?t {{ {PERSON} }} "
        f"?x {RDF_TYPE} ?t "
        "}"
    )
    once = rewrite_type_predicate_to_closure(q)
    twice = rewrite_type_predicate_to_closure(once)
    assert once == twice
    # Exactly one closure path was introduced (no double-wrapping).
    assert once.count("subClassOf>*") == 1


def test_filter_in_rewrite_is_idempotent():
    q = (
        "SELECT ?x WHERE { "
        f"?x {RDF_TYPE} ?t . "
        f"FILTER(?t IN ({PERSON}, {ARTIST})) "
        "}"
    )
    once = rewrite_type_predicate_to_closure(q)
    twice = rewrite_type_predicate_to_closure(once)
    assert once == twice
    assert once.count("subClassOf>*") == 1


# ---------------------------------------------------------------------------
# Negative case — a non-type predicate bound to a type-URI variable is untouched
# ---------------------------------------------------------------------------


def test_non_type_predicate_with_type_uri_not_rewritten():
    # ?t is bound to a types URI via VALUES, but it's the object of a NORMAL
    # predicate (works_at), not rdf:type. Must NOT be rewritten.
    q = (
        "SELECT ?x WHERE { "
        f"VALUES ?t {{ {PERSON} }} "
        "?x <https://cograph.tech/onto/works_at> ?t "
        "}"
    )
    assert rewrite_type_predicate_to_closure(q) == q


def test_unconstrained_type_variable_not_rewritten():
    # ?t is the object of an rdf:type triple but has NO VALUES/FILTER constraint
    # to a types URI. The indirect pass must leave it alone (would be a no-op
    # closure that changes semantics for an open type variable).
    q = (
        "SELECT ?x ?t WHERE { "
        f"?x {RDF_TYPE} ?t "
        "}"
    )
    assert rewrite_type_predicate_to_closure(q) == q
