"""INF-564 redaction: classify, strip, or fail closed.

Export is the moment a private workspace becomes a publishable artifact.
Anything we cannot confidently put in one of the allowed Blueprint
categories must raise :class:`ExportRedactionError` rather than ship.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse, urlunparse

from infona_client.blueprint.models import FORBIDDEN_TOP_LEVEL_KEYS
from infona_client.graph.iri import ATTR_META_NS, ENTITY_URI_PREFIX
from infona_client.graph.predicates import ATTR_META_SUFFIXES

#: Workspace-only categories INF-564 / INF-565 must never emit.
WORKSPACE_SIDE_CATEGORIES: frozenset[str] = frozenset(
    {
        "records",
        "credentials",
        "scheduled_jobs",
        "citations_provenance",
        "freshness_status",
    }
)

#: Keys that classify as workspace-only wherever they appear.
FORBIDDEN_LEAVES: frozenset[str] = frozenset(FORBIDDEN_TOP_LEVEL_KEYS) | frozenset(
    ATTR_META_SUFFIXES
) | frozenset(
    {
        "citation",
        "citations",
        "password",
        "token",
        "api_key",
        "access_token",
        "secret",
        "secret_ref",
        "secret_refs",
        "endpoint_url",
        "sample_values",
        "last_run",
        "next_run",
        "cron",
        "interval_seconds",
        "enabled",
        "job_id",
        "schedule_id",
    }
)

_URL_USERINFO = re.compile(r"://[^/\s@]+:[^/\s@]+@")
_URL_CRED_PARAM = re.compile(
    r"[?&](?:api[_-]?key|apikey|token|access[_-]?token|secret|password|passwd|key)="
    r"[^&\s]+",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"^(?:sk-|rk-|pk_live_|pk_test_|Bearer\s+|Basic\s+)[A-Za-z0-9_\-.=]{8,}$"
)
_ENV_ASSIGNMENT = re.compile(
    r"\b[A-Z][A-Z0-9_]{2,}=(sk-|rk-|pk_|eyJ|[A-Za-z0-9+/]{24,})"
)


class ExportRedactionError(ValueError):
    """Export hit something it cannot classify. Fail closed — do not ship."""


def classify_key(key: str) -> str | None:
    """Return a workspace-side category name, or None if the key is allowed."""
    leaf = key.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    if leaf in ATTR_META_SUFFIXES or leaf.endswith("_source_url") or leaf.endswith(
        "_provenance"
    ):
        return "citations_provenance"
    if leaf in {
        "credentials",
        "secrets",
        "tokens",
        "api_key",
        "api_keys",
        "password",
        "token",
        "secret",
        "secret_ref",
        "access_token",
    }:
        return "credentials"
    if leaf in {
        "jobs",
        "schedules",
        "cron",
        "last_run",
        "next_run",
        "job_id",
        "schedule_id",
        "interval_seconds",
    }:
        return "scheduled_jobs"
    if leaf in {
        "freshness_status",
        "last_refresh",
        "source_health",
        "staleness",
    }:
        return "freshness_status"
    if leaf in {"records", "entities", "instances", "data", "triples", "graph"}:
        return "records"
    if leaf in FORBIDDEN_LEAVES:
        return "unclassified"
    return None


def redact_definition_url(url: str) -> str:
    """Strip userinfo and credential query params from a source *definition* URL.

    Raises if the URL is empty after redaction or still embeds credentials.
    """
    if not url or not str(url).strip():
        raise ExportRedactionError("source url is empty; cannot classify")
    raw = str(url).strip()
    parsed = urlparse(raw)
    if parsed.username or parsed.password or _URL_USERINFO.search(raw):
        host = parsed.hostname or ""
        netloc = host if not parsed.port else f"{host}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    if parsed.query and _URL_CRED_PARAM.search("?" + parsed.query):
        kept = []
        for part in parsed.query.split("&"):
            if not part:
                continue
            if _URL_CRED_PARAM.search("&" + part) or _URL_CRED_PARAM.search("?" + part):
                continue
            kept.append(part)
        parsed = parsed._replace(query="&".join(kept))
    cleaned = urlunparse(parsed)
    if _url_embeds_credentials(cleaned):
        raise ExportRedactionError(
            f"source url still embeds credentials after redaction: {cleaned!r}"
        )
    if not cleaned:
        raise ExportRedactionError("source url redacted to empty")
    return cleaned


def _url_embeds_credentials(url: str) -> bool:
    if _URL_USERINFO.search(url) or _URL_CRED_PARAM.search(url):
        return True
    parsed = urlparse(url)
    return bool(parsed.username or parsed.password)


def assert_exportable(value: Any, *, path: str = "<root>") -> None:
    """Walk a would-be package and fail on anything workspace-side or unknown.

    Allowed leaves are the frozen v1 keys plus ordinary strings/numbers.
    Instance IRIs, attr_meta URIs, secret-shaped values, and forbidden keys
    all raise :class:`ExportRedactionError`.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExportRedactionError(
                    f"{path}: mapping key {key!r} is not a string; cannot classify"
                )
            category = classify_key(key)
            if category is not None:
                raise ExportRedactionError(
                    f"{path}.{key}: workspace-side {category} is not exportable"
                )
            if "/attr_meta/" in key or key.startswith(ATTR_META_NS):
                raise ExportRedactionError(
                    f"{path}.{key}: attr_meta provenance companion is not exportable"
                )
            assert_exportable(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            assert_exportable(item, path=f"{path}[{i}]")
        return
    if isinstance(value, str):
        _assert_exportable_string(value, path)
        return
    if value is None or isinstance(value, (int, float, bool)):
        return
    raise ExportRedactionError(
        f"{path}: value of type {type(value).__name__} cannot be classified"
    )


def _assert_exportable_string(value: str, path: str) -> None:
    if value.startswith(ENTITY_URI_PREFIX) or "/entities/" in value and "graph.infona.ai" in value:
        raise ExportRedactionError(
            f"{path}: instance entity URI is workspace-side (actual records)"
        )
    if ATTR_META_NS in value or "/attr_meta/" in value:
        raise ExportRedactionError(
            f"{path}: attr_meta provenance companion URI is not exportable"
        )
    if _url_embeds_credentials(value):
        raise ExportRedactionError(
            f"{path}: value embeds credentials"
        )
    if _SECRET_VALUE.match(value.strip()) or _ENV_ASSIGNMENT.search(value):
        raise ExportRedactionError(
            f"{path}: value looks like a credential"
        )


def scan_text_for_workspace_leak(
    text: str,
    *,
    banned_markers: Iterable[str] = (),
) -> list[str]:
    """Return human-readable leak descriptions found in serialized output."""
    leaks: list[str] = []
    if ENTITY_URI_PREFIX in text or "/entities/" in text and "graph.infona.ai" in text:
        leaks.append("actual records (entity URI)")
    if ATTR_META_NS in text or "/attr_meta/" in text:
        leaks.append("citations/provenance (attr_meta)")
    for marker in banned_markers:
        if marker and marker in text:
            leaks.append(f"seeded workspace marker {marker!r}")
    for key in (
        "last_run",
        "next_run",
        "last_refresh",
        "freshness_status",
        "source_health",
        "secret_ref",
        "api_key",
    ):
        if re.search(rf"(^|[^A-Za-z0-9_]){key}([^A-Za-z0-9_]|$)", text):
            leaks.append(f"workspace-side key {key!r}")
    return leaks


__all__ = [
    "FORBIDDEN_LEAVES",
    "WORKSPACE_SIDE_CATEGORIES",
    "ExportRedactionError",
    "assert_exportable",
    "classify_key",
    "redact_definition_url",
    "scan_text_for_workspace_leak",
]
