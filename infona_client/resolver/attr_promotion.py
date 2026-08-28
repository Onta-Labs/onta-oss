"""Prefix-cluster promotion gates (Option D).

Defense-in-depth for ``check_promotion``: ordinal/aggregate prefixes and
identifier-only leftover leaves never mint a type, even when promotion is
enabled. CSV mapped ingest skips ``check_promotion`` entirely.
"""

from __future__ import annotations

from infona_client.resolver.models import ExtractedAttribute

# Minimum attributes sharing a prefix to pass the cluster test.
_PROMOTION_CLUSTER_MIN = 3

# Identity leaves (the short name after the shared prefix is stripped) that
# satisfy the "can you point at this sub-concept?" test. Domain-free.
_IDENTITY_LEAVES = frozenset(
    {
        "name",
        "id",
        "label",
        "title",
        "street",
        "code",
        "number",
        "key",
        "identifier",
        "uri",
        "url",
        "address",
        "value",
        "line1",
        "line_1",
    }
)

# Ordinal/aggregate prefixes that never name an entity (domain-free; not CRM nouns).
_ORDINAL_PREFIXES = frozenset(
    {"last", "first", "next", "total", "previous", "current", "original"}
)

_IDENTIFIER_LEAVES = frozenset({"id", "key", "identifier"})


def _leftover_leaf(prefix: str, attr_name: str) -> str:
    from infona_client.resolver.attribute_resolver import _normalize_attr_name

    short = _normalize_attr_name(attr_name)
    if short.startswith(prefix + "_"):
        short = short[len(prefix) + 1 :]
    return short


def _cluster_has_identity(prefix: str, attrs: list[ExtractedAttribute]) -> bool:
    """True when the cluster has an identity leaf (name/id/street/…) — test 1."""
    for attr in attrs:
        short = _leftover_leaf(prefix, attr.name)
        last = short.rsplit("_", 1)[-1]
        if short in _IDENTITY_LEAVES or last in _IDENTITY_LEAVES:
            return True
    return False


def _is_identifier_leaf(leaf: str) -> bool:
    return bool(leaf) and (leaf in _IDENTIFIER_LEAVES or leaf.endswith("_id"))


def prefix_cluster_skip_event(
    prefix: str, attrs: list[ExtractedAttribute],
) -> str | None:
    """Structured log event when a prefix cluster must not mint a type, else None."""
    if prefix in _ORDINAL_PREFIXES:
        return "attr_promotion_rejected_ordinal_prefix"
    leftover = [_leftover_leaf(prefix, a.name) for a in attrs]
    if leftover and all(_is_identifier_leaf(leaf) for leaf in leftover):
        return "attr_promotion_rejected_identifier_cluster"
    return None
