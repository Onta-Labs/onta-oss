"""One Infona wrapper that owns ``import dlt`` (ONTA-553).

Extract only. Yields ``{resource_name, rows}``. Never configures a dlt
destination (DuckDB, Snowflake, filesystem, …) — Infona is the destination
via ``SchemaResolver.ingest_structured_rows`` → ``insert_facts``.

Call sites talk Infona types only (:class:`DltSourceSpec`). CLI / MCP /
Explorer all go through ``POST /ingest/dlt``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Optional

from infona_client.ingestion.errors import (
    DltExtractError,
    DltNotInstalled,
    DltSecretMissing,
)
from infona_client.ingestion.models import DltAuthSpec, DltSourceSpec
from infona_client.ingestion.secrets import ResolvedSecrets

# ``import dlt`` is allowed ONLY in this module. tests/test_dlt_import_allowlist.py
# greps the tree. Keep every dlt import inside _import_dlt / _build_dlt_source.

__all__ = [
    "DltExtractError",
    "DltNotInstalled",
    "DltSecretMissing",
    "ExtractedResource",
    "dlt_available",
    "extract_records",
    "lookup_resource_map",
    "require_dlt",
]


@dataclass
class ExtractedResource:
    """One dlt resource projected into Infona rows (string cells)."""

    name: str
    rows: list[dict[str, str]] = field(default_factory=list)


def dlt_available() -> bool:
    try:
        _import_dlt()
        return True
    except DltNotInstalled:
        return False


def require_dlt() -> None:
    _import_dlt()


def _import_dlt() -> Any:
    try:
        import dlt  # noqa: F401
    except ImportError as exc:
        raise DltNotInstalled() from exc
    return dlt


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, default=str)


def _stringify_row(row: Any) -> dict[str, str]:
    if not isinstance(row, dict):
        return {"value": _cell(row)}
    out: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        out[str(key)] = _cell(value)
    return out


def _rest_auth_cfg(auth: Optional[DltAuthSpec], secrets: ResolvedSecrets) -> dict[str, Any] | None:
    if auth is None or auth.type == "none":
        return None
    token = secrets.token or ""
    if auth.type == "bearer":
        return {"type": "bearer", "token": token}
    if auth.type == "api_key":
        header = auth.api_key_header or "X-API-Key"
        return {"type": "api_key", "api_key": token, "name": header, "location": "header"}
    if auth.type == "basic":
        return {
            "type": "http_basic",
            "username": secrets.username or auth.username or "",
            "password": token,
        }
    return None


def _build_dlt_source(spec: DltSourceSpec, secrets: ResolvedSecrets) -> Any:
    """Construct a dlt source object. No pipeline, no destination."""
    _import_dlt()
    if spec.kind == "rest_api":
        from dlt.sources.rest_api import rest_api_source

        if not spec.base_url:
            raise DltExtractError("rest_api source requires base_url")
        client_cfg: dict[str, Any] = {
            "base_url": spec.base_url,
            # Follow Link headers / JSON ``next`` so a 2-page CRM dump is not
            # truncated to page 1. ``auto`` is dlt's detector; no destination.
            "paginator": "auto",
        }
        if spec.headers:
            client_cfg["headers"] = dict(spec.headers)
        auth_cfg = _rest_auth_cfg(spec.auth, secrets)
        if auth_cfg:
            client_cfg["auth"] = auth_cfg
        resources = [
            {
                "name": name,
                "endpoint": {
                    "path": name.lstrip("/"),
                    "paginator": "auto",
                },
            }
            for name in spec.resources
        ]
        return rest_api_source({"client": client_cfg, "resources": resources})

    if spec.kind == "sql":
        try:
            from dlt.sources.sql_database import sql_database
        except ImportError as exc:
            raise DltNotInstalled(
                "dlt SQL extra is missing. Install: pip install 'infona-client[dlt]'"
            ) from exc
        dsn = secrets.dsn
        if not dsn:
            raise DltSecretMissing(
                "sql source is missing a DSN. Set source.dsn to env:YOUR_DSN "
                "or paste the connection string into the secret store."
            )
        kwargs: dict[str, Any] = {"credentials": dsn}
        tables = list(spec.resources)
        try:
            return sql_database(**kwargs, table_names=tables)
        except TypeError:
            try:
                return sql_database(**kwargs, include_tables=tables)
            except TypeError:
                return sql_database(credentials=dsn)

    raise DltExtractError(f"unknown source kind: {spec.kind}")


def _iter_resource_rows(resource: Any, limit: int) -> Iterator[dict[str, str]]:
    count = 0
    for raw in resource:
        if count >= limit:
            return
        yield _stringify_row(raw)
        count += 1


def _resource_items(source: Any) -> Iterable[tuple[str, Any]]:
    resources = getattr(source, "resources", None)
    if isinstance(resources, dict):
        return list(resources.items())
    if resources is not None:
        out = []
        for res in resources:
            name = getattr(res, "name", None) or str(res)
            out.append((name, res))
        return out
    # A single resource / generator.
    name = getattr(source, "name", "records")
    return [(name, source)]


def extract_records(
    spec: DltSourceSpec,
    *,
    secrets: Optional[ResolvedSecrets] = None,
    source_factory: Optional[Callable[[DltSourceSpec, ResolvedSecrets], Any]] = None,
    extracted_at: Optional[str] = None,
) -> list[ExtractedResource]:
    """Yield Infona records from a dlt source. Does not write.

    ``source_factory`` is the unit-test seam: pass a fake source so this module
    is testable without the ``[dlt]`` extra. Production leaves it unset.
    """
    resolved = secrets or ResolvedSecrets()
    if source_factory is None:
        require_dlt()
        try:
            source = _build_dlt_source(spec, resolved)
        except (DltNotInstalled, DltSecretMissing, DltExtractError):
            raise
        except Exception as exc:  # noqa: BLE001 — map dlt/http/sql failures
            raise DltExtractError(_public_extract_error(exc)) from exc
    else:
        source = source_factory(spec, resolved)

    stamp = extracted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wanted = set(spec.resources)
    out: list[ExtractedResource] = []
    try:
        for name, resource in _resource_items(source):
            if wanted and name not in wanted and _norm(name) not in {_norm(w) for w in wanted}:
                continue
            rows = []
            for row in _iter_resource_rows(resource, spec.limit):
                row.setdefault("extracted_at", stamp)
                if spec.kind == "rest_api" and spec.base_url:
                    row.setdefault(
                        "source_url",
                        f"{spec.base_url.rstrip('/')}/{name.lstrip('/')}",
                    )
                rows.append(row)
            out.append(ExtractedResource(name=name, rows=rows))
    except (DltNotInstalled, DltSecretMissing, DltExtractError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise DltExtractError(_public_extract_error(exc)) from exc
    return out


def _norm(name: str) -> str:
    return name.strip().strip("/").replace("/", "_").replace("-", "_").lower()


def lookup_resource_map(resource_name: str, mapping: dict[str, Any]) -> Any | None:
    """Match a dlt resource name onto the request ``map`` keys.

    dlt may sanitize ``v1/contacts`` → ``v1_contacts``. Try exact, then
    slash/underscore normalized.
    """
    if resource_name in mapping:
        return mapping[resource_name]
    target = _norm(resource_name)
    for key, value in mapping.items():
        if _norm(key) == target:
            return value
    return None


def _public_extract_error(exc: BaseException) -> str:
    """Actionable, secret-free message. Never dump a stack or a DSN."""
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if "401" in text or "unauthorized" in lowered:
        return (
            "upstream 401 unauthorized — check the token / DSN "
            "(secret_ref). The credential is never echoed."
        )
    if "403" in text or "forbidden" in lowered:
        return "upstream 403 forbidden — the credential is valid but not allowed for this resource."
    if "404" in text:
        return "upstream 404 — check source.base_url and resource paths / table names."
    # Strip anything that looks like a URL-with-password or bearer token.
    for needle in ("password=", "Bearer ", "postgresql://", "mysql://"):
        if needle.lower() in lowered:
            return f"extract failed ({type(exc).__name__}). Check the source config and credential."
    if len(text) > 280:
        text = text[:277] + "..."
    return f"extract failed: {text}"
