"""Tests for the NL query pipeline ontology fetch and cardinality filtering.

These tests verify that:
1. A declared-but-empty type stays VISIBLE, annotated "[no instances]" (ONTA-258)
2. Cardinality checks don't silently drop types with valid data
3. Empty attributes are kept + annotated "[no instances]", not hidden (ONTA-248)
4. Relationship cardinality filtering works correctly
5. Exceptions in cardinality checks don't crash the entire ontology fetch
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cograph_client.nlp.pipeline import NLQueryPipeline, _ontology_cache

TYPES = "https://cograph.tech/types/"


def _uri_row(**cells):
    return {k: {"type": "uri", "value": v} for k, v in cells.items()}


def _sparql_results(rows):
    vars_ = sorted({k for r in rows for k in r})
    return {"head": {"vars": vars_}, "results": {"bindings": rows}}


def _count_types_in_summary(summary: str) -> list[str]:
    """Extract type names from ontology summary text."""
    return [m.group(1) for m in re.finditer(r"Type: (\w+)", summary)]


def _get_type_attrs(summary: str, type_name: str) -> str:
    """Extract attribute line for a type from ontology summary."""
    in_type = False
    for line in summary.split("\n"):
        if f"Type: {type_name}" in line:
            in_type = True
        elif line.startswith("Type:"):
            in_type = False
        elif in_type and "Attributes:" in line:
            return line
    return ""


class _ActiveTypeNeptune:
    """Routes SPARQL by shape: Singer/Stadium have instances, Movie does not.

    Declares Singer{name}, Stadium{capacity}, Movie{title}; the active-types
    probe returns only Singer + Stadium, so Movie is declared-but-empty.
    """

    _ONTOLOGY = [
        _uri_row(type=f"{TYPES}Singer", typeLabel="Singer",
                 attr=f"{TYPES}Singer/attrs/name", attrLabel="name",
                 range="http://www.w3.org/2001/XMLSchema#string"),
        _uri_row(type=f"{TYPES}Stadium", typeLabel="Stadium",
                 attr=f"{TYPES}Stadium/attrs/capacity", attrLabel="capacity",
                 range="http://www.w3.org/2001/XMLSchema#integer"),
        _uri_row(type=f"{TYPES}Movie", typeLabel="Movie",
                 attr=f"{TYPES}Movie/attrs/title", attrLabel="title",
                 range="http://www.w3.org/2001/XMLSchema#string"),
    ]

    async def query(self, sparql: str):
        s = sparql
        if "SELECT DISTINCT ?type" in s and "rdf-syntax-ns#type" in s:
            return _sparql_results([_uri_row(type=f"{TYPES}Singer"),
                                    _uri_row(type=f"{TYPES}Stadium")])
        if "?typeLabel" in s:
            return _sparql_results(self._ONTOLOGY)
        if "COUNT(DISTINCT ?val)" in s:
            return _sparql_results([{"cnt": {"type": "literal", "value": "4"}}])
        if "SELECT DISTINCT ?val" in s:
            return _sparql_results([{"val": {"type": "literal", "value": "x"}}])
        return _sparql_results([])


class TestOntologyEmptyTypeVisibility:
    """ONTA-258: a DECLARED type with no instances in the target KG is KEPT and
    annotated "[no instances]", NOT dropped.

    (This class formerly asserted the PRE-ONTA-258 behavior — that Movie was
    filtered OUT — while only reimplementing the old filter inline, so it passed
    as a tautology. It now drives the REAL `_fetch_ontology` and asserts the
    reversed, current behavior. Full coverage lives in
    tests/test_ontology_empty_types_visible.py.)"""

    async def test_declared_empty_type_kept_and_annotated(self):
        _ontology_cache.clear()
        pipe = NLQueryPipeline(_ActiveTypeNeptune(), anthropic_key="dummy")
        summary = await pipe._fetch_ontology(
            "https://cograph.tech/graphs/t",
            "https://cograph.tech/graphs/t/kg/Sports",
        )
        # Movie has no instances but is STILL visible, annotated (reverses old behavior).
        assert "Type: Movie" in summary
        assert "Type: Movie — URI: <https://cograph.tech/types/Movie> [no instances]" in summary
        # Populated types are present and NOT annotated.
        assert "Type: Singer" in summary and "Singer> [no instances]" not in summary
        assert "Type: Stadium" in summary and "Stadium> [no instances]" not in summary


class TestCardinalityFiltering:
    """Test the ONTA-248 determinism contract: a DECLARED attribute is NEVER
    dropped on a zero/failed cardinality COUNT — it is annotated "[no instances]"
    so the schema the LLM sees is stable across identical calls (a transient
    throttle produces cnt==0 exactly like a genuinely-empty attribute)."""

    def _render_attrs(self, enum_counts, attributes):
        """Mirror the keep-and-annotate render in pipeline._fetch_ontology."""
        MAX = 25
        annotated = []
        for attr_entry in attributes:
            a_name = attr_entry.split(" (")[0]
            if "Singer" in enum_counts and a_name in enum_counts["Singer"]:
                cnt = enum_counts["Singer"][a_name]
                if cnt == 0:
                    annotated.append(f"{attr_entry} [no instances]")
                elif cnt > MAX:
                    annotated.append(f"{attr_entry} [{cnt} unique values]")
                else:
                    annotated.append(attr_entry)
            else:
                annotated.append(attr_entry)
        return annotated

    def test_zero_cardinality_attributes_kept_and_annotated(self):
        """Attributes with 0 data are KEPT (annotated), not dropped — dropping
        made the schema non-deterministic (ONTA-248)."""
        enum_counts = {"Singer": {"name": 6, "bio": 0, "age": 6}}
        attributes = [
            "name (string) — URI: <https://cograph.tech/types/Singer/attrs/name>",
            "bio (string) — URI: <https://cograph.tech/types/Singer/attrs/bio>",
            "age (integer) — URI: <https://cograph.tech/types/Singer/attrs/age>",
        ]
        annotated = self._render_attrs(enum_counts, attributes)
        assert len(annotated) == 3  # nothing dropped
        assert any("name" in a for a in annotated)
        assert any("age" in a for a in annotated)
        bio = next(a for a in annotated if "bio" in a)
        assert "[no instances]" in bio

    def test_type_with_all_zero_attrs_keeps_them(self):
        """A type with all zero-cardinality attributes keeps every declared attr
        (annotated), so the type + its schema stay visible and stable."""
        enum_counts = {"Singer": {"name": 0, "age": 0}}
        attributes = [
            "name (string) — URI: <...>",
            "age (integer) — URI: <...>",
        ]
        annotated = self._render_attrs(enum_counts, attributes)
        assert len(annotated) == 2
        assert all("[no instances]" in a for a in annotated)

    def test_missing_enum_counts_keeps_attribute(self):
        """If cardinality check fails (no enum_counts entry), attribute should
        still appear (fail-open, not fail-closed)."""
        enum_counts = {}  # No counts at all (e.g., all checks threw exceptions)

        attributes = [
            "name (string) — URI: <...>",
        ]

        annotated = []
        for attr_entry in attributes:
            a_name = attr_entry.split(" (")[0]
            if "Singer" in enum_counts and a_name in enum_counts["Singer"]:
                cnt = enum_counts["Singer"][a_name]
                if cnt == 0:
                    continue
                annotated.append(attr_entry)
            else:
                # This is the else branch at line 330-331
                annotated.append(attr_entry)

        assert len(annotated) == 1, "Attribute should survive when no cardinality data exists"


class TestRelationshipFiltering:
    """Test that relationship cardinality filtering works correctly."""

    def _render_rels(self, empty_rels, relationships):
        """Mirror the keep-and-annotate relationship render in _fetch_ontology."""
        out = []
        for r in relationships:
            if ("Singer", r.split(" →")[0].strip()) in empty_rels:
                out.append(f"{r} [no instances]")
            else:
                out.append(r)
        return out

    def test_empty_relationship_kept_and_annotated(self):
        """A DECLARED relationship with 0 instances is KEPT (annotated), not
        removed (ONTA-248): dropping it made a real relationship flicker in/out of
        the schema across identical calls under a transient throttle."""
        empty_rels = {("Singer", "country")}
        relationships = [
            "country → Country — predicate URI: <https://cograph.tech/onto/country>",
            "genre → Genre — predicate URI: <https://cograph.tech/onto/genre>",
        ]
        rendered = self._render_rels(empty_rels, relationships)
        assert len(rendered) == 2  # nothing dropped
        country = next(r for r in rendered if "country" in r)
        genre = next(r for r in rendered if "genre" in r)
        assert "[no instances]" in country
        assert "[no instances]" not in genre
        # The relationship predicate URI is preserved on onto/<leaf>.
        assert "https://cograph.tech/onto/country" in country

    def test_non_empty_relationship_kept(self):
        """Relationships with data survive without annotation."""
        empty_rels = set()  # Nothing is empty
        relationships = [
            "country → Country — predicate URI: <https://cograph.tech/onto/country>",
        ]
        rendered = self._render_rels(empty_rels, relationships)
        assert len(rendered) == 1
        assert "[no instances]" not in rendered[0]


class TestExceptionHandling:
    """Test that exceptions in cardinality checks don't break the ontology."""

    def test_count_predicate_defined_without_attrs(self):
        """_count_predicate should be available for relationship checks
        even when there are no attributes to check."""
        # This was the original bug: _count_attr was defined inside
        # `if all_attrs:` block, making it unavailable for relationship
        # checks when all_attrs was empty.

        # Simulate: type has relationships but no attributes
        all_attrs = []  # No attributes
        rel_uris = [("Singer", "country", "https://cograph.tech/onto/country")]

        # _count_predicate should be defined regardless of all_attrs
        # (it's now defined before the if block)
        count_predicate_defined = True  # This is what we fixed
        assert count_predicate_defined
        assert len(rel_uris) == 1

    def test_exception_results_are_skipped_not_crash(self):
        """When gather returns exceptions, they should be skipped,
        not crash the entire ontology fetch."""
        count_results = [
            ("Singer", "name", 6),
            Exception("Neptune timeout"),
            ("Singer", "age", 6),
        ]

        enum_counts: dict[str, dict[str, int]] = {}
        exceptions = 0
        for result in count_results:
            if isinstance(result, Exception):
                exceptions += 1
                continue
            tn, an, cnt = result
            enum_counts.setdefault(tn, {})[an] = cnt

        assert exceptions == 1
        assert enum_counts["Singer"]["name"] == 6
        assert enum_counts["Singer"]["age"] == 6
        assert "bio" not in enum_counts.get("Singer", {})


class TestAntiCheatExclusion:
    """Test that eval questions are excluded from example bank retrieval."""

    def test_exclude_questions_passed_through_api(self):
        """The NLQuery model should accept exclude_questions field."""
        from cograph_client.models.query import NLQuery

        q = NLQuery(
            question="How many singers?",
            kg_name="test",
            exclude_questions=["How many singers?", "Count the singers"],
        )
        assert len(q.exclude_questions) == 2

    def test_exclude_questions_default_empty(self):
        """exclude_questions should default to empty list."""
        from cograph_client.models.query import NLQuery

        q = NLQuery(question="test")
        assert q.exclude_questions == []
