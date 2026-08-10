"""ONTA-405 — workspace base pinning, upgrade preview, rollback, entitlement degrade.

Acceptance:
- Pin stability: pinned stack keeps v1 graph URI after live publishes v2
- auto_upgrade path sees latest on ensure
- Upgrade then rollback restores exact fingerprint
- Backfill: no pin → ensure at latest; second ensure is no-op
- Collision preview for overlapping tenant/base names
- Entitlement degrade: enhanced pin + entitled=False → no throw, public only
- Deprecation preview for attrs present on tenant shape
"""

from __future__ import annotations

import re
from collections import defaultdict

import pytest

from infona_client.graph.layers import Layer, LayerStack, public_graph_uri
from infona_client.graph.ontology_base_pin import (
    BasePin,
    BasePinReadError,
    base_graph_uri_for_stack,
    base_pin_graph_uri,
    ensure_workspace_base_pin,
    fingerprint_base_layer,
    get_base_pin,
    layer_stack_for_workspace,
    layer_stack_from_pin,
    preview_base_upgrade,
    rollback_base_pin,
    set_base_pin,
    upgrade_base_pin,
)
from infona_client.graph.ontology_commit import (
    commit_ontology,
    release_graph_uri,
)
from infona_client.graph.ontology_snapshots import snapshot_ontology
from infona_client.models.ontology import (
    ChangeKind,
    OntologyMutation,
    OntologyOpKind,
)


# ---------------------------------------------------------------------------
# In-memory Neptune (extended from ONTA-406 snapshot tests for pin SELECT)
# ---------------------------------------------------------------------------


class MemNeptune:
    """Triple store sufficient for commit + snapshot + base pin."""

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

        # Base pin: SELECT ?p ?o for a fixed subject
        if "?p" in sparql and "?o" in sparql and "WorkspaceBasePin" in sparql:
            subj_m = re.search(r"<(https://graph\.infona\.ai/meta/WorkspaceBasePin)>\s+\?p\s+\?o", sparql)
            subj = subj_m.group(1) if subj_m else "https://graph.infona.ai/meta/WorkspaceBasePin"
            for gg, s, p, o in self.triples:
                if gg == g and s == subj:
                    val = o
                    if "^^" in o:
                        val = o.split("^^")[0].strip('"')
                    else:
                        val = o.strip('"') if o.startswith('"') else o
                    bindings.append({"p": {"value": p}, "o": {"value": val}})
            return self._sparql_json(bindings)

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

        if "?child" in sparql and "?parent" in sparql:
            for gg, s, p, o in self.triples:
                if gg == g and p.endswith("#subClassOf"):
                    bindings.append(
                        {"child": {"value": s}, "parent": {"value": o}}
                    )
            return self._sparql_json(bindings)

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
                    bindings.append(
                        {"old": {"value": s}, "new": {"value": o}}
                    )
            return self._sparql_json(bindings)

        # list_snapshots SELECT
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
                def _get(suffix: str, _props=props) -> str | None:
                    for k, v in _props.items():
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
            _add(m.group(1), m.group(2), f'"{_unesc(m.group(3))}"')
        return out


PUBLIC = "https://graph.infona.ai/graphs/global/public"
ENHANCED = "https://graph.infona.ai/graphs/global/enhanced"
TENANT_ID = "acme"
TENANT = f"https://graph.infona.ai/graphs/{TENANT_ID}"


async def _seed_public_v1(n: MemNeptune) -> str:
    """Seed public live with Person.name and snapshot as v1. Returns fingerprint."""
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="name",
                datatype="string",
            ),
        ],
        actor="seed",
        message="public v1",
    )
    rec = await snapshot_ontology(n, PUBLIC, kind="release", version=1, publisher="ops")
    return rec.fingerprint


async def _publish_public_v2(n: MemNeptune) -> str:
    """Mutate live public (add email) and snapshot v2."""
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="email",
                datatype="string",
            ),
        ],
        actor="seed",
        message="public v2",
    )
    rec = await snapshot_ontology(n, PUBLIC, kind="release", version=2, publisher="ops")
    return rec.fingerprint


# ---------------------------------------------------------------------------
# LayerStack version dimension (additive; existing callers unbroken)
# ---------------------------------------------------------------------------


def test_layer_stack_defaults_are_live():
    stack = LayerStack(TENANT, entitled=False)
    assert stack.public_version is None
    assert stack.enhanced_version is None
    assert stack.graph_uri_for(Layer.PUBLIC) == PUBLIC


def test_layer_stack_public_version_pins_release_uri():
    stack = LayerStack(TENANT, entitled=False, public_version=3)
    assert stack.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 3)
    assert stack.graph_uri_for(Layer.TENANT) == TENANT


def test_layer_stack_enhanced_version_pins_when_entitled():
    stack = LayerStack(TENANT, entitled=True, enhanced_version=5)
    assert stack.graph_uri_for(Layer.ENHANCED) == release_graph_uri(ENHANCED, 5)
    # Non-entitled still excludes enhanced from layers even if version set.
    free = LayerStack(TENANT, entitled=False, enhanced_version=5)
    assert Layer.ENHANCED not in free.layers


# ---------------------------------------------------------------------------
# Pin stability core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_stability_core():
    """Pin at v1; publish v2; pinned stack still uses v1 graph URI + same fp.

    auto_upgrade=True path sees v2 on ensure.
    """
    n = MemNeptune()
    fp_v1 = await _seed_public_v1(n)

    pin = await set_base_pin(
        n,
        TENANT_ID,
        BasePin(
            base_layer="public",
            base_version=1,
            auto_upgrade=False,
            tenant_id=TENANT_ID,
        ),
    )
    assert pin.base_version == 1
    assert pin.auto_upgrade is False

    stack_before = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=False
    )
    assert stack_before.public_version == 1
    assert stack_before.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 1)
    fp_pinned_before = await fingerprint_base_layer(n, stack_before)
    assert fp_pinned_before == fp_v1

    fp_v2 = await _publish_public_v2(n)
    assert fp_v2 != fp_v1

    # Pinned stack unchanged.
    stack_after = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=True
    )
    assert stack_after.public_version == 1
    assert stack_after.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 1)
    fp_pinned_after = await fingerprint_base_layer(n, stack_after)
    assert fp_pinned_after == fp_v1
    assert fp_pinned_after == fp_pinned_before

    # auto_upgrade path sees v2.
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(
            base_layer="public",
            base_version=1,
            auto_upgrade=True,
            tenant_id=TENANT_ID,
        ),
    )
    stack_auto = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=True
    )
    assert stack_auto.public_version == 2
    assert stack_auto.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 2)
    fp_auto = await fingerprint_base_layer(n, stack_auto)
    assert fp_auto == fp_v2


# ---------------------------------------------------------------------------
# Upgrade + rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_then_rollback_restores_fingerprint():
    n = MemNeptune()
    fp_v1 = await _seed_public_v1(n)
    fp_v2 = await _publish_public_v2(n)

    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    stack_v1 = layer_stack_from_pin(
        TENANT_ID,
        await get_base_pin(n, TENANT_ID),
        entitled=False,
    )
    assert await fingerprint_base_layer(n, stack_v1) == fp_v1

    upgraded = await upgrade_base_pin(
        n, TENANT_ID, entitled=False, to_version=2
    )
    assert upgraded.base_version == 2
    assert upgraded.previous_version == 1
    assert upgraded.has_previous is True

    stack_v2 = layer_stack_from_pin(TENANT_ID, upgraded, entitled=False)
    assert await fingerprint_base_layer(n, stack_v2) == fp_v2

    rolled = await rollback_base_pin(n, TENANT_ID)
    assert rolled.base_version == 1
    assert rolled.previous_version == 2
    stack_rolled = layer_stack_from_pin(TENANT_ID, rolled, entitled=False)
    assert await fingerprint_base_layer(n, stack_rolled) == fp_v1
    assert stack_rolled.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 1)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_ensure_at_latest_then_noop():
    n = MemNeptune()
    await _seed_public_v1(n)
    await _publish_public_v2(n)

    assert await get_base_pin(n, TENANT_ID) is None

    pin1 = await ensure_workspace_base_pin(n, TENANT_ID, entitled=False)
    assert pin1.base_layer == "public"
    assert pin1.base_version == 2  # latest
    assert pin1.auto_upgrade is False
    assert pin1.has_previous is False

    pin2 = await ensure_workspace_base_pin(n, TENANT_ID, entitled=False)
    assert pin2.base_version == 2
    assert pin2.updated_at == pin1.updated_at  # no rewrite on second ensure

    # No releases → live pin
    n2 = MemNeptune()
    pin_live = await ensure_workspace_base_pin(n2, "empty", entitled=False)
    assert pin_live.base_version is None
    assert pin_live.is_live


# ---------------------------------------------------------------------------
# Collision preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collision_preview_tenant_overlaps_base_addition():
    n = MemNeptune()
    await _seed_public_v1(n)

    # Tenant overlay already defines Person.risk_score
    await commit_ontology(
        n,
        TENANT,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="risk_score",
                datatype="float",
            ),
        ],
        actor="tenant",
        message="tenant risk_score",
    )

    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )

    # v2 of public also adds Person.risk_score
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="risk_score",
                datatype="float",
            ),
        ],
        actor="ops",
        message="public risk_score",
    )
    await snapshot_ontology(n, PUBLIC, kind="release", version=2, publisher="ops")

    preview = await preview_base_upgrade(
        n, TENANT_ID, entitled=False, to_version=2
    )
    assert preview.from_version == 1
    assert preview.to_version == 2
    assert any(
        c.kind is ChangeKind.ADD_ATTRIBUTE
        and c.type_name == "Person"
        and c.slot_name == "risk_score"
        for c in preview.changes
    )
    assert any(
        c.type_name == "Person" and c.slot_name == "risk_score"
        for c in preview.collisions
    )
    assert any("risk_score" in s for s in preview.summary)


# ---------------------------------------------------------------------------
# Entitlement degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entitlement_degrade_enhanced_pin_without_entitlement():
    n = MemNeptune()
    # Seed enhanced v1
    await commit_ontology(
        n,
        ENHANCED,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Org"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Org",
                slot_name="lei",
                datatype="string",
            ),
        ],
        actor="ops",
        message="enhanced v1",
    )
    await snapshot_ontology(n, ENHANCED, kind="release", version=7, publisher="ops")

    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(
            base_layer="enhanced",
            base_version=7,
            tenant_id=TENANT_ID,
        ),
    )

    # entitled=True → enhanced pin applied
    stack_paid = layer_stack_from_pin(
        TENANT_ID,
        await get_base_pin(n, TENANT_ID),
        entitled=True,
    )
    assert Layer.ENHANCED in stack_paid.layers
    assert stack_paid.enhanced_version == 7
    assert stack_paid.graph_uri_for(Layer.ENHANCED) == release_graph_uri(ENHANCED, 7)

    # entitled=False → enhanced excluded, no throw; public still in stack
    stack_free = layer_stack_from_pin(
        TENANT_ID,
        await get_base_pin(n, TENANT_ID),
        entitled=False,
    )
    assert Layer.ENHANCED not in stack_free.layers
    assert Layer.PUBLIC in stack_free.layers
    assert stack_free.graph_uri_for(Layer.PUBLIC) == public_graph_uri()

    # layer_stack_for_workspace also does not throw
    stack_ws = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=False
    )
    assert Layer.ENHANCED not in stack_ws.layers


# ---------------------------------------------------------------------------
# Deprecation preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_deprecation_used_on_tenant_shape():
    n = MemNeptune()
    await _seed_public_v1(n)

    # Tenant has Person (and name) — overlapping the base type.
    await commit_ontology(
        n,
        TENANT,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="name",
                datatype="string",
            ),
        ],
        actor="tenant",
        message="tenant person",
    )

    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )

    # v2 deprecates Person.name on public
    await commit_ontology(
        n,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                slot_name="name",
                superseded_by="full_name",
            ),
        ],
        actor="ops",
        message="deprecate name",
    )
    await snapshot_ontology(n, PUBLIC, kind="release", version=2, publisher="ops")

    preview = await preview_base_upgrade(
        n, TENANT_ID, entitled=False, to_version=2
    )
    assert any(c.kind is ChangeKind.DEPRECATE for c in preview.changes)
    assert any(
        d.kind is ChangeKind.DEPRECATE
        and d.type_name == "Person"
        and d.slot_name == "name"
        for d in preview.deprecated_used
    )
    assert any("deprecated" in s.lower() or "name" in s for s in preview.summary)


# ---------------------------------------------------------------------------
# Helpers / URI
# ---------------------------------------------------------------------------


def test_base_pin_graph_uri():
    assert base_pin_graph_uri("acme") == "https://graph.infona.ai/graphs/acme/base-pin"
    with pytest.raises(ValueError):
        base_pin_graph_uri("")


@pytest.mark.asyncio
async def test_rollback_without_previous_raises():
    n = MemNeptune()
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    with pytest.raises(ValueError, match="previous_version"):
        await rollback_base_pin(n, TENANT_ID)


# ---------------------------------------------------------------------------
# B1: read failure must not re-pin to latest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_read_failure_does_not_repin_to_latest():
    """Pin at v1, auto_upgrade=False; pin SELECT raises → ensure must not
    CLEAR/INSERT and must not change the stored version (review B1)."""
    n = MemNeptune()
    await _seed_public_v1(n)
    await _publish_public_v2(n)

    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(
            base_layer="public",
            base_version=1,
            auto_upgrade=False,
            tenant_id=TENANT_ID,
        ),
    )
    pin_before = await get_base_pin(n, TENANT_ID)
    assert pin_before is not None and pin_before.base_version == 1

    updates_before = len(n.updates)
    original_query = n.query

    async def failing_pin_query(sparql: str):
        if "WorkspaceBasePin" in sparql:
            raise RuntimeError("neptune unavailable")
        return await original_query(sparql)

    n.query = failing_pin_query  # type: ignore[method-assign]

    with pytest.raises(BasePinReadError):
        await get_base_pin(n, TENANT_ID)

    with pytest.raises(BasePinReadError):
        await ensure_workspace_base_pin(n, TENANT_ID, entitled=False)

    # No pin-graph writes after the failure.
    new_updates = n.updates[updates_before:]
    pin_g = base_pin_graph_uri(TENANT_ID)
    assert not any(pin_g in u for u in new_updates), new_updates

    # Soft degrade on workspace stack: live, no write.
    stack = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=True
    )
    assert stack.public_version is None
    assert stack.graph_uri_for(Layer.PUBLIC) == PUBLIC
    assert not any(pin_g in u for u in n.updates[updates_before:])

    # Pin still v1 once reads work again.
    n.query = original_query  # type: ignore[method-assign]
    pin_after = await get_base_pin(n, TENANT_ID)
    assert pin_after is not None
    assert pin_after.base_version == 1
    assert pin_after.auto_upgrade is False


@pytest.mark.asyncio
async def test_upgrade_refuses_missing_target_version():
    n = MemNeptune()
    await _seed_public_v1(n)
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    with pytest.raises(ValueError, match="no public release v99"):
        await upgrade_base_pin(n, TENANT_ID, entitled=False, to_version=99)
    pin = await get_base_pin(n, TENANT_ID)
    assert pin is not None and pin.base_version == 1


def test_fingerprint_base_uri_entitled_uses_enhanced():
    """N2: entitled live stack keys base fingerprint off Enhanced."""
    stack = LayerStack(TENANT, entitled=True)
    assert base_graph_uri_for_stack(stack) == ENHANCED
    stack_pinned = LayerStack(TENANT, entitled=True, enhanced_version=3)
    assert base_graph_uri_for_stack(stack_pinned) == release_graph_uri(ENHANCED, 3)
    free = LayerStack(TENANT, entitled=False, public_version=2)
    assert base_graph_uri_for_stack(free) == release_graph_uri(PUBLIC, 2)
