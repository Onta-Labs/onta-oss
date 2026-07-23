"""``cograph_client.pipeline`` — cross-cutting types shared by every A1-A10
pipeline artifact in the Onta decomposition (P0-P9; ONTA-265).

Today this holds :class:`~cograph_client.pipeline.envelope.ArtifactEnvelope`,
the universal ``workspace_id`` / ``run_id`` / ``fact_id`` / parent-lineage /
``observed_at`` / ``spend_usd`` (+ ``ontology_version`` on A5/A6) metadata every
pipeline artifact carries. See
``docs/adr/0011-universal-artifact-envelope-schema.md`` for the decision record.

**Schema/type stub only** — no pipeline stage constructs or consumes these types
yet; wiring is deliberately deferred to later waves (ONTA-271, ONTA-273,
ONTA-270).

Boundary: OSS. Imports only stdlib.
"""

from __future__ import annotations

from cograph_client.pipeline.envelope import (
    FACT_ID_NAMESPACE,
    ArtifactEnvelope,
    derive_fact_id,
)
from cograph_client.pipeline.answer_run import (
    answer_run_lookup_path,
    record_answer_run,
)
from cograph_client.pipeline.stage_trace import (
    STAGE_CATALOG,
    JobStageTrace,
    StageAction,
    StageProjectId,
    StageProjectTrace,
    StageStatus,
    StageTraceRecorder,
    attach_recorder,
    ensure_job_stage_trace_open,
    finalize_job_stage_trace,
    new_trace_for_job,
    open_job_stage_trace,
    reconstruct_from_job,
    resolve_trace,
    stamp_enrichment_job_created,
    stamp_enrichment_run_cancelled,
    stamp_enrichment_run_failed,
    stamp_enrichment_run_finished,
    stamp_enrichment_run_started,
)

__all__ = [
    "FACT_ID_NAMESPACE",
    "ArtifactEnvelope",
    "derive_fact_id",
    "STAGE_CATALOG",
    "JobStageTrace",
    "StageAction",
    "StageProjectId",
    "StageProjectTrace",
    "StageStatus",
    "StageTraceRecorder",
    "attach_recorder",
    "ensure_job_stage_trace_open",
    "finalize_job_stage_trace",
    "new_trace_for_job",
    "open_job_stage_trace",
    "reconstruct_from_job",
    "resolve_trace",
    "stamp_enrichment_job_created",
    "stamp_enrichment_run_cancelled",
    "stamp_enrichment_run_failed",
    "stamp_enrichment_run_finished",
    "stamp_enrichment_run_started",
    "answer_run_lookup_path",
    "record_answer_run",
]
