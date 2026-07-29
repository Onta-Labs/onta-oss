"""Cheap "does this KG exist / does it hold anything" probe (ONTA-413).

Why this exists
---------------
SPARQL against a named graph that does not exist returns ZERO ROWS, not an
error. Every read rail therefore collapsed three very different situations into
one indistinguishable answer, ``"No results found."``:

  (a) the KG does not exist at all (typo, wrong workspace, never created),
  (b) the KG is registered but holds no triples (created, never ingested),
  (c) the KG holds data and the question genuinely matched nothing.

Only (c) is an answer. (a) and (b) are states the caller has to know about to
act, and an MCP/CLI agent in particular cannot self-correct a typo it is never
told about. This module separates them with at most two ASK queries, both O(1)
in any triple store, and every interface (webapp, CLI, MCP) reaches it through
the SAME canonical backend routes rather than re-deriving the check client-side.

Deliberately NOT ``knowledge_graphs._live_triple_count``: that is a full
``COUNT(*)`` scan, seconds slow on a large KG, and its own docstring forbids the
hot path. ``ASK { ?s ?p ?o }`` short-circuits on the first match instead.

Caching is POSITIVE-ONLY. Once a KG is known to hold data that fact cannot
become false without a delete, so a short TTL is safe. A "missing" or "empty"
verdict is NEVER cached: create-KG-then-immediately-ask (and
ingest-then-immediately-ask) is the exact flow the agent and MCP exercise
constantly, and a cached negative would break it.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import (
    KG_NAME_PRED,
    kg_graph_uri,
    kg_meta_uri,
    tenant_graph_uri,
)

logger = structlog.stdlib.get_logger("cograph.graph.kg_status")

# Verdicts. Plain strings (not an Enum) so they serialize into telemetry and
# route payloads without ceremony.
KG_OK = "ok"
KG_EMPTY = "empty"
KG_MISSING = "missing"

# {(tenant_id, kg_name): checked_at}. Positive verdicts only (see module docstring).
_kg_ok_cache: dict[tuple[str, str], float] = {}
KG_STATUS_CACHE_TTL = 60  # seconds, mirrors nlp.pipeline's _ontology_cache


def invalidate_kg_status(tenant_id: str, kg_name: str | None = None) -> None:
    """Drop cached positive verdicts for a tenant (or one KG). Test/admin hook."""
    if kg_name is not None:
        _kg_ok_cache.pop((tenant_id, kg_name), None)
        return
    for key in [k for k in _kg_ok_cache if k[0] == tenant_id]:
        _kg_ok_cache.pop(key, None)


async def kg_data_status(neptune, tenant_id: str, kg_name: str) -> str:
    """Return :data:`KG_OK`, :data:`KG_EMPTY` or :data:`KG_MISSING`.

    Two ASKs, issued CONCURRENTLY so this costs one round-trip of latency:

    * ``registered``: the ``<kgs/{tenant}/{name}> <onto/kg_name> "{name}"``
      record in the tenant base graph, the same record ``list_kgs`` reads.
    * ``has_data``: whether the KG's own named graph holds a single triple.

    Both are needed, and the combination matters. A KG that holds data but has
    NO registration record (a legacy graph written before
    ``ensure_kg_registered`` folded registration into the shared write path) is
    reported :data:`KG_OK`, not :data:`KG_MISSING`. Refusing to answer a
    question about a graph that demonstrably has data would be a far worse
    regression than the bug being fixed. Only "no record AND no triples" is
    :data:`KG_MISSING`.

    Fails OPEN: any backend error returns :data:`KG_OK` so a transient Neptune
    hiccup degrades to today's behaviour (attempt the question) rather than
    inventing a "your graph does not exist" claim, which is exactly the
    "errors masquerade as facts" failure mode this codebase already guards
    against elsewhere.
    """
    if not kg_name:
        return KG_OK
    cached = _kg_ok_cache.get((tenant_id, kg_name))
    if cached is not None and (time.time() - cached) < KG_STATUS_CACHE_TTL:
        return KG_OK

    base = tenant_graph_uri(tenant_id)
    # kg_graph_uri validates kg_name (ONTA-414); an invalid name raises before
    # any string reaches a query, which is the intended fail-closed behaviour.
    kg_graph = kg_graph_uri(tenant_id, kg_name)
    meta = kg_meta_uri(tenant_id, kg_name)

    registered_q = (
        f"ASK FROM <{base}> WHERE {{ <{meta}> <{KG_NAME_PRED}> ?n }}"
    )
    has_data_q = f"ASK FROM <{kg_graph}> WHERE {{ ?s ?p ?o }}"

    try:
        registered, has_data = await asyncio.gather(
            neptune.ask(registered_q), neptune.ask(has_data_q)
        )
    except Exception:  # noqa: BLE001 - never turn a probe failure into a false claim
        logger.warning(
            "kg_status_probe_failed", tenant=tenant_id, kg_name=kg_name, exc_info=True
        )
        return KG_OK

    if has_data:
        _kg_ok_cache[(tenant_id, kg_name)] = time.time()
        return KG_OK
    if registered:
        return KG_EMPTY
    return KG_MISSING


async def list_kg_names(neptune, tenant_id: str, limit: int = 25) -> list[str]:
    """Names of the tenant's registered KGs, for a "did you mean" hint.

    Reads the SAME registration record ``list_kgs`` serves from, but projects
    only the name so this stays a tiny lookup (no triple counts, no stats store,
    no per-KG scan). Best-effort: returns ``[]`` on any error, since this only
    ever enriches an error message.
    """
    base = tenant_graph_uri(tenant_id)
    sparql = (
        f"SELECT DISTINCT ?name FROM <{base}> WHERE {{ ?kg <{KG_NAME_PRED}> ?name }} "
        f"LIMIT {int(limit)}"
    )
    try:
        _, rows = parse_sparql_results(await neptune.query(sparql))
    except Exception:  # noqa: BLE001 - hint only, never fail the request for it
        logger.warning("kg_status_list_failed", tenant=tenant_id, exc_info=True)
        return []
    names: list[str] = []
    for row in rows:
        name = row.get("name", "")
        if name and name not in names:
            names.append(name)
    return names


def missing_kg_message(kg_name: str, available: list[str]) -> str:
    """One human/agent-readable sentence naming the missing KG + the real ones."""
    if available:
        return (
            f"Knowledge graph '{kg_name}' does not exist in this workspace. "
            f"Available knowledge graphs: {', '.join(available)}."
        )
    return (
        f"Knowledge graph '{kg_name}' does not exist in this workspace, and this "
        "workspace has no knowledge graphs yet. Create one and ingest data first."
    )


def empty_kg_message(kg_name: str) -> str:
    """One sentence for the registered-but-empty case."""
    return (
        f"Knowledge graph '{kg_name}' exists but contains no data yet, so there is "
        "nothing to query. Ingest data into it first."
    )
