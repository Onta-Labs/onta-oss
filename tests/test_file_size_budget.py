"""File-size budget ratchet (modularity / contributor contract).

Soft cap (docs / AGENTS.md): **500** lines.
Hard cap for **new** ``infona_client`` / ``packages`` / ``tests`` files: **550**.

Existing files already over 550 on ``origin/main`` (this slice) are
**allowlisted at their pinned count**. They must not grow. A ``+20`` slack
absorbs newline-only churn (a file that gained a trailing newline plus a
few editor-inserted blanks should not fail CI). A real extract that drops
more than the slack must **lower the pin** so the file cannot grow back.

Deny-by-default
---------------
``OVERSIZE_ALLOWLIST`` is a snapshot, not a guest list. A **new** file that
is not in the map fails if it exceeds the hard cap. Do not add a pin for a
file you just created — extract instead. Pins may be lowered or removed;
raising one needs a one-line PR justification.

This guard is the first shippable slice of the modularity cleanup. It does
**not** fail the production/test files that already exceed 550 on day one;
it only stops them (and new files) from getting worse.

Scanned (skip ``node_modules``, ``dist``, ``.venv``, ``__pycache__``, ``*.d.ts``):

* ``infona_client/**/*.py``
* ``packages/**/*.ts``
* ``tests/**/*.py``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SOFT_CAP = 500
NEW_FILE_HARD_CAP = 550
ALLOWLIST_GROWTH_SLACK = 20

SKIP_DIR_NAMES = frozenset({"node_modules", "dist", ".venv", "__pycache__"})
SCAN_SPECS: tuple[tuple[str, str], ...] = (
    ("infona_client", ".py"),
    ("packages", ".ts"),
    ("tests", ".py"),
)

# Snapshot of files already > NEW_FILE_HARD_CAP on origin/main when this
# ratchet landed (regenerated after #387–#393 extracts + #390 web-ingest
# descope). Do not raise a number. Lower or delete an entry after an extract.
OVERSIZE_ALLOWLIST: dict[str, int] = {
    "infona_client/agent/capabilities/ontology_cap.py": 588,
    "infona_client/agent/planner.py": 1463,
    "infona_client/api/routes/actions.py": 713,
    "infona_client/api/routes/explore_schema.py": 554,
    "infona_client/api/routes/grep.py": 649,
    "infona_client/api/routes/ingest.py": 592,
    "infona_client/api/routes/knowledge_graphs.py": 1160,
    "infona_client/api/routes/lambda_functions.py": 709,
    "infona_client/api/routes/ontology.py": 1462,
    "infona_client/api/routes/workspace_invites.py": 726,
    "infona_client/api_registry/enrichment.py": 556,
    "infona_client/api_registry/spec.py": 898,
    "infona_client/auth/workspace_store.py": 1067,
    "infona_client/enrichment/executor.py": 2811,
    "infona_client/enrichment/models.py": 596,
    "infona_client/eval.py": 1857,
    "infona_client/graph/explore_store.py": 975,
    "infona_client/graph/global_ontology.py": 877,
    "infona_client/graph/neo4j_store.py": 1181,
    "infona_client/graph/ontology_base_pin.py": 887,
    "infona_client/graph/ontology_catalog.py": 1025,
    "infona_client/graph/ontology_queries.py": 1312,
    "infona_client/graph/ontology_snapshots.py": 1421,
    "infona_client/graph/pg_ops.py": 805,
    "infona_client/graph/provenance.py": 1171,
    "infona_client/graph/queries.py": 798,
    "infona_client/graph/rdfs_helpers.py": 1095,
    "infona_client/graph/schema_bootstrap.py": 873,
    "infona_client/graph/sparql_scope.py": 648,
    "infona_client/models/ontology.py": 960,
    "infona_client/nlp/cypher_example_seeds.py": 761,
    "infona_client/nlp/dim_registry.py": 1059,
    "infona_client/nlp/example_bank.py": 1250,
    "infona_client/nlp/numeric_attr_resolve.py": 872,
    "infona_client/nlp/numeric_plan_grounding.py": 793,
    "infona_client/nlp/ontology_embeddings.py": 900,
    "infona_client/nlp/ontology_mention_index.py": 972,
    "infona_client/nlp/ontology_subgraph_match.py": 1623,
    "infona_client/nlp/query_constraint_coverage.py": 1315,
    "infona_client/nlp/query_intent.py": 607,
    "infona_client/nlp/schema_valid_cypher.py": 859,
    "infona_client/normalization/execute.py": 1069,
    "infona_client/normalization/inference.py": 598,
    "infona_client/pipeline/discovery_quality.py": 657,
    "infona_client/pipeline/mutations.py": 1178,
    "infona_client/qc/boundary.py": 689,
    "infona_client/qc/tier3_grade.py": 1118,
    "infona_client/research/harness.py": 706,
    "infona_client/research/types.py": 613,
    "infona_client/resolver/attribute_resolver.py": 609,
    "infona_client/resolver/governance.py": 803,
    "infona_client/resolver/models.py": 1179,
    "infona_client/resolver/ontology_resolver.py": 710,
    "infona_client/semantic/postgres.py": 1057,
    "infona_client/semantic/reconciler.py": 1484,
    "packages/cli/src/cli.ts": 1578,
    "packages/cli/src/connect.ts": 630,
    "packages/cli/src/shell.ts": 1566,
    "packages/cli/test/connectWizard.test.ts": 688,
    "packages/cli/test/raw.test.ts": 824,
    "packages/mcp/src/index.ts": 1734,
    "tests/test_agent.py": 3712,
    "tests/test_agent_conversation.py": 576,
    "tests/test_api_registry_enrichment.py": 1015,
    "tests/test_api_registry_executor.py": 775,
    "tests/test_ask_cypher_pipeline.py": 785,
    "tests/test_cross_tenant_isolation_suite.py": 1780,
    "tests/test_csv_resolver.py": 2433,
    "tests/test_dim_bind_coverage.py": 556,
    "tests/test_dim_registry.py": 599,
    "tests/test_discovery_anti_fabrication.py": 626,
    "tests/test_discovery_quality_mechanisms.py": 867,
    "tests/test_drift_integration.py": 710,
    "tests/test_enrichment.py": 3968,
    "tests/test_enrichment_provenance_convergence.py": 580,
    "tests/test_generated_sparql_scoping.py": 1008,
    "tests/test_global_ontology_browser.py": 1446,
    "tests/test_governance.py": 615,
    "tests/test_graph_store.py": 763,
    "tests/test_iri_segment_validation.py": 640,
    "tests/test_jobs_actions.py": 982,
    "tests/test_kg_coverage_caveat.py": 722,
    "tests/test_kg_scoped_ontology_retrieval.py": 786,
    "tests/test_kg_writer.py": 710,
    "tests/test_layered_reads.py": 859,
    "tests/test_llm_router.py": 592,
    "tests/test_mapping_governance.py": 553,
    "tests/test_multityping_hospitality.py": 588,
    "tests/test_normalization.py": 1823,
    "tests/test_numeric_price_grounding.py": 852,
    "tests/test_ontology_api.py": 668,
    "tests/test_ontology_compat.py": 699,
    "tests/test_ontology_semantic_resolve.py": 697,
    "tests/test_ontology_subgraph_grounding.py": 1145,
    "tests/test_plan_store.py": 720,
    "tests/test_query_constraint_coverage.py": 651,
    "tests/test_query_tenant_scoping.py": 722,
    "tests/test_rails_graph_store_write.py": 605,
    "tests/test_resolver_relationships.py": 651,
    "tests/test_schedules.py": 606,
    "tests/test_semantic_postgres.py": 1282,
    "tests/test_semantic_reconciler.py": 1468,
    "tests/test_semantic_registry.py": 692,
    "tests/test_soft_focus_type_floor.py": 611,
    "tests/test_spatiotemporal.py": 714,
    "tests/test_stage_trace.py": 681,
    "tests/test_type_skills.py": 682,
    "tests/test_web_research_hardening.py": 1066,
    "tests/test_workspace_invites.py": 879,
    "tests/test_write_capability_convergence.py": 1163,
    "tests/test_write_path_convergence.py": 602,
}


def line_count(path: Path) -> int:
    """Physical source lines (newline count; last line counted if no trailing NL).

    Matches ``wc -l`` for newline-terminated files, which every file on
    ``main`` is today.
    """
    data = path.read_bytes()
    if not data:
        return 0
    n = data.count(b"\n")
    if not data.endswith(b"\n"):
        n += 1
    return n


def iter_scanned_files(root: Path):
    """Yield ``(posix_relpath, path)`` under the scanned trees."""
    for rel_root, suffix in SCAN_SPECS:
        base = root / rel_root
        if not base.is_dir():
            continue
        for path in base.rglob(f"*{suffix}"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.name.endswith(".d.ts"):
                continue
            yield path.relative_to(root).as_posix(), path


@dataclass(frozen=True)
class Violation:
    relpath: str
    lines: int
    reason: str

    def __str__(self) -> str:
        return f"{self.relpath}: {self.lines} lines — {self.reason}"


def collect_violations(
    root: Path,
    allowlist: dict[str, int] | None = None,
    *,
    hard_cap: int = NEW_FILE_HARD_CAP,
    slack: int = ALLOWLIST_GROWTH_SLACK,
) -> list[Violation]:
    """Return every scanned file that violates the budget.

    Used by the repo-level test *and* by planted-path self-tests (pass a
    throwaway ``root`` / ``allowlist``).
    """
    pins = OVERSIZE_ALLOWLIST if allowlist is None else allowlist
    found: set[str] = set()
    out: list[Violation] = []
    for rel, path in iter_scanned_files(root):
        found.add(rel)
        n = line_count(path)
        if rel in pins:
            pin = pins[rel]
            if n > pin + slack:
                out.append(
                    Violation(
                        rel,
                        n,
                        f"allowlisted at {pin}; grew past pin+{slack} slack "
                        f"— extract a seam instead of adding to this file",
                    )
                )
            elif n < pin - slack:
                out.append(
                    Violation(
                        rel,
                        n,
                        f"allowlisted at {pin}; now {n} (shrunk past -{slack} "
                        f"slack) — lower or drop the pin so it cannot grow back",
                    )
                )
        elif n > hard_cap:
            out.append(
                Violation(
                    rel,
                    n,
                    f"new/unpinned file over hard cap {hard_cap} — split it; "
                    f"do not add it to OVERSIZE_ALLOWLIST",
                )
            )
    for rel, pin in pins.items():
        if rel not in found:
            out.append(
                Violation(
                    rel,
                    0,
                    f"allowlist pin {pin} but file is gone — remove the entry",
                )
            )
    out.sort(key=lambda v: v.relpath)
    return out


def test_allowlist_is_deny_by_default():
    """The allowlist is a snapshot of pre-existing giants, not an exemption.

    New files are rejected by ``collect_violations`` when they exceed the
    hard cap *without* being listed. Adding a pin for a file you just
    created is the thing this test exists to make painful.
    """
    assert NEW_FILE_HARD_CAP == 550
    assert SOFT_CAP == 500
    assert ALLOWLIST_GROWTH_SLACK == 20
    assert SOFT_CAP < NEW_FILE_HARD_CAP
    # Sanity: we actually pinned remaining mega-files, not an empty map.
    # Extracted facades (#387–#392) and deleted web_ingest_cap (#390) stay off.
    assert "infona_client/enrichment/executor.py" in OVERSIZE_ALLOWLIST
    assert "infona_client/agent/capabilities/enrich_cap.py" not in OVERSIZE_ALLOWLIST
    assert "tests/test_enrichment.py" in OVERSIZE_ALLOWLIST
    assert "infona_client/agent/capabilities/web_ingest_cap.py" not in OVERSIZE_ALLOWLIST
    assert "infona_client/resolver/schema_resolver.py" not in OVERSIZE_ALLOWLIST
    assert "infona_client/nlp/pipeline.py" not in OVERSIZE_ALLOWLIST
    assert all(n > NEW_FILE_HARD_CAP for n in OVERSIZE_ALLOWLIST.values())


def test_planted_new_file_over_hard_cap_fails(tmp_path: Path):
    src = tmp_path / "infona_client"
    src.mkdir()
    (src / "brand_new.py").write_text("x\n" * (NEW_FILE_HARD_CAP + 1))
    hits = collect_violations(tmp_path, allowlist={})
    assert len(hits) == 1
    assert hits[0].relpath == "infona_client/brand_new.py"
    assert hits[0].lines == NEW_FILE_HARD_CAP + 1
    assert "hard cap" in hits[0].reason


def test_planted_new_file_at_hard_cap_passes(tmp_path: Path):
    src = tmp_path / "infona_client"
    src.mkdir()
    (src / "ok.py").write_text("x\n" * NEW_FILE_HARD_CAP)
    assert collect_violations(tmp_path, allowlist={}) == []


def test_planted_allowlisted_growth_beyond_slack_fails(tmp_path: Path):
    src = tmp_path / "infona_client"
    src.mkdir()
    pin = 800
    (src / "giant.py").write_text("x\n" * (pin + ALLOWLIST_GROWTH_SLACK + 1))
    hits = collect_violations(tmp_path, allowlist={"infona_client/giant.py": pin})
    assert len(hits) == 1
    assert "grew past" in hits[0].reason


def test_planted_allowlisted_within_slack_passes(tmp_path: Path):
    src = tmp_path / "infona_client"
    src.mkdir()
    pin = 800
    (src / "giant.py").write_text("x\n" * (pin + ALLOWLIST_GROWTH_SLACK))
    assert (
        collect_violations(tmp_path, allowlist={"infona_client/giant.py": pin})
        == []
    )


def test_planted_allowlisted_shrink_beyond_slack_requires_pin_drop(tmp_path: Path):
    src = tmp_path / "infona_client"
    src.mkdir()
    pin = 800
    (src / "giant.py").write_text("x\n" * (pin - ALLOWLIST_GROWTH_SLACK - 1))
    hits = collect_violations(tmp_path, allowlist={"infona_client/giant.py": pin})
    assert len(hits) == 1
    assert "lower or drop the pin" in hits[0].reason


def test_planted_missing_allowlist_entry_fails(tmp_path: Path):
    (tmp_path / "infona_client").mkdir()
    hits = collect_violations(
        tmp_path, allowlist={"infona_client/deleted.py": 900}
    )
    assert len(hits) == 1
    assert hits[0].relpath == "infona_client/deleted.py"
    assert "gone" in hits[0].reason


def test_skips_dist_venv_node_modules_and_d_ts(tmp_path: Path):
    """Excluded trees must not trip the hard cap (deny-by-default still holds)."""
    for rel in (
        "packages/cli/dist/huge.ts",
        "packages/cli/node_modules/pkg/huge.ts",
        "infona_client/.venv/lib/huge.py",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n" * (NEW_FILE_HARD_CAP + 50))
    dts = tmp_path / "packages/cli/src/huge.d.ts"
    dts.parent.mkdir(parents=True, exist_ok=True)
    dts.write_text("x\n" * (NEW_FILE_HARD_CAP + 50))
    assert collect_violations(tmp_path, allowlist={}) == []


def test_this_guard_stays_under_the_hard_cap():
    n = line_count(Path(__file__))
    assert n <= NEW_FILE_HARD_CAP, (
        f"{Path(__file__).name} is {n} lines; split the allowlist out "
        f"before growing this file past {NEW_FILE_HARD_CAP}"
    )


def test_repo_passes_today():
    """The committed tree must pass. Existing giants are pinned, not failed."""
    hits = collect_violations(REPO)
    assert hits == [], "\n".join(str(h) for h in hits)
