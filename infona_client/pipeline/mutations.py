"""P6 mutation ops — supersession & retraction (ONTA-277).

P6 is the sole KG writer. Until now it could only ADD facts: a refresh that
brought a new CEO left the old ``hasCEO`` edge in place, and the answer layer (P7)
would then cite the stale one. Freshness that ADDS without SUPERSEDING makes the
graph worse over time. This module gives P6 the ability to *retire* facts:

- **Supersession** (:func:`supersede_fact`) — a NEWER fact for the same
  subject+functional-attribute CLOSES the old fact's validity interval. The old
  fact is no longer *current* (a "current facts" query stops citing it) but STAYS
  in the graph, queryable as history, marked with a closed interval +
  ``superseded_by`` pointer.
- **Retraction** (:func:`retract_fact`) — an explicit assertion that a fact is
  no-longer-true, distinct from supersession (which is driven by a replacement).
  The default path closes the fact's currency (history stays queryable); an opt-in
  ``hard_delete`` genuinely removes the instance triple via ``delete_facts``.
- **Conflict resolution** (:func:`write_with_conflict_resolution`, ONTA-276) — when
  an incoming fact CONTRADICTS the existing current value on a FUNCTIONAL attribute
  (revenue $10M vs $12M), a deterministic policy (``pipeline/conflict.py``) picks
  the WINNER by authority + confidence + recency and CLOSES the loser's interval
  with ``STATUS_DEPRECATED`` — the loser stays present-but-not-current, queryable
  WITH its provenance and the reason it lost. Same closure mechanism as
  supersession (never a delete), driven by a stronger CONTEMPORANEOUS source rather
  than a newer replacement.

**The banned ghost-edge mechanism (do NOT reintroduce).** Supersession closes a
validity interval — it does NOT delete or re-point the superseded edge as a
cleanup hack. The superseded triple remains in the instance graph exactly as
written; only a companion validity node (``graph/validity.py``) records that its
interval closed. This keeps history and lineage intact.

**Orchestration, not hand-rolled writes.** These ops compose the kg_writer
primitives — ``insert_facts`` (the new fact + companion validity/provenance
triples, returning the A6 :class:`~infona_client.graph.kg_writer.GraphDelta`
receipt), ``delete_facts`` (only on the opt-in hard-delete path), and one
``refresh_after_write`` per op. They construct NO raw SPARQL and touch NO graph
directly; the valid-time interval triples and governance events are built by the
``graph/validity.py`` and ``graph/provenance.py`` builders and routed to their
companion graphs by ``insert_facts``. This is why a mutation stays on the shared
write path the convergence guard enforces.

Implementation lives in sibling ``mutations_*.py`` modules. Every previously
importable name is re-exported here.

Boundary: OSS. Imports only stdlib / ``infona_client.*``.
"""

from __future__ import annotations

import structlog

from infona_client.graph.kg_writer import (  # noqa: F401 — _host() / monkeypatch surface
    GraphDelta,
    build_graph_delta,
    delete_facts,
    insert_facts,
    refresh_after_write,
    rewrite_subject,
)
from infona_client.pipeline.mutations_apply import (  # noqa: F401 — public re-exports
    retract_fact,
    supersede_fact,
)
from infona_client.pipeline.mutations_conflict import (  # noqa: F401 — public re-exports
    ConflictReceipt,
    _parse_ts,
    _read_existing_claims,
    write_with_conflict_resolution,
)
from infona_client.pipeline.mutations_merge import (  # noqa: F401 — public re-exports
    SAME_AS,
    MergeReceipt,
    SplitReceipt,
    _fetch_node_triples,
    _to_canonical_form,
    merge_entities,
    split_entity,
)
from infona_client.pipeline.mutations_policy import (  # noqa: F401 — public re-exports
    DEFAULT_RECENCY_POLICY,
    MutationReceipt,
    RecencyPolicy,
    Triple,
    _host,
    _predicate_leaf,
    _provenance_enabled,
    _scope,
)

logger = structlog.stdlib.get_logger("infona.pipeline.mutations")

__all__ = [
    "RecencyPolicy",
    "DEFAULT_RECENCY_POLICY",
    "MutationReceipt",
    "supersede_fact",
    "retract_fact",
    "ConflictReceipt",
    "write_with_conflict_resolution",
    "MergeReceipt",
    "SplitReceipt",
    "SAME_AS",
    "merge_entities",
    "split_entity",
]
