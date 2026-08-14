"""Claim-based reconciler for the semantic instance index (ONTA-181).

The CORRECTNESS half of the ONTA-173 consistency model (see
``semantic/protocol.py`` for the full diagram). Implementation lives in
sibling ``reconciler_*.py`` modules. Every previously importable name is
re-exported here.

Writes: schema verdicts via ``commit_ontology`` (SET_TEXT_KIND); the index
itself via the ``SemanticIndex`` protocol. This module never writes instance
triples (no ``insert_facts`` / ``insert_triples``).

Tests monkeypatch names on this module (``_MAX_SCAN_PAGES``,
``_MAX_CANDIDACY_ATTRS_PER_RUN``, ``_UPSERT_BATCH_CHUNKS``,
``_ASSERTION_HISTORY_HARD_CAP``, ``_now_monotonic``, ``random.shuffle``,
``reconcile_kg``, ``run_embed_fill_sweep``). Siblings look them up at call
time via ``_host()``.
"""

from __future__ import annotations

import random  # noqa: F401 — tests patch rec.random.shuffle

import structlog

from infona_client.semantic.reconciler_candidacy import (  # noqa: F401
    _apply_default_candidacy,
    _catalog_domain_for_attr,
    _distinct_literal_predicates,
    _distinct_literal_predicates_store,
    _fetch_marker_map,
    _sample_literal_values,
)
from infona_client.semantic.reconciler_common import _host  # noqa: F401
from infona_client.semantic.reconciler_const import (  # noqa: F401
    EMBED_FILL_SCHEDULE_ID,
    SEMANTIC_EMBED_FILL_ACTION,
    SEMANTIC_RECONCILE_ACTION,
    TEXT_KIND_NOT_TEXT,
    Triple,
    _ASSERTION_HISTORY_HARD_CAP,
    _ATTR_URI_RE,
    _CANDIDACY_SAMPLE_SIZE,
    _GLOBAL_KG,
    _LABEL_LOCALS,
    _MAX_CANDIDACY_ATTRS_PER_RUN,
    _MAX_SCAN_PAGES,
    _MAX_SWEEP_ITERATIONS,
    _RDF_TYPE,
    _RDFS_LABEL,
    _SYSTEM_TENANT,
    _UPSERT_BATCH_CHUNKS,
)
from infona_client.semantic.reconciler_embed import run_embed_fill_sweep  # noqa: F401
from infona_client.semantic.reconciler_env import (  # noqa: F401
    _ensure_memo_ttl_s,
    _int_env,
    _now,
    _now_monotonic,
    _scan_page_size,
    embed_fill_interval_s,
    embed_max_attempts,
    reconcile_interval_s,
    semantic_index_enabled,
)
from infona_client.semantic.reconciler_keys import (  # noqa: F401
    _assertion_row_to_semantic_triples,
    identity_doc_keys,
    indexable_doc_keys,
    marked_doc_keys,
)
from infona_client.semantic.reconciler_run import (  # noqa: F401
    _upsert_in_doc_batches,
    reconcile_kg,
)
from infona_client.semantic.reconciler_scan import (  # noqa: F401
    _scan_query,
    _scan_triples,
    _scan_triples_sparql,
    _scan_triples_store,
    _sparql_string_literal,
    _store_property_ids_for_scan,
)
from infona_client.semantic.reconciler_sched import (  # noqa: F401
    dispatch_semantic_schedule,
    ensure_embed_fill_schedule,
    ensure_reconcile_schedule,
    ensure_reconcile_schedule_from_hook,
    reconcile_schedule_id,
    remove_reconcile_schedule,
    reset_for_tests,
    schedule_reconcile_task,
)

logger = structlog.stdlib.get_logger("infona.semantic.reconciler")

# The write hook's ensure path. A module-level store (same selection logic as
# the runner's) plus a TTL memo so the hook pays the ensure round-trip once
# per (tenant, kg) per TTL window, not once per write. Lives here so
# ``reset_for_tests`` and hook memos stay on the monkeypatch surface.
_hook_store: object | None = None
#: ``(tenant, kg) -> monotonic deadline``; entries past their deadline are
#: re-ensured on the next hook write.
_ensured_reconcile: dict[tuple[str, str], float] = {}

# Fire-and-forget reconciles for deployments with NO runner (zero-config OSS:
# no DSN, scheduler off). Strong refs, mirroring explore.schedule_recompute.
_bg_tasks: set = set()
