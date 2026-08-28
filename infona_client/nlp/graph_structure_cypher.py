"""Deterministic graph-op Cypher: compile from NL slots + repair LLM near-misses.

Tiny models fail these shapes by inventing property-graph hops (``[:member_of]``,
``:KgNode``, inverted SUBJECT). When the question parses, we fill frozen
Assertion Cypher instead of trusting the generated body.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

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

_EXISTS_HINT_RE = re.compile(r"(?i)triplet?\s+fact\s+present")
# Two-comma (s, p, o) parens only — "(Yes/No)" has no commas and must not
# glue onto the following triple as "Yes/No)? (Widget A".
_TRIPLE_PARENS_RE = re.compile(
    r"\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)"
)
_PATH_WHAT_FROM_RE = re.compile(
    r"(?i)what\s+is\s+the\s+shortest\s+path\s+from\s+(.+?)\s+to\s+(.+?)"
    r"(?:[.?\n]|$)"
)
_PATH_FROM_RE = re.compile(
    r"(?i)shortest\s+path\s+from\s+(.+?)\s+to\s+(.+?)(?:[.?\n]|$)"
)
_PATH_BETWEEN_RE = re.compile(
    r"(?i)shortest\s+path\s+between\s+(.+?)\s+and\s+(.+?)(?:[.?\n]|$)"
)
_DEGREE_RE = re.compile(
    r"(?i)highest\s+number\s+of\s+(outgoing|incoming|total)\s+"
    r"(?:edges|relations|degree)"
)
_REL_COUNT_RE = re.compile(
    r"(?i)how\s+many\s+(incoming|outgoing)\s+relations\s+of\s+type\s+"
    r"['\"]([^'\"]+)['\"]\s+does\s+(.+?)\s+have"
)
_NEIGHBOR_RE = re.compile(
    r"(?i)directly\s+connected\s+entities\s+to\s+"
    r"(?:['\"](?P<quoted>[^'\"]+)['\"]|(?P<bare>.+?))"
    r"\s+have\s+an\s+outgoing\s+property\s+of\s+type\s+"
    r"['\"](?P<rel>[^'\"]+)['\"]"
)

# Canned instruction example in the published shortest-path prompt template.
_PATH_EXAMPLE_PAIRS = frozenset(
    {("argentina", "mexico"), ("mexico", "argentina")}
)

_BIND = (
    "replace(toLower(coalesce({a}.display_name, {a}.display_label, "
    "{a}.name, '')), '_', ' ') = replace(toLower(${p}), '_', ' ')"
)
_REL = (
    "replace(toLower(p.name), '_', ' ') = replace(toLower($rel_attr), '_', ' ')"
)
_SCOPE = "{tenant_id: $tenant_id, kg: $kg}"


def _clean_slot(raw: str) -> str:
    t = re.sub(r"\s+", " ", (raw or "").strip().strip("\"'"))
    t = re.sub(r"\s*[.?]\s*$", "", t)
    # Truncate instruction tails ("For example", "Answer:").
    t = re.split(r"(?i)\s+(?:for example|answer:|your response)\b", t, maxsplit=1)[0]
    return t.strip(" ,")


def _entity_bind(alias: str, param: str) -> str:
    return _BIND.format(a=alias, p=param)


def _one_line(cypher: str) -> str:
    return " ".join((cypher or "").split())


def _is_path_example_pair(start: str, end: str) -> bool:
    pair = (start.lower(), end.lower())
    if pair in _PATH_EXAMPLE_PAIRS:
        return True
    return "through bolivia" in end.lower()


def _iter_path_pairs(regex: re.Pattern[str], q: str) -> Iterator[tuple[str, str]]:
    for m in regex.finditer(q):
        start, end = _clean_slot(m.group(1)), _clean_slot(m.group(2))
        if start and end and start.lower() != "the" and not _is_path_example_pair(
            start, end
        ):
            yield start, end


def _path_slots(q: str) -> tuple[str, str] | None:
    """Prefer the explicit question line; never the Argentina/Mexico example."""
    for regex in (_PATH_WHAT_FROM_RE, _PATH_FROM_RE, _PATH_BETWEEN_RE):
        pairs = list(_iter_path_pairs(regex, q))
        if pairs:
            return pairs[-1]
    return None


def _exists_slots(q: str) -> tuple[str, str, str] | None:
    if not _EXISTS_HINT_RE.search(q):
        return None
    matches = list(_TRIPLE_PARENS_RE.finditer(q))
    if not matches:
        return None
    frm, rel, to = (_clean_slot(matches[-1].group(i)) for i in (1, 2, 3))
    if not (frm and rel and to) or frm.lower() in {"yes", "no"}:
        return None
    return frm, rel, to


def compile_graph_op_cypher(
    question: str,
) -> tuple[str, dict[str, Any]] | None:
    """If the NL parses as a known graph-op, return frozen Assertion Cypher + params."""
    q = question or ""
    exists = _exists_slots(q)
    if exists is not None:
        frm, rel, to = exists
        cypher = _one_line(
            f"""
MATCH (from_e:Entity {_SCOPE}) WHERE {_entity_bind("from_e", "from_name")}
MATCH (to_e:Entity {_SCOPE}) WHERE {_entity_bind("to_e", "to_name")}
MATCH (a:Assertion {_SCOPE})-[:SUBJECT]->(from_e)
MATCH (a)-[:OBJECT]->(to_e)
MATCH (a)-[:PREDICATE]->(p:Property {_SCOPE})
WHERE {_REL}
RETURN CASE WHEN count(a) > 0 THEN 'Yes' ELSE 'No' END AS answer
"""
        )
        return cypher, {"from_name": frm, "to_name": to, "rel_attr": rel}

    path = _path_slots(q)
    if path is not None:
        start, end = path
        cypher = _one_line(
            f"""
MATCH (s:Entity {_SCOPE}) WHERE {_entity_bind("s", "start_name")}
MATCH (t:Entity {_SCOPE}) WHERE {_entity_bind("t", "end_name")}
MATCH p = shortestPath((s)-[:SUBJECT|OBJECT*..12]-(t))
WITH [n IN nodes(p) WHERE n:Entity | coalesce(n.display_name, n.display_label, n.name, '')] AS labels
RETURN 'SHORTEST PATH: [' + reduce(acc = '', x IN labels | acc + CASE WHEN acc = '' THEN "'" + toString(x) + "'" ELSE ", '" + toString(x) + "'" END) + ']' AS answer
"""
        )
        return cypher, {"start_name": start, "end_name": end}

    m = _DEGREE_RE.search(q)
    if m:
        direction = m.group(1).lower()
        if direction == "incoming":
            hop = f"MATCH (a:Assertion {_SCOPE})-[:OBJECT]->(e)"
        elif direction == "total":
            hop = (
                f"MATCH (a:Assertion {_SCOPE})-[:SUBJECT|OBJECT]->(e) "
                f"MATCH (a)-[:OBJECT]->(:Entity {_SCOPE})"
            )
        else:
            hop = (
                f"MATCH (a:Assertion {_SCOPE})-[:SUBJECT]->(e) "
                f"MATCH (a)-[:OBJECT]->(:Entity {_SCOPE})"
            )
        cypher = _one_line(
            f"""
MATCH (e:Entity {_SCOPE})
{hop}
WITH e, count(DISTINCT a) AS deg
ORDER BY deg DESC LIMIT 1
RETURN 'Answer: ' + coalesce(e.display_name, e.display_label, e.name) AS answer
"""
        )
        return cypher, {}

    m = _REL_COUNT_RE.search(q)
    if m:
        direction = m.group(1).lower()
        rel, name = _clean_slot(m.group(2)), _clean_slot(m.group(3))
        if rel and name:
            edge = "OBJECT" if direction == "incoming" else "SUBJECT"
            cypher = _one_line(
                f"""
MATCH (e:Entity {_SCOPE}) WHERE {_entity_bind("e", "entity_name")}
MATCH (a:Assertion {_SCOPE})-[:{edge}]->(e)
MATCH (a)-[:OBJECT]->(:Entity {_SCOPE})
MATCH (a)-[:PREDICATE]->(p:Property {_SCOPE})
WHERE {_REL}
RETURN 'Answer: ' + toString(count(DISTINCT a)) AS answer
"""
            )
            return cypher, {"entity_name": name, "rel_attr": rel}

    m = _NEIGHBOR_RE.search(q)
    if m:
        name = _clean_slot(m.group("quoted") or m.group("bare") or "")
        rel = _clean_slot(m.group("rel") or "")
        if name and rel:
            cypher = _one_line(
                f"""
MATCH (e:Entity {_SCOPE}) WHERE {_entity_bind("e", "entity_name")}
MATCH (hop:Assertion {_SCOPE})-[:SUBJECT|OBJECT]->(e)
MATCH (hop)-[:SUBJECT|OBJECT]->(nbr:Entity {_SCOPE})
WHERE nbr <> e
WITH DISTINCT nbr
MATCH (out:Assertion {_SCOPE})-[:SUBJECT]->(nbr)
MATCH (out)-[:OBJECT]->(:Entity {_SCOPE})
MATCH (out)-[:PREDICATE]->(p:Property {_SCOPE})
WHERE {_REL}
RETURN 'Answer: ' + toString(count(DISTINCT nbr)) AS answer
"""
            )
            return cypher, {"entity_name": name, "rel_attr": rel}
    return None


def compiled_graph_op_params(
    existing: dict[str, Any] | None, extra: dict[str, Any]
) -> dict[str, Any]:
    """Keep only scope keys from the LLM; compiled slots replace the rest."""
    src = existing or {}
    keep = {k: src[k] for k in ("tenant_id", "kg") if k in src}
    return {**keep, **extra}


def apply_compiled_graph_op(
    question: str, cypher: str, params: dict[str, Any]
) -> tuple[str, dict[str, Any], bool]:
    """Overwrite LLM Cypher with a frozen graph-op body when NL slots parse.

    Always-LLM: this runs after generation. Returns ``(cypher, params, compiled)``.
    """
    planned = compile_graph_op_cypher(question)
    if planned is None:
        return cypher, params, False
    body, extra = planned
    return body, compiled_graph_op_params(params, extra), True


def repair_graph_structure_cypher(
    cypher: str, question: str = ""
) -> tuple[str, bool, dict[str, Any] | None]:
    """Return ``(cypher, changed, extra_params)``.

    ``extra_params`` is a dict (possibly empty) when the question compiled to a
    known graph-op — callers MUST replace LLM params with it. ``None`` means
    mechanical rewrite only; leave params alone.
    """
    planned = compile_graph_op_cypher(question)
    if planned is not None:
        body, extra = planned
        return body, body != (cypher or ""), extra

    c = cypher or ""
    orig = c
    extra = None
    c = c.replace(":KgNode", ":Entity")
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
    return c, c != orig, extra
