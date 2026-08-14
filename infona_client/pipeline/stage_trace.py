"""Contract-level **job stage traces** for P0–P9 (operator Job Trace page).

Infona's decomposition is ten sub-projects (P0 Runtime … P9 Surfaces). Operators
debugging a job need to see, per project that participated:

* **input** the stage was given
* **what it did** (actions / steps)
* **output** it produced

aligned with the Stage Contract (Notion Sub-Project Stage Contracts / A0–A10).

This module is the durable schema + a small recorder + a **reconstructor** that
builds a best-effort view from fields already on :class:`EnrichJob` (manifest,
provider_logs, progress, …) so jobs that ran *before* live instrumentation still
render something useful.

Implementation lives in sibling ``stage_trace_*.py`` modules. Every previously
importable name is re-exported here.

Boundary: OSS. Imports only stdlib + pydantic (+ lazy EnrichJob for reconstructor).
"""

from __future__ import annotations

from infona_client.pipeline.stage_trace_enrich import (  # noqa: F401 — public re-exports
    _ENRICHMENT_SKIP_REASONS,
    _enum_value,
    stamp_enrichment_entities_selected,
    stamp_enrichment_job_created,
    stamp_enrichment_run_cancelled,
    stamp_enrichment_run_failed,
    stamp_enrichment_run_finished,
    stamp_enrichment_run_started,
    stamp_enrichment_write_phase,
)
from infona_client.pipeline.stage_trace_jobs import (  # noqa: F401 — public re-exports
    ensure_job_stage_trace_open,
    finalize_job_stage_trace,
    open_job_stage_trace,
)
from infona_client.pipeline.stage_trace_models import (  # noqa: F401 — public re-exports
    STAGE_CATALOG,
    JobStageTrace,
    StageAction,
    StageProjectId,
    StageProjectTrace,
    StageStatus,
    _catalog_fields,
    _now,
    empty_project,
    ensure_all_projects,
)
from infona_client.pipeline.stage_trace_reconstruct import (  # noqa: F401 — public re-exports
    _iso,
    _progress_dict,
    _provider_log_meta,
    reconstruct_from_job,
    resolve_trace,
)
from infona_client.pipeline.stage_trace_recorder import (  # noqa: F401 — public re-exports
    StageTraceRecorder,
    attach_recorder,
    new_trace_for_job,
)
from infona_client.pipeline.stage_trace_summaries import (  # noqa: F401 — public re-exports
    _SAMPLE_CAP,
    _cap_list,
    merge_a1_summaries,
    merge_a3_counts,
    summarize_a1_source_bundle,
    summarize_a2_candidates,
    summarize_a3_clean_report,
    summarize_a6_graph_delta,
)
