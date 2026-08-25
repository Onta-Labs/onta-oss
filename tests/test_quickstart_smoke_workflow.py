"""ONTA-287: the quickstart smoke workflow is the README guard."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WF = REPO / ".github" / "workflows" / "oss-quickstart-smoke.yml"
SCRIPT = REPO / "scripts" / "ci_quickstart_smoke.sh"
FRAG = REPO / "docs" / "_fragments" / "ONTA-287.md"
GOLDEN = REPO / "examples" / "suppliers-messy.er-rebuild.txt"


def test_workflow_is_clean_room_and_gates_live_key() -> None:
    text = WF.read_text(encoding="utf-8")
    assert WF.is_file()
    assert "actions/cache" not in text
    assert "cache-from" not in text
    assert "cache: npm" not in text
    assert "cache: 'npm'" not in text
    assert "setup-buildx" not in text
    assert '-m "not integration"' in text
    assert "github.repository == 'infona-ai/infona-oss'" in text
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "secrets.OPENROUTER_API_KEY" in text
    mocked, _, live = text.partition("name: Live-key")
    assert "secrets.OPENROUTER_API_KEY" not in mocked
    assert "SMOKE_MODE: mocked" in mocked
    assert "oss_up.sh" in live
    assert "google/gemini-2.5-flash" in live
    live_if = next(
        line for line in live.splitlines() if line.lstrip().startswith("if:")
    )
    assert "pull_request" not in live_if
    assert "workflow_dispatch" in live_if
    assert "refs/heads/main" in live_if
    assert "secrets." not in live_if
    assert "if: env.OPENROUTER_API_KEY != ''" in live
    assert "exit 1" not in live
    assert "live ingest+ask skipped" in live


def test_script_and_fragment_guard_readme_claims() -> None:
    assert SCRIPT.is_file()
    script = SCRIPT.read_text(encoding="utf-8")
    assert "FLAURA2" in script
    assert "SMOKE_MODE" in script
    assert "neo4j" in script and "healthy" in script
    frag = FRAG.read_text(encoding="utf-8")
    assert "oss-quickstart-smoke.yml" in frag
    assert "FLAURA2" in frag
    assert "credit_rating" in frag
    assert GOLDEN.is_file()
    golden = GOLDEN.read_text(encoding="utf-8")
    assert "merge" in golden
    assert "unresolved  credit_rating" in golden
