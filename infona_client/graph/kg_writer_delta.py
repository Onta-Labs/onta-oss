"""A6 Graph Delta (ONTA-271) — replay-stable receipt of a write.

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.graph.kg_writer.<name>`` keep working.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from infona_client.graph.iri import IRI_BASE
from infona_client.pipeline.envelope import derive_fact_id

Triple = tuple[str, str, str]


def _host():
    from infona_client.graph import kg_writer as _mod

    return _mod

# --- A6 Graph Delta (ONTA-271) ------------------------------------------------
#
# Predicates PROJECTED OUT of a deterministic Graph Delta: the write-path
# bookkeeping NONCES that qc/boundary also omits from its byte-stable A2/A4/A5
# fixtures. ``ingested_at`` is a wall-clock stamp and ``batch_id`` a per-run
# token; neither is a domain fact, and including either would make a replayed
# run's delta differ even when every real fact is identical. Excluding them is
# what lets a byte-identical Graph Delta prove an upstream replay reproduced the
# graph (the P6 determinism the ticket requires). The instance-graph values are
# additionally made replay-stable at the source (batch_id derived from run_id,
# ingested_at sourced from the runf's observed_at), so the store write itself is
# idempotent — the exclusion here is belt-and-suspenders + a clean fact-level
# delta.
DELTA_NONCE_PREDICATES = frozenset(
    {
        f"{IRI_BASE}/onto/ingested_at",
        f"{IRI_BASE}/onto/batch_id",  # == graph.queries.BATCH_PREDICATE
    }
)


@dataclass(frozen=True)
class GraphDelta:
    """A6 — a deterministic, replay-stable receipt of the domain facts a write applied.

    Mirrors qc/boundary's determinism discipline: the de-duplicated, SORTED set
    of instance ``(subject, predicate, object)`` triples with the bookkeeping
    NONCES (``DELTA_NONCE_PREDICATES``) projected out, each stamped with the
    stable per-subject ``fact_id`` (a pure function of ``run_id`` + subject URI,
    the content-stable ``local_key`` that flows A2→A6). Two runs of the SAME
    facts under the SAME ``run_id`` therefore produce byte-identical
    :meth:`canonical_bytes`, so P6 can dedupe an upstream replay instead of
    duplicating the graph (ONTA-271).

    ``fan_in`` records source-fact → canonical-node merges (ER auto-merge,
    key-join, in-run same-key dedup) as sorted ``(source_fact_id,
    canonical_fact_id)`` pairs — the mapping is otherwise invisible once several
    source facts collapse onto one node.
    """

    run_id: Optional[str]
    instance_graph: str
    facts: tuple[tuple[str, str, str, str], ...]  # (fact_id, s, p, o), sorted
    fan_in: tuple[tuple[str, str], ...] = ()  # (source_fact_id, canonical_fact_id), sorted

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "instance_graph": self.instance_graph,
            "facts": [list(f) for f in self.facts],
            "fan_in": [list(p) for p in self.fan_in],
        }

    def canonical_bytes(self) -> bytes:
        """Byte-stable serialization for replay comparison (sorted keys, UTF-8)."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def build_graph_delta(
    instance_graph: str,
    instance_triples: list[Triple],
    *,
    run_id: Optional[str] = None,
    fan_in: Optional[dict[str, str]] = None,
) -> GraphDelta:
    """Project written instance triples into a deterministic A6 :class:`GraphDelta`.

    Drops the bookkeeping nonces (``DELTA_NONCE_PREDICATES``), de-dups + SORTS the
    remaining ``(s, p, o)``, and stamps each with the stable per-subject
    ``fact_id = derive_fact_id(run_id, stage="A6", local_key=subject)``. Pure and
    deterministic: the same triples under the same ``run_id`` always yield
    byte-identical :meth:`GraphDelta.canonical_bytes`, so a caller can PROVE an
    upstream replay reproduced the graph exactly (ONTA-271).

    ``fan_in`` maps ``{source_subject_uri: canonical_subject_uri}`` for facts that
    merged onto one node; both sides are resolved to their stable fact_ids and
    recorded, sorted. ``run_id=None`` still yields a valid (run-agnostic) delta.
    """

    def _fid(subject: str) -> str:
        return derive_fact_id(run_id=run_id or "", stage="A6", local_key=subject)

    domain = {
        (s, p, o)
        for (s, p, o) in instance_triples
        if s and p and p not in DELTA_NONCE_PREDICATES
    }
    facts = tuple(sorted((_fid(s), s, p, o) for (s, p, o) in domain))
    fan_pairs = tuple(
        sorted(
            (_fid(src), _fid(dst))
            for src, dst in (fan_in or {}).items()
            if src and dst and src != dst
        )
    )
    return GraphDelta(
        run_id=run_id, instance_graph=instance_graph, facts=facts, fan_in=fan_pairs
    )
