"""Classify a Bolt/Neo4j URI host without echoing the URI.

Used by ``GET /health`` so a pinned RFC1918 Bolt target (the 2026-08-24
Explorer hang: API still on ``bolt://10.0.10.176:7687`` after Neo4j's ENI
moved) is visible as ``neo4j_uri_kind=private_ip`` instead of a silent
connect timeout. Never log or return the raw host — kinds only.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# Kind strings are the health-payload contract. Keep them stable.
KIND_MISSING = "missing"
KIND_LOOPBACK = "loopback"
KIND_PRIVATE_IP = "private_ip"
KIND_PUBLIC_IP = "public_ip"
KIND_HOSTNAME = "hostname"


def classify_bolt_uri(uri: str | None) -> str:
    """Return a host kind for ``NEO4J_URI``.

    * ``missing`` — empty / unparseable
    * ``loopback`` — localhost / 127.0.0.0/8 / ::1 (OSS local docker)
    * ``private_ip`` — RFC1918 / link-local / reserved (hosted pin; dies on
      Fargate ENI replace)
    * ``public_ip`` — any other literal IP
    * ``hostname`` — DNS name (Cloud Map ``neo4j.infona.local`` on hosted)
    """
    raw = (uri or "").strip()
    if not raw:
        return KIND_MISSING
    try:
        parsed = urlparse(raw if "://" in raw else f"bolt://{raw}")
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return KIND_MISSING
    if not host:
        return KIND_MISSING
    if host in {"localhost", "localhost.localdomain"}:
        return KIND_LOOPBACK
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return KIND_HOSTNAME
    if ip.is_loopback:
        return KIND_LOOPBACK
    if ip.is_private or ip.is_link_local or ip.is_reserved:
        return KIND_PRIVATE_IP
    return KIND_PUBLIC_IP
