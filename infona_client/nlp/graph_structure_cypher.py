"""Deterministic repairs for graph-structure Cypher the LLM almost gets right."""
from __future__ import annotations

import re

_SHORTEST_RE = re.compile(r"(?i)\bshortestpath\s*\(")
_NODES_P_WITH_RE = re.compile(
    r"(?i)\bWITH\s+nodes\s*\(\s*p\s*\)\s+AS\s+(\w+)"
)
_COALESCE_NAME_RE = re.compile(
    r"(?i)coalesce\(((?:[^()]*\.(?:display_name|display_label|name)\s*,\s*)+"
    r"[^()]*\.(?:display_name|display_label|name))\)"
)
_DIRECTLY_CONNECTED_RE = re.compile(r"(?i)\bdirectly\s+connected\b")
_START_SUBJECT_RE = re.compile(
    r"-\[:SUBJECT\]->\((e|start_node|startNode|entity)\b"
)
_NBR_OBJECT_RE = re.compile(
    r"-\[:OBJECT\]->\((neighbor|nbr|direct_neighbor|connected_entity|"
    r"related_entity)\b"
)


def repair_graph_structure_cypher(
    cypher: str, question: str = ""
) -> tuple[str, bool]:
    """Return ``(cypher, changed)`` after mechanical graph-structure repairs.

    - shortestPath: collect Entity nodes only; never let null Assertion names
      NULL the concatenated answer string.
    - "directly connected" neighbor counts: first hop is undirected
      ``[:SUBJECT|OBJECT]``, not outgoing-only.
    """
    c = cypher or ""
    orig = c
    if _SHORTEST_RE.search(c):
        c = _NODES_P_WITH_RE.sub(
            r"WITH [n IN nodes(p) WHERE n:Entity] AS \1", c
        )
        c = _COALESCE_NAME_RE.sub(
            lambda m: (
                m.group(0)
                if "''" in m.group(1)
                else f"coalesce({m.group(1)}, '')"
            ),
            c,
        )
        # Official shortest-path answers quote each label.
        c = re.sub(
            r"THEN\s+toString\(coalesce\(([^()]*)\)\)",
            r'''THEN "'" + toString(coalesce(\1)) + "'"''',
            c,
        )
        c = re.sub(
            r"ELSE\s+', '\s*\+\s*toString\(coalesce\(([^()]*)\)\)",
            r'''ELSE ", '" + toString(coalesce(\1)) + "'"''',
            c,
        )
    if (
        _DIRECTLY_CONNECTED_RE.search(question or "")
        and "[:SUBJECT|OBJECT]" not in c
    ):
        c = _START_SUBJECT_RE.sub(r"-[:SUBJECT|OBJECT]->(\1", c, count=1)
        c = _NBR_OBJECT_RE.sub(r"-[:SUBJECT|OBJECT]->(\1", c, count=1)
        if "<>" not in c:
            c = re.sub(
                r"(MATCH\s+\([^)]*\)-\[:SUBJECT\|OBJECT\]->\((neighbor)(:Entity\b[^)]*)\))",
                r"\1 WHERE \2 <> e",
                c,
                count=1,
            )
    changed = c != orig
    return c, changed
