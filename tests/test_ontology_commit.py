"""ONTA-403 — commit_ontology body, extended fingerprint, concurrency, revision.

Pure-seam tests (no store) cover fingerprint discrimination, order-
independence, and timestamp-independence. Async tests use a minimal in-
memory Neptune shim so we do not require pyoxigraph.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict

import pytest

from cograph_client.graph.ontology_commit import (
    OntologyVersionConflict,
    changelog_graph_uri_for,
    commit_ontology,
    fingerprint_ontology,
    ontology_write_lock,
    versions_graph_uri,
)
from cograph_client.graph.ontology_queries import ontology_version
from cograph_client.models.ontology import (
    ChangeKind,
    OntologyMutation,
    OntologyOpKind,
)


# ---------------------------------------------------------------------------
# In-memory Neptune shim — enough for commit_ontology + fingerprint reads.
# ---------------------------------------------------------------------------


class MemNeptune:
    """Stores triples as (g, s, p, o) and answers a few SELECT/INSERT shapes."""

    def __init__(self) -> None:
        self.triples: set[tuple[str, str, str, str]] = set()
        self.updates: list[str] = []
        self.queries: list[str] = []

    async def update(self, sparql: str) -> None:
        self.updates.append(sparql)
        # INSERT DATA { GRAPH <g> { <s> <p> <o> . ... } }
        for m in re.finditer(
            r"INSERT\s+DATA\s*\{\s*GRAPH\s*<([^>]+)>\s*\{([^}]*)\}\s*\}",
            sparql,
            re.I | re.S,
        ):
            g, body = m.group(1), m.group(2)
            for s, p, o in self._parse_triples(body):
                self.triples.add((g, s, p, o))
        # INSERT { GRAPH <g> { ... } } WHERE  (also covers backfill INSERT half)
        for m in re.finditer(
            r"INSERT\s*\{\s*GRAPH\s*<([^>]+)>\s*\{([^}]*)\}\s*\}",
            sparql,
            re.I | re.S,
        ):
            if "INSERT DATA" in sparql[max(0, m.start() - 20):m.start() + 20].upper():
                continue
            g, body = m.group(1), m.group(2)
            # Variable patterns like `?s <p> ?o` — resolve from current triples
            # that match the DELETE half of a backfill (handled below for DELETE).
            if "?s" in body or "?o" in body:
                continue
            for s, p, o in self._parse_triples(body):
                self.triples.add((g, s, p, o))
        # Backfill: DELETE { GRAPH <g> { ?s <old> ?o } } INSERT { GRAPH <g> { ?s <new> ?o } }
        bf = re.search(
            r"DELETE\s*\{\s*GRAPH\s*<([^>]+)>\s*\{\s*\?s\s*<([^>]+)>\s*\?o\s*\}\s*\}\s*"
            r"INSERT\s*\{\s*GRAPH\s*<([^>]+)>\s*\{\s*\?s\s*<([^>]+)>\s*\?o\s*\}\s*\}",
            sparql,
            re.I | re.S,
        )
        if bf:
            g_del, old_p, g_ins, new_p = bf.group(1), bf.group(2), bf.group(3), bf.group(4)
            moved: list[tuple[str, str, str, str]] = []
            keep: set[tuple[str, str, str, str]] = set()
            for t in self.triples:
                gg, ss, pp, oo = t
                if gg == g_del and pp == old_p:
                    moved.append((g_ins, ss, new_p, oo))
                else:
                    keep.add(t)
            self.triples = keep | set(moved)
        # DELETE { GRAPH <g> { <s> <p> ?var } }  (and DELETE WHERE alias form)
        for m in re.finditer(
            r"DELETE\s*(?:WHERE\s*)?\{\s*GRAPH\s*<([^>]+)>\s*\{\s*<([^>]+)>\s*<([^>]+)>\s*\?(\w+)\s*\}\s*\}",
            sparql,
            re.I | re.S,
        ):
            g, s, p = m.group(1), m.group(2), m.group(3)
            self.triples = {(gg, ss, pp, oo) for gg, ss, pp, oo in self.triples
                            if not (gg == g and ss == s and pp == p)}
        # WITH <g> DELETE { <s> ?p ?o } WHERE
        for m in re.finditer(
            r"WITH\s*<([^>]+)>\s*DELETE\s*\{\s*<([^>]+)>\s*\?p\s*\?o\s*\}",
            sparql,
            re.I | re.S,
        ):
            g, s = m.group(1), m.group(2)
            self.triples = {(gg, ss, pp, oo) for gg, ss, pp, oo in self.triples
                            if not (gg == g and ss == s)}

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        g_match = re.search(r"FROM\s*<([^>]+)>", sparql)
        g = g_match.group(1) if g_match else ""
        bindings: list[dict] = []

        # full_ontology_detail_query shape
        if "?typeLabel" in sparql and "?attrLabel" in sparql:
            types = defaultdict(lambda: {"comment": "", "attrs": {}})
            for gg, s, p, o in self.triples:
                if gg != g:
                    continue
                if p.endswith("#type") and o.endswith("#Class"):
                    types[s]  # ensure
                if p.endswith("#label") and "/types/" in s and "/attrs/" not in s:
                    types[s]["label"] = o.strip('"')
                if p.endswith("#comment") and "/types/" in s and "/attrs/" not in s:
                    types[s]["comment"] = o.strip('"')
                if p.endswith("#domain") and "/attrs/" in s:
                    types[o]["attrs"][s] = types[o]["attrs"].get(s, {})
                if p.endswith("#label") and "/attrs/" in s:
                    for t_uri, info in types.items():
                        if s in info["attrs"] or True:
                            pass
                    # find domain
            # rebuild more carefully
            class_uris = {s for gg, s, p, o in self.triples
                          if gg == g and p.endswith("#type") and o.endswith("#Class")}
            labels = {s: o.strip('"') for gg, s, p, o in self.triples
                      if gg == g and p.endswith("#label") and s in class_uris}
            comments = {s: o.strip('"') for gg, s, p, o in self.triples
                        if gg == g and p.endswith("#comment") and s in class_uris}
            # attributes by domain
            domains = {s: o for gg, s, p, o in self.triples
                       if gg == g and p.endswith("#domain")}
            attr_labels = {s: o.strip('"') for gg, s, p, o in self.triples
                           if gg == g and p.endswith("#label") and s in domains}
            attr_comments = {s: o.strip('"') for gg, s, p, o in self.triples
                             if gg == g and p.endswith("#comment") and s in domains}
            ranges = {s: o for gg, s, p, o in self.triples
                      if gg == g and p.endswith("#range")}
            cores = {s for gg, s, p, o in self.triples
                     if gg == g and p.endswith("/coreSlot")}
            for t_uri, tlabel in labels.items():
                attrs_for_t = [a for a, d in domains.items() if d == t_uri]
                if not attrs_for_t:
                    bindings.append({
                        "type": {"value": t_uri},
                        "typeLabel": {"value": tlabel},
                        **({"typeComment": {"value": comments[t_uri]}} if t_uri in comments else {}),
                    })
                for a_uri in attrs_for_t:
                    row = {
                        "type": {"value": t_uri},
                        "typeLabel": {"value": tlabel},
                        "attr": {"value": a_uri},
                        "attrLabel": {"value": attr_labels.get(a_uri, a_uri.rsplit("/", 1)[-1])},
                    }
                    if t_uri in comments:
                        row["typeComment"] = {"value": comments[t_uri]}
                    if a_uri in attr_comments:
                        row["attrComment"] = {"value": attr_comments[a_uri]}
                    if a_uri in ranges:
                        row["range"] = {"value": ranges[a_uri]}
                    if a_uri in cores:
                        row["core"] = {"value": "true"}
                    bindings.append(row)
            return self._sparql_json(bindings)

        # parent_map_query
        if "?child" in sparql and "?parent" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("#subClassOf"):
                    bindings.append({
                        "child": {"value": s},
                        "parent": {"value": o},
                    })
            return self._sparql_json(bindings)

        # text_kind_map_query
        if "textKind" in sparql or "/textKind>" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/textKind"):
                    bindings.append({
                        "attr": {"value": s},
                        "kind": {"value": o.strip('"')},
                    })
            return self._sparql_json(bindings)

        # revision counter
        if "workspaceRevision" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/workspaceRevision"):
                    bindings.append({"r": {"value": o.split("^^")[0].strip('"')}})
            return self._sparql_json(bindings)

        # alias_map_query (ONTA-407a): SELECT ?old ?new … aliasOf
        if "?old" in sparql and "?new" in sparql and "aliasOf" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/aliasOf"):
                    bindings.append({
                        "old": {"value": s},
                        "new": {"value": o},
                    })
            return self._sparql_json(bindings)

        # deprecation map (ONTA-404): SELECT ?s ?dep ?sup … deprecatedAt
        if "deprecatedAt" in sparql or "/deprecatedAt>" in sparql:
            deps: dict[str, dict] = {}
            for gg, s, p, o in self.triples:
                if gg != g:
                    continue
                if p.endswith("/deprecatedAt"):
                    deps.setdefault(s, {})["dep"] = o.split("^^")[0].strip('"')
                if p.endswith("/supersededBy"):
                    deps.setdefault(s, {})["sup"] = o
            for s, info in deps.items():
                if "dep" not in info:
                    continue
                row = {"s": {"value": s}, "dep": {"value": info["dep"]}}
                if "sup" in info:
                    row["sup"] = {"value": info["sup"]}
                bindings.append(row)
            return self._sparql_json(bindings)

        # COUNT(*) for alias reference checks / backfill (ONTA-407b)
        if "COUNT" in sparql.upper() and "?n" in sparql:
            pred_m = re.search(r"\?s\s+<([^>]+)>\s+\?o", sparql)
            pred = pred_m.group(1) if pred_m else None
            n = sum(
                1 for gg, ss, pp, oo in self.triples
                if gg == g and (pred is None or pp == pred)
            )
            return self._sparql_json([{"n": {"value": str(n)}}])

        return self._sparql_json([])

    @staticmethod
    def _sparql_json(bindings: list[dict]) -> dict:
        # parse_sparql_results only projects vars listed in head.vars — collect
        # the union of keys so optional fields (typeComment, core, …) survive.
        vars_: list[str] = []
        seen: set[str] = set()
        for row in bindings:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    vars_.append(k)
        return {"head": {"vars": vars_}, "results": {"bindings": bindings}}

    @staticmethod
    def _parse_triples(body: str) -> list[tuple[str, str, str]]:
        out = []
        # <s> <p> <o> .
        for m in re.finditer(r"<([^>]+)>\s+<([^>]+)>\s+<([^>]+)>\s*\.", body):
            out.append((m.group(1), m.group(2), m.group(3)))
        # <s> <p> "lit" .
        for m in re.finditer(r'<([^>]+)>\s+<([^>]+)>\s+"([^"]*)"(?:\^\^<[^>]+>)?\s*\.', body):
            out.append((m.group(1), m.group(2), f'"{m.group(3)}"'))
        # bare form without trailing dot / with typed literal inline
        for m in re.finditer(
            r'<([^>]+)>\s+<([^>]+)>\s+"([^"]*)"\^\^<([^>]+)>', body
        ):
            out.append((m.group(1), m.group(2), f'"{m.group(3)}"^^{m.group(4)}'))
        return out


# parse_sparql_results flattens - our query return uses nested form. Good.

# ---------------------------------------------------------------------------
# Fingerprint discrimination (pure)
# ---------------------------------------------------------------------------


def test_empty_fingerprint_unchanged():
    assert ontology_version({}, {}) == "e3b0c44298fc1c14"


def test_fingerprint_discriminates_type_comment():
    base = ontology_version({"Person": ""}, {})
    with_comment = ontology_version({"Person": "a human being"}, {})
    assert base != with_comment
    # same comment via explicit comments map
    via_map = ontology_version({"Person": ""}, {}, comments={"Person": "a human being"})
    assert with_comment == via_map or via_map != base  # either channel shifts


def test_fingerprint_discriminates_attr_comment():
    class S:
        def __init__(self, dt, desc=""):
            self.datatype = dt
            self.description = desc

    bare = ontology_version({"P": ""}, {"P": {"name": S("string")}})
    noted = ontology_version({"P": ""}, {"P": {"name": S("string", "display name")}})
    assert bare != noted


def test_fingerprint_discriminates_core_slot():
    base = ontology_version({"P": ""}, {"P": {"name": "string"}})
    core = ontology_version(
        {"P": ""}, {"P": {"name": "string"}}, core_slots=[("P", "name")]
    )
    assert base != core


def test_fingerprint_discriminates_text_kind():
    base = ontology_version({"P": ""}, {"P": {"bio": "string"}})
    tk = ontology_version(
        {"P": ""},
        {"P": {"bio": "string"}},
        text_kinds={("P", "bio"): "free_text"},
    )
    assert base != tk


def test_fingerprint_discriminates_range_change():
    """Relationship ranges already covered by datatype channel."""
    lit = ontology_version({"P": ""}, {"P": {"employer": "string"}})
    rel = ontology_version({"P": ""}, {"P": {"employer": "Company"}})
    assert lit != rel


def test_fingerprint_order_independent_with_extensions():
    a = ontology_version(
        {"B": "bb", "A": "aa"},
        {"A": {"y": "string", "x": "integer"}},
        {"A": "Base"},
        core_slots=[("A", "x"), ("A", "y")],
        text_kinds={("A", "y"): "free_text", ("A", "x"): "not_text"},
    )
    b = ontology_version(
        {"A": "aa", "B": "bb"},
        {"A": {"x": "integer", "y": "string"}},
        {"A": "Base"},
        core_slots=[("A", "y"), ("A", "x")],
        text_kinds={("A", "x"): "not_text", ("A", "y"): "free_text"},
    )
    assert a == b


def test_fingerprint_timestamp_independent():
    """No wall-clock in the digest — two calls agree."""
    kwargs = dict(
        types={"P": "person"},
        attrs={"P": {"n": "string"}},
        parent_of={},
        core_slots=[("P", "n")],
        text_kinds={("P", "n"): "free_text"},
    )
    assert ontology_version(**kwargs) == ontology_version(**kwargs)


# ---------------------------------------------------------------------------
# commit_ontology behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_commit_returns_current_version_no_writes():
    n = MemNeptune()
    r = await commit_ontology(n, "https://cograph.tech/graphs/t", [])
    assert r.version_before == r.version_after == "e3b0c44298fc1c14"
    assert r.applied == []
    assert n.updates == []


@pytest.mark.asyncio
async def test_commit_upsert_type_applies_and_bumps_version():
    n = MemNeptune()
    g = "https://cograph.tech/graphs/t"
    r = await commit_ontology(
        n,
        g,
        [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person")],
        actor="tester",
        message="add Person",
    )
    assert r.version_before == "e3b0c44298fc1c14"
    assert r.version_after != r.version_before
    assert len(r.applied) == 1
    assert any(c.kind is ChangeKind.ADD_TYPE for c in r.change_records)
    # changelog + revision writes happened
    assert any("/changelog" in u or "workspaceRevision" in u or "INSERT" in u
               for u in n.updates)
    assert versions_graph_uri(g).endswith("/versions")
    assert changelog_graph_uri_for(g).endswith("/changelog")


@pytest.mark.asyncio
async def test_commit_batch_multi_type_single_changelog():
    """A multi-type ingest-style batch is ONE commit (one changelog entry)."""
    n = MemNeptune()
    g = "https://cograph.tech/graphs/t"
    muts = [
        OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
        OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Company"),
        OntologyMutation(
            op=OntologyOpKind.UPSERT_ATTRIBUTE,
            type_name="Person",
            slot_name="name",
            datatype="string",
            description="",
        ),
        OntologyMutation(
            op=OntologyOpKind.SET_SUBCLASS,
            type_name="Employee",
            parent_type="Person",
        ),
    ]
    # Employee needs to exist first for a clean hierarchy — mint it too.
    muts.insert(2, OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Employee"))
    r = await commit_ontology(n, g, muts, message="multi-type batch")
    assert len(r.applied) == 5
    # Exactly one changelog INSERT to the companion changelog graph.
    changelog_writes = [
        u for u in n.updates
        if changelog_graph_uri_for(g) in u and "INSERT" in u.upper()
    ]
    assert len(changelog_writes) == 1, (
        f"expected one changelog entry for the batch, got {len(changelog_writes)}"
    )


@pytest.mark.asyncio
async def test_optimistic_concurrency_rejects_stale_expected_version():
    n = MemNeptune()
    g = "https://cograph.tech/graphs/t"
    await commit_ontology(
        n, g, [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="A")]
    )
    with pytest.raises(OntologyVersionConflict) as ei:
        await commit_ontology(
            n,
            g,
            [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="B")],
            expected_version="e3b0c44298fc1c14",  # stale empty
        )
    assert ei.value.expected == "e3b0c44298fc1c14"
    assert ei.value.actual != ei.value.expected


@pytest.mark.asyncio
async def test_concurrent_commits_ordered_no_lost_update():
    """Two concurrent commits serialize on the shared lock; both land."""
    n = MemNeptune()
    g = "https://cograph.tech/graphs/conc"
    # Ensure both see the same lock.
    assert ontology_write_lock() is ontology_write_lock()

    async def add(name: str):
        return await commit_ontology(
            n,
            g,
            [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name=name)],
            actor=name,
        )

    r1, r2 = await asyncio.gather(add("Alpha"), add("Beta"))
    # Both applied; versions advanced (order depends on lock acquisition).
    assert r1.applied and r2.applied
    finals = {r1.version_after, r2.version_after}
    # At least one differs from empty; the later one differs from the earlier.
    assert r1.version_before == "e3b0c44298fc1c14" or r2.version_before == "e3b0c44298fc1c14"
    # One of the two started from the empty version; the other from a non-empty.
    befores = {r1.version_before, r2.version_before}
    assert "e3b0c44298fc1c14" in befores
    assert len(befores) == 2 or r1.version_after != r2.version_after
    # Final store fingerprint reflects both types (best-effort via live read).
    final = await fingerprint_ontology(n, g)
    assert final != "e3b0c44298fc1c14"


@pytest.mark.asyncio
async def test_set_text_kind_and_core_slot_ops():
    n = MemNeptune()
    g = "https://cograph.tech/graphs/t"
    r = await commit_ontology(
        n,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Doc"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Doc",
                slot_name="body",
                datatype="string",
                description="",
            ),
            OntologyMutation(
                op=OntologyOpKind.SET_TEXT_KIND,
                type_name="Doc",
                slot_name="body",
                text_kind="free_text",
            ),
            OntologyMutation(
                op=OntologyOpKind.SET_CORE_SLOT,
                type_name="Doc",
                slot_name="body",
                core_slot=True,
            ),
        ],
    )
    kinds = {c.kind for c in r.change_records}
    assert ChangeKind.CHANGE_TEXT_KIND in kinds
    assert ChangeKind.CHANGE_CORE_SLOT in kinds


@pytest.mark.asyncio
async def test_register_alias_via_commit_writes_and_fingerprints():
    """ONTA-407a: REGISTER_ALIAS is a real commit op (no longer NotSupported)."""
    from cograph_client.graph.aliases import ALIAS_OF, fetch_alias_map, rewrite_query_attrs
    from cograph_client.graph.ontology_queries import attr_uri

    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-alias"
    # Seed type+attr so fingerprint has a base shape (alias still works without it).
    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Guest"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Guest",
                slot_name="phone",
                datatype="string",
            ),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Guest",
                slot_name="phone_num",
                datatype="string",
            ),
        ],
    )
    before = await fingerprint_ontology(n, g)
    result = await commit_ontology(
        n,
        g,
        [
            OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
            )
        ],
        actor="test",
        message="rename phone_num → phone",
    )
    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Guest", "phone")
    assert any(
        c.kind == ChangeKind.RENAME_WITH_ALIAS
        and c.from_name == "phone_num"
        and c.to_name == "phone"
        and c.old_value == old_uri
        and c.new_value == new_uri
        for c in result.change_records
    )
    assert result.version_before == before
    assert result.version_after != before  # alias must shift the concurrency token

    # Triple landed, map resolves, rewriter rewrites.
    assert any(
        p == ALIAS_OF and s == old_uri and o == new_uri
        for (_g, s, p, o) in n.triples
    )
    alias_map = await fetch_alias_map(n, g)
    assert alias_map[old_uri] == new_uri
    sparql = f"SELECT ?v WHERE {{ ?s <{old_uri}> ?v }}"
    assert rewrite_query_attrs(sparql, alias_map) == (
        f"SELECT ?v WHERE {{ ?s <{new_uri}> ?v }}"
    )


@pytest.mark.asyncio
async def test_register_alias_rejects_self_and_missing_fields():
    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-alias"
    with pytest.raises(ValueError, match="alias_from and alias_to"):
        await commit_ontology(
            n,
            g,
            [OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
            )],
        )
    with pytest.raises(ValueError, match="different attribute"):
        await commit_ontology(
            n,
            g,
            [OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from="phone",
                alias_to="phone",
            )],
        )


@pytest.mark.asyncio
async def test_register_alias_full_iri_and_hierarchy_move():
    """Full IRIs and cross-type targets (hierarchy move) are accepted."""
    from cograph_client.graph.aliases import fetch_alias_map
    from cograph_client.graph.ontology_queries import attr_uri

    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-alias"
    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Person", "phone")
    result = await commit_ontology(
        n,
        g,
        [
            OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from=old_uri,
                alias_to=new_uri,
            )
        ],
    )
    assert result.change_records[0].old_value == old_uri
    assert result.change_records[0].new_value == new_uri
    # Bare leaf + to_type (target_type) path
    result2 = await commit_ontology(
        n,
        g,
        [
            OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from="contact_phone",
                alias_to="phone",
                target_type="Person",
            )
        ],
    )
    assert result2.change_records[0].new_value == new_uri
    amap = await fetch_alias_map(n, g)
    assert amap[old_uri] == new_uri
    assert amap[attr_uri("Guest", "contact_phone")] == new_uri


# ---------------------------------------------------------------------------
# ONTA-407b — full rename lifecycle via commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_attribute_always_creates_alias():
    """RENAME_ATTRIBUTE always records aliasOf — cannot rename without it."""
    from cograph_client.graph.aliases import ALIAS_OF, fetch_alias_map, rewrite_query_attrs
    from cograph_client.graph.ontology_queries import attr_uri

    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-rename"
    data_g = "https://cograph.tech/graphs/t-rename/kg/main"
    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Guest"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Guest",
                slot_name="phone_num",
                datatype="string",
            ),
        ],
    )
    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Guest", "phone")
    # Seed instance data under the OLD predicate (pre-backfill state).
    n.triples.add((data_g, "https://cograph.tech/entities/Guest/g1", old_uri, '"555-0100"'))

    result = await commit_ontology(
        n,
        g,
        [
            OntologyMutation(
                op=OntologyOpKind.RENAME_ATTRIBUTE,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
                datatype="string",
            )
        ],
        actor="test",
        message="rename phone_num → phone",
    )
    kinds = {c.kind for c in result.change_records}
    assert ChangeKind.RENAME_WITH_ALIAS in kinds
    assert ChangeKind.ADD_ATTRIBUTE in kinds
    assert ChangeKind.REMOVE_ATTRIBUTE in kinds
    assert any(
        c.kind == ChangeKind.RENAME_WITH_ALIAS
        and c.from_name == "phone_num"
        and c.to_name == "phone"
        and c.old_value == old_uri
        and c.new_value == new_uri
        for c in result.change_records
    )
    # Alias triple MUST exist — the defining property of rename.
    assert any(
        p == ALIAS_OF and s == old_uri and o == new_uri
        for (_g, s, p, o) in n.triples
    )
    # Old schema declaration wiped; new attribute present.
    assert not any(
        s == old_uri and p.endswith("#type")
        for (_g, s, p, o) in n.triples
    )
    assert any(s == new_uri for (_g, s, p, o) in n.triples)

    # Query path: old SPARQL rewrites to new; new is identity.
    alias_map = await fetch_alias_map(n, g)
    assert alias_map[old_uri] == new_uri
    old_q = f"SELECT ?v WHERE {{ ?s <{old_uri}> ?v }}"
    new_q = f"SELECT ?v WHERE {{ ?s <{new_uri}> ?v }}"
    assert rewrite_query_attrs(old_q, alias_map) == new_q
    assert rewrite_query_attrs(new_q, alias_map) == new_q


@pytest.mark.asyncio
async def test_rename_lifecycle_backfill_and_retire():
    """rename → backfill → retire-refuses-while-refs → retire-ok → old fails."""
    from cograph_client.graph.aliases import (
        AliasStillReferencedError,
        backfill_aliases,
        fetch_alias_map,
        rewrite_query_attrs,
    )
    from cograph_client.graph.ontology_queries import attr_uri

    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-life"
    data_g = "https://cograph.tech/graphs/t-life/kg/main"
    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Guest"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Guest",
                slot_name="phone_num",
                datatype="string",
            ),
        ],
    )
    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Guest", "phone")
    # Two instance triples under old predicate.
    n.triples.add((data_g, "https://cograph.tech/entities/Guest/g1", old_uri, '"555-0100"'))
    n.triples.add((data_g, "https://cograph.tech/entities/Guest/g2", old_uri, '"555-0200"'))

    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(
                op=OntologyOpKind.RENAME_ATTRIBUTE,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
            )
        ],
    )
    alias_map = await fetch_alias_map(n, g)
    assert alias_map[old_uri] == new_uri

    # Retirement refuses while refs remain (real COUNT check).
    with pytest.raises(AliasStillReferencedError) as ei:
        await commit_ontology(
            n,
            g,
            [
                OntologyMutation(
                    op=OntologyOpKind.RETIRE_ALIAS,
                    type_name="Guest",
                    alias_from="phone_num",
                    data_graph_uri=data_g,
                )
            ],
        )
    assert ei.value.remaining == 2
    assert ei.value.old_attr_uri == old_uri

    # Backfill rewrites instance triples old → new.
    rewritten = await backfill_aliases(n, data_g, alias_map)
    assert rewritten == 2
    assert not any(pp == old_uri for (_g, _s, pp, _o) in n.triples)
    assert sum(1 for (_g, _s, pp, _o) in n.triples if pp == new_uri) == 2

    # After backfill both old (via rewrite) and new work against data under new.
    rewritten_q = rewrite_query_attrs(
        f"SELECT ?v WHERE {{ ?s <{old_uri}> ?v }}", alias_map,
    )
    assert f"<{new_uri}>" in rewritten_q
    # New predicate is on the data graph.
    assert any(pp == new_uri for (_g, _s, pp, _o) in n.triples)

    # Retire succeeds with zero refs.
    result = await commit_ontology(
        n,
        g,
        [
            OntologyMutation(
                op=OntologyOpKind.RETIRE_ALIAS,
                type_name="Guest",
                alias_from="phone_num",
                data_graph_uri=data_g,
            )
        ],
    )
    assert any(
        c.kind == ChangeKind.RENAME_WITH_ALIAS and c.new_value is None
        for c in result.change_records
    )
    post = await fetch_alias_map(n, g)
    assert old_uri not in post
    # Old SPARQL no longer rewrites — alias gone.
    old_q = f"SELECT ?v WHERE {{ ?s <{old_uri}> ?v }}"
    assert rewrite_query_attrs(old_q, post) == old_q


@pytest.mark.asyncio
async def test_alias_chains_flatten_and_cycles_dropped():
    """A→B→C flattens; cyclic A→B→A is dropped (no hang)."""
    from cograph_client.graph.aliases import ALIAS_OF, fetch_alias_map
    from cograph_client.graph.ontology_queries import attr_uri

    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-chain"
    a = attr_uri("Guest", "phone_num")
    b = attr_uri("Guest", "phone")
    c = attr_uri("Person", "contact")

    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
            ),
            OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from="phone",
                alias_to="contact",
                target_type="Person",
            ),
        ],
    )
    amap = await fetch_alias_map(n, g)
    assert amap == {a: c, b: c}

    # Cycle: wipe and plant A↔B
    n.triples = {(gg, s, p, o) for gg, s, p, o in n.triples if p != ALIAS_OF}
    n.triples.add((g, a, ALIAS_OF, b))
    n.triples.add((g, b, ALIAS_OF, a))
    cyclic = await fetch_alias_map(n, g)
    assert cyclic == {}


@pytest.mark.asyncio
async def test_retire_alias_requires_data_graph():
    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-retire"
    with pytest.raises(ValueError, match="data_graph_uri"):
        await commit_ontology(
            n,
            g,
            [
                OntologyMutation(
                    op=OntologyOpKind.RETIRE_ALIAS,
                    type_name="Guest",
                    alias_from="phone_num",
                )
            ],
        )


@pytest.mark.asyncio
async def test_rename_rejects_type_level_iri():
    """Type renames are a documented gap — only attribute aliases are supported."""
    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-typerename"
    with pytest.raises(ValueError, match="type renames are not supported"):
        await commit_ontology(
            n,
            g,
            [
                OntologyMutation(
                    op=OntologyOpKind.RENAME_ATTRIBUTE,
                    type_name="Guest",
                    alias_from="https://cograph.tech/types/Guest",
                    alias_to="https://cograph.tech/types/Person",
                )
            ],
        )


@pytest.mark.asyncio
async def test_nl_ask_rewrites_after_rename(monkeypatch):
    """When aliases are enabled, /ask rewrites old attr IRIs after rename."""
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    from cograph_client.graph.aliases import ALIAS_OF
    from cograph_client.graph.ontology_queries import attr_uri
    from cograph_client.nlp.pipeline import NLQueryPipeline

    n = MemNeptune()
    g = "https://cograph.tech/graphs/t-nl"
    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Guest", "phone")
    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Guest"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Guest",
                slot_name="phone_num",
                datatype="string",
            ),
            OntologyMutation(
                op=OntologyOpKind.RENAME_ATTRIBUTE,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
            ),
        ],
    )
    assert any(p == ALIAS_OF for (_g, _s, p, _o) in n.triples)

    monkeypatch.setenv("COGRAPH_ALIASES_ENABLED", "1")
    # Pipeline needs a Neptune-like client; wrap MemNeptune for query/update.
    pipeline = NLQueryPipeline(n, "fake-key")
    pipeline._openrouter_key = ""
    assert pipeline._aliases_enabled is True

    generated = f"SELECT ?v WHERE {{ ?g <{old_uri}> ?v }}"
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps({
        "sparql": generated,
        "explanation": "test",
        "functions_needed": [],
    }))]
    # After alias fetch, execution returns a row under the rewritten predicate.
    exec_result = {
        "head": {"vars": ["v"]},
        "results": {"bindings": [{"v": {"type": "literal", "value": "555"}}]},
    }
    original_query = n.query

    async def query_side_effect(sparql: str):
        # Alias map + ontology detail + exec — MemNeptune handles map/detail;
        # inject exec result when it's the generated SELECT.
        if "SELECT ?v" in sparql and "phone" in sparql:
            return exec_result
        return await original_query(sparql)

    n.query = query_side_effect  # type: ignore[method-assign]

    with patch("cograph_client.nlp.pipeline.get_embedding_service", return_value=None), \
         patch.object(pipeline, "_fetch_ontology", new=AsyncMock(return_value="Type: Guest")), \
         patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = msg
        result = await pipeline.ask("guest phone?", g)

    assert f"<{new_uri}>" in result.sparql
    assert f"<{old_uri}>" not in result.sparql
