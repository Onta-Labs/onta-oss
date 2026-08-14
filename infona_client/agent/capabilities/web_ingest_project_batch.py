"""Project a discovered batch through A1 gates into a SourceBundle.

Pre-write only. Suppression check mints via ``entity_uri``. Instance
edges stay on ``onto/<leaf>`` when the resolver later writes.
"""
from __future__ import annotations

from infona_client.pipeline.discovery_quality import catalog_path_segments
from infona_client.pipeline.source_bundle import build_source_bundle
from infona_client.pipeline.stage_trace import (
    StageProjectId,
    attach_recorder,
    merge_a1_summaries,
    summarize_a1_source_bundle,
)
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_fetch import (
    _emit_source_bundle,
    _platforms,
    _provider_secret_refs,
    _provider_tier,
)
from infona_client.agent.capabilities.web_ingest_plan_enum import (
    _dedupe_rows_with_source_urls,
)
from infona_client.agent.capabilities.web_ingest_project import (
    _drop_suppressed_rows,
    _screen_a1_rows,
    _structural_identity_keys,
    apply_post_a1_structural_gates,
)


async def _project_discovered_batch(
    *,
    rows_found,
    key_attr,
    seen_keys,
    provenance,
    proposed_type,
    suppressed_entities,
    full,
    prov,
    job,
    job_store,
    attributes,
    ctx,
    run_id,
    sub_query,
    kg_name,
    instance_graph,
    run_envelope,
    acc: dict,
):
    """Dedupe → suppress → A1 screen → structural gates → SourceBundle.

    Mutates ``acc`` (counters, seen_identity_keys, platforms, a1_acc).
    Returns the bundle, or ``None`` when every row was dropped.
    """
    job_catalog_inventory = acc["job_catalog_inventory"]
    seen_identity_keys = acc["seen_identity_keys"]
    a1_rows_dropped = acc["a1_rows_dropped"]
    a1_cells_scrubbed = acc["a1_cells_scrubbed"]
    a1_drop_reasons = acc["a1_drop_reasons"]
    role_drops = acc["role_drops"]
    identity_merges = acc["identity_merges"]
    a1_acc = acc["a1_acc"]
    a2_source_rows = acc["a2_source_rows"]
    platforms = acc["platforms"]
    plogs = acc["plogs"]

    # Per-record source-URL provenance (ONTA-151): stamp
    # each row with the page it was drawn from BEFORE
    # serialization, so it rides through the SAME extract →
    # ingest → insert_facts path as the rest of the row's
    # data and lands as a `source_url` citation.
    #
    # ONTA-256: bind the URL BEFORE _dedupe_rows drops rows.
    # Dedupe SHIFTS every surviving row's positional index;
    # the provenance map is keyed by each row's ORIGINAL
    # position, so stamping AFTER the drop (re-derived by the
    # shifted index) mis-binds a survivor to a DROPPED
    # neighbour's page. Binding first — indices still
    # original — and carrying the URL on the row object makes
    # the citation immune to the reindex.
    batch = _dedupe_rows_with_source_urls(
        rows_found,
        key_attr,
        seen_keys,
        provenance,
    )
    # ONTA-345: entity-level re-acquisition guard. DROP any
    # row whose would-be canonical subject
    # (entity_uri(proposed_type, row[key_attr])) is on the
    # entity-suppression list fetched once per run above —
    # BEFORE the SourceBundle is built and BEFORE
    # resolver.ingest*, so an ERASED entity never enters the
    # bundle and never reaches the writer. Each drop is
    # logged. No-op (returns `batch` unchanged) when the run
    # has nothing suppressed.
    batch = _drop_suppressed_rows(
        batch,
        proposed_type,
        key_attr,
        suppressed_entities,
        provider=prov.name,
        job_id=job.id if job is not None else None,
    )
    # A1 VALIDATORS (ONTA-393): reject nav-chrome NAMES (drop
    # the whole row) and type-invalid CELLS (city=year,
    # website=no-host, address=enrolment phrase — scrub the
    # cell) at the A1 boundary — same seam as the suppression
    # guard, BEFORE the SourceBundle is built and BEFORE
    # resolver.ingest*, so chrome never becomes a graph
    # entity. Fill rate ≠ correctness: this is the gate that
    # keeps a non-empty-but-wrong cell out of the write.
    (
        batch,
        _rows_dropped,
        _cells_scrubbed,
        _drop_reasons,
    ) = _screen_a1_rows(
        batch,
        key_attr,
        attributes,
        provider=prov.name,
        job_id=job.id if job is not None else None,
    )
    if _rows_dropped or _cells_scrubbed:
        a1_rows_dropped += _rows_dropped
        a1_cells_scrubbed += _cells_scrubbed
        for _r in _drop_reasons:
            if (
                _r not in a1_drop_reasons
                and len(a1_drop_reasons) < 20
            ):
                a1_drop_reasons.append(_r)
        # Keep the operator Job Trace honest: a P1 action
        # explaining the A1 rejections. Isolated so a trace
        # hiccup can never sink the write path.
        try:
            if job is not None:
                rec = attach_recorder(job)
                if rec is not None:
                    rec.action(
                        StageProjectId.p1,
                        "a1_validate",
                        detail=(
                            f"dropped {_rows_dropped} "
                            f"nav-chrome rows, scrubbed "
                            f"{_cells_scrubbed} type-invalid "
                            f"cells"
                        ),
                        meta={
                            "rows_dropped": _rows_dropped,
                            "cells_scrubbed": _cells_scrubbed,
                            "reasons": _drop_reasons[:8],
                            "provider": prov.name,
                        },
                    )
        except Exception:  # noqa: BLE001 — trace never breaks the run
            _wic.logger.warning(
                "web_ingest_a1_validate_trace_failed",
                job_id=job.id if job is not None else None,
                exc_info=True,
            )
    if not batch:
        acc["job_catalog_inventory"] = job_catalog_inventory
        acc["seen_identity_keys"] = seen_identity_keys
        acc["a1_rows_dropped"] = a1_rows_dropped
        acc["a1_cells_scrubbed"] = a1_cells_scrubbed
        acc["a1_drop_reasons"] = a1_drop_reasons
        acc["role_drops"] = role_drops
        acc["identity_merges"] = identity_merges
        acc["a1_acc"] = a1_acc
        acc["a2_source_rows"] = a2_source_rows
        acc["platforms"] = platforms
        return None
    # STRUCTURAL QUALITY GATES (ONTA-465 / WS6): after A1
    # shape validators, before SourceBundle / write —
    # (1) role-membership (drop role entities mistaken for
    # instances) then (2) discovery quality (website policy
    # + structural identity merge including catalog-path ↔
    # surface form). No brand/platform denylists. Helper
    # never raises into the write path.
    # Mark inventory before gates so this batch's own
    # catalog paths count for sparse brand drops.
    for _br in batch:
        if isinstance(_br, dict) and catalog_path_segments(
            _br.get(key_attr)
        ):
            job_catalog_inventory = True
            break
    sg = apply_post_a1_structural_gates(
        batch,
        key_attr,
        list(attributes),
        focus_type=proposed_type,
        catalog_inventory=job_catalog_inventory,
    )
    batch = sg.rows
    # Drop cross-batch structural dups (display name of a
    # catalog id already written earlier in this job).
    if seen_identity_keys and batch:
        _kept_id: list = []
        _xdrop = 0
        for _br in batch:
            if not isinstance(_br, dict):
                _kept_id.append(_br)
                continue
            _iks = _structural_identity_keys(
                _br.get(key_attr)
            )
            if _iks and _iks & seen_identity_keys:
                _xdrop += 1
                continue
            _kept_id.append(_br)
        if _xdrop:
            identity_merges += _xdrop
            batch = _kept_id
    # Remember identity keys for later batches.
    for _br in batch:
        if isinstance(_br, dict):
            seen_identity_keys |= _structural_identity_keys(
                _br.get(key_attr)
            )
    if sg.role_drops:
        role_drops += int(sg.role_drops)
    if sg.identity_merges:
        identity_merges += int(sg.identity_merges)
    if sg.websites_scrubbed:
        a1_cells_scrubbed += int(sg.websites_scrubbed)
    for _r in sg.reasons:
        if (
            _r not in a1_drop_reasons
            and len(a1_drop_reasons) < 20
        ):
            a1_drop_reasons.append(_r)
    if sg.role_drops or sg.identity_merges or sg.websites_scrubbed:
        try:
            if job is not None:
                rec = attach_recorder(job)
                if rec is not None:
                    if sg.role_drops:
                        rec.action(
                            StageProjectId.p1,
                            "role_membership_gate",
                            detail=(
                                f"dropped {sg.role_drops} "
                                f"role-inverted rows"
                            ),
                            meta={
                                "role_drops": sg.role_drops,
                                "rows_out": len(batch),
                                "reasons": [
                                    r
                                    for r in sg.reasons
                                    if r.startswith(
                                        (
                                            "role-inversion",
                                            "sparse-self-role",
                                        )
                                    )
                                ][:8],
                                "provider": prov.name,
                            },
                        )
                    if (
                        sg.identity_merges
                        or sg.websites_scrubbed
                    ):
                        rec.action(
                            StageProjectId.p1,
                            "quality_gate",
                            detail=(
                                f"scrubbed "
                                f"{sg.websites_scrubbed} "
                                f"website cells, "
                                f"identity_merges="
                                f"{sg.identity_merges} → "
                                f"{len(batch)} rows"
                            ),
                            meta={
                                "websites_scrubbed": (
                                    sg.websites_scrubbed
                                ),
                                "near_dups_merged": (
                                    sg.identity_merges
                                ),
                                "identity_merges": (
                                    sg.identity_merges
                                ),
                                "rows_out": len(batch),
                                "reasons": sg.reasons[:8],
                                "provider": prov.name,
                            },
                        )
        except Exception:  # noqa: BLE001
            _wic.logger.warning(
                "web_ingest_structural_gate_trace_failed",
                job_id=(
                    job.id if job is not None else None
                ),
                exc_info=True,
            )
    if not batch:
        acc["job_catalog_inventory"] = job_catalog_inventory
        acc["seen_identity_keys"] = seen_identity_keys
        acc["a1_rows_dropped"] = a1_rows_dropped
        acc["a1_cells_scrubbed"] = a1_cells_scrubbed
        acc["a1_drop_reasons"] = a1_drop_reasons
        acc["role_drops"] = role_drops
        acc["identity_merges"] = identity_merges
        acc["a1_acc"] = a1_acc
        acc["a2_source_rows"] = a2_source_rows
        acc["platforms"] = platforms
        return None
    # A1 SOURCE BUNDLE (ONTA-346): materialize the
    # Find→Extract boundary artifact from THIS provider's
    # post-dedupe batch, BEFORE the extract/write below.
    # The rows already carry their per-record `source_url`
    # (bound above, pre-dedupe); the bundle stamps run
    # identity + per-row fact-id lineage + the source tier
    # (registry Tier -1 = authoritative vs web) + the
    # provider's LOGICAL secret_ref (never a resolved
    # credential). This is a PRE-write artifact — it does
    # NOT write; ONTA-371 makes it the extract DRIVER below
    # (the loop iterates `bundle.rows`), and because each
    # row's `data` is a snapshot copy of the batch row the
    # KG write stays byte-identical — lineage rides along.
    bundle = build_source_bundle(
        batch,
        workspace_id=ctx.tenant_id,
        run_id=run_id,
        provider=prov.name,
        tier=_provider_tier(prov),
        secret_refs=_provider_secret_refs(prov),
        key_attribute=key_attr,
        bundle_key=f"{prov.name}:{sub_query}",
    )
    # ONTA-371: the bundle is now the LIVE extract driver —
    # the micro-batch loop below iterates ``bundle.rows``
    # (not the built-then-dropped raw ``batch``) and hands
    # each row's A1 ``fact_id`` / ``tier`` to the resolver
    # ingest call (the real A1→A2 handoff). The observer
    # sink stays as a SUPPLEMENTARY hook (a no-op when
    # unset); the bundle is genuinely consumed regardless.
    _emit_source_bundle(ctx, bundle)
    # Operator Job Trace: A1 Source Bundle boundary (P1→P2).
    # Contract-shaped A1 summary (ONTA-385); try/except so
    # observability never sinks discovery.
    try:
        a1_piece = summarize_a1_source_bundle(bundle)
        a1_acc = merge_a1_summaries(a1_acc, a1_piece)
        a2_source_rows += len(bundle.rows)
        if job is not None:
            rec = attach_recorder(job)
            if rec is not None:
                rec.action(
                    StageProjectId.p1,
                    "source_bundle",
                    detail=(
                        f"A1 provider={prov.name} "
                        f"rows={len(bundle.rows)} "
                        f"tier={_provider_tier(prov)}"
                    ),
                    meta={
                        **{
                            k: a1_piece.get(k)
                            for k in (
                                "artifact",
                                "run_id",
                                "root_fact_id",
                                "row_count",
                                "tiers",
                                "providers",
                            )
                        },
                        "provider": prov.name,
                        "sub_query": (sub_query or "")[:200],
                    },
                )
                # Progressive P1 output = run-level A1 aggregate.
                for _p in job.stage_trace.projects:
                    if _p.project_id == StageProjectId.p1:
                        _p.output = {
                            **_p.output,
                            **(a1_acc or {}),
                        }
                        break
                rec.begin(
                    StageProjectId.p2,
                    input={
                        "artifact": "A1",
                        "name": "Source Bundle",
                        "source_row_count": len(bundle.rows),
                        "provider": prov.name,
                        "run_id": a1_piece.get("run_id"),
                        "root_fact_id": a1_piece.get(
                            "root_fact_id"
                        ),
                        "tier": _provider_tier(prov),
                        "contract_consumes": (
                            "A1 Source Bundle (or uploaded file)"
                        ),
                        "contract_emits": "A2 Candidate Facts",
                    },
                )
                rec.action(
                    StageProjectId.p2,
                    "extract_from_bundle",
                    detail="A1→A2 extract driver",
                )
                rec.begin(
                    StageProjectId.p6,
                    input={
                        "artifact": "A5",
                        "name": "Placement Plan (fused)",
                        "kg_name": kg_name,
                        "type_name": proposed_type,
                        "instance_graph": instance_graph,
                        "run_id": run_envelope.run_id,
                        "contract_consumes": "A5 Placement Plan",
                        "contract_emits": "A6 Graph Delta",
                    },
                )
    except Exception:
        _wic.logger.warning(
            "stage_trace_a1_failed",
            job_id=getattr(job, "id", None) if job else None,
            exc_info=True,
        )
    platforms = list(
        dict.fromkeys(
            [
                *platforms,
                *_platforms(
                    getattr(full, "sources", None), prov
                ),
            ]
        )
    )
    # Live status BEFORE the (slower) LLM-extraction
    # ingest, so a poll mid-batch already shows which
    # providers were consulted + what they found — the
    # single-batch classic path otherwise sits at 0/0 for
    # the whole extraction (adversarial-review F5). Flip the
    # user-facing phase to "ingesting" (ONTA-238): we have
    # rows and are about to run the extract→insert path, the
    # slowest leg of the run.
    if job is not None and job_store is not None:
        job.progress.phase = "ingesting"
        job.platforms = platforms
        job.provider_logs = list(plogs.values())
        await job_store.update(job)
    # SUB-BATCHED ingest (ONTA-243) — split the batch's
    # rows into small sub-batches, commit each, and flush
    # ``processed``/``filled`` AFTER EVERY ONE so both
    # headline counters move WHILE the job is still
    # ``running`` — not just once the whole (slow)
    # extraction of the entire batch completes. This is the
    # single-list ask's fix: one sub-query × one provider is
    # the WHOLE run, so without sub-batching a poller sees a
    # flat 0/0 for the entire extraction and concludes the
    # job stalled (persona-eval RCA). Mirrors enrichment's
    # per-record flush cadence. Source names the provider
    # that actually produced the batch.
    #
    # CITATION BINDING (persona-eval RCA — citation
    # mis-binding): partition the batch by source_url FIRST,
    # then size-chunk within each group, so every micro-batch
    # handed to the extractor is homogeneous in its citation.
    # An extraction that sees rows from exactly one page can
    # only stamp THAT page's URL on any entity it mints — the
    # LLM can no longer copy page A's URL onto an entity drawn
    # from page B. Groups are consecutive, so order + counts
    # are unchanged; a batch that already shares one URL (or
    # none) is one group — identical to the prior behavior.
    # ONTA-371: drive the extract loop from the A1
    # SourceBundle's rows (each a ``SourceRow`` carrying the
    # row's snapshot ``data`` + its A1 ``fact_id`` + source
    # ``tier``), not the built-then-dropped raw ``batch``.
    # ``bundle.rows`` is index-aligned with ``batch``, so
    # grouping/chunking (and the records extracted) are
    # byte-identical — the change threads lineage, it does

    acc["job_catalog_inventory"] = job_catalog_inventory
    acc["seen_identity_keys"] = seen_identity_keys
    acc["a1_rows_dropped"] = a1_rows_dropped
    acc["a1_cells_scrubbed"] = a1_cells_scrubbed
    acc["a1_drop_reasons"] = a1_drop_reasons
    acc["role_drops"] = role_drops
    acc["identity_merges"] = identity_merges
    acc["a1_acc"] = a1_acc
    acc["a2_source_rows"] = a2_source_rows
    acc["platforms"] = platforms
    return bundle
