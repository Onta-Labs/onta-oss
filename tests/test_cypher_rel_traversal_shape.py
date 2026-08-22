"""Relationship-traversal plan shape: never a typed edge named after the leaf.

Regression pin for the /ask silent-wrong-answer shipped with the packaged
example bank (``DEFAULT_BANK_PATH`` → ``infona_client/nlp/data/example_bank.jsonl``).

What happened
-------------
The old bank had **zero** rows carrying a ``cypher`` body, so the Cypher
few-shot pool in :meth:`ExampleBank.retrieve` was empty and the Cypher prompt
carried no examples. The model then followed the system prompt's "prefer
allowlisted semantic helper templates" instruction and emitted
``template: related_entities``, which supersedes its raw Cypher in
``_execute_confined_cypher`` — so the answer was right.

The packaged bank ships 20 Cypher rows. Each is rendered as a bare expanded
body (no ``template`` line), the model imitated that and stopped emitting the
``template`` field, and its free-form relationship Cypher —

    MATCH (ct)-[:lead_sponsor]->(comp:Entity {...})

— was executed for real. ``lead_sponsor`` is a ``:Property`` NAME, not a
relationship type, so it matched nothing and /ask answered "No results found."
with ``query_confidence: high`` on a KG that plainly has the relationship.

Why a lower-case rel type is provably dead
------------------------------------------
Every relationship type in the graph is minted by
:func:`infona_client.graph.facts.sanitize_rel_type`, which upper-cases, and the
structural rels are upper-case literals in ``graph/*.py``. Neo4j rel types are
case-sensitive. So the check needs no ontology and no live store: a rel type
carrying a lower-case letter cannot exist.

These are plan-shape assertions — no LLM is called anywhere in this file.
"""

from __future__ import annotations

import json

import pytest

from infona_client.graph.facts import sanitize_rel_type
from infona_client.graph.store import GraphQueryError
from infona_client.nlp.example_bank_models import DEFAULT_BANK_PATH
from infona_client.nlp.pipeline_cypher_exec import PipelineCypherExecMixin
from infona_client.nlp.pipeline_helpers import (
    _cypher_invented_rel_types,
    _cypher_rel_types,
)

# The exact Cypher production returned for "Which companies sponsor the most
# clinical trials?" on gt-demo/label-compliance after the pin move. Verbatim.
REGRESSION_CYPHER = """
MATCH (ct:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(ctClass:Class) WHERE ctClass.name = 'ClinicalTrial'
MATCH (ct)-[:lead_sponsor]->(comp:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (comp)-[:INSTANCE_OF]->(compClass:Class) WHERE compClass.name = 'Company'
WITH comp, count(DISTINCT ct) AS trial_count
RETURN comp.company_name AS company_name, trial_count
ORDER BY trial_count DESC
""".strip()

# The Assertion shape the same question must plan instead (RELATED_ENTITIES_CYPHER).
ASSERTION_CYPHER = """
MATCH (from_e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(fc:Class {tenant_id: $tenant_id, kg: $kg})
WHERE fc.name IN $from_types
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from_e)
MATCH (a)-[:OBJECT]->(to_e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
RETURN to_e.name AS company_name, count(DISTINCT from_e) AS trial_count
""".strip()


# ---------------------------------------------------------------------------
# The invariant the guard rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaf",
    ["lead_sponsor", "directed_by", "has_genre", "makes", "Author", "phase3"],
)
def test_every_minted_rel_type_is_upper_case(leaf: str) -> None:
    """`sanitize_rel_type` is the ONE minter of rel types and always upper-cases.

    If this ever stops holding, `_cypher_invented_rel_types` must be revisited
    before the graph starts carrying lower-case rel types.
    """
    rel_type = sanitize_rel_type(leaf)
    assert rel_type == rel_type.upper()
    assert not any(ch.islower() for ch in rel_type)


# ---------------------------------------------------------------------------
# Rel-type extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (a)-[:SUBJECT]->(b)", ["SUBJECT"]),
        ("MATCH (a)<-[:OBJECT]-(b)", ["OBJECT"]),
        ("MATCH (a)-[r:HAS_GENRE]->(b)", ["HAS_GENRE"]),
        # Variable-length must not swallow the bounds into the type token.
        ("MATCH (a)-[:SUBCLASS_OF*1..3]->(b)", ["SUBCLASS_OF"]),
        # A property map must not be read as part of the type.
        ("MATCH (a)-[r:LEAD_SPONSOR {tenant_id: $t}]->(b)", ["LEAD_SPONSOR"]),
        # Alternation, both spellings.
        ("MATCH (a)-[:SUBJECT|OBJECT]->(b)", ["SUBJECT", "OBJECT"]),
        ("MATCH (a)-[:SUBJECT|:OBJECT]->(b)", ["SUBJECT", "OBJECT"]),
        # Backticked type.
        ("MATCH (a)-[:`LEAD_SPONSOR`]->(b)", ["LEAD_SPONSOR"]),
        # An untyped rel is not a type claim at all.
        ("MATCH (a)-[r]->(b)", []),
        ("MATCH (a)-->(b)", []),
    ],
)
def test_rel_type_extraction(cypher: str, expected: list[str]) -> None:
    assert _cypher_rel_types(cypher) == expected


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_regression_cypher_is_flagged() -> None:
    """The exact query that shipped the silent wrong answer must be caught."""
    assert _cypher_invented_rel_types(REGRESSION_CYPHER) == ["lead_sponsor"]


def test_assertion_shape_is_not_flagged() -> None:
    """The shape relationship questions MUST plan stays clean."""
    assert _cypher_invented_rel_types(ASSERTION_CYPHER) == []


@pytest.mark.parametrize(
    "cypher",
    [
        # Every structural rel type the graph layer actually creates.
        "MATCH (e)-[:INSTANCE_OF]->(c)",
        "MATCH (a)-[:SUBJECT]->(e)",
        "MATCH (a)-[:PREDICATE]->(p)",
        "MATCH (a)-[:OBJECT]->(o)",
        "MATCH (a)-[:OBJECT_CLASS]->(c)",
        "MATCH (c)-[:SUBCLASS_OF*]->(p)",
        "MATCH (p)-[:SUBPROPERTY_OF]->(q)",
        "MATCH (p)-[:RANGE_TYPE]->(c)",
        "MATCH (t)-[:DECLARES]->(a)",
        "MATCH (n)-[:ABOUT]->(e)",
        "MATCH (n)-[:HAS_CITATION]->(c)",
        # A correctly-cased dual-written shortcut is legitimate.
        "MATCH (ct)-[:LEAD_SPONSOR]->(comp)",
        # Lower-case elsewhere in the query is irrelevant — only rel types count.
        "MATCH (e:Entity) WHERE e.company_name = $v RETURN e.company_name",
    ],
)
def test_valid_shapes_are_not_flagged(cypher: str) -> None:
    assert _cypher_invented_rel_types(cypher) == []


@pytest.mark.parametrize(
    ("cypher", "flagged"),
    [
        ("MATCH (a)-[:lead_sponsor]->(b)", ["lead_sponsor"]),
        ("MATCH (a)-[:Lead_Sponsor]->(b)", ["Lead_Sponsor"]),
        ("MATCH (a)-[r:directed_by]->(b)", ["directed_by"]),
        # Mixed: report only the impossible one.
        (
            "MATCH (a)-[:INSTANCE_OF]->(c) MATCH (a)-[:lead_sponsor]->(b)",
            ["lead_sponsor"],
        ),
        # De-duplicated, first-seen order.
        (
            "MATCH (a)-[:lead_sponsor]->(b) MATCH (c)-[:lead_sponsor]->(d)",
            ["lead_sponsor"],
        ),
    ],
)
def test_invented_rel_types_flagged(cypher: str, flagged: list[str]) -> None:
    assert _cypher_invented_rel_types(cypher) == flagged


# ---------------------------------------------------------------------------
# Wiring: the guard must fire where the raw Cypher would actually execute
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Minimal GraphSession stand-in that records what it was asked to run."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, dict]] = []
        self.templates: list[tuple[str, dict]] = []

    async def execute_read(self, cypher: str, params: dict) -> list:
        self.reads.append((cypher, params))
        return []

    async def execute_template(self, name: str, params: dict) -> list:
        self.templates.append((name, params))
        return [{"from_id": "NCT1", "to_id": "Roche"}]


class _Pipeline(PipelineCypherExecMixin):
    """Bare mixin host — the exec path under test needs no pipeline state."""


@pytest.mark.asyncio
async def test_free_form_invented_edge_never_reaches_the_store() -> None:
    """No `template` to rescue it → raise, do NOT execute and answer zero rows.

    This is the regression itself: before the guard this call ran the query,
    got 0 records, and /ask rendered "No results found."
    """
    session = _RecordingSession()
    gen = {"cypher": REGRESSION_CYPHER, "explanation": "..."}

    with pytest.raises(GraphQueryError) as excinfo:
        await _Pipeline()._execute_confined_cypher(
            session, gen, REGRESSION_CYPHER, {"tenant_id": "t", "kg": "k"}
        )

    assert session.reads == [], "invented-edge Cypher must not be executed"
    detail = str(excinfo.value)
    assert "lead_sponsor" in detail
    # The message is the retry feedback, so it must name the shape to use.
    assert ":Assertion" in detail
    assert "related_entities" in detail


@pytest.mark.asyncio
async def test_assertion_shape_still_executes() -> None:
    session = _RecordingSession()
    gen = {"cypher": ASSERTION_CYPHER}

    records, path = await _Pipeline()._execute_confined_cypher(
        session, gen, ASSERTION_CYPHER, {"tenant_id": "t", "kg": "k"}
    )

    assert path == "execute_read"
    assert len(session.reads) == 1
    assert records == []


@pytest.mark.asyncio
async def test_valid_template_still_supersedes_invented_edge() -> None:
    """The rescue path that kept production correct must keep working.

    A model that invents the edge but ALSO sets a usable `template` answered
    correctly before this change — the template supersedes its raw Cypher. The
    guard sits after that branch precisely so this keeps working.
    """
    session = _RecordingSession()
    gen = {
        "cypher": REGRESSION_CYPHER,
        "template": "related_entities",
        "params": {
            "from_types": ["ClinicalTrial"],
            "to_types": ["Company"],
            "rel_attr": "lead_sponsor",
        },
    }

    records, path = await _Pipeline()._execute_confined_cypher(
        session, gen, REGRESSION_CYPHER, {"tenant_id": "t", "kg": "k"}
    )

    assert path == "template:related_entities"
    assert session.reads == [], "template must supersede the raw Cypher"
    assert records


# ---------------------------------------------------------------------------
# The shipped few-shot bank must never teach the dead shape
# ---------------------------------------------------------------------------


def _bank_cypher_rows() -> list[dict]:
    if not DEFAULT_BANK_PATH.exists():  # pragma: no cover - packaging guard
        pytest.skip(f"packaged example bank missing at {DEFAULT_BANK_PATH}")
    rows = [
        json.loads(line)
        for line in DEFAULT_BANK_PATH.read_text().splitlines()
        if line.strip()
    ]
    return [r for r in rows if (r.get("cypher") or "").strip()]


def test_packaged_bank_has_cypher_few_shots() -> None:
    """Production had ZERO Cypher few-shots before the packaged bank.

    Pinned so a future bank regeneration cannot silently empty the Cypher pool
    again (which is what makes the shape depend on luck rather than teaching).
    """
    assert len(_bank_cypher_rows()) >= 20


def test_no_bank_cypher_example_teaches_an_invented_edge() -> None:
    """Every shipped Cypher few-shot must survive the same guard /ask applies."""
    offenders = {
        row["question"]: _cypher_invented_rel_types(row["cypher"])
        for row in _bank_cypher_rows()
        if _cypher_invented_rel_types(row["cypher"])
    }
    assert offenders == {}


def test_bank_relationship_examples_use_the_assertion_shape() -> None:
    """The rows that traverse a relationship must show SUBJECT/PREDICATE/OBJECT.

    Without this a regenerated bank could keep 20 Cypher rows, drop every
    relationship traversal among them, and leave the model with nothing to copy
    for exactly the question class that broke.
    """
    rel_rows = [
        row
        for row in _bank_cypher_rows()
        if "-[:OBJECT]->" in row["cypher"] or ":Assertion" in row["cypher"]
    ]
    assert len(rel_rows) >= 10

    traversals = [row for row in rel_rows if "-[:OBJECT]->" in row["cypher"]]
    assert len(traversals) >= 8
    for row in traversals:
        cypher = row["cypher"]
        assert "-[:SUBJECT]->" in cypher, row["question"]
        assert "-[:PREDICATE]->" in cypher, row["question"]
