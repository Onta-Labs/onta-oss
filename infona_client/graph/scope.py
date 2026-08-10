"""Tenant + KG scope for the Neo4j GraphStore session (ADR 0012 / model §3).

Isolation is structural: every node and relationship carries ``tenant_id`` and
``kg``. The session is constructed with an immutable scope and **forces** those
values onto every parameterized query — callers never get to pick a different
workspace via Cypher parameters (ONTA-424 successor / model §3.3 T2).

This module has **no** ``neo4j`` import: scope is pure protocol surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from infona_client.graph.queries import (
    InvalidKGName,
    is_valid_kg_name,
    require_valid_tenant_id,
)

# Scope sentinels frozen in the property-graph model (§3.1). Keep as module
# constants so writers and catalog readers never scatter magic strings.
GLOBAL_TENANT_ID = "__global__"
ONTOLOGY_KG = "__ontology__"
PUBLIC_KG = "public"
ENHANCED_KG = "enhanced"

# Reserved kg values that are legal but not ordinary instance KG names.
# ``__ontology__`` matches ``is_valid_kg_name``; public/enhanced are catalog
# sentinels under ``tenant_id=__global__``.
_RESERVED_KG_SENTINELS = frozenset({ONTOLOGY_KG, PUBLIC_KG, ENHANCED_KG})


class GraphScopeError(ValueError):
    """Scope construction or session enforcement rejected the operation.

    Distinct from query failures: this is a client/API contract violation
    (missing scope, wrong tenant, unscoped Cypher), not a Neo4j error.
    """


@dataclass(frozen=True, slots=True)
class GraphScope:
    """Immutable unit-of-work scope for instance or catalog work.

    Parameters
    ----------
    tenant_id:
        Workspace id. Validated with the same spirit as
        :func:`require_valid_tenant_id`. The global catalog uses
        :data:`GLOBAL_TENANT_ID`.
    kg:
        Knowledge-graph name for instance data, or a reserved sentinel
        (``__ontology__`` / ``public`` / ``enhanced``) for catalog layers.
    database:
        Optional Neo4j database name. Wave 1 default is a single DB; this
        field exists so premium multi-DB routing later is a connection concern,
        not a model rewrite. ``None`` means "driver default / env".
    privileged:
        When True, the session may write with ``tenant_id=__global__``
        (governance/admin). App sessions leave this False (model §3.3 T7).
    """

    tenant_id: str
    kg: str
    database: str | None = None
    privileged: bool = False

    def __post_init__(self) -> None:
        # Validate tenant_id (allows __global__; write gate is separate).
        object.__setattr__(self, "tenant_id", require_valid_tenant_id(self.tenant_id))
        if not isinstance(self.kg, str) or not self.kg:
            raise InvalidKGName(
                f"Invalid kg {self.kg!r}: must be a non-empty string"
            )
        if self.kg not in _RESERVED_KG_SENTINELS and not is_valid_kg_name(self.kg):
            raise InvalidKGName(
                f"Invalid kg {self.kg!r}: must be one or more of [a-zA-Z0-9_-] "
                "or a reserved catalog sentinel (__ontology__/public/enhanced)"
            )
        if self.database is not None and (
            not isinstance(self.database, str) or not self.database.strip()
        ):
            raise GraphScopeError(
                f"Invalid database name {self.database!r}: must be non-empty when set"
            )

    @classmethod
    def for_instance(
        cls,
        tenant_id: str,
        kg: str,
        *,
        database: str | None = None,
    ) -> GraphScope:
        """Scope for ordinary instance-KG reads/writes.

        Rejects the global tenant and ontology/catalog kg sentinels so a
        mis-wired caller cannot accidentally land instance facts in catalog
        space.
        """
        tid = require_valid_tenant_id(tenant_id)
        if tid == GLOBAL_TENANT_ID:
            raise GraphScopeError(
                "for_instance cannot use the global catalog tenant; "
                "use for_catalog(...) or a privileged scope"
            )
        if kg in _RESERVED_KG_SENTINELS:
            raise GraphScopeError(
                f"for_instance cannot use reserved kg {kg!r}; "
                "use for_catalog(...) for ontology layers"
            )
        if not is_valid_kg_name(kg):
            raise InvalidKGName(
                f"Invalid kg_name {kg!r}: must be one or more of [a-zA-Z0-9_-]"
            )
        return cls(tenant_id=tid, kg=kg, database=database, privileged=False)

    @classmethod
    def for_catalog(
        cls,
        *,
        layer: str,
        tenant_id: str | None = None,
        database: str | None = None,
        privileged: bool = False,
    ) -> GraphScope:
        """Scope for ontology catalog elements.

        * ``layer="public"`` / ``"enhanced"`` → ``tenant_id=__global__``,
          ``kg`` matching the layer name.
        * ``layer="tenant"`` → real ``tenant_id`` + ``kg=__ontology__``.
        """
        layer_norm = (layer or "").strip().lower()
        if layer_norm == "public":
            return cls(
                tenant_id=GLOBAL_TENANT_ID,
                kg=PUBLIC_KG,
                database=database,
                privileged=privileged,
            )
        if layer_norm == "enhanced":
            return cls(
                tenant_id=GLOBAL_TENANT_ID,
                kg=ENHANCED_KG,
                database=database,
                privileged=privileged,
            )
        if layer_norm == "tenant":
            if tenant_id is None:
                raise GraphScopeError(
                    "for_catalog(layer='tenant') requires tenant_id"
                )
            tid = require_valid_tenant_id(tenant_id)
            if tid == GLOBAL_TENANT_ID:
                raise GraphScopeError(
                    "tenant catalog layer requires a real workspace tenant_id, "
                    "not __global__"
                )
            return cls(
                tenant_id=tid,
                kg=ONTOLOGY_KG,
                database=database,
                privileged=privileged,
            )
        raise GraphScopeError(
            f"Unknown catalog layer {layer!r}; expected public|enhanced|tenant"
        )

    def as_params(self) -> dict[str, str]:
        """Scope parameters injected into every Cypher call."""
        return {"tenant_id": self.tenant_id, "kg": self.kg}

    def allows_global_write(self) -> bool:
        """Whether this session may mutate global-catalog elements."""
        if self.tenant_id != GLOBAL_TENANT_ID:
            return True
        return self.privileged
