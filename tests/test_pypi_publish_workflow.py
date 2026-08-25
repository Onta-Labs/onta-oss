"""Lockstep publish tags a GitHub Release going forward; no historical backfill."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WF = REPO / ".github" / "workflows" / "pypi-publish.yml"


def test_pypi_publish_tags_github_release_going_forward_only() -> None:
    assert WF.is_file()
    text = WF.read_text(encoding="utf-8")
    _, sep, tag_step = text.partition("name: Tag and GitHub Release")
    assert sep, "pypi-publish.yml must have a Tag and GitHub Release step"

    tag_if = next(
        line for line in tag_step.splitlines() if line.lstrip().startswith("if:")
    )
    assert "steps.skip.outputs.bump == 'true'" in tag_if
    assert "steps.skip.outputs.skip != 'true'" in tag_if
    assert "gh release create" in tag_step
    assert "refs/tags/${TAG}" in tag_step
    assert "already exists" in tag_step

    header = text.split("jobs:", 1)[0]
    assert "Do not backfill" in header
    assert "0.1.17" in header and "0.1.42" in header
    assert "v0.1.17" not in tag_step
    assert "v0.1.42" not in tag_step
    assert "for " not in tag_step
    assert "seq " not in tag_step
    before_tag = text[: text.index("name: Tag and GitHub Release")]
    assert "gh release create" not in before_tag
