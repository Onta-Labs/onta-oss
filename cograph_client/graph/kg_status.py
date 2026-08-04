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
told about. This module separates them with two ASK queries on the hot path
(three on the rare registered-but-empty path), all O(1) in any triple store, and
every interface (webapp, CLI, MCP) reaches it through the SAME canonical backend
routes rather than re-deriving the check client-side.

"Empty" is defined against the query's ACTUAL dataset, which is a union of named
graphs rather than the one per-KG graph, so the probe can never call a workspace
empty that the answer query could still have answered from. That union rule
stops at EXISTENCE: a name with no registration record and no triples of its own
is :data:`KG_MISSING` however much data the rest of the union holds (ONTA-453).
See :func:`kg_data_status` for the mechanics.

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

from cograph_client.graph.iri import IRI_BASE
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

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
# Every instancef's rdf:type object lives under this prefix (tenant, public and
# enhanced type URIs alike). Ontology CLASS declarations do not (their object is
# rdfs:Class), which is exactly the discrimination _base_has_instances_query needs.
_TYPES_PREFIX = f"{IRI_BASE}/types/"

# {(tenant_id, kg_name): checked_at}. Positive verdicts only (see module docstring).
_kg_ok_cache: dict[tuple[str, str], float] = {}
KG_STATUS_CACHE_TTL = 60  # seconds, mirrors nlp.pipeline's _ontology_cache

# {tenant_id: checked_at} for "the tenant BASE graph holds instance data".
# Positive only, for the same reason as `_kg_ok_cache`: a base graph that holds
# instances cannot stop holding them without a delete, while a NEGATIVE verdict
# would survive the first ingest into a brand-new workspace.
_base_instances_cache: dict[str, float] = {}


def invalidate_kg_status(tenant_id: str, kg_name: str | None = None) -> None:
    """Drop cached positive verdicts for a tenant (or one KG). Test/admin hook."""
    if kg_name is not None:
        _kg_ok_cache.pop((tenant_id, kg_name), None)
        return
    _base_instances_cache.pop(tenant_id, None)
    for key in [k for k in _kg_ok_cache if k[0] == tenant_id]:
        _kg_ok_cache.pop(key, None)


def _base_has_instances_query(base_graph: str) -> str:
    """ASK whether the tenant BASE graph holds INSTANCE data (not just schema).

    See :func:`kg_data_status` for why a bare ``?s ?p ?o`` would be wrong here.
    ``rdf:type`` is the scan-bounding predicate, and the object-namespace FILTER
    is what separates instances from the ontology's own class declarations. The
    prefix covers layered type URIs (``types/public/X``, ``types/enhanced/X``)
    too, since those are still instances when they appear as an object.
    """
    return (
        f"ASK FROM <{base_graph}> WHERE {{ "
        f"?s <{_RDF_TYPE}> ?t . "
        f'FILTER(STRSTARTS(STR(?t), "{_TYPES_PREFIX}")) '
        f"}}"
    )


async def kg_data_status(neptune, tenant_id: str, kg_name: str) -> str:
    """Return :data:`KG_OK`, :data:`KG_EMPTY` or :data:`KG_MISSING`.

    Two ASKs on the hot path, issued CONCURRENTLY so this costs one round-trip
    of latency:

    * ``registered``: the ``<kgs/{tenant}/{name}> <onto/kg_name> "{name}"``
      record in the tenant base graph, the same record ``list_kgs`` reads.
    * ``kg_has_data``: whether the KG's own named graph holds a single triple.

    Both are needed, and the combination matters. A KG that holds data but has
    NO registration record (a legacy graph written before
    ``ensure_kg_registered`` folded registration into the shared write path) is
    reported :data:`KG_OK`, not :data:`KG_MISSING`. Refusing to answer a
    question about a graph that demonstrably has data would be a far worse
    regression than the bug being fixed. Only "no record AND no data" is
    :data:`KG_MISSING`.

    The dataset is a UNION, not one graph, but only for a REGISTERED KG
    -------------------------------------------------------------------
    A THIRD ASK fires only when a REGISTERED KG's own graph is empty, because
    "empty" has to mean what the ANSWER QUERY means by empty. ``/ask`` threads
    ``layer_stack_for(tenant).visible_graph_uris()`` into the pipeline, that
    stack ALWAYS contains the tenant BASE graph (``LayerStack.visible_layers``
    always includes ``Layer.TENANT``), and ``add_layer_from_clauses`` splices
    those in as extra ``FROM`` clauses. So the effective default graph is the
    union of ``kg/<name>`` + the tenant base graph + the global layers.

    A workspace can legitimately hold its instance data in the tenant BASE graph
    rather than a per-KG graph (``api/routes/ingest.py`` writes there whenever
    ``kg_name`` is absent, and explicitly falls back to it). For such a
    workspace an empty ``kg/<name>`` does NOT mean the question is unanswerable,
    and short-circuiting on the per-KG ASK alone would turn a working answer
    into a confident refusal. That is the same class of dishonesty this probe
    exists to remove, just pointed the other way.

    ONTA-453: that rescue used to apply to UNREGISTERED names too, and that was
    wrong. The user supplied a name; if no record and no triples answer to it,
    it is not a graph. Rescuing it does not preserve an answer, it fabricates
    one: the reproduction on demo-tenant asked "how many records are there?"
    against ``deffinitely_not_a_real_kg_xyz`` and got a confident 255210, every
    row of which came from the tenant base graph and the global public layer
    and none from the graph named in the question. "Do no harm" only holds when
    the union answer is plausibly ABOUT the thing the caller named, which
    requires the thing to exist. A registered-but-empty KG does exist, so it
    keeps the rescue; an unregistered name is a typo and is now
    :data:`KG_MISSING` regardless of what the base graph holds. This also makes
    the verdict cheaper on that path (two ASKs, not three) and honest for every
    caller of this probe at once: ``/ask``'s 404, ``QueryCapability``'s
    clarify, and ``agent/kg_scope``'s write-turn gate.

    An OMITTED ``kg_name`` is untouched: it short-circuits to :data:`KG_OK`
    above and legitimately reads the base graph (ONTA-426).

    That third ASK is deliberately NOT a bare ``?s ?p ?o`` over the base graph:
    the base graph is also the ONTOLOGY graph and always holds at least the KG's
    own registration triple, so a bare pattern would be true for every
    registered KG and this feature would never fire. It looks for INSTANCE data
    specifically, a subject typed with a ``graph.onta.sh/types/`` class. Ontology
    class declarations are ``<types/X> rdf:type <rdfs#Class>`` (object outside
    that namespace), so they do not count, while instances
    (``<entities/X/id> rdf:type <types/X>``) do. This is the same notion of
    "active type" the pipeline's own instance-graph probe uses.

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
    kg_has_data_q = f"ASK FROM <{kg_graph}> WHERE {{ ?s ?p ?o }}"

    try:
        registered, kg_has_data = await asyncio.gather(
            neptune.ask(registered_q), neptune.ask(kg_has_data_q)
        )
        if kg_has_data:
            _kg_ok_cache[(tenant_id, kg_name)] = time.time()
            return KG_OK
        if not registered:
            # ONTA-453. No record and no triples: the name the caller supplied
            # is not a graph in this workspace. The base-graph rescue below must
            # NOT cover this case, it would answer a question about a graph
            # that does not exist, out of data that is not in it.
            logger.info(
                "kg_status_missing", tenant=tenant_id, kg_name=kg_name
            )
            return KG_MISSING
        # Only now, on the rare path where we are about to declare a REGISTERED
        # KG empty, pay for the base-graph instance check. The common
        # populated-KG case stays at exactly two ASKs, and so does a typo.
        if await neptune.ask(_base_has_instances_query(base)):
            _kg_ok_cache[(tenant_id, kg_name)] = time.time()
            return KG_OK
    except Exception:  # noqa: BLE001 - never turn a probe failure into a false claim
        logger.warning(
            "kg_status_probe_failed", tenant=tenant_id, kg_name=kg_name, exc_info=True
        )
        return KG_OK

    return KG_EMPTY


async def base_graph_has_instances(neptune, tenant_id: str) -> bool:
    """Does the tenant BASE graph hold INSTANCE data of its own? (ONTA-454)

    The same ``ASK`` :func:`kg_data_status` already runs on its rare
    registered-but-empty path, hoisted so the coverage caveat can reuse it rather
    than growing a second way to ask the same question.

    Why the caveat needs it: a generated query that constrains no type at all
    (``?s rdf:type ?type``, the shape "how many rows of data are there in total?"
    produces) reads the WHOLE union, so a count answers for the workspace and not
    for the KG the caller named. That is only worth saying when the union
    actually contains something other than the named graph, which on this
    platform is overwhelmingly the base graph: it IS the instance graph for every
    ``kg_name``-less ingest (18,515 typed instance subjects on demo-tenant,
    measured read-only 2026-08-03). In a workspace whose data lives entirely in
    one per-KG graph the union IS that graph, and the caveat would be noise.

    Fails toward SILENCE, which is the OPPOSITE of :func:`kg_data_status`'s
    fail-open rule, and deliberately so. The two functions gate different claims.
    ``kg_data_status`` fails open because its failure mode would be inventing
    "your graph does not exist"; this one gates a POSITIVE assertion that other
    data exists in the workspace, and asserting that unverified would be exactly
    the "a caveat that is wrong about which graph answered" failure the caveat
    exists to prevent. Unproven means unsaid.

    Cached POSITIVE-only, so a real workspace pays for this once per TTL and the
    steady-state cost is zero.
    """
    if not tenant_id:
        return False
    cached = _base_instances_cache.get(tenant_id)
    if cached is not None and (time.time() - cached) < KG_STATUS_CACHE_TTL:
        return True
    base = tenant_graph_uri(tenant_id)
    try:
        found = await neptune.ask(_base_has_instances_query(base))
    except Exception:  # noqa: BLE001 - an unverified claim is not made at all
        logger.warning(
            "base_graph_instance_probe_failed", tenant=tenant_id, exc_info=True
        )
        return False
    if found:
        _base_instances_cache[tenant_id] = time.time()
    return bool(found)


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
