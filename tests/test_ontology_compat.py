"""ONTA-404 — backward-compatibility classifier + publish gate.

Table-driven pure classifier tests + gated release integration via MemNeptune.
"""

from __future__ import annotations

import re
from collections import defaultdict

import pytest

from cograph_client.graph.ontology_commit import (
    OntologyShape,
    commit_ontology,
    fingerprint_ontology,
    load_ontology_shape,
)
from cograph_client.graph.ontology_compat import (
    CompatClass,
    OntologyCompatError,
    assert_publishable,
    classify_change,
    classify_diff,
    describe_range_change,
    is_ancestor,
)
from cograph_client.graph.ontology_snapshots import (
    diff_shapes,
    execute_snapshot,
    plan_snapshot,
    snapshot_ontology,
)
from cograph_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    OntologyMutation,
    OntologyOpKind,
)


# ---------------------------------------------------------------------------
# Table-driven per-kind classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record,expected",
    [
        (ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person"), CompatClass.ADDITIVE),
        (
            ChangeRecord(kind=ChangeKind.ADD_ATTRIBUTE, type_name="P", slot_name="name"),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.ADD_RELATIONSHIP, type_name="P", slot_name="employer",
                new_value="Company",
            ),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(kind=ChangeKind.ADD_SUBCLASS, type_name="E", parent_type="P"),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.RENAME_WITH_ALIAS, from_name="phone_num", to_name="phone",
            ),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(kind=ChangeKind.CHANGE_COMMENT, type_name="P", new_value="hi"),
            CompatClass.ANNOTATIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_TEXT_KIND, type_name="P", slot_name="bio",
                new_value="prose",
            ),
            CompatClass.ANNOTATIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT, type_name="P", slot_name="name",
                new_value="true",
            ),
            CompatClass.ANNOTATIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.DEPRECATE, type_name="Legacy", superseded_by="Thing",
            ),
            CompatClass.DEPRECATING,
        ),
        (
            ChangeRecord(kind=ChangeKind.DEPRECATE, type_name="Legacy"),
            CompatClass.DEPRECATING,
        ),
        (ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name="X"), CompatClass.BREAKING),
        (
            ChangeRecord(kind=ChangeKind.REMOVE_ATTRIBUTE, type_name="P", slot_name="x"),
            CompatClass.BREAKING,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.REMOVE_RELATIONSHIP, type_name="P", slot_name="rel",
            ),
            CompatClass.BREAKING,
        ),
        (
            ChangeRecord(kind=ChangeKind.REMOVE_SUBCLASS, type_name="E", parent_type="P"),
            CompatClass.BREAKING,
        ),
        # Widening integer → float: BREAKING (ONTA-404 ruling)
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="n",
                old_value="integer", new_value="float",
            ),
            CompatClass.BREAKING,
        ),
        # Narrowing float → integer: BREAKING
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="n",
                old_value="float", new_value="integer",
            ),
            CompatClass.BREAKING,
        ),
        # Relationship range change: BREAKING
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="employer",
                old_value="Org", new_value="Company",
            ),
            CompatClass.BREAKING,
        ),
        # Equal range: annotative no-op
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="n",
                old_value="string", new_value="string",
            ),
            CompatClass.ANNOTATIVE,
        ),
    ],
    ids=lambda x: (
        x.value if isinstance(x, CompatClass)
        else f"{x.kind.value}:{x.old_value or ''}->{x.new_value or x.superseded_by or x.type_name or ''}"
    ),
)
def test_classify_change_table(record, expected):
    result = classify_change(record)
    assert result.compat_class is expected


def test_widening_and_narrowing_messages_both_breaking():
    w = describe_range_change("integer", "float")
    n = describe_range_change("float", "integer")
    assert "widened" in w and "breaking" in w
    assert "narrowed" in n and "breaking" in n


# ---------------------------------------------------------------------------
# Re-parent ancestry
# ---------------------------------------------------------------------------


def _chain_shape() -> OntologyShape:
    # Thing <- Entity <- Person <- Employee
    return OntologyShape(
        types={"Thing": "", "Entity": "", "Person": "", "Employee": "", "Org": ""},
        parent_of={
            "Entity": "Thing",
            "Person": "Entity",
            "Employee": "Person",
        },
    )


def test_is_ancestor_walks_parent_chain():
    s = _chain_shape()
    assert is_ancestor(s, of="Employee", ancestor="Person")
    assert is_ancestor(s, of="Employee", ancestor="Entity")
    assert is_ancestor(s, of="Employee", ancestor="Thing")
    assert not is_ancestor(s, of="Employee", ancestor="Org")
    assert not is_ancestor(s, of="Person", ancestor="Employee")


def test_reparent_to_ancestor_is_non_breaking():
    # Employee was Person; re-parent to Entity (ancestor of Person).
    records = [
        ChangeRecord(
            kind=ChangeKind.REMOVE_SUBCLASS, type_name="Employee", parent_type="Person",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_SUBCLASS, type_name="Employee", parent_type="Entity",
        ),
    ]
    v = classify_diff(records, parent_shape=_chain_shape())
    assert v.overall is CompatClass.ANNOTATIVE
    assert not v.requires_major


def test_reparent_to_sibling_is_breaking():
    records = [
        ChangeRecord(
            kind=ChangeKind.REMOVE_SUBCLASS, type_name="Employee", parent_type="Person",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_SUBCLASS, type_name="Employee", parent_type="Org",
        ),
    ]
    v = classify_diff(records, parent_shape=_chain_shape())
    assert v.overall is CompatClass.BREAKING
    assert v.requires_major


# ---------------------------------------------------------------------------
# Adversarial rename / delete-then-re-add
# ---------------------------------------------------------------------------


def test_rename_with_alias_is_non_breaking():
    v = classify_diff([
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS, from_name="a", to_name="b",
        ),
    ])
    assert v.overall is CompatClass.ADDITIVE
    assert not v.requires_major


def test_remove_plus_add_type_is_breaking_not_silent_rename():
    """Adversarial: delete-then-re-add under a new name in one release."""
    v = classify_diff([
        ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name="OldName"),
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="NewName"),
    ])
    assert v.overall is CompatClass.BREAKING
    assert v.requires_major


def test_remove_attr_plus_add_unrelated_attr_is_breaking():
    v = classify_diff([
        ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE, type_name="P", slot_name="old",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE, type_name="P", slot_name="new",
            new_value="string",
        ),
    ])
    assert v.overall is CompatClass.BREAKING
    assert v.requires_major


def test_empty_diff_is_additive_ok():
    v = classify_diff([])
    assert v.overall is CompatClass.ADDITIVE
    assert not v.requires_major
    assert v.semver_bump == "minor"
    assert "empty" in v.summary[0]


def test_diff_a_a_empty_still():
    s = OntologyShape(types={"P": "person"}, attrs={"P": {"name": "string"}})
    assert diff_shapes(s, s) == []
    assert classify_diff(diff_shapes(s, s)).overall is CompatClass.ADDITIVE


def test_overall_worst_of_set():
    v = classify_diff([
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="X"),
        ChangeRecord(kind=ChangeKind.CHANGE_COMMENT, type_name="Y", new_value="z"),
        ChangeRecord(kind=ChangeKind.REMOVE_ATTRIBUTE, type_name="P", slot_name="a"),
    ])
    assert v.overall is CompatClass.BREAKING


def test_deprecating_outranks_additive_for_overall():
    v = classify_diff([
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="X"),
        ChangeRecord(kind=ChangeKind.DEPRECATE, type_name="Y", superseded_by="X"),
    ])
    assert v.overall is CompatClass.DEPRECATING
    assert v.semver_bump == "minor"
    assert not v.requires_major


def test_assert_publishable_blocks_and_allows():
    breaking = [ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name="X")]
    with pytest.raises(OntologyCompatError) as ei:
        assert_publishable(breaking)
    assert ei.value.verdict.requires_major
    assert "declare_major" in str(ei.value)

    v = assert_publishable(breaking, declare_major=True)
    assert v.overall is CompatClass.BREAKING
    assert v.stored_compat_class == "breaking"


# ---------------------------------------------------------------------------
# MemNeptune for gate + deprecation integration
# ---------------------------------------------------------------------------


class MemNeptune:
    """Minimal triple store for commit + snapshot + deprecation (ONTA-404)."""

    def __init__(self) -> None:
        self.triples: set[tuple[str, str, str, str]] = set()
        self.updates: list[str] = []
        self.queries: list[str] = []

    async def update(self, sparql: str) -> None:
        self.updates.append(sparql)
        s_up = sparql

        for m in re.finditer(r"DROP\s+SILENT\s+GRAPH\s*<([^>]+)>", s_up, re.I):
            g = m.group(1)
            self.triples = {(gg, s, p, o) for gg, s, p, o in self.triples if gg != g}

        for m in re.finditer(r"CLEAR\s+SILENT\s+GRAPH\s*<([^>]+)>", s_up, re.I):
            g = m.group(1)
            self.triples = {(gg, s, p, o) for gg, s, p, o in self.triples if gg != g}

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

        for m in re.finditer(
            r"INSERT\s+DATA\s*\{\s*GRAPH\s*<([^>]+)>\s*\{",
            s_up,
            re.I | re.S,
        ):
            g = m.group(1)
            body = self._extract_braced_body(s_up, m.end() - 1)
            for s, p, o in self._parse_triples(body):
                self.triples.add((g, s, p, o))

        for m in re.finditer(
            r"INSERT\s*\{\s*GRAPH\s*<([^>]+)>\s*\{([^}]*)\}\s*\}",
            s_up,
            re.I | re.S,
        ):
            if "?s" in m.group(2) and "?p" in m.group(2):
                continue
            if "INSERT DATA" in s_up[max(0, m.start() - 20) : m.start() + 20].upper():
                continue
            g, body = m.group(1), m.group(2)
            for s, p, o in self._parse_triples(body):
                self.triples.add((g, s, p, o))

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

        if "?typeLabel" in sparql and "?attrLabel" in sparql:
            class_uris = {
                s for gg, s, p, o in self.triples
                if gg == g and p.endswith("#type") and o.endswith("#Class")
            }
            labels = {
                s: o.strip('"') for gg, s, p, o in self.triples
                if gg == g and p.endswith("#label") and s in class_uris
            }
            comments = {
                s: o.strip('"') for gg, s, p, o in self.triples
                if gg == g and p.endswith("#comment") and s in class_uris
            }
            domains = {
                s: o for gg, s, p, o in self.triples
                if gg == g and p.endswith("#domain")
            }
            attr_labels = {
                s: o.strip('"') for gg, s, p, o in self.triples
                if gg == g and p.endswith("#label") and s in domains
            }
            attr_comments = {
                s: o.strip('"') for gg, s, p, o in self.triples
                if gg == g and p.endswith("#comment") and s in domains
            }
            ranges = {
                s: o for gg, s, p, o in self.triples
                if gg == g and p.endswith("#range")
            }
            cores = {
                s for gg, s, p, o in self.triples
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

        if "?child" in sparql and "?parent" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("#subClassOf"):
                    bindings.append({"child": {"value": s}, "parent": {"value": o}})
            return self._sparql_json(bindings)

        if "textKind" in sparql or "/textKind>" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/textKind"):
                    bindings.append({
                        "attr": {"value": s},
                        "kind": {"value": o.strip('"')},
                    })
            return self._sparql_json(bindings)

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

        if "workspaceRevision" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/workspaceRevision"):
                    bindings.append(
                        {"r": {"value": o.split("^^")[0].strip('"')}}
                    )
            return self._sparql_json(bindings)

        if "?old" in sparql and "?new" in sparql and "aliasOf" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("/aliasOf"):
                    bindings.append({"old": {"value": s}, "new": {"value": o}})
            return self._sparql_json(bindings)

        if "snapshotGraph" in sparql or "/snapshotGraph>" in sparql or "?snap" in sparql:
            by_s: dict[str, dict[str, str]] = defaultdict(dict)
            for gg, s, p, o in self.triples:
                if gg != g:
                    continue
                if "^^" in o:
                    val = o.split("^^")[0].strip('"')
                else:
                    val = o.strip('"') if o.startswith('"') else o
                by_s[s][p] = val
            for s, props in by_s.items():
                def _p(suffix: str) -> str | None:
                    for k, v in props.items():
                        if k.endswith(suffix) or k.endswith("/" + suffix.lstrip("/")):
                            return v
                    return None
                version = _p("version")
                snap = _p("snapshotGraph")
                fp = _p("fingerprint")
                kind = _p("snapshotKind")
                layer = _p("layer")
                if not (version and snap and fp):
                    continue
                row = {
                    "s": {"value": s},
                    "version": {"value": version},
                    "snap": {"value": snap},
                    "fp": {"value": fp},
                    "kind": {"value": kind or "release"},
                    "layer": {"value": layer or "tenant"},
                }
                parent = _p("parentVersion")
                if parent:
                    row["parent"] = {"value": parent}
                pub = _p("publisher")
                if pub:
                    row["pub"] = {"value": pub}
                ts = _p("timestamp")
                if ts:
                    row["ts"] = {"value": ts}
                summary = _p("changeSummary")
                if summary:
                    row["sum"] = {"value": summary}
                compat = _p("compatClass")
                if compat:
                    row["compat"] = {"value": compat}
                delta = _p("changeDelta")
                if delta:
                    row["delta"] = {"value": delta}
                bindings.append(row)
            return self._sparql_json(bindings)

        return self._sparql_json([])

    @staticmethod
    def _extract_braced_body(text: str, open_brace_idx: int) -> str:
        depth = 0
        in_str = False
        escape = False
        for i in range(open_brace_idx, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_brace_idx + 1 : i]
        return text[open_brace_idx + 1 :]

    @staticmethod
    def _parse_triples(body: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        # <s> <p> <o> .  or  <s> <p> "lit" .  or typed literal
        for m in re.finditer(
            r"<([^>]+)>\s+<([^>]+)>\s+(?:<([^>]+)>|\"([^\"]*)\"(?:\^\^<([^>]+)>)?)\s*\.",
            body,
        ):
            s, p = m.group(1), m.group(2)
            if m.group(3) is not None:
                o = m.group(3)
            else:
                lit = m.group(4) or ""
                if m.group(5):
                    o = f'"{lit}"^^{m.group(5)}'
                else:
                    o = f'"{lit}"'
            out.append((s, p, o))
        return out

    @staticmethod
    def _sparql_json(bindings: list[dict]) -> dict:
        # parse_sparql_results only projects vars listed in head.vars.
        vars_: list[str] = []
        seen: set[str] = set()
        for row in bindings:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    vars_.append(k)
        return {"head": {"vars": vars_}, "results": {"bindings": bindings}}


PUBLIC = "https://cograph.tech/graphs/global/public"
TENANT = "https://cograph.tech/graphs/acme"


async def _seed_type(n: MemNeptune, g: str, name: str = "Person") -> None:
    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name=name),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name=name,
                slot_name="name",
                datatype="string",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Publish gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_blocks_breaking_release_by_default():
    n = MemNeptune()
    await _seed_type(n, PUBLIC)
    r1 = await snapshot_ontology(n, PUBLIC, kind="release")
    assert r1.compat_class == "additive"

    # Breaking mutation: remove an attribute.
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name="name",
            )
        ],
    )
    with pytest.raises(OntologyCompatError) as ei:
        await snapshot_ontology(n, PUBLIC, kind="release")
    assert ei.value.verdict.overall is CompatClass.BREAKING
    assert "declare_major" in str(ei.value)


@pytest.mark.asyncio
async def test_gate_allows_with_declare_major_and_stores_breaking():
    n = MemNeptune()
    await _seed_type(n, PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name="name",
            )
        ],
    )
    rec = await snapshot_ontology(
        n, PUBLIC, kind="release", declare_major=True,
    )
    assert rec.compat_class == "breaking"
    assert rec.version == 2
    assert any(r.kind is ChangeKind.REMOVE_ATTRIBUTE for r in rec.change_records)


@pytest.mark.asyncio
async def test_gate_ignores_freeform_compat_class_on_release():
    n = MemNeptune()
    await _seed_type(n, PUBLIC)
    rec = await snapshot_ontology(
        n, PUBLIC, kind="release", compat_class="major",
    )
    # First release empty delta → classifier wins over free-form "major".
    assert rec.compat_class == "additive"


@pytest.mark.asyncio
async def test_revision_not_gated_on_breaking():
    n = MemNeptune()
    await _seed_type(n, TENANT)
    await commit_ontology(
        n,
        TENANT,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name="name",
            )
        ],
    )
    # Revisions must not refuse even when the live shape would be breaking vs
    # a prior revision (no parent → empty delta → additive; if parent exists
    # with remove, still no raise).
    rec = await snapshot_ontology(n, TENANT, kind="revision")
    assert rec.kind == "revision"
    assert rec.compat_class in ("additive", "breaking", "annotative", "deprecating")


@pytest.mark.asyncio
async def test_dry_run_also_enforces_gate():
    n = MemNeptune()
    await _seed_type(n, PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_TYPE, type_name="Person",
            )
        ],
    )
    plan = await plan_snapshot(n, PUBLIC, kind="release")
    with pytest.raises(OntologyCompatError):
        await execute_snapshot(n, plan, dry_run=True)


# ---------------------------------------------------------------------------
# Deprecation as first-class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprecate_op_keeps_type_and_sets_marker():
    n = MemNeptune()
    await _seed_type(n, TENANT)
    fp_before = await fingerprint_ontology(n, TENANT)

    result = await commit_ontology(
        n,
        TENANT,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                superseded_by="Entity",
            )
        ],
    )
    assert any(r.kind is ChangeKind.DEPRECATE for r in result.change_records)
    assert result.version_after != fp_before  # deprecation shifts fingerprint

    shape = await load_ontology_shape(n, TENANT)
    assert "Person" in shape.types  # still loads
    assert "Person" in shape.deprecated_types
    assert shape.deprecated_types["Person"] == "Entity"

    # Marker triples present.
    assert any(
        p.endswith("/deprecatedAt") and "Person" in s
        for (_g, s, p, _o) in n.triples
    )
    assert any(
        p.endswith("/supersededBy") and "Person" in s and "Entity" in o
        for (_g, s, p, o) in n.triples
    )


@pytest.mark.asyncio
async def test_deprecate_slot_marker():
    n = MemNeptune()
    await _seed_type(n, TENANT)
    await commit_ontology(
        n,
        TENANT,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                slot_name="name",
                superseded_by="full_name",
            )
        ],
    )
    shape = await load_ontology_shape(n, TENANT)
    assert ("Person", "name") in shape.deprecated_slots
    assert shape.attrs.get("Person", {}).get("name") == "string"  # still present


@pytest.mark.asyncio
async def test_deprecation_release_is_minor_not_breaking():
    n = MemNeptune()
    await _seed_type(n, PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                superseded_by="Entity",
            )
        ],
    )
    # No declare_major needed — deprecating is minor.
    rec = await snapshot_ontology(n, PUBLIC, kind="release")
    assert rec.compat_class == "deprecating"
    assert any(r.kind is ChangeKind.DEPRECATE for r in rec.change_records)


@pytest.mark.asyncio
async def test_diff_shapes_emits_deprecate():
    a = OntologyShape(types={"Person": ""})
    b = OntologyShape(
        types={"Person": ""},
        deprecated_types={"Person": "Entity"},
    )
    recs = diff_shapes(a, b)
    assert any(r.kind is ChangeKind.DEPRECATE and r.type_name == "Person" for r in recs)
    v = classify_diff(recs)
    assert v.overall is CompatClass.DEPRECATING
