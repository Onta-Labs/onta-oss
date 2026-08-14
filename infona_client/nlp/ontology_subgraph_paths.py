"""Enumerate ontology relationship paths (1-hop and multi-hop chains).

Looked up on :mod:`infona_client.nlp.ontology_subgraph_match` at call time via
``_host()`` when a sibling needs a patchable name.
"""

from __future__ import annotations

from typing import Sequence

from infona_client.nlp.cypher_generate import (
    _SAFE_PROP_RE,
    _ontology_section_for_type,
    _relationship_specs_in_section,
    extract_type_names_from_ontology,
)
from infona_client.nlp.ontology_subgraph_types import (
    OntologyPath,
    _DEFAULT_MAX_HOPS,
    _HARD_MAX_HOPS,
)


def _clamp_max_hops(max_hops: int | None) -> int:
    n = _DEFAULT_MAX_HOPS if max_hops is None else int(max_hops)
    if n < 1:
        n = 1
    return min(n, _HARD_MAX_HOPS)


def _outbound_edges(
    ontology_summary: str,
    type_names: Sequence[str],
) -> dict[str, list[tuple[str, str | None]]]:
    """type → list of (rel_attr, range_type) for relationship edges only."""
    graph: dict[str, list[tuple[str, str | None]]] = {}
    for tname in type_names:
        section = _ontology_section_for_type(tname, ontology_summary)
        if not section:
            continue
        edges: list[tuple[str, str | None]] = []
        seen: set[tuple[str, str | None]] = set()
        for leaf, range_type in _relationship_specs_in_section(section):
            if not leaf or not _SAFE_PROP_RE.match(leaf):
                continue
            key = (leaf, range_type)
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)
        if edges:
            graph[tname] = edges
    return graph


def candidate_ontology_paths(
    ontology_summary: str,
    type_names: Sequence[str] | None = None,
    *,
    domain_type: str | None = None,
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> list[OntologyPath]:
    """Enumerate relationship paths from the ontology summary text.

    Only **relationship** edges (not pure literals). When ``domain_type`` is
    set, only paths starting at that type are returned.

    Enumerates 1-hop edges and multi-hop chains up to ``max_hops`` (default 2,
    hard-capped at 3). Multi-hop walks require typed intermediate ranges so
    the next hop can be resolved from the ontology graph.
    """
    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )
    if not names:
        names = extract_type_names_from_ontology(ontology_summary)

    hop_limit = _clamp_max_hops(max_hops)
    graph = _outbound_edges(ontology_summary, names)

    start_types = [domain_type] if domain_type else list(graph.keys())
    # If domain_type was requested but not in graph keys (no edges), still empty.
    if domain_type and domain_type not in graph:
        # Case-insensitive lookup
        lower_map = {k.lower(): k for k in graph}
        resolved = lower_map.get(domain_type.lower())
        start_types = [resolved] if resolved else []

    paths: list[OntologyPath] = []
    seen: set[tuple] = set()

    def _add(path: OntologyPath) -> None:
        key = (
            path.domain_type,
            path.rel_attr,
            path.range_type,
            path.chain,
        )
        if key in seen:
            return
        seen.add(key)
        paths.append(path)

    for start in start_types:
        if not start or start not in graph:
            continue
        # BFS: state = (current_type, list of (rel, range) hops so far)
        # hop length 1..hop_limit
        stack: list[tuple[str, list[tuple[str, str | None]]]] = []
        for rel, rng in graph[start]:
            stack.append((rng or "", [(rel, rng)]))
            _add(
                OntologyPath(
                    domain_type=start,
                    rel_attr=rel,
                    range_type=rng,
                    chain=(),
                )
            )

        while stack:
            cur_type, hops = stack.pop()
            if len(hops) >= hop_limit:
                continue
            if not cur_type:
                continue
            # Resolve current type for outbound edges (case-insensitive).
            next_key = cur_type if cur_type in graph else None
            if next_key is None:
                lower_map = {k.lower(): k for k in graph}
                next_key = lower_map.get(cur_type.lower())
            if next_key is None:
                continue
            for rel, rng in graph[next_key]:
                new_hops = hops + [(rel, rng)]
                first_rel, first_rng = new_hops[0]
                rest = tuple(new_hops[1:])
                _add(
                    OntologyPath(
                        domain_type=start,
                        rel_attr=first_rel,
                        range_type=first_rng,
                        chain=rest,
                    )
                )
                if len(new_hops) < hop_limit and rng:
                    stack.append((rng, new_hops))

    return paths
