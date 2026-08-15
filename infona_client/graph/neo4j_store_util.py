"""Neo4j driver import + record coercion helpers.

No store I/O. Shared by :class:`Neo4jGraphSession` / :class:`Neo4jGraphStore`.
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.store import GraphConfigError

_MAX_TRANSPORT_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.5


def _import_neo4j():
    """Import the official driver; raise a clear config error if missing."""
    try:
        import neo4j
        from neo4j.exceptions import (
            AuthError,
            ClientError,
            DriverError,
            Neo4jError,
            ServiceUnavailable,
            SessionExpired,
            TransientError,
        )
    except ImportError as exc:  # pragma: no cover - env dependent
        raise GraphConfigError(
            "The neo4j package is not installed. Install the optional extra: "
            "pip install 'infona-client[neo4j]' (or pip install neo4j)"
        ) from exc
    return neo4j, {
        "AuthError": AuthError,
        "ClientError": ClientError,
        "DriverError": DriverError,
        "Neo4jError": Neo4jError,
        "ServiceUnavailable": ServiceUnavailable,
        "SessionExpired": SessionExpired,
        "TransientError": TransientError,
    }


def _node_to_plain(value: Any) -> Any:
    """Convert neo4j driver values to plain Python for :class:`GraphRecord`."""
    # Avoid importing neo4j types at module level for protocol purity of store.py;
    # isinstance checks use duck typing on common attributes.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_node_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _node_to_plain(v) for k, v in value.items()}
    # neo4j.graph.Node / Relationship / Path — expose as dict-ish
    labels = getattr(value, "labels", None)
    if labels is not None and hasattr(value, "items"):
        data = {k: _node_to_plain(v) for k, v in value.items()}
        data["_labels"] = list(labels)
        if hasattr(value, "element_id"):
            data["_element_id"] = value.element_id
        return data
    if hasattr(value, "type") and hasattr(value, "items") and hasattr(value, "start_node"):
        data = {k: _node_to_plain(v) for k, v in value.items()}
        data["_type"] = value.type
        return data
    # DateTime / Date / etc. — stringify for portable Wave-1 ISO preference
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return value
