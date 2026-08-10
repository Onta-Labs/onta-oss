"""Golden-query validation harness for the Neo4j RDF-semantic model (ADR 0013).

Compares **answer sets** from structured helper plans against frozen gold
rows — never SPARQL↔Cypher string equality.

Run::

    python -m cograph_client.graph.golden_neo4j
    pytest tests/test_golden_rdf_semantics.py -q

Pass criterion: every registered case is PASS under answer comparison rules
(docs/plans/neo4j-golden-queries.md §3).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from cograph_client.graph.assertion_memory import AssertionMemoryStore
from cograph_client.graph.assertion_model import MiniPeopleIds, canonical_literal
from cograph_client.graph.golden_fixture import (
    MiniPeopleFixture,
    build_mini_people,
    expand_symbol,
)
from cograph_client.graph import rdfs_helpers as H

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = (
    _REPO_ROOT / "tests" / "fixtures" / "golden_kg" / "mini_people"
)
DEFAULT_SUITE = DEFAULT_FIXTURE_DIR / "golden_queries.json"


# ---------------------------------------------------------------------------
# Answer comparison (plan §3)
# ---------------------------------------------------------------------------


def normalize_cell(value: Any) -> Any:
    """Canonicalize one cell for equality (literals / numbers / bools)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        # decimal-normalize via string round-trip key
        return float(canonical_literal(value))
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return tuple(normalize_cell(v) for v in value)
    return value


def normalize_row(row: Mapping[str, Any], columns: Sequence[str] | None) -> tuple:
    """Row → hashable sorted tuple of (col, normalized value)."""
    if columns:
        items = [(c, normalize_cell(row.get(c))) for c in columns]
    else:
        items = sorted((k, normalize_cell(v)) for k, v in row.items())
    return tuple(items)


def compare_answers(
    actual: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
    *,
    compare: str = "set",
    columns: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Return (ok, message). Default multiset/set equality of rows."""
    if compare == "ordered":
        a = [normalize_row(r, columns) for r in actual]
        e = [normalize_row(r, columns) for r in expected]
        if a == e:
            return True, "PASS (ordered)"
        return False, _diff_message(a, e, ordered=True)

    # set equality of rows (default). Multiset: use Counter if duplicates matter.
    from collections import Counter

    a_c = Counter(normalize_row(r, columns) for r in actual)
    e_c = Counter(normalize_row(r, columns) for r in expected)
    if a_c == e_c:
        return True, "PASS"
    missing = e_c - a_c
    extra = a_c - e_c
    parts = []
    if missing:
        parts.append(f"missing={list(missing.elements())}")
    if extra:
        parts.append(f"extra={list(extra.elements())}")
    return False, "FAIL " + "; ".join(parts)


def _diff_message(actual: list, expected: list, *, ordered: bool) -> str:
    return f"FAIL ordered={ordered} actual={actual!r} expected={expected!r}"


# ---------------------------------------------------------------------------
# Structured plan executor
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    category: str
    status: str  # PASS | FAIL | ERROR | SKIP
    message: str = ""
    actual: list[dict[str, Any]] = field(default_factory=list)
    expected: list[dict[str, Any]] = field(default_factory=list)


def _resolve_subject(
    store: AssertionMemoryStore,
    ids: MiniPeopleIds,
    structured: Mapping[str, Any],
    *,
    tenant_id: str,
    kg: str,
) -> str:
    if "subject_id" in structured:
        return expand_symbol(structured["subject_id"], ids)
    if "subject_name" in structured:
        name = structured["subject_name"]
        # Prefer fixture name map so isolation cases can resolve Alice under a
        # sibling kg where the entity row was never written.
        eid = ids.entities.get(name)
        if eid is None:
            eid = store.resolve_entity_by_name(tenant_id, kg, name)
        if eid is None:
            # Last resort: name lookup on the primary fixture kg
            eid = store.resolve_entity_by_name(ids.tenant_id, ids.kg, name)
        if eid is None:
            raise KeyError(f"subject_name {name!r} not found")
        return eid
    raise KeyError("structured plan needs subject_id or subject_name")


def execute_structured(
    store: AssertionMemoryStore,
    ids: MiniPeopleIds,
    structured: Mapping[str, Any],
    *,
    tenant_id: str | None = None,
    kg: str | None = None,
) -> list[dict[str, Any]]:
    """Run one helper plan against the assertion store.

    ``structured.helper`` names a rdfs_helpers function or a named composition
    used by the golden suite (see golden_queries.json).
    """
    tid = tenant_id or ids.tenant_id
    kid = kg if kg is not None else ids.kg
    # Allow isolation cases to override scope (resolve subject against fixture
    # ids first; query still uses the overridden kg).
    if structured.get("scope_kg") is not None:
        kid = str(expand_symbol(structured["scope_kg"], ids))
    if structured.get("scope_tenant") is not None:
        tid = str(expand_symbol(structured["scope_tenant"], ids))

    helper = structured["helper"]
    project = structured.get("project")
    columns = structured.get("columns")

    rows: list[dict[str, Any]]

    if helper == "count_entities_of_type":
        rows = H.count_entities_of_type(
            store,
            structured.get("class") or structured.get("class_name") or "Person",
            tenant_id=tid,
            kg=kid,
            include_subclasses=bool(structured.get("include_subclasses", True)),
        )
    elif helper == "entities_of_type":
        rows = H.entities_of_type(
            store,
            structured.get("class") or structured.get("class_name") or "Person",
            tenant_id=tid,
            kg=kid,
            include_subclasses=bool(structured.get("include_subclasses", True)),
        )
        if project == "entity_id":
            rows = [{"entity_id": r["entity_id"]} for r in rows]
    elif helper == "assertions_for_subject":
        subject = _resolve_subject(store, ids, structured, tenant_id=tid, kg=kid)
        rows = H.assertions_for_subject(
            store,
            subject,
            tenant_id=tid,
            kg=kid,
            property_name=structured.get("property") or structured.get("property_name"),
            property_id=(
                expand_symbol(structured["property_id"], ids)
                if "property_id" in structured
                else None
            ),
        )
        if project == "literal_value":
            rows = [
                {"entity_id": r["subject_id"], "value": r["literal_value"]}
                for r in rows
                if r.get("literal_value") is not None
            ]
        elif project == "object_id":
            rows = [
                {
                    "subject_id": r["subject_id"],
                    "object_id": r["object_id"],
                }
                for r in rows
                if r.get("object_id")
            ]
        elif project == "values":
            rows = [{"value": r.get("literal_value")} for r in rows]
        elif project == "provenance":
            rows = [
                {
                    "subject_id": r["subject_id"],
                    "property": r.get("property"),
                    "value": r.get("value"),
                    "source_url": r.get("source_url"),
                    "verified_at": r.get("verified_at"),
                }
                for r in rows
            ]
    elif helper == "asserted_types":
        subject = _resolve_subject(store, ids, structured, tenant_id=tid, kg=kid)
        rows = H.asserted_types(store, subject, tenant_id=tid, kg=kid)
        if project == "type_name":
            rows = [{"type_name": r["type_name"]} for r in rows]
    elif helper == "reverse_object_assertions":
        obj = structured.get("object_id") or structured.get("object_name")
        if structured.get("object_name"):
            obj = store.resolve_entity_by_name(
                tid, kid, structured["object_name"]
            ) or ids.entities.get(structured["object_name"])
        else:
            obj = expand_symbol(obj, ids)
        rows = H.reverse_object_assertions(
            store,
            str(obj),
            tenant_id=tid,
            kg=kid,
            property_name=structured.get("property") or structured.get("property_name"),
        )
        if project == "subject_id":
            rows = [{"subject_id": r["subject_id"]} for r in rows]
    elif helper == "parent_classes":
        rows = H.parent_classes(
            store,
            structured.get("class") or structured.get("class_name"),
            tenant_id=tid,
            kg=kid,
            transitive=bool(structured.get("transitive", True)),
        )
        if project == "type_name":
            rows = [{"type_name": r["type_name"]} for r in rows]
    elif helper == "entities_with_literal_filter":
        rows = H.entities_with_literal_filter(
            store,
            structured.get("class") or "Person",
            structured.get("property") or structured.get("property_name") or "birth_year",
            tenant_id=tid,
            kg=kid,
            op=structured.get("op", ">"),
            value=structured.get("value"),
            include_subclasses=bool(structured.get("include_subclasses", True)),
        )
        if project == "entity_id":
            rows = [{"entity_id": r["entity_id"]} for r in rows]
    elif helper == "fact_provenance":
        # Resolve via subject + property + value match, then project provenance
        subject = _resolve_subject(store, ids, structured, tenant_id=tid, kg=kid)
        prop = structured.get("property") or structured.get("property_name")
        match_value = structured.get("value")
        matches = H.assertions_for_subject(
            store, subject, tenant_id=tid, kg=kid, property_name=prop
        )
        rows = []
        for r in matches:
            if match_value is not None and normalize_cell(r.get("value")) != normalize_cell(
                match_value
            ):
                continue
            rows.append(
                {
                    "subject_id": r["subject_id"],
                    "property": r.get("property"),
                    "value": r.get("value"),
                    "source_url": r.get("source_url"),
                    "verified_at": r.get("verified_at"),
                }
            )
    else:
        raise KeyError(f"unknown helper {helper!r}")

    if columns:
        rows = H.project_rows(rows, columns)
    return rows


# ---------------------------------------------------------------------------
# Suite loader + runner
# ---------------------------------------------------------------------------


def load_suite(path: Path | None = None) -> dict[str, Any]:
    suite_path = path or DEFAULT_SUITE
    data = json.loads(suite_path.read_text(encoding="utf-8"))
    if "queries" not in data:
        raise ValueError(f"suite {suite_path} missing 'queries'")
    return data


def load_gold(
    fixture_dir: Path,
    gold_ref: str,
    ids: MiniPeopleIds,
) -> tuple[list[dict[str, Any]], str, list[str] | None]:
    """Load gold file; expand $entity:… symbols. Returns (rows, compare, columns)."""
    gold_path = fixture_dir / gold_ref
    if not gold_path.is_file():
        # also try answers/ prefix if not already
        alt = fixture_dir / "answers" / gold_ref
        gold_path = alt if alt.is_file() else gold_path
    data = json.loads(gold_path.read_text(encoding="utf-8"))
    rows = expand_symbol(data.get("rows", []), ids)
    compare = data.get("compare", "set")
    columns = data.get("columns")
    return rows, compare, columns


def run_case(
    case: Mapping[str, Any],
    fixture: MiniPeopleFixture,
    fixture_dir: Path,
) -> CaseResult:
    cid = case["id"]
    category = case.get("category", "")
    try:
        actual = execute_structured(
            fixture.store, fixture.ids, case["structured"]
        )
        expected, compare, gold_columns = load_gold(
            fixture_dir, case["gold"], fixture.ids
        )
        columns = case.get("columns") or gold_columns or case["structured"].get(
            "columns"
        )
        ok, msg = compare_answers(
            actual, expected, compare=compare, columns=columns
        )
        return CaseResult(
            id=cid,
            category=category,
            status="PASS" if ok else "FAIL",
            message=msg,
            actual=[dict(r) for r in actual],
            expected=[dict(r) for r in expected],
        )
    except Exception as exc:  # noqa: BLE001 — harness surfaces ERROR
        return CaseResult(
            id=cid,
            category=category,
            status="ERROR",
            message=f"{type(exc).__name__}: {exc}",
        )


def run_suite(
    suite_path: Path | None = None,
    fixture_dir: Path | None = None,
) -> list[CaseResult]:
    suite_path = suite_path or DEFAULT_SUITE
    fixture_dir = fixture_dir or suite_path.parent
    suite = load_suite(suite_path)
    fixture = build_mini_people()
    results = [
        run_case(case, fixture, fixture_dir) for case in suite["queries"]
    ]
    return results


def format_report(results: Sequence[CaseResult]) -> str:
    lines = []
    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        lines.append(f"  [{r.status}] {r.id} ({r.category}) — {r.message}")
        if r.status == "FAIL":
            lines.append(f"         actual={r.actual!r}")
            lines.append(f"         expected={r.expected!r}")
    summary = (
        f"golden: {counts['PASS']} PASS, {counts['FAIL']} FAIL, "
        f"{counts['ERROR']} ERROR, {counts['SKIP']} SKIP "
        f"(total {len(results)})"
    )
    return summary + "\n" + "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Neo4j RDF-semantic golden queries (answer parity)"
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=DEFAULT_SUITE,
        help="Path to golden_queries.json",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=None,
        help="Directory containing answers/ (default: suite parent)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    results = run_suite(args.suite, args.fixture_dir)
    print(format_report(results))
    if any(r.status in ("FAIL", "ERROR") for r in results):
        return 1
    return 0


# Optional stub for future Neptune baseline dual-run (plan §2.1).
class NeptuneBaselineAnswers:
    """File-backed gold answers stand in for a live Neptune dual-run.

    Load expected rows from the same answers/*.json files. A future
    implementation may query Neptune SPARQL and write these files.
    """

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.fixture_dir = fixture_dir or DEFAULT_FIXTURE_DIR

    def load(self, query_id: str, ids: MiniPeopleIds) -> list[dict[str, Any]]:
        path = self.fixture_dir / "answers" / f"{query_id}.json"
        rows, _, _ = load_gold(self.fixture_dir, f"answers/{query_id}.json", ids)
        if not path.is_file():
            # load_gold already tried answers/
            pass
        return rows


if __name__ == "__main__":
    sys.exit(main())
