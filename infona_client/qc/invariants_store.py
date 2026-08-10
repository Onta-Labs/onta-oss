"""Property-graph structural invariants (E8) — GraphStore / Cypher path.

Mirrors the spirit of :mod:`infona_client.qc.invariants` (RDFUnit-style
violation list) but for Neo4j-era instance data:

* entities missing ``primary_type``
* relationships missing ``tenant_id`` / ``kg`` (scope on the rel)
* orphan relationship endpoints (target/start Entity missing in scope)
* **ADR 0013 dual-write skew** — derived ``INSTANCE_OF`` without a type Assertion
  (Assertion is SoT; Entity props / ``INSTANCE_OF`` / shortcut rels are derived)

These are **cheap structural** checks — not the full SPARQL ontology-aware
suite (attrs-vs-onto, range membership). The SPARQL path in ``invariants.py``
is retained and must not be deleted.

Prefer native Memory scans in hermetic tests; Neo4j uses parameterized Cypher
via :meth:`GraphSession.execute_read` (admin/QC surface).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from infona_client.qc.invariants import Violation

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

# Stable names for include= filters (distinct from SPARQL invariant names).
STORE_INVARIANT_MISSING_PRIMARY_TYPE = "entity_missing_primary_type"
STORE_INVARIANT_REL_MISSING_SCOPE = "relationship_missing_scope"
STORE_INVARIANT_ORPHAN_REL_TARGET = "orphan_relationship_endpoint"
# ADR 0013: derived INSTANCE_OF cache without backing type Assertion SoT.
STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION = (
    "instance_of_without_type_assertion"
)

STORE_INVARIANT_NAMES: frozenset[str] = frozenset(
    {
        STORE_INVARIANT_MISSING_PRIMARY_TYPE,
        STORE_INVARIANT_REL_MISSING_SCOPE,
        STORE_INVARIANT_ORPHAN_REL_TARGET,
        STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION,
    }
)

# Cypher used when the session has no Memory-style scan helpers.
_CYPHER_MISSING_PRIMARY = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE e.primary_type IS NULL OR e.primary_type = ''
RETURN e.id AS id
""".strip()

_CYPHER_REL_MISSING_SCOPE = """
MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg})-[r]->(b:Entity)
WHERE r.tenant_id IS NULL OR r.tenant_id = '' OR r.kg IS NULL OR r.kg = ''
RETURN a.id AS start_id, b.id AS end_id, type(r) AS rel_type,
       coalesce(r.attr, '') AS attr,
       coalesce(r.tenant_id, '') AS tenant_id, coalesce(r.kg, '') AS kg
""".strip()

# Orphan: relationship end is missing Entity label or wrong/missing scope.
_CYPHER_ORPHAN_V2 = """
MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg})-[r]->(b)
WHERE NOT b:Entity OR b.tenant_id <> $tenant_id OR b.kg <> $kg
   OR b.id IS NULL
RETURN a.id AS start_id, coalesce(b.id, '') AS end_id,
       coalesce(r.attr, type(r)) AS attr, 'end' AS side
""".strip()

# ADR 0013 skew: INSTANCE_OF cache edge with no type Assertion (OBJECT_CLASS).
_CYPHER_INSTANCE_OF_NO_TYPE_ASSERTION = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
      -[:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg})
WHERE NOT EXISTS {
  MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
        -[:OBJECT_CLASS]->(c)
}
RETURN e.id AS entity_id, c.id AS class_id
""".strip()


def _selected(include: Optional[set[str]]) -> set[str]:
    if include is None:
        return set(STORE_INVARIANT_NAMES)
    return set(include) & STORE_INVARIANT_NAMES


def _scope_from_session(session: "GraphSession") -> tuple[str, str]:
    scope = getattr(session, "scope", None)
    if scope is None:
        raise ValueError("GraphSession must carry a GraphScope for store invariants")
    return str(scope.tenant_id), str(scope.kg)


def _record_to_dict(rec: Any) -> dict[str, Any]:
    if hasattr(rec, "to_dict"):
        return rec.to_dict()
    if hasattr(rec, "data") and isinstance(rec.data, dict):
        return dict(rec.data)
    return dict(rec)


def _violation_instance_of_skew(row: dict[str, Any]) -> Violation:
    eid = row.get("entity_id", "")
    cid = row.get("class_id", "")
    return Violation(
        invariant=STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION,
        severity="error",
        detail=(
            f"{eid} -[:INSTANCE_OF]-> {cid} "
            "(derived INSTANCE_OF with no type Assertion — ADR 0013 dual-write skew)"
        ),
        binding=dict(row),
    )


async def check_assertion_cache_skew(
    session: "GraphSession",
    *,
    include_entity_props: bool = False,
) -> list[Violation]:
    """Detect ADR 0013 dual-write skew: derived cache present without Assertion SoT.

    **Mandate:** Assertion is source of truth. Entity property cache,
    ``INSTANCE_OF``, and shortcut rels are **derived only**.

    Currently reports:

    * ``instance_of_without_type_assertion`` — Entity has
      ``-[:INSTANCE_OF]->(Class)`` with no type Assertion linking that subject
      to that Class via ``OBJECT_CLASS``.

    Entity property cache without a matching datatype Assertion is **optional**
    (``include_entity_props=True``) and off by default: many dual-write paths
    only cache a subset of leaves (``name`` / ``source`` / hot props), so a
    full prop scan is noisy relative to the high-signal ``INSTANCE_OF`` check.

    Prefer Memory native scans when present; otherwise Cypher via
    :meth:`GraphSession.execute_read`.
    """
    tenant_id, kg = _scope_from_session(session)
    violations: list[Violation] = []

    store = getattr(session, "_store", None)
    if store is not None and hasattr(
        store, "scan_instance_of_without_type_assertion"
    ):
        for row in store.scan_instance_of_without_type_assertion(tenant_id, kg):
            violations.append(_violation_instance_of_skew(row))
    else:
        rows = await session.execute_read(
            _CYPHER_INSTANCE_OF_NO_TYPE_ASSERTION, {}
        )
        for rec in rows:
            violations.append(_violation_instance_of_skew(_record_to_dict(rec)))

    # Reserved for a quieter prop-cache scan later; not wired by default.
    if include_entity_props:
        # Deliberately empty: full Entity.props ⊆ Assertion is too noisy until
        # dual-write coverage is complete for every leaf (see module doc).
        pass

    violations.sort(key=lambda v: 0 if v.severity == "error" else 1)
    return violations


async def check_store_invariants(
    session: "GraphSession",
    *,
    include: Optional[set[str]] = None,
) -> list[Violation]:
    """Run structural store-path invariants in session scope; return violations."""
    names = _selected(include)
    if not names:
        return []
    tenant_id, kg = _scope_from_session(session)
    violations: list[Violation] = []

    # Prefer native Memory scans (hermetic, no Cypher parser).
    store = getattr(session, "_store", None)
    if store is not None and hasattr(store, "scan_entities_missing_primary_type"):
        violations.extend(
            await _check_via_memory_scans(store, tenant_id, kg, names)
        )
        violations.sort(
            key=lambda v: 0 if v.severity == "error" else 1
        )
        return violations

    violations.extend(await _check_via_cypher(session, names))
    violations.sort(key=lambda v: 0 if v.severity == "error" else 1)
    return violations


async def _check_via_memory_scans(
    store: Any,
    tenant_id: str,
    kg: str,
    names: set[str],
) -> list[Violation]:
    out: list[Violation] = []
    if STORE_INVARIANT_MISSING_PRIMARY_TYPE in names:
        for row in store.scan_entities_missing_primary_type(tenant_id, kg):
            eid = row.get("id", "")
            out.append(
                Violation(
                    invariant=STORE_INVARIANT_MISSING_PRIMARY_TYPE,
                    severity="error",
                    detail=f"{eid} (Entity missing primary_type)",
                    binding=dict(row),
                )
            )
    if STORE_INVARIANT_REL_MISSING_SCOPE in names:
        for row in store.scan_rels_missing_scope(tenant_id, kg):
            out.append(
                Violation(
                    invariant=STORE_INVARIANT_REL_MISSING_SCOPE,
                    severity="error",
                    detail=(
                        f"{row.get('start_id')} --[{row.get('attr')}]--> "
                        f"{row.get('end_id')} (relationship missing tenant_id/kg)"
                    ),
                    binding=dict(row),
                )
            )
    if STORE_INVARIANT_ORPHAN_REL_TARGET in names:
        for row in store.scan_orphan_rel_targets(tenant_id, kg):
            out.append(
                Violation(
                    invariant=STORE_INVARIANT_ORPHAN_REL_TARGET,
                    severity="error",
                    detail=(
                        f"{row.get('start_id')} --[{row.get('attr')}]--> "
                        f"{row.get('end_id')} (orphan rel {row.get('side')} endpoint)"
                    ),
                    binding=dict(row),
                )
            )
    if STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION in names:
        scan = getattr(store, "scan_instance_of_without_type_assertion", None)
        if callable(scan):
            for row in scan(tenant_id, kg):
                out.append(_violation_instance_of_skew(row))
    return out


async def _check_via_cypher(
    session: "GraphSession",
    names: set[str],
) -> list[Violation]:
    out: list[Violation] = []
    if STORE_INVARIANT_MISSING_PRIMARY_TYPE in names:
        rows = await session.execute_read(_CYPHER_MISSING_PRIMARY, {})
        for rec in rows:
            eid = rec.get("id", "")
            out.append(
                Violation(
                    invariant=STORE_INVARIANT_MISSING_PRIMARY_TYPE,
                    severity="error",
                    detail=f"{eid} (Entity missing primary_type)",
                    binding=_record_to_dict(rec),
                )
            )
    if STORE_INVARIANT_REL_MISSING_SCOPE in names:
        rows = await session.execute_read(_CYPHER_REL_MISSING_SCOPE, {})
        for rec in rows:
            d = _record_to_dict(rec)
            out.append(
                Violation(
                    invariant=STORE_INVARIANT_REL_MISSING_SCOPE,
                    severity="error",
                    detail=(
                        f"{d.get('start_id')} --[{d.get('attr')}]--> "
                        f"{d.get('end_id')} (relationship missing tenant_id/kg)"
                    ),
                    binding=d,
                )
            )
    if STORE_INVARIANT_ORPHAN_REL_TARGET in names:
        rows = await session.execute_read(_CYPHER_ORPHAN_V2, {})
        for rec in rows:
            d = _record_to_dict(rec)
            out.append(
                Violation(
                    invariant=STORE_INVARIANT_ORPHAN_REL_TARGET,
                    severity="error",
                    detail=(
                        f"{d.get('start_id')} --[{d.get('attr')}]--> "
                        f"{d.get('end_id')} (orphan rel {d.get('side')} endpoint)"
                    ),
                    binding=d,
                )
            )
    if STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION in names:
        rows = await session.execute_read(
            _CYPHER_INSTANCE_OF_NO_TYPE_ASSERTION, {}
        )
        for rec in rows:
            out.append(_violation_instance_of_skew(_record_to_dict(rec)))
    return out


async def check_invariants_for_store(
    store: "GraphStore",
    tenant_id: str,
    kg: str,
    *,
    include: Optional[set[str]] = None,
) -> list[Violation]:
    """Open a scoped session on ``store`` and run :func:`check_store_invariants`."""
    from infona_client.graph.scope import GraphScope

    session = store.session(GraphScope.for_instance(tenant_id, kg))
    return await check_store_invariants(session, include=include)


__all__ = [
    "STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION",
    "STORE_INVARIANT_MISSING_PRIMARY_TYPE",
    "STORE_INVARIANT_ORPHAN_REL_TARGET",
    "STORE_INVARIANT_REL_MISSING_SCOPE",
    "STORE_INVARIANT_NAMES",
    "check_assertion_cache_skew",
    "check_invariants_for_store",
    "check_store_invariants",
]
