"""``apply_rule``'s LITERAL rule shapes, on the shipped store (ONTA-534).

Every test here FAILS on ``origin/main``: ``normalization/execute.py`` READ
through the retired SPARQL HTTP client, so ``apply_rule`` raised
``SparqlClientRetired`` on its FIRST read for ``strip_emoji``,
``promote_to_node`` and BOTH ``list_explode`` shapes. The route acks 202 and the
apply runs detached, so the whole feature was dead with no user-visible error
until #452 made the failure durable.

The pre-existing ``tests/test_normalization.py`` cannot cover this: its
``FakeNeptune`` seeds a SPARQL quad store while every write — and now every
read — goes to the process ``GraphStore``, so its execution layer is
xfail(strict) by construction. These tests seed the SAME ``MemoryGraphStore``
the code reads and writes, and assert the GRAPH actually changed.

``strip_emoji`` + ``list_explode`` (packed literal) here; the node-valued
shapes are in ``test_normalization_apply_store_nodes.py``.
"""

from __future__ import annotations

import pytest

from infona_client.graph.schema_bootstrap import TEMPLATES
from infona_client.normalization.apply_job import apply_and_record
from infona_client.normalization.execute import apply_rule
from infona_client.normalization.rules import NormalizationRuleStore
from tests._norm_apply_store import (  # noqa: F401 — pytest collects the fixtures
    KG,
    TENANT,
    TYPES,
    _mentor,
    _no_background_recompute,
    _props,
    _rule,
    _seed,
    _values,
    store,
)


# --------------------------------------------------------------------------- #
# Template registry contract
# --------------------------------------------------------------------------- #
def test_apply_read_templates_are_registered_and_read_only():
    for name in (
        "entity_literals_by_prop",
        "entity_rels_by_attr",
        "entity_orphans_of_type",
    ):
        assert name in TEMPLATES
        assert TEMPLATES[name].writing is False
        assert "$tenant_id" in TEMPLATES[name].cypher
        assert "$kg" in TEMPLATES[name].cypher


# --------------------------------------------------------------------------- #
# strip_emoji
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_strip_emoji_cleans_the_literal(store):
    uri, triples = _mentor("m1", "\U0001f3a8 design")
    await _seed(store, triples)

    summary = await apply_rule(None, TENANT, _rule("Mentor", "skills", "strip_emoji"))

    assert _values(store, uri, "skills") == ["design"]
    assert summary == {"literals_cleaned": 1, "triples_rewritten": 1}


@pytest.mark.asyncio
async def test_strip_emoji_is_predicate_scoped_across_types(store):
    """One rule on Mentor.skills ALSO cleans Coach.skills.

    The SPARQL this replaces filtered on the predicate alone — no
    ``?s rdf:type`` constraint — so a rule cleaned every type's values of that
    leaf. Scoping the port to the rule's own type would have quietly stopped
    cleaning the others: no error, no migration, a rule silently doing less.
    This pins the decision so a future "tidy-up" has to argue with a test.
    """
    mentor, m_triples = _mentor("m1", "\U0001f3a8 design")
    coach, c_triples = _mentor("c1", "\U0001f680 growth", type_name="Coach")
    await _seed(store, [*m_triples, *c_triples])

    summary = await apply_rule(None, TENANT, _rule("Mentor", "skills", "strip_emoji"))

    assert _values(store, mentor, "skills") == ["design"]
    assert _values(store, coach, "skills") == ["growth"]
    assert summary["literals_cleaned"] == 2


@pytest.mark.asyncio
async def test_strip_emoji_keeps_the_subjects_untouched_sibling_values(store):
    """A multi-valued leaf keeps its clean values when one sibling is rewritten.

    A property-graph literal delete is PREDICATE-scoped (``pg_ops.
    delete_literals`` drops the key, not one value of it), so the handler must
    re-insert the whole cleaned set. Rewriting only the dirty value would take
    ``python`` with it.
    """
    uri, triples = _mentor("m1", "\U0001f3a8 design")
    await _seed(store, [*triples, (uri, f"{TYPES}Mentor/attrs/skills", "python")])
    assert sorted(_values(store, uri, "skills")) == ["python", "\U0001f3a8 design"]

    await apply_rule(None, TENANT, _rule("Mentor", "skills", "strip_emoji"))

    assert sorted(_values(store, uri, "skills")) == ["design", "python"]


@pytest.mark.asyncio
async def test_strip_emoji_drops_a_pure_emoji_value(store):
    uri, triples = _mentor("m1", "\U0001f3a8\U0001f680")
    await _seed(store, triples)

    await apply_rule(None, TENANT, _rule("Mentor", "skills", "strip_emoji"))

    assert _values(store, uri, "skills") == []


@pytest.mark.asyncio
async def test_strip_emoji_rerun_is_a_no_op(store):
    uri, triples = _mentor("m1", "\U0001f3a8 design")
    await _seed(store, triples)
    rule = _rule("Mentor", "skills", "strip_emoji")

    await apply_rule(None, TENANT, rule)
    before = _props(store, uri)
    summary = await apply_rule(None, TENANT, rule)

    assert _props(store, uri) == before
    assert summary == {"literals_cleaned": 0, "triples_rewritten": 0}


@pytest.mark.asyncio
async def test_strip_emoji_leaves_real_skill_names_alone(store):
    uri, triples = _mentor("m1", "c++")
    await _seed(store, [*triples, (uri, f"{TYPES}Mentor/attrs/skills", "R&D")])

    summary = await apply_rule(None, TENANT, _rule("Mentor", "skills", "strip_emoji"))

    assert sorted(_values(store, uri, "skills")) == ["R&D", "c++"]
    assert summary["literals_cleaned"] == 0


@pytest.mark.asyncio
async def test_strip_emoji_does_not_retype_a_numeric_sibling(store):
    """A typed literal on the same leaf survives a rewrite in its NATIVE type.

    Ingest writes ``"4.6"^^xsd:float`` and the store holds a real float. The
    handler rewrites the WHOLE leaf (a literal delete is predicate-scoped), so
    it must hand the untouched siblings back exactly as it found them —
    stringifying them would silently retype a column the rule never matched.
    """
    uri, triples = _mentor("m1", "\U0001f3a8 design")
    await _seed(
        store,
        [
            *triples,
            (
                uri,
                f"{TYPES}Mentor/attrs/skills",
                "4.6^^http://www.w3.org/2001/XMLSchema#float",
            ),
        ],
    )
    assert 4.6 in _values(store, uri, "skills"), "premise: the store holds a float"

    await apply_rule(None, TENANT, _rule("Mentor", "skills", "strip_emoji"))

    assert sorted(_values(store, uri, "skills"), key=str) == [4.6, "design"]


# --------------------------------------------------------------------------- #
# list_explode — packed literal
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_list_explode_literal_splits_into_atoms(store):
    uri, triples = _mentor("m1", "Python; SQL")
    await _seed(store, triples)

    summary = await apply_rule(
        None,
        TENANT,
        _rule("Mentor", "skills", "list_explode", delimiters=["; "], target="literal"),
    )

    assert sorted(_values(store, uri, "skills")) == ["Python", "SQL"]
    assert summary == {
        "edges_rewritten": 1,
        "atomic_created": 2,
        "orphans_dropped": 0,
    }


@pytest.mark.asyncio
async def test_list_explode_literal_rerun_is_a_no_op(store):
    uri, triples = _mentor("m1", "Python; SQL")
    await _seed(store, triples)
    rule = _rule(
        "Mentor", "skills", "list_explode", delimiters=["; "], target="literal"
    )

    await apply_rule(None, TENANT, rule)
    before = _props(store, uri)
    summary = await apply_rule(None, TENANT, rule)

    assert _props(store, uri) == before
    assert summary["edges_rewritten"] == 0
    assert summary["atomic_created"] == 0


# --------------------------------------------------------------------------- #
# End-to-end through the durable outcome recorder (#452)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_apply_and_record_marks_the_rule_applied(store):
    """The user-visible outcome: ``applied``, not ``failed`` + ``last_error``.

    On ``origin/main`` this same call records
    ``last_error="SparqlClientRetired: …"`` and leaves the graph untouched.
    """
    uri, triples = _mentor("m1", "\U0001f3a8 design")
    await _seed(store, triples)
    rule = _rule("Mentor", "skills", "strip_emoji")
    rule_store = NormalizationRuleStore(None)
    await rule_store.save(TENANT, rule)

    outcome = await apply_and_record(None, TENANT, rule)

    assert outcome.ok, outcome.error
    assert outcome.summary["literals_cleaned"] == 1
    stored = await rule_store.get(TENANT, rule.id)
    assert stored is not None
    assert stored.status == "applied"
    assert stored.last_error in (None, "")
    assert _values(store, uri, "skills") == ["design"]
