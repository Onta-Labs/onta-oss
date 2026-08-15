"""Recency policy + shared helpers for P6 mutation ops.

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.mutations.<name>`` keep working.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from infona_client.graph.kg_writer import GraphDelta
from infona_client.graph.queries import parse_kg_graph_uri

Triple = tuple[str, str, str]


def _host():
    """Call-time lookup of the public ``mutations`` module.

    Tests monkeypatch names on ``infona_client.pipeline.mutations``
    (``refresh_after_write``, and the other kg_writer primitives). Sibling
    modules must look these up at call time.
    """
    from infona_client.pipeline import mutations as _mod

    return _mod


def _provenance_enabled() -> bool:
    """Whether the op writes a companion-graph governance event (supersede /
    retract), gated by the SAME ``INFONA_PROVENANCE_ENABLED`` env var the rest of
    the write path uses for tombstone/rewrite provenance (default OFF). The
    valid-time interval (``graph/validity.py``) is ALWAYS written regardless — it
    is load-bearing for the "current facts" read, not optional governance."""
    return os.environ.get("INFONA_PROVENANCE_ENABLED", "0") == "1"


def _predicate_leaf(predicate: str) -> str:
    """The leaf name of a predicate URI (``…/onto/hasCEO`` → ``hasCEO``).

    Used to key the recency policy per (type, attribute). Falls back to the whole
    string for a predicate with no path separator.
    """
    return predicate.rstrip("/").rsplit("/", 1)[-1] if predicate else predicate


# --------------------------------------------------------------------------- #
# Per-entity-class recency policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecencyPolicy:
    """Decides, per entity type + attribute, whether a newer fact SUPERSEDES the
    old (recency wins — functional / single-valued) or COEXISTS with it
    (multi-valued — append).

    The default is **single-valued (supersede)**: most attributes are functional
    (an entity has one current CEO, one current headquarters), and the whole point
    of ONTA-277 is that a fresh fact should retire the stale one. A caller marks
    the genuinely multi-valued attributes (a company has many employees, a paper
    many authors) as ``multivalued`` so those append instead.

    Fully injectable/overridable — every op takes a ``policy=`` argument. Overrides
    are keyed by ``(type_name, attribute_leaf)``; ``single_valued`` overrides win
    over ``multivalued`` when both name the same key (an explicit "this one is
    functional" beats a broad default), and both win over ``default_multivalued``.
    """

    default_multivalued: bool = False
    multivalued: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    single_valued: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def supersedes(self, type_name: str, attribute: str) -> bool:
        """True → a newer fact should CLOSE the old (recency wins).
        False → the values COEXIST (multi-valued append)."""
        key = (type_name or "", attribute or "")
        if key in self.single_valued:
            return True
        if key in self.multivalued:
            return False
        return not self.default_multivalued


# The sensible default: single-valued everywhere → recency wins. Callers override
# for their multi-valued attributes.
DEFAULT_RECENCY_POLICY = RecencyPolicy()


@dataclass(frozen=True)
class MutationReceipt:
    """The result of a P6 mutation op — its A6 receipt plus what it retired.

    ``graph_delta`` is the deterministic A6 :class:`GraphDelta` (the same replay-
    stable receipt every KG write produces): for a supersede it reflects the NEW
    instance fact written; for a coexist-append it reflects the appended fact; for
    a pure interval-close retraction it is an empty-facts delta (a retraction adds
    no facts — the closure is recorded in the validity/provenance companions).
    ``superseded`` / ``retracted`` list the ``(s, p, o)`` facts whose interval was
    CLOSED (present-but-not-current afterward); ``inserted`` the new instance
    facts. ``removed`` counts triples hard-deleted (only on the opt-in path).
    """

    op: str  # "supersede" | "retract"
    graph_delta: GraphDelta
    inserted: tuple[Triple, ...] = ()
    superseded: tuple[Triple, ...] = ()
    retracted: tuple[Triple, ...] = ()
    coexisted: bool = False
    removed: int = 0


def _scope(instance_graph: str, tenant_id: Optional[str], kg_name: Optional[str]):
    """Resolve (tenant_id, kg_name) for the post-write refresh, preferring explicit
    args and falling back to parsing the instance-graph URI. Returns ``None`` when
    neither is available (a non-KG test stub graph), so the caller skips refresh —
    the mutation itself already landed in the store (mirrors ``er/rebuild``)."""
    if tenant_id and kg_name:
        return tenant_id, kg_name
    scope = parse_kg_graph_uri(instance_graph)
    if scope is None:
        return None
    return (tenant_id or scope[0], kg_name or scope[1])
