"""Honest-answer metadata assembly (ONTA-280 — the P7 answer layer).

Read-only, POST-execution assembly that attaches, PER CITED FACT, its

  * **recency** — read from the Wave-3 valid-time intervals (``graph/validity.py``):
    ``valid_from`` and ``is_current`` (current == the ABSENCE of a ``valid_to``);
  * **verdict** — ``current`` when the interval is open, else the closure
    ``status`` (``superseded`` / ``retracted`` / ``lost_conflict``); a fact with
    NO validity node is current by convention;
  * **confidence** — read from the companion provenance graph
    (``graph/provenance.py``); ``None`` when provenance is disabled/absent
    (``COGRAPH_PROVENANCE_ENABLED``),

plus a **coverage caveat** that composes the A9 :class:`RunCoverage` summary
("N of M items…") with a validity-derived "K facts stale".

This lives entirely OUT of the SPARQL-generation path: it consults the
ALREADY-EXECUTED bindings and reads two companion graphs. It NEVER writes, never
touches the planner, and DEGRADES gracefully — a fact with no validity node is
current; a missing/failed provenance read yields ``confidence=None``; any read
failure yields no citation for that fact rather than breaking the answer.

**CENTRAL CONSTRAINT (why some rows are not cited).** A generated ``SELECT``
projects arbitrary variables and often does NOT expose the
``(subject IRI, predicate URI, object term)`` a fact must be keyed by. A citation
is attached ONLY for rows that DO expose those — a describe-shape row projecting
subject + predicate + object, or a row projecting ``?uri``/``?s`` alongside a
predicate and object. For non-keyable rows the citation list is legitimately
empty — that is honest, not a bug.

Boundary: OSS. Imports only stdlib / ``cograph_client.*`` — no ``from cograph.*``.
"""

from __future__ import annotations

from typing import Optional

from cograph_client.graph.predicates import RDF_TYPE, is_internal_predicate
from cograph_client.graph.provenance import ProvenanceRecord, fetch_provenance
from cograph_client.graph.validity import (
    STATUS_SUPERSEDED,
    ValidityInterval,
    fetch_history,
)
from cograph_client.models.query import FactCitation

# Column-name aliases we accept for each role, in priority order. A generated
# describe-shape query typically projects ``?s ?p ?o`` (or ``?p ?o`` with a
# projected ``?uri`` subject); other shapes expose the same facts under these
# common synonyms. Matched case-insensitively (see :func:`_pick`).
_SUBJECT_KEYS = ("s", "subject", "subj", "uri", "iri", "entity")
_PREDICATE_KEYS = ("p", "predicate", "pred", "prop", "property")
_OBJECT_KEYS = ("o", "object", "obj", "value", "val")
_LABEL_KEYS = ("label", "name", "title", "l")


def _is_iri(value: str) -> bool:
    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://")
    )


def _pick(row: dict, keys: tuple[str, ...]) -> Optional[str]:
    """First non-empty value in ``row`` whose (case-insensitive) key is in ``keys``."""
    lower = {k.lower(): k for k in row}
    for cand in keys:
        actual = lower.get(cand)
        if actual is not None:
            val = row.get(actual)
            if val not in (None, ""):
                return val
    return None


def _key_row(row: dict) -> Optional[tuple[str, str, str, str]]:
    """Derive ``(subject, predicate, object, label)`` from one parsed binding, or
    ``None`` when the row does not expose a keyable fact.

    A row is keyable iff it carries a subject column that is an IRI, a predicate
    column that is an IRI, and an object column (literal or IRI). ``label`` falls
    back to the object term when no display column is present.
    """
    s = _pick(row, _SUBJECT_KEYS)
    p = _pick(row, _PREDICATE_KEYS)
    o = _pick(row, _OBJECT_KEYS)
    if s is None or p is None or o is None:
        return None
    if not _is_iri(s) or not _is_iri(p):
        return None
    label = _pick(row, _LABEL_KEYS) or o
    return s, p, o, label


async def _safe_history(
    neptune, instance_graph: str, subject: str, predicate: str
) -> list[ValidityInterval]:
    """``fetch_history`` already swallows read failures (returns ``[]``); wrap it
    once more so a validity read never breaks citation assembly."""
    try:
        return await fetch_history(neptune, instance_graph, subject, predicate)
    except Exception:  # noqa: BLE001 — a recency read is informational, never load-bearing
        return []


async def _safe_provenance(
    neptune, instance_graph: str, subject: str, predicate: str
) -> list[ProvenanceRecord]:
    """``fetch_provenance`` does NOT catch (unlike ``fetch_history``) and the
    provenance graph may be absent (``COGRAPH_PROVENANCE_ENABLED`` off) — so wrap
    it and degrade to ``[]`` (⇒ ``confidence=None``) on any failure."""
    try:
        return await fetch_provenance(neptune, instance_graph, subject, predicate)
    except Exception:  # noqa: BLE001 — confidence is best-effort; degrade to "unknown"
        return []


def _match_interval(
    intervals: list[ValidityInterval], obj: str
) -> Optional[ValidityInterval]:
    """The validity interval for exactly this object term, if one exists.

    ``fetch_history`` and the bindings both flow through ``parse_sparql_results``,
    so both carry the bare (datatype-dropped) object value — a direct string match
    keys the two consistently. Prefer a CLOSED interval over an open one for the
    same object (a value both re-asserted and closed reads as not-current)."""
    match: Optional[ValidityInterval] = None
    for iv in intervals:
        if iv.obj == obj:
            if not iv.is_current:
                return iv
            match = iv
    return match


def _match_provenance(
    records: list[ProvenanceRecord], obj: str
) -> Optional[ProvenanceRecord]:
    """The highest-confidence provenance record for this object term, if any."""
    best: Optional[ProvenanceRecord] = None
    for rec in records:
        if rec.obj == obj:
            if best is None or rec.confidence > best.confidence:
                best = rec
    return best


async def build_citations(
    neptune,
    instance_graph: str,
    variables: list[str],
    bindings: list[dict],
) -> list[FactCitation]:
    """Assemble per-fact citations for the keyable rows of a result set.

    For every row that exposes ``(subject, predicate, object)`` (see the module
    docstring), read the fact's validity interval + provenance and emit a
    :class:`FactCitation` carrying verdict + confidence + ``valid_from`` +
    ``is_current`` + source. Validity/provenance are read ONCE per unique
    ``(subject, predicate)`` (batched), not once per row. Non-keyable rows produce
    no citation; ``rdf:type`` and internal/housekeeping predicates are skipped so
    the answer cites domain facts, not bookkeeping. Fully read-only and
    degrade-safe.
    """
    citations: list[FactCitation] = []
    if not bindings or not instance_graph:
        return citations

    # 1. Collect keyable facts and the unique (s, p) pairs to read once.
    keyed: list[tuple[str, str, str, str]] = []
    sp_pairs: dict[tuple[str, str], None] = {}
    for row in bindings:
        key = _key_row(row)
        if key is None:
            continue
        s, p, o, label = key
        if p == RDF_TYPE or is_internal_predicate(p, is_relationship=_is_iri(o)):
            continue
        keyed.append((s, p, o, label))
        sp_pairs[(s, p)] = None
    if not keyed:
        return citations

    # 2. Batch the validity + provenance reads per (s, p).
    history_by_sp: dict[tuple[str, str], list[ValidityInterval]] = {}
    prov_by_sp: dict[tuple[str, str], list[ProvenanceRecord]] = {}
    for (s, p) in sp_pairs:
        history_by_sp[(s, p)] = await _safe_history(neptune, instance_graph, s, p)
        prov_by_sp[(s, p)] = await _safe_provenance(neptune, instance_graph, s, p)

    # 3. Assemble one citation per keyable fact.
    for (s, p, o, label) in keyed:
        interval = _match_interval(history_by_sp.get((s, p), []), o)
        record = _match_provenance(prov_by_sp.get((s, p), []), o)
        if interval is not None and not interval.is_current:
            verdict = interval.status or STATUS_SUPERSEDED
            is_current = False
            valid_from = interval.valid_from
        elif interval is not None:
            verdict = "current"
            is_current = True
            valid_from = interval.valid_from
        else:
            # No validity node → current by convention (append-only history).
            verdict = "current"
            is_current = True
            valid_from = ""
        citations.append(
            FactCitation(
                subject=s,
                predicate=p,
                object=o,
                label=label,
                verdict=verdict,
                confidence=(record.confidence if record is not None else None),
                valid_from=valid_from,
                is_current=is_current,
                source=(record.source if record is not None else ""),
            )
        )
    return citations


def build_coverage_caveat(
    coverage,
    *,
    stale_count: int = 0,
    total_cited: int = 0,
) -> str:
    """Compose the honest coverage caveat for an answer.

    Joins the A9 :class:`~cograph_client.pipeline.manifest.RunCoverage` summary
    ("N of M items completed; K dropped; <halt reason>", which already contains
    the "N of M" fragment) with the validity-derived "K facts stale". When no
    coverage manifest is available (the common ``/ask`` path today), still emits
    the stale-count caveat on its own so an answer built partly on superseded
    facts is never silently presented as fully fresh. Returns ``""`` when there is
    nothing to caveat.
    """
    parts: list[str] = []
    summary = getattr(coverage, "summary", "") if coverage is not None else ""
    if summary:
        parts.append(f"answered from {summary}")
    if stale_count > 0:
        noun = "fact" if stale_count == 1 else "facts"
        if total_cited:
            parts.append(f"{stale_count} of {total_cited} cited {noun} stale")
        else:
            parts.append(f"{stale_count} {noun} stale")
    return "; ".join(parts)


__all__ = ["build_citations", "build_coverage_caveat"]
