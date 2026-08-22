"""Suppression READ path — "may this value be (re-)acquired?" (ONTA-279).

The companion to :mod:`infona_client.graph.suppression` (which owns the marker
SEMANTICS + triple builders) and :mod:`infona_client.graph.suppression_store`
(which owns the property-graph STORAGE). Extracted so neither file carries both
the write-shape and the read-ladder; the facade re-exports every name here, so
``from infona_client.graph.suppression import is_suppressed`` keeps working.

Each reader runs the GraphStore arm FIRST (the shipped Neo4j backend, ONTA-527)
and keeps the residual SPARQL arm below it for the dual-arm unit tests — the same
ladder ``nlp/pipeline_active_types._scan_instance_types`` uses.

Facade names are looked up lazily via :func:`_sup` so this module never imports
its facade at module scope (no import cycle) — the ``_host()`` pattern the
``kg_writer_*`` siblings use.

Boundary: OSS. Imports only stdlib / ``infona_client.*``.
"""

from __future__ import annotations

import structlog

from infona_client.graph.queries import _escape_value

logger = structlog.stdlib.get_logger("infona.graph.suppression")

_XSD = "http://www.w3.org/2001/XMLSchema"


def _sup():
    """The :mod:`infona_client.graph.suppression` facade (lazy — no import cycle)."""
    from infona_client.graph import suppression as _mod

    return _mod


def suppressed_objects_query(instance_graph: str, subject: str, predicate: str) -> str:
    """SELECT every SUPPRESSED object of ``(subject, predicate)``.

    Reads the companion suppression graph for all ``sup:object`` marked under this
    ``(subject, predicate)``. Used by :func:`fetch_suppressed` / :func:`is_suppressed`
    to decide whether a refresh may (re-)acquire a value.
    """
    sup_graph = _sup().suppression_graph_uri(instance_graph)
    s, p = _escape_value(subject), _escape_value(predicate)
    return (
        f"SELECT ?o WHERE {{\n"
        f"  GRAPH <{sup_graph}> {{\n"
        f"    ?node <{_sup().SUP_SUBJECT}> {s} ;\n"
        f"          <{_sup().SUP_PREDICATE}> {p} ;\n"
        f"          <{_sup().SUP_OBJECT}> ?o .\n"
        f"  }}\n"
        f"}}"
    )


def _object_term(binding: dict) -> str:
    """Reconstruct the write-convention object string from a raw SPARQL JSON binding.

    The inverse of ``graph/queries._escape_value`` on the read side (mirrors
    ``validity._object_term``): preserve the EXACT term so a suppressed typed
    literal round-trips and matches term-for-term (the ONTA-247 typed-literal
    lesson). ``uri`` → the URI string; a typed literal → ``value^^datatype``; a
    plain / ``xsd:string`` literal → the bare value.
    """
    kind = binding.get("type")
    value = binding.get("value", "")
    if kind == "uri":
        return value
    dt = binding.get("datatype")
    if dt and dt != f"{_XSD}#string":
        return f"{value}^^{dt}"
    return value


async def _sparql_suppressed(
    neptune, instance_graph: str, subject: str, predicate: str
) -> set[str] | None:
    """RESIDUAL SPARQL arm of :func:`read_suppressed` (dual-arm unit tests).

    Reads the raw SPARQL JSON (not ``parse_sparql_results``, which drops datatype)
    so each term round-trips exactly. Returns ``None`` — never an empty set — when
    there is no usable client or the query fails, so "could not read" stays
    distinguishable from "nothing is suppressed".
    """
    if neptune is None or not callable(getattr(neptune, "query", None)):
        return None
    try:
        raw = await neptune.query(
            suppressed_objects_query(instance_graph, subject, predicate)
        )
    except Exception:  # noqa: BLE001 — retired client / store hiccup → unknown
        return None
    bindings = raw.get("results", {}).get("bindings", [])
    out: set[str] = set()
    for row in bindings:
        o = row.get("o")
        if o is not None:
            out.add(_object_term(o))
    return out


async def read_suppressed(
    neptune, instance_graph: str, subject: str, predicate: str, *, store=None
) -> set[str] | None:
    """Suppressed object terms for ``(subject, predicate)``, or ``None`` if unknown.

    GraphStore arm FIRST (ONTA-279 / E7 port): marks live on ``:Suppression`` nodes
    scoped to this KG's ``(tenant_id, kg)``. The residual SPARQL arm is kept for the
    dual-arm unit tests and answers only when a live client is supplied.

    ``None`` means NOBODY could answer; ``SuppressionUnavailable`` propagates when
    the store WAS asked and failed. Callers need both cases apart — see
    :func:`is_suppressed`.
    """
    from infona_client.graph.suppression_store import read_suppressed_terms

    from_store = await read_suppressed_terms(
        instance_graph, subject, predicate, store=store
    )
    if from_store is not None:
        return from_store
    return await _sparql_suppressed(neptune, instance_graph, subject, predicate)


async def fetch_suppressed(
    neptune, instance_graph: str, subject: str, predicate: str, *, store=None
) -> set[str]:
    """The write-convention object terms currently SUPPRESSED for ``(subject, predicate)``.

    Best-effort projection of :func:`read_suppressed` for LISTING "what is
    suppressed here": an unreadable store collapses to the empty set. Do NOT use it
    to DECIDE whether a value may be written — that needs the unknown case, which
    is why :func:`is_suppressed` reads the tri-state directly.
    """
    try:
        return await read_suppressed(
            neptune, instance_graph, subject, predicate, store=store
        ) or set()
    except Exception:  # noqa: BLE001 — the listing projection never raises
        return set()


async def is_suppressed(
    neptune, instance_graph: str, subject: str, predicate: str, obj: str, *, store=None
) -> bool:
    """True iff ``(subject, predicate, obj)`` is on the suppression list.

    Term-faithful: ``obj`` must match the suppressed term exactly (typed-literal
    convention included), so suppressing ``"42"^^xsd:integer`` does not
    accidentally suppress the plain string ``"42"`` and vice-versa.

    **Fail direction (deliberate, asymmetric).** This gates whether a refresh may
    re-acquire a value, so a wrong FALSE silently resurrects what the user
    retracted — the exact ONTA-279 promise. Therefore:

    * store ASKED and FAILED (``SuppressionUnavailable``) → **True, fail CLOSED**.
      Transient and per-call: withholding ONE value from ONE refresh self-heals
      next run, and the same session is what the ensuing write would use, so the
      refresh is degraded either way. Re-acquisition self-heals for nobody — the
      user must notice and retract a second time.
    * store could not be ASKED AT ALL, and no SPARQL arm answered → **False, fail
      OPEN**, logged loudly. That is a STATIC deployment property (no store, no
      per-KG scope), identical for every value on every run, so failing closed
      would not withhold one value — it would silently brick the refresh rail for
      every tenant, a freshness harm indistinguishable from the rail not running.
    """
    from infona_client.graph.suppression_store import SuppressionUnavailable

    try:
        terms = await read_suppressed(
            neptune, instance_graph, subject, predicate, store=store
        )
    except SuppressionUnavailable:
        logger.warning(
            "suppression_read_failed_withholding_value",
            instance_graph=instance_graph,
            subject=subject,
            predicate=predicate,
            detail="suppression store unreadable — value treated as suppressed",
        )
        return True
    if terms is None:
        logger.warning(
            "suppression_store_unavailable_allowing_value",
            instance_graph=instance_graph,
            subject=subject,
            predicate=predicate,
            detail="no suppression store to consult — retractions unenforceable",
        )
        return False
    return obj in terms


def suppressed_entities_query(instance_graph: str) -> str:
    """SELECT every ENTITY-level suppressed subject in the companion graph.

    Reads ONLY the ``sup:entity`` marks (the subject-only erasure tombstones), so
    a ``(s, p, o)`` FACT mark — which carries ``sup:subject`` / ``sup:predicate`` /
    ``sup:object``, never ``sup:entity`` — is structurally excluded. Used by
    :func:`fetch_suppressed_entities` / :func:`is_entity_suppressed` to decide, in
    ONE batched read per run, whether a discovered row may be (re-)acquired. Mirrors
    :func:`suppressed_objects_query` in shape (an inline SELECT over the companion
    suppression graph) — a READ, not a write, so it stays outside the write-path
    convergence guard exactly as the ``(s, p, o)`` reader does.
    """
    sup_graph = _sup().suppression_graph_uri(instance_graph)
    return (
        f"SELECT ?s WHERE {{\n"
        f"  GRAPH <{sup_graph}> {{\n"
        f"    ?node <{_sup().SUP_ENTITY}> ?s .\n"
        f"  }}\n"
        f"}}"
    )


async def read_suppressed_entities(
    neptune, instance_graph: str, *, store=None
) -> set[str] | None:
    """ENTITY-level suppressed subjects, or ``None`` when nobody could answer.

    GraphStore arm first, residual SPARQL arm second — the same ladder (and the
    same ``SuppressionUnavailable`` propagation) as :func:`read_suppressed`.
    """
    if not instance_graph:
        return None
    from infona_client.graph.suppression_store import read_suppressed_entity_uris

    from_store = await read_suppressed_entity_uris(instance_graph, store=store)
    if from_store is not None:
        return from_store
    if neptune is None or not callable(getattr(neptune, "query", None)):
        return None
    try:
        raw = await neptune.query(suppressed_entities_query(instance_graph))
    except Exception:  # noqa: BLE001 — retired client / store hiccup → unknown
        return None
    bindings = raw.get("results", {}).get("bindings", [])
    out: set[str] = set()
    for row in bindings:
        s = row.get("s")
        if s is not None:
            out.add(_object_term(s))
    return out


async def fetch_suppressed_entities(
    neptune, instance_graph: str, *, store=None
) -> set[str]:
    """The set of ENTITY subjects currently SUPPRESSED (erased/tombstoned) in a graph.

    ONE read per run (the FIND-path guard checks set-membership per row, so a
    discovery of N rows costs a single read, not N reads). Each subject is compared
    term-identically to a discovered row's would-be ``entity_uri``.

    **Best-effort on purpose, unlike :func:`is_suppressed`.** This is a BULK filter
    whose consumer drops every row whose subject is in the set, so failing closed
    here would drop an entire discovery run rather than withhold one value. An
    unreadable store therefore collapses to the empty set (a degraded run, logged
    by the store arm). For the per-subject decision use
    :func:`is_entity_suppressed`, which carries the fail-closed contract.
    """
    try:
        return await read_suppressed_entities(
            neptune, instance_graph, store=store
        ) or set()
    except Exception:  # noqa: BLE001 — the bulk filter never fails a run
        return set()


async def is_entity_suppressed(
    neptune, instance_graph: str, subject: str, *, store=None
) -> bool:
    """True iff the ENTITY ``subject`` is on the entity-level suppression list.

    Term-faithful and kind-faithful: matches only ``sup:entity`` marks, so a
    ``(s, p, o)`` FACT suppression of the same subject does NOT make this return
    True (and an entity suppression does not make :func:`is_suppressed` return
    True) — the two suppression kinds are independent.

    Same asymmetric fail direction as :func:`is_suppressed`: an unreadable store
    withholds the subject (fail CLOSED, transient), while a store that cannot be
    consulted at all allows it (fail OPEN, static config) with a loud log.
    """
    from infona_client.graph.suppression_store import SuppressionUnavailable

    try:
        subjects = await read_suppressed_entities(neptune, instance_graph, store=store)
    except SuppressionUnavailable:
        logger.warning(
            "suppression_entity_read_failed_withholding_subject",
            instance_graph=instance_graph,
            subject=subject,
        )
        return True
    if subjects is None:
        logger.warning(
            "suppression_store_unavailable_allowing_subject",
            instance_graph=instance_graph,
            subject=subject,
        )
        return False
    return subject in subjects


__all__ = [
    "fetch_suppressed",
    "fetch_suppressed_entities",
    "is_entity_suppressed",
    "is_suppressed",
    "read_suppressed",
    "read_suppressed_entities",
    "suppressed_entities_query",
    "suppressed_objects_query",
]
