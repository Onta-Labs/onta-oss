"""ONTA-406 — ontology snapshots, structural diff, restore, immutability.

Acceptance:
- Snapshot → mutate heavily → restore → fingerprint identity
- Write into published version graph refused
- Diff correctness + symmetry (diff(a,a)=[], invert(diff(a,b))==diff(b,a))
- Cleanup drops version artifacts
- plan_*/execute dry-run writes nothing
"""

from __future__ import annotations

import re
from collections import defaultdict

import pytest

from infona_client.graph.ontology_commit import (
    OntologyGraphImmutable,
    OntologyShape,
    commit_ontology,
    fingerprint_ontology,
    is_immutable_version_graph,
    release_graph_uri,
    revision_graph_uri,
    versions_graph_uri,
)
from infona_client.graph.ontology_snapshots import (
    ReleaseRecord,
    cleanup_version_artifacts,
    diff_shapes,
    diffs_symmetric,
    execute_restore,
    execute_snapshot,
    invert_diff,
    layer_for_graph,
    list_snapshots,
    plan_cleanup_version_artifacts,
    plan_restore,
    plan_snapshot,
    restore_ontology,
    snapshot_ontology,
)
from infona_client.graph.ontology_queries import ontology_version
from infona_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    OntologyMutation,
    OntologyOpKind,
)


# ---------------------------------------------------------------------------
# In-memory Neptune — copy/clear/drop + the SELECT shapes we need
# ---------------------------------------------------------------------------


class MemNeptune:
    """Triple store sufficient for commit_ontology + snapshot/diff/restore."""

    def __init__(self) -> None:
        self.triples: set[tuple[str, str, str, str]] = set()
        self.updates: list[str] = []
        self.queries: list[str] = []

    async def update(self, sparql: str) -> None:
        self.updates.append(sparql)
        s_up = sparql

        # DROP SILENT GRAPH <g>
        for m in re.finditer(r"DROP\s+SILENT\s+GRAPH\s*<([^>]+)>", s_up, re.I):
            g = m.group(1)
            self.triples = {(gg, s, p, o) for gg, s, p, o in self.triples if gg != g}

        # CLEAR SILENT GRAPH <g>
        for m in re.finditer(r"CLEAR\s+SILENT\s+GRAPH\s*<([^>]+)>", s_up, re.I):
            g = m.group(1)
            self.triples = {(gg, s, p, o) for gg, s, p, o in self.triples if gg != g}

        # INSERT { GRAPH <t> { ?s ?p ?o } } WHERE { GRAPH <src> { ?s ?p ?o } }
        m_copy = re.search(
            r"INSERT\s*\{\s*GRAPH\s*<([^>]+)>\s*\{\s*\?s\s+\?p\s+\?o\s*\}\s*\}\s*"
            r"WHERE\s*\{\s*GRAPH\s*<([^>]+)>\s*\{\s*\?s\s+\?p\s+\?o\s*\}\s*\}",
            s_up,
            re.I | re.S,
        )
        if m_copy:
            tgt, src = m_copy.group(1), m_copy.group(2)
            for gg, s, p, o in list(self.triples):
                if gg == src:
                    self.triples.add((tgt, s, p, o))

        # INSERT DATA { GRAPH <g> { ... } } — body may contain `}` inside
        # JSON string literals (release changeDelta), so we cannot use
        # `[^}]*`. Match GRAPH <g> { then scan triples with the same
        # triple parser, stopping at the GRAPH-closing brace that is not
        # inside a quoted literal.
        for m in re.finditer(
            r"INSERT\s+DATA\s*\{\s*GRAPH\s*<([^>]+)>\s*\{",
            s_up,
            re.I | re.S,
        ):
            g = m.group(1)
            body = self._extract_braced_body(s_up, m.end() - 1)
            for s, p, o in self._parse_triples(body):
                self.triples.add((g, s, p, o))

        # INSERT { GRAPH <g> { ... } } WHERE  (non-copy)
        for m in re.finditer(
            r"INSERT\s*\{\s*GRAPH\s*<([^>]+)>\s*\{([^}]*)\}\s*\}",
            s_up,
            re.I | re.S,
        ):
            if "?s" in m.group(2) and "?p" in m.group(2):
                continue  # handled by copy
            if "INSERT DATA" in s_up[max(0, m.start() - 20) : m.start() + 20].upper():
                continue
            g, body = m.group(1), m.group(2)
            for s, p, o in self._parse_triples(body):
                self.triples.add((g, s, p, o))

        # DELETE { GRAPH <g> { <s> <p> ?var } }
        for m in re.finditer(
            r"DELETE\s*\{\s*GRAPH\s*<([^>]+)>\s*\{\s*<([^>]+)>\s*<([^>]+)>\s*\?(\w+)\s*\}\s*\}",
            s_up,
            re.I | re.S,
        ):
            g, s, p = m.group(1), m.group(2), m.group(3)
            self.triples = {
                (gg, ss, pp, oo)
                for gg, ss, pp, oo in self.triples
                if not (gg == g and ss == s and pp == p)
            }

        # WITH <g> DELETE { <s> ?p ?o } WHERE
        for m in re.finditer(
            r"WITH\s*<([^>]+)>\s*DELETE\s*\{\s*<([^>]+)>\s*\?p\s*\?o\s*\}",
            s_up,
            re.I | re.S,
        ):
            g, s = m.group(1), m.group(2)
            self.triples = {
                (gg, ss, pp, oo)
                for gg, ss, pp, oo in self.triples
                if not (gg == g and ss == s)
            }

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        g_match = re.search(r"FROM\s*<([^>]+)>", sparql)
        g = g_match.group(1) if g_match else ""
        bindings: list[dict] = []

        # full_ontology_detail_query
        if "?typeLabel" in sparql and "?attrLabel" in sparql:
            class_uris = {
                s
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("#type") and o.endswith("#Class")
            }
            labels = {
                s: o.strip('"')
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("#label") and s in class_uris
            }
            comments = {
                s: o.strip('"')
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("#comment") and s in class_uris
            }
            domains = {
                s: o
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("#domain")
            }
            attr_labels = {
                s: o.strip('"')
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("#label") and s in domains
            }
            attr_comments = {
                s: o.strip('"')
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("#comment") and s in domains
            }
            ranges = {
                s: o
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("#range")
            }
            cores = {
                s
                for gg, s, p, o in self.triples
                if gg == g and p.endswith("/coreSlot")
            }
            for t_uri, tlabel in labels.items():
                attrs_for_t = [a for a, d in domains.items() if d == t_uri]
                if not attrs_for_t:
                    row = {
                        "type": {"value": t_uri},
                        "typeLabel": {"value": tlabel},
                    }
                    if t_uri in comments:
                        row["typeComment"] = {"value": comments[t_uri]}
                    bindings.append(row)
                for a_uri in attrs_for_t:
                    row = {
                        "type": {"value": t_uri},
                        "typeLabel": {"value": tlabel},
                        "attr": {"value": a_uri},
                        "attrLabel": {
                            "value": attr_labels.get(a_uri, a_uri.rsplit("/", 1)[-1])
                        },
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
                    bindings.append(
                        {"child": {"value": s}, "parent": {"value": o}}
                    )
            return self._sparql_json(bindings)

        # text_kind_map_query
        if "textKind" in sparql or "/textKind>" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/textKind"):
                    bindings.append(
                        {
                            "attr": {"value": s},
                            "kind": {"value": o.strip('"')},
                        }
                    )
            return self._sparql_json(bindings)

        # deprecation map (ONTA-404)
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

        # workspaceRevision counter
        if "workspaceRevision" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/workspaceRevision"):
                    bindings.append(
                        {"r": {"value": o.split("^^")[0].strip('"')}}
                    )
            return self._sparql_json(bindings)

        # alias_map_query
        if "?old" in sparql and "?new" in sparql and "aliasOf" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/aliasOf"):
                    bindings.append(
                        {"old": {"value": s}, "new": {"value": o}}
                    )
            return self._sparql_json(bindings)

        # list_snapshots SELECT
        if "snapshotGraph" in sparql or "/snapshotGraph>" in sparql or "?snap" in sparql:
            # Group by subject
            by_s: dict[str, dict[str, str]] = defaultdict(dict)
            for gg, s, p, o in self.triples:
                if gg != g:
                    continue
                leaf = p.rsplit("/", 1)[-1]
                by_s[s][leaf] = o.strip('"').split("^^")[0]
            for s, props in by_s.items():
                if "snapshotGraph" not in props and "version" not in props:
                    # try full-pred leaf names from our vocabulary
                    pass
                # Collect via predicate endswith
            by_s = defaultdict(dict)
            for gg, s, p, o in self.triples:
                if gg != g:
                    continue
                val = o if not o.startswith('"') else o.strip('"').split("^^")[0]
                # strip typed literal wrapper if present as "n"^^xsd
                if "^^" in o:
                    val = o.split("^^")[0].strip('"')
                else:
                    val = o.strip('"') if o.startswith('"') else o
                by_s[s][p] = val
            for s, props in by_s.items():
                def _get(suffix: str) -> str | None:
                    for k, v in props.items():
                        if k.endswith("/" + suffix) or k.endswith("#" + suffix):
                            return v
                    return None

                version = _get("version")
                snap = _get("snapshotGraph")
                fp = _get("fingerprint")
                kind = _get("snapshotKind")
                layer = _get("layer")
                if not (version and snap and fp and kind):
                    continue
                row = {
                    "s": {"value": s},
                    "version": {"value": version},
                    "snap": {"value": snap},
                    "fp": {"value": fp},
                    "kind": {"value": kind},
                    "layer": {"value": layer or "tenant"},
                }
                parent = _get("parentVersion")
                if parent is not None:
                    row["parent"] = {"value": parent}
                pub = _get("publisher")
                if pub:
                    row["pub"] = {"value": pub}
                ts = _get("timestamp")
                if ts:
                    row["ts"] = {"value": ts}
                summary = _get("changeSummary")
                if summary:
                    row["sum"] = {"value": summary}
                compat = _get("compatClass")
                if compat:
                    row["compat"] = {"value": compat}
                delta = _get("changeDelta")
                if delta:
                    row["delta"] = {"value": delta}
                bindings.append(row)
            return self._sparql_json(bindings)

        return self._sparql_json([])

    @staticmethod
    def _sparql_json(bindings: list[dict]) -> dict:
        vars_: list[str] = []
        seen: set[str] = set()
        for row in bindings:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    vars_.append(k)
        return {"head": {"vars": vars_}, "results": {"bindings": bindings}}

    @staticmethod
    def _extract_braced_body(src: str, open_brace_idx: int) -> str:
        """Return the text inside ``{...}`` starting at ``open_brace_idx``,
        respecting double-quoted string literals so JSON ``}`` does not end
        the GRAPH block early."""
        assert src[open_brace_idx] == "{"
        depth = 0
        i = open_brace_idx
        in_str = False
        escape = False
        while i < len(src):
            ch = src[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return src[open_brace_idx + 1 : i]
            i += 1
        return src[open_brace_idx + 1 :]

    @staticmethod
    def _parse_triples(body: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        def _add(s: str, p: str, o: str) -> None:
            t = (s, p, o)
            if t not in seen:
                seen.add(t)
                out.append(t)

        for m in re.finditer(r"<([^>]+)>\s+<([^>]+)>\s+<([^>]+)>\s*\.", body):
            _add(m.group(1), m.group(2), m.group(3))
        # Typed literals first (more specific).
        def _unesc(lit: str) -> str:
            return lit.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")

        for m in re.finditer(
            r'<([^>]+)>\s+<([^>]+)>\s+"((?:[^"\\]|\\.)*)"\^\^<([^>]+)>\s*\.',
            body,
        ):
            _add(m.group(1), m.group(2), f'"{_unesc(m.group(3))}"^^{m.group(4)}')
        for m in re.finditer(
            r'<([^>]+)>\s+<([^>]+)>\s+"((?:[^"\\]|\\.)*)"\s*\.',
            body,
        ):
            # Skip if this position was already captured as typed.
            _add(m.group(1), m.group(2), f'"{_unesc(m.group(3))}"')
        return out


PUBLIC = "https://graph.infona.ai/graphs/global/public"
ENHANCED = "https://graph.infona.ai/graphs/global/enhanced"
TENANT = "https://graph.infona.ai/graphs/acme"


async def _seed_basic(n: MemNeptune, graph: str) -> None:
    await commit_ontology(
        n,
        graph,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="name",
                datatype="string",
            ),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_TYPE,
                type_name="Employee",
                parent_type="Person",
            ),
        ],
        actor="seed",
        message="seed",
    )


# ---------------------------------------------------------------------------
# URI / immutability helpers
# ---------------------------------------------------------------------------


def test_version_graph_uri_helpers():
    assert release_graph_uri(PUBLIC, 3) == f"{PUBLIC}/v3"
    assert revision_graph_uri(TENANT, 7) == f"{TENANT}/revisions/r7"
    assert versions_graph_uri(PUBLIC).endswith("/versions")
    assert layer_for_graph(PUBLIC) == "public"
    assert layer_for_graph(ENHANCED) == "enhanced"
    assert layer_for_graph(TENANT) == "tenant"


def test_is_immutable_version_graph():
    assert is_immutable_version_graph(f"{PUBLIC}/v1")
    assert is_immutable_version_graph(f"{ENHANCED}/v12")
    assert is_immutable_version_graph(f"{TENANT}/revisions/r3")
    assert not is_immutable_version_graph(PUBLIC)
    assert not is_immutable_version_graph(TENANT)
    assert not is_immutable_version_graph(versions_graph_uri(TENANT))
    assert not is_immutable_version_graph(f"{TENANT}/kg/foo")


# ---------------------------------------------------------------------------
# Pure diff
# ---------------------------------------------------------------------------


def _shape(**kwargs) -> OntologyShape:
    return OntologyShape(**kwargs)


def test_diff_identity_is_empty():
    a = _shape(
        types={"Person": "a human"},
        attrs={"Person": {"name": "string", "employer": "Company"}},
        parent_of={"Employee": "Person"},
        core_slots=[("Person", "name")],
        text_kinds={("Person", "bio"): "free_text"},
    )
    assert diff_shapes(a, a) == []


def test_diff_add_remove_type_attr_rel_subclass():
    a = _shape(types={"Person": ""}, attrs={"Person": {"name": "string"}})
    b = _shape(
        types={"Person": "", "Company": ""},
        attrs={
            "Person": {"name": "string", "employer": "Company"},
            "Company": {"legal_name": "string"},
        },
        parent_of={"Employee": "Person"},
    )
    # Also add Employee type for the subclass edge target to be meaningful.
    b.types["Employee"] = ""
    records = diff_shapes(a, b)
    kinds = {r.kind for r in records}
    assert ChangeKind.ADD_TYPE in kinds
    assert ChangeKind.ADD_RELATIONSHIP in kinds or ChangeKind.ADD_ATTRIBUTE in kinds
    assert ChangeKind.ADD_SUBCLASS in kinds
    # employer is a relationship (non-literal range)
    assert any(
        r.kind is ChangeKind.ADD_RELATIONSHIP and r.slot_name == "employer"
        for r in records
    )
    assert any(
        r.kind is ChangeKind.ADD_ATTRIBUTE and r.slot_name == "legal_name"
        for r in records
    )


def test_diff_comment_range_core_text_kind():
    a = _shape(
        types={"Person": "old"},
        attrs={"Person": {"name": "string", "employer": "Org"}},
        attr_comments={"Person": {"name": "display"}},
        core_slots=[("Person", "name")],
        text_kinds={("Person", "bio"): "free_text"},
    )
    b = _shape(
        types={"Person": "new"},
        attrs={"Person": {"name": "string", "employer": "Company"}},
        attr_comments={"Person": {"name": "full name"}},
        core_slots=[],
        text_kinds={("Person", "bio"): "identifier"},
    )
    records = diff_shapes(a, b)
    by_kind = defaultdict(list)
    for r in records:
        by_kind[r.kind].append(r)
    assert any(
        r.old_value == "old" and r.new_value == "new"
        for r in by_kind[ChangeKind.CHANGE_COMMENT]
        if r.slot_name is None
    )
    assert any(
        r.slot_name == "name" and r.old_value == "display"
        for r in by_kind[ChangeKind.CHANGE_COMMENT]
    )
    assert any(
        r.slot_name == "employer"
        and r.old_value == "Org"
        and r.new_value == "Company"
        for r in by_kind[ChangeKind.CHANGE_RANGE]
    )
    assert any(
        r.slot_name == "name" and r.new_value == "false"
        for r in by_kind[ChangeKind.CHANGE_CORE_SLOT]
    )
    assert any(
        r.slot_name == "bio" and r.new_value == "identifier"
        for r in by_kind[ChangeKind.CHANGE_TEXT_KIND]
    )


def test_diff_symmetry():
    a = _shape(
        types={"Person": "p", "Company": ""},
        attrs={
            "Person": {"name": "string", "age": "integer"},
            "Company": {"legal_name": "string"},
        },
        parent_of={"Employee": "Person"},
        core_slots=[("Person", "name")],
        text_kinds={("Person", "bio"): "free_text"},
        alias_map={
            "https://graph.infona.ai/types/Person/attrs/phone_num":
            "https://graph.infona.ai/types/Person/attrs/phone",
        },
    )
    b = _shape(
        types={"Person": "person", "Org": ""},
        attrs={
            "Person": {"name": "string", "employer": "Org"},
            "Org": {"legal_name": "string"},
        },
        parent_of={"Staff": "Person"},
        core_slots=[("Person", "employer")],
        text_kinds={("Person", "bio"): "identifier"},
    )
    assert diffs_symmetric(a, b)
    assert diffs_symmetric(b, a)
    # Multiset equality of inverted lists
    ab = diff_shapes(a, b)
    ba = diff_shapes(b, a)
    inv = invert_diff(ab)

    from infona_client.graph.ontology_snapshots import _record_key

    assert sorted(_record_key(r) for r in inv) == sorted(_record_key(r) for r in ba)


def test_diff_empty_fingerprint_constant_still_holds():
    assert ontology_version({}, {}) == "e3b0c44298fc1c14"
    assert _shape().fingerprint() == "e3b0c44298fc1c14"


# ---------------------------------------------------------------------------
# Snapshot / restore / immutability (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_mutate_restore_fingerprint_identity():
    n = MemNeptune()
    await _seed_basic(n, PUBLIC)
    fp0 = await fingerprint_ontology(n, PUBLIC)

    rec = await snapshot_ontology(
        n,
        PUBLIC,
        kind="release",
        publisher="ops@infona.ai",
        change_summary="initial public release",
        # Free-form compat_class is ignored for releases (ONTA-404 classifier).
        compat_class="major",
    )
    assert isinstance(rec, ReleaseRecord)
    assert rec.version == 1
    assert rec.fingerprint == fp0
    assert rec.snapshot_graph_uri == f"{PUBLIC}/v1"
    assert rec.publisher == "ops@infona.ai"
    # First release (no parent / empty delta) → classifier says additive.
    assert rec.compat_class == "additive"

    # Heavy mutation on the live graph.
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Company"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="email",
                datatype="string",
            ),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_RELATIONSHIP,
                type_name="Person",
                slot_name="employer",
                target_type="Company",
                description="works at",
            ),
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name="name",
            ),
            OntologyMutation(
                op=OntologyOpKind.SET_COMMENT,
                type_name="Person",
                description="a human being",
            ),
            OntologyMutation(
                op=OntologyOpKind.SET_CORE_SLOT,
                type_name="Person",
                slot_name="email",
                core_slot=True,
            ),
        ],
    )
    fp_mutated = await fingerprint_ontology(n, PUBLIC)
    assert fp_mutated != fp0

    # Restore from v1.
    after = await restore_ontology(n, PUBLIC, 1, kind="release")
    assert after == fp0
    assert await fingerprint_ontology(n, PUBLIC) == fp0


@pytest.mark.asyncio
async def test_write_into_published_version_graph_refused():
    n = MemNeptune()
    await _seed_basic(n, PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    snap = f"{PUBLIC}/v1"

    with pytest.raises(OntologyGraphImmutable):
        await commit_ontology(
            n,
            snap,
            [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="X")],
        )

    with pytest.raises(OntologyGraphImmutable):
        await plan_snapshot(n, snap, kind="release")

    with pytest.raises(OntologyGraphImmutable):
        await plan_restore(n, snap, 1)


@pytest.mark.asyncio
async def test_snapshot_overwrite_refused():
    n = MemNeptune()
    await _seed_basic(n, PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release", version=1)
    # Mutate so a second snapshot of the same number is a real overwrite attempt.
    await commit_ontology(
        n,
        PUBLIC,
        [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Extra")],
    )
    plan = await plan_snapshot(n, PUBLIC, kind="release", version=1)
    with pytest.raises(OntologyGraphImmutable):
        await execute_snapshot(n, plan)


@pytest.mark.asyncio
async def test_list_and_get_snapshots():
    n = MemNeptune()
    await _seed_basic(n, PUBLIC)
    r1 = await snapshot_ontology(n, PUBLIC, kind="release", publisher="a")
    await commit_ontology(
        n,
        PUBLIC,
        [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Org")],
    )
    r2 = await snapshot_ontology(
        n, PUBLIC, kind="release", publisher="b", change_summary="add Org"
    )
    listed = await list_snapshots(n, PUBLIC, kind="release")
    assert [r.version for r in listed] == [1, 2]
    assert listed[0].fingerprint == r1.fingerprint
    assert listed[1].fingerprint == r2.fingerprint
    assert listed[1].parent_version == 1
    assert listed[1].change_summary == "add Org"
    # Parent delta should include ADD_TYPE Org
    assert any(
        c.kind is ChangeKind.ADD_TYPE and c.type_name == "Org"
        for c in listed[1].change_records
    )


@pytest.mark.asyncio
async def test_revision_snapshot_for_workspace_c():
    n = MemNeptune()
    await _seed_basic(n, TENANT)
    # After seed, revision counter is 1 (one commit_ontology call).
    rec = await snapshot_ontology(
        n, TENANT, kind="revision", change_summary="job boundary"
    )
    assert rec.kind == "revision"
    assert rec.snapshot_graph_uri == f"{TENANT}/revisions/r{rec.version}"
    assert rec.layer == "tenant"
    listed = await list_snapshots(n, TENANT, kind="revision")
    assert len(listed) == 1
    assert listed[0].version == rec.version


@pytest.mark.asyncio
async def test_plan_execute_dry_run_writes_nothing():
    n = MemNeptune()
    await _seed_basic(n, PUBLIC)
    n.updates.clear()

    plan = await plan_snapshot(n, PUBLIC, kind="release")
    # plan_snapshot only reads
    write_ops = [u for u in n.updates if "INSERT" in u.upper() or "CLEAR" in u.upper()]
    assert write_ops == []

    rec = await execute_snapshot(n, plan, dry_run=True, publisher="dry")
    assert rec.version == plan.version
    assert rec.fingerprint == plan.fingerprint
    write_ops = [u for u in n.updates if "INSERT" in u.upper() or "CLEAR" in u.upper()]
    assert write_ops == []

    # Actual execute writes
    await execute_snapshot(n, plan, publisher="real")
    assert any("INSERT" in u.upper() for u in n.updates)

    # Restore dry-run
    n.updates.clear()
    rplan = await plan_restore(n, PUBLIC, 1)
    assert rplan.fingerprint_after == plan.fingerprint
    after = await execute_restore(n, rplan, dry_run=True)
    assert after == plan.fingerprint
    assert n.updates == []


@pytest.mark.asyncio
async def test_cleanup_drops_version_artifacts():
    n = MemNeptune()
    await _seed_basic(n, TENANT)
    await snapshot_ontology(n, TENANT, kind="revision")
    await snapshot_ontology(n, TENANT, kind="release", version=1)

    planned = await plan_cleanup_version_artifacts(n, TENANT)
    assert versions_graph_uri(TENANT) in planned
    assert any("/revisions/r" in u for u in planned) or any(
        u.endswith("/v1") for u in planned
    )

    # Live graph still has content before cleanup.
    assert (await fingerprint_ontology(n, TENANT)) != "e3b0c44298fc1c14"

    dropped = await cleanup_version_artifacts(n, TENANT)
    assert versions_graph_uri(TENANT) in dropped
    # Snapshot content graphs are gone.
    for u in dropped:
        remaining = [t for t in n.triples if t[0] == u]
        assert remaining == [], f"orphans left in {u}: {remaining}"
    # Live ontology graph is intentionally NOT dropped by version cleanup.
    assert await fingerprint_ontology(n, TENANT) != "e3b0c44298fc1c14"
    # No release records remain.
    assert await list_snapshots(n, TENANT) == []


@pytest.mark.asyncio
async def test_cleanup_dry_run():
    n = MemNeptune()
    await _seed_basic(n, TENANT)
    await snapshot_ontology(n, TENANT, kind="release", version=1)
    before = set(n.triples)
    planned = await cleanup_version_artifacts(n, TENANT, dry_run=True)
    assert planned
    assert set(n.triples) == before


@pytest.mark.asyncio
async def test_enhanced_layer_release_uri():
    n = MemNeptune()
    await _seed_basic(n, ENHANCED)
    rec = await snapshot_ontology(n, ENHANCED, kind="release")
    assert rec.layer == "enhanced"
    assert rec.snapshot_graph_uri == f"{ENHANCED}/v1"


@pytest.mark.asyncio
async def test_diff_graphs_round_trip_via_snapshot():
    """diff between consecutive releases matches change_records_vs_parent."""
    n = MemNeptune()
    await _seed_basic(n, PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Place"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Place",
                slot_name="city",
                datatype="string",
            ),
        ],
    )
    rec2 = await snapshot_ontology(n, PUBLIC, kind="release")
    assert any(
        c.kind is ChangeKind.ADD_TYPE and c.type_name == "Place"
        for c in rec2.change_records
    )
    # Structural symmetry still holds on the shapes of v1 vs v2 content graphs.
    from infona_client.graph.ontology_commit import load_ontology_shape

    s1 = await load_ontology_shape(n, f"{PUBLIC}/v1")
    s2 = await load_ontology_shape(n, f"{PUBLIC}/v2")
    assert diffs_symmetric(s1, s2)
    assert s1.fingerprint() != s2.fingerprint()
