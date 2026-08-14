"""Stage-trace types + P0–P9 catalog.

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.stage_trace.<name>`` keep working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _host():
    from infona_client.pipeline import stage_trace as _mod

    return _mod

def _now() -> datetime:
    return datetime.now(timezone.utc)


class StageProjectId(str, Enum):
    """The ten Stage Contract projects (P0–P9)."""

    p0 = "P0"
    p1 = "P1"
    p2 = "P2"
    p3 = "P3"
    p4 = "P4"
    p5 = "P5"
    p6 = "P6"
    p7 = "P7"
    p8 = "P8"
    p9 = "P9"


# Catalog: id → display name + contract blurb (producer→consumer artifact).
# Keep in sync with Notion "Sub-Project Stage Contracts" (v2).
STAGE_CATALOG: dict[StageProjectId, dict[str, str]] = {
    StageProjectId.p0: {
        "name": "Runtime & Orchestration",
        "consumes": "A9 Run Manifest (from every stage)",
        "emits": "run status; cost envelope; terminal halt reasons",
        "goal": "Own the run as an object — state machine, retries, partial-failure, cost.",
    },
    StageProjectId.p1: {
        "name": "Find Data",
        "consumes": "user goal · A8 Refresh Delta",
        "emits": "A1 Source Bundle",
        "goal": "Turn a goal into complete-enough, provenance-stamped source material.",
    },
    StageProjectId.p2: {
        "name": "Extraction",
        "consumes": "A1 Source Bundle (or uploaded file)",
        "emits": "A2 Candidate Facts",
        "goal": "Pull evidence-linked candidate facts from sources (soft-typed).",
    },
    StageProjectId.p3: {
        "name": "Clean",
        "consumes": "A2 Candidate Facts",
        "emits": "A3 Clean Facts",
        "goal": "Normalize values; log every transform/drop; preserve surface form.",
    },
    StageProjectId.p4: {
        "name": "Verify",
        "consumes": "A3 Clean Facts",
        "emits": "A4 Verified Facts",
        "goal": "Truth verdicts + evidence refs (identity-conditional where needed).",
    },
    StageProjectId.p5: {
        "name": "Ontology / Placement",
        "consumes": "A4 Verified Facts",
        "emits": "A5 Placement Plan",
        "goal": "Map facts to ontology terms; stamp ontology version.",
    },
    StageProjectId.p6: {
        "name": "Write",
        "consumes": "A5 Placement Plan",
        "emits": "A6 Graph Delta",
        "goal": "Mutate the graph (write / supersede / retract / merge) with receipts.",
    },
    StageProjectId.p7: {
        "name": "Answer",
        "consumes": "A6 Graph Delta · A9 Run Manifest",
        "emits": "A7 Answer",
        "goal": "Cited answer + coverage caveats from the run manifest.",
    },
    StageProjectId.p8: {
        "name": "Freshness",
        "consumes": "graph state · schedule",
        "emits": "A8 Refresh Delta → P1",
        "goal": "Diff-scoped re-acquisition; refresh as supersession, not silent add.",
    },
    StageProjectId.p9: {
        "name": "Surfaces",
        "consumes": "all artifacts (user-facing)",
        "emits": "A10 Correction & Feedback",
        "goal": "Everything the user touches; corrections re-enter P6 / gold sets.",
    },
}


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    skipped = "skipped"
    failed = "failed"
    reconstructed = "reconstructed"  # synthesized from job fields, not live-recorded


class StageAction(BaseModel):
    """One step the project took."""

    name: str
    detail: Optional[str] = None
    at: Optional[datetime] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class StageProjectTrace(BaseModel):
    """One P0–P9 project's participation in a job run."""

    project_id: StageProjectId
    name: str
    status: StageStatus = StageStatus.pending
    # Contract summary (from STAGE_CATALOG) — rendered even when no live data.
    contract_goal: Optional[str] = None
    contract_consumes: Optional[str] = None
    contract_emits: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    # Free-form but intentionally structured for the UI (JSON-serializable).
    input: dict[str, Any] = Field(default_factory=dict)
    actions: list[StageAction] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    # True when this entry was synthesized after-the-fact from other job fields.
    reconstructed: bool = False


class JobStageTrace(BaseModel):
    """Full operator-facing stage trace for one job."""

    job_id: str
    tenant_id: str
    kg_name: str
    category: Optional[str] = None
    status: Optional[str] = None
    # How complete is the live instrumentation for this job?
    source: Literal["live", "reconstructed", "mixed"] = "reconstructed"
    projects: list[StageProjectTrace] = Field(default_factory=list)
    # Job-level summary (always useful at the top of the page).
    summary: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=_now)


def _catalog_fields(pid: StageProjectId) -> dict[str, str]:
    cat = STAGE_CATALOG[pid]
    return {
        "name": cat["name"],
        "contract_goal": cat["goal"],
        "contract_consumes": cat["consumes"],
        "contract_emits": cat["emits"],
    }


def empty_project(pid: StageProjectId, *, status: StageStatus = StageStatus.skipped) -> StageProjectTrace:
    fields = _catalog_fields(pid)
    return StageProjectTrace(
        project_id=pid,
        status=status,
        **fields,
    )


def ensure_all_projects(projects: list[StageProjectTrace]) -> list[StageProjectTrace]:
    """Return P0…P9 in order, filling missing entries as ``skipped``."""
    by_id = {p.project_id: p for p in projects}
    out: list[StageProjectTrace] = []
    for pid in StageProjectId:
        if pid in by_id:
            out.append(by_id[pid])
        else:
            out.append(empty_project(pid))
    return out
