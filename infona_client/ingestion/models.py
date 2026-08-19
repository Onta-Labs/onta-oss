"""Frozen Wave-1 contract for 3rd-party extract (ONTA-553 / ONTA-554).

Canonical execute route (OSS, BYOK, ungated)::

    POST /graphs/{tenant}/ingest/dlt

Body (field names are frozen — Explorer / CLI / MCP follow this SDK shape,
never the other way around)::

    {
      "source": {
        "kind": "rest_api" | "sql",
        "base_url": "https://api.example.com",   # rest_api
        "dsn": "postgresql://…",                 # sql; prefer secret_ref
        "auth": {"type": "bearer", "secret_ref": "env:EXAMPLE_TOKEN"},
        "resources": ["v1/contacts"],
        "headers": {},
        "limit": 1000
      },
      "map": {"v1/contacts": {"type": "Contact", "id_field": "id"}},
      "kg": "…"
    }

``kg`` is the frozen name; ``kg_name`` is accepted as an alias so this matches
the rest of the ingest family.

Persist family (ONTA-554; NOT ``ApiSourceSpec`` / ``/api-sources``)::

    /graphs/{tenant}/extract-sources

Secrets: ``secret_ref`` is BYOK (``env:VAR`` or the per-tenant encrypted store
``store:<slug>/<logical>``). An inline ``auth.token`` is allowed for CLI UX
and is never logged. No shared/platform HubSpot key.

Do not invent ``/api/demo/hubspot-sync``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

DltSourceKind = Literal["rest_api", "sql"]
DltAuthType = Literal["bearer", "basic", "api_key", "none"]
CREDENTIAL_HEADER_NAMES = frozenset(
    {"authorization", "cookie", "x-api-key", "x-auth-token", "proxy-authorization"}
)

#: Wave-1 per-resource row cap. Bound so a naive CRM dump cannot fill memory.
DEFAULT_EXTRACT_LIMIT = 1000
MAX_EXTRACT_LIMIT = 100_000

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


class DltAuthSpec(BaseModel):
    """Auth for a REST extract. SQL uses ``dsn`` / ``secret_ref`` on the source."""

    model_config = ConfigDict(extra="forbid")

    type: DltAuthType = "bearer"
    #: BYOK pointer. ``env:VAR`` or ``store:<slug>/<logical>``. Never a raw token.
    secret_ref: Optional[str] = None
    #: Write-only inline token for CLI UX. Never returned, never logged.
    token: Optional[str] = None
    username: Optional[str] = None
    #: Header name when ``type="api_key"``. Default ``X-API-Key``.
    api_key_header: Optional[str] = None


class DltSourceSpec(BaseModel):
    """How to extract (REST or SQL). Does not write."""

    model_config = ConfigDict(extra="forbid")

    kind: DltSourceKind
    base_url: Optional[str] = None
    #: SQL connection string, or a ``secret_ref`` (``env:`` / ``store:``).
    dsn: Optional[str] = None
    auth: Optional[DltAuthSpec] = None
    resources: list[str] = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=DEFAULT_EXTRACT_LIMIT, ge=1, le=MAX_EXTRACT_LIMIT)

    @field_validator("resources")
    @classmethod
    def _resources_nonempty(cls, v: list[str]) -> list[str]:
        cleaned = [r.strip() for r in v if isinstance(r, str) and r.strip()]
        if not cleaned:
            raise ValueError("source.resources must list at least one resource / table")
        return cleaned

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, v: Optional[str]) -> Optional[str]:
        return v.rstrip("/") if isinstance(v, str) and v else v

    @model_validator(mode="after")
    def _kind_fields(self) -> "DltSourceSpec":
        if self.kind == "rest_api" and not self.base_url:
            raise ValueError("rest_api source requires base_url")
        if self.kind == "sql" and not (self.dsn or (self.auth and self.auth.secret_ref)):
            raise ValueError("sql source requires dsn or auth.secret_ref")
        return self

    def redacted(self) -> dict[str, Any]:
        """JSON-able dict with inline token / literal DSN / auth headers stripped."""
        data = self.model_dump()
        if data.get("auth"):
            data["auth"] = {**data["auth"], "token": None}
        dsn = data.get("dsn")
        if isinstance(dsn, str) and not dsn.startswith(("env:", "store:")):
            data["dsn"] = "***"
        headers = data.get("headers") or {}
        data["headers"] = {
            k: "***"
            for k in headers
            if str(k).lower() in CREDENTIAL_HEADER_NAMES
        } | {
            k: v
            for k, v in headers.items()
            if str(k).lower() not in CREDENTIAL_HEADER_NAMES
        }
        return data


class DltResourceMap(BaseModel):
    """Explicit resource → ontology type. Wave 1 does not auto-infer types."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str = Field(min_length=1)
    id_field: str = Field(default="id", min_length=1)
    #: Optional exhaustive attribute allowlist (ONTA-382 structured ceiling).
    attributes: Optional[list[str]] = None


class DltIngestRequest(BaseModel):
    """Body for ``POST /graphs/{tenant}/ingest/dlt``. Frozen Wave-1 shape."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: DltSourceSpec
    map: dict[str, DltResourceMap] = Field(min_length=1)
    kg: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("kg", "kg_name"),
        serialization_alias="kg",
    )

    @field_validator("map")
    @classmethod
    def _map_keys(cls, v: dict[str, DltResourceMap]) -> dict[str, DltResourceMap]:
        if not v:
            raise ValueError("map is required — name each resource's ontology type")
        return v


class DltExtractSource(BaseModel):
    """Persisted per-workspace extract-source config (ONTA-554). Not ApiSourceSpec."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    slug: str
    title: str = ""
    kind: DltSourceKind
    source: DltSourceSpec
    map: dict[str, DltResourceMap] = Field(default_factory=dict)
    kg: Optional[str] = None
    enabled: bool = True

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not SLUG_RE.match(s):
            raise ValueError(
                "slug must be lowercase alphanumeric (start with a letter), "
                "dashes/underscores allowed, max 63 chars"
            )
        return s


class ExtractSourceSummary(BaseModel):
    """List/get shape. Secret-free by construction (``has_secret`` only)."""

    slug: str
    title: str
    kind: DltSourceKind
    enabled: bool
    has_secret: bool
    resources: list[str] = Field(default_factory=list)
    mapped: list[str] = Field(default_factory=list)
    kg: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateExtractSourceRequest(BaseModel):
    """Create body: config + optional write-only ``secrets`` map."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    slug: str
    title: str = ""
    source: DltSourceSpec
    map: dict[str, DltResourceMap] = Field(default_factory=dict)
    kg: Optional[str] = None
    enabled: bool = True
    secrets: dict[str, str] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _create_slug(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not SLUG_RE.match(s):
            raise ValueError(
                "slug must be lowercase alphanumeric (start with a letter), "
                "dashes/underscores allowed, max 63 chars"
            )
        return s


class UpdateExtractSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    source: Optional[DltSourceSpec] = None
    map: Optional[dict[str, DltResourceMap]] = None
    kg: Optional[str] = None
    enabled: Optional[bool] = None
    secrets: dict[str, str] = Field(default_factory=dict)


class RunExtractSourceRequest(BaseModel):
    """Optional overrides when running a saved extract source."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kg: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("kg", "kg_name"),
    )
    limit: Optional[int] = Field(default=None, ge=1, le=MAX_EXTRACT_LIMIT)
