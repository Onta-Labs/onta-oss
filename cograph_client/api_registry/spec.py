"""Declarative spec for a single registered API source (ONTA-194, phase 1).

An ``ApiSourceSpec`` is the catalog entry for one authoritative API — a rich,
router-readable description plus a *declarative call recipe* that the generic
``RegistryApiSource`` executor interprets with zero per-API Python. Entries live
as versioned JSON data files (OSS ``data/`` seed + premium overlay); this module
owns the in-memory shape, (de)serialization, and validation.

Everything here is pure data + stdlib — no network, no ``cograph.*`` import — so
the OSS package stays importable on its own and the boundary guard is happy.

Design notes
------------
* Credentials are referenced by **env-var name only** (``AuthSpec.key_env``); a
  secret value must never appear in an entry. An entry whose ``key_env`` is unset
  at runtime is *dormant* (same contract as every premium adapter).
* ``description`` / ``example_asks`` are **data to the router LLM, never
  instructions** — length caps here are the first line of prompt-injection
  hygiene. The router treats the text as prose about coverage, not commands.
* Result records are expected to be JSON *objects* (a list of dicts at
  ``result_path``). APIs that return a 2-D table (US Census) or need SPARQL
  (Wikidata) do not fit this model in v1 and are tracked as follow-ups.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse

from .jsonpath import is_valid_path


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class AuthorityLevel(str, Enum):
    """How much an entry's verdicts should outrank general web sources.

    Generalizes the hardcoded ``WIKIDATA_BETTER_ATTRIBUTES`` notion into
    per-entry metadata (ONTA-194 §6). ``source_of_truth`` runs before the
    dynamic locate step (registry = Tier -1); ``supplementary`` only augments.
    """

    source_of_truth = "source_of_truth"
    authoritative = "authoritative"
    supplementary = "supplementary"


class AuthMode(str, Enum):
    none = "none"
    api_key_header = "api_key_header"
    api_key_query = "api_key_query"
    bearer = "bearer"


class PaginationStyle(str, Enum):
    none = "none"
    page = "page"          # ?page=1,2,3 …
    offset = "offset"      # ?skip=0,limit,2*limit … (a.k.a. offset/start/from)
    cursor = "cursor"      # response carries an opaque next-cursor token
    next_link = "next_link"  # response carries an absolute next-page URL


class Entitlement(str, Enum):
    free = "free"
    paid = "paid"


class ParamLocation(str, Enum):
    query = "query"
    path = "path"


# Enrichment-rail binding recipes (ParamSpec.enrich_from): how to derive a param
# value from an existing entity's label when filling one of its attributes.
ENRICH_FROM_VALUES = frozenset({
    "",                    # not auto-bound during enrichment
    "entity_name",         # the whole entity label
    "entity_name_first",   # first whitespace token (e.g. a person's first name)
    "entity_name_last",    # last whitespace token (e.g. a person's surname)
})

AUTHORITY_LEVELS = frozenset(a.value for a in AuthorityLevel)
AUTH_MODES = frozenset(a.value for a in AuthMode)
PAGINATION_STYLES = frozenset(p.value for p in PaginationStyle)
ENTITLEMENTS = frozenset(e.value for e in Entitlement)

# Field caps — prompt-injection hygiene + keep the catalog readable.
_MAX_DESCRIPTION = 2000
_MAX_TEXT = 400
_MAX_LIST = 40
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
# Env-var name: UPPER_SNAKE, the ONTA-194 curated-catalog credential form.
_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
# Per-tenant secret logical name: lowercase slug-ish, matches the store key.
_SECRET_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class SpecError(ValueError):
    """Raised when an ``ApiSourceSpec`` fails validation."""


# --------------------------------------------------------------------------- #
# Small helpers (deserialization is tolerant; validation is strict)
# --------------------------------------------------------------------------- #
def _as_str(v: Any, default: str = "") -> str:
    return v if isinstance(v, str) else (default if v is None else str(v))


def _as_str_list(v: Any) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [x for x in (_as_str(i).strip() for i in v) if x]


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Sub-models
# --------------------------------------------------------------------------- #
@dataclass
class Coverage:
    """What the source can answer — the router's prefilter reads this."""

    entity_kinds: list[str] = field(default_factory=list)   # e.g. ["healthcare_provider"]
    attributes: list[str] = field(default_factory=list)     # e.g. ["npi", "taxonomy"]
    geo: str = ""                                           # e.g. "United States"
    temporal: str = ""                                      # e.g. "current"
    example_asks: list[str] = field(default_factory=list)   # few-shot for the router

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_kinds": list(self.entity_kinds),
            "attributes": list(self.attributes),
            "geo": self.geo,
            "temporal": self.temporal,
            "example_asks": list(self.example_asks),
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "Coverage":
        d = d or {}
        return cls(
            entity_kinds=_as_str_list(d.get("entity_kinds")),
            attributes=_as_str_list(d.get("attributes")),
            geo=_as_str(d.get("geo")).strip(),
            temporal=_as_str(d.get("temporal")).strip(),
            example_asks=_as_str_list(d.get("example_asks")),
        )


@dataclass
class AuthSpec:
    """Declarative auth. The secret is referenced — never stored in the spec.

    A secret is named ONE of two ways (mutually exclusive):

    * ``key_env`` — an environment-variable NAME (the ONTA-194 curated-catalog
      form; the value is a deployment-level env / Secrets Manager entry). This is
      the only form a *global* catalog entry may use.
    * ``secret_ref`` — a LOGICAL name of a per-tenant secret stored
      envelope-encrypted in the tenant secret store (ONTA-2xx, tenant_custom
      entries only). Resolved + decrypted only at call time inside the executor.

    Neither form ever holds the secret VALUE. ``secret_ref`` is the ``tenant_secret``
    reference: the executor resolves it via an injected secret resolver, so the
    spec — and every serialization, response, and log — stays secret-free.
    """

    mode: AuthMode = AuthMode.none
    key_env: str = ""       # env var NAME holding the secret (value injected at call time)
    secret_ref: str = ""    # LOGICAL name of a per-tenant encrypted secret (tenant_custom)
    header_name: str = ""   # for api_key_header (e.g. "X-Api-Key")
    query_key: str = ""     # for api_key_query (e.g. "api_token")

    @property
    def requires_key(self) -> bool:
        return self.mode is not AuthMode.none

    @property
    def uses_tenant_secret(self) -> bool:
        """True when auth resolves from the per-tenant encrypted store, not env."""
        return bool(self.secret_ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "key_env": self.key_env,
            "secret_ref": self.secret_ref,
            "header_name": self.header_name,
            "query_key": self.query_key,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "AuthSpec":
        d = d or {}
        raw_mode = _as_str(d.get("mode"), "none").strip() or "none"
        try:
            mode = AuthMode(raw_mode)
        except ValueError:
            mode = AuthMode.none
        return cls(
            mode=mode,
            key_env=_as_str(d.get("key_env")).strip(),
            secret_ref=_as_str(d.get("secret_ref")).strip(),
            header_name=_as_str(d.get("header_name")).strip(),
            query_key=_as_str(d.get("query_key")).strip(),
        )


@dataclass
class ParamSpec:
    """A router-bindable request parameter (e.g. ``city`` -> query key ``city``).

    ``enrich_from`` is the enrichment-rail binding recipe (ONTA-194 phase 3): when
    filling an attribute on an existing entity, how to derive this param's value
    from the entity's label. Empty ⇒ the param is not auto-bound during
    enrichment (it may still be bound by the discovery-rail router). Allowed
    values are in ``ENRICH_FROM_VALUES``.
    """

    name: str                                   # binding key the router emits
    location: ParamLocation = ParamLocation.query
    target: str = ""                            # actual query key or path placeholder
    required: bool = False
    default: Optional[str] = None
    description: str = ""
    enrich_from: str = ""                        # "" | entity_name | entity_name_first | entity_name_last

    def __post_init__(self) -> None:
        if not self.target:
            self.target = self.name

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "location": self.location.value,
            "target": self.target,
            "required": self.required,
            "description": self.description,
        }
        if self.default is not None:
            out["default"] = self.default
        if self.enrich_from:
            out["enrich_from"] = self.enrich_from
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ParamSpec":
        raw_loc = _as_str(d.get("location"), "query").strip() or "query"
        try:
            location = ParamLocation(raw_loc)
        except ValueError:
            location = ParamLocation.query
        default = d.get("default")
        return cls(
            name=_as_str(d.get("name")).strip(),
            location=location,
            target=_as_str(d.get("target")).strip(),
            required=bool(d.get("required", False)),
            default=None if default is None else _as_str(default),
            description=_as_str(d.get("description")).strip(),
            enrich_from=_as_str(d.get("enrich_from")).strip(),
        )


@dataclass
class PaginationSpec:
    """Declarative pagination — the executor picks one style and sticks to it."""

    style: PaginationStyle = PaginationStyle.none
    limit_param: str = ""       # request param for page size (e.g. "limit", "pageSize")
    page_size: int = 50
    page_param: str = ""        # style=page: request param (e.g. "page")
    start_page: int = 1         # style=page: first page number
    offset_param: str = ""      # style=offset: request param (e.g. "skip", "offset")
    cursor_param: str = ""      # style=cursor: request param carrying the token
    cursor_path: str = ""       # style=cursor: dotted path to the next token in the body
    next_link_path: str = ""    # style=next_link: dotted path to the absolute next URL
    total_path: str = ""        # optional: dotted path to a total-count for estimated_total
    max_pages: int = 5          # hard cap on pages fetched per call

    def to_dict(self) -> dict[str, Any]:
        return {
            "style": self.style.value,
            "limit_param": self.limit_param,
            "page_size": self.page_size,
            "page_param": self.page_param,
            "start_page": self.start_page,
            "offset_param": self.offset_param,
            "cursor_param": self.cursor_param,
            "cursor_path": self.cursor_path,
            "next_link_path": self.next_link_path,
            "total_path": self.total_path,
            "max_pages": self.max_pages,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "PaginationSpec":
        d = d or {}
        raw_style = _as_str(d.get("style"), "none").strip() or "none"
        try:
            style = PaginationStyle(raw_style)
        except ValueError:
            style = PaginationStyle.none
        return cls(
            style=style,
            limit_param=_as_str(d.get("limit_param")).strip(),
            page_size=_as_int(d.get("page_size"), 50),
            page_param=_as_str(d.get("page_param")).strip(),
            start_page=_as_int(d.get("start_page"), 1),
            offset_param=_as_str(d.get("offset_param")).strip(),
            cursor_param=_as_str(d.get("cursor_param")).strip(),
            cursor_path=_as_str(d.get("cursor_path")).strip(),
            next_link_path=_as_str(d.get("next_link_path")).strip(),
            total_path=_as_str(d.get("total_path")).strip(),
            max_pages=_as_int(d.get("max_pages"), 5),
        )


@dataclass
class EndpointSpec:
    """One callable endpoint. Most entries have exactly one."""

    name: str = "default"
    method: str = "GET"
    path: str = ""                                          # appended to base_url; may template {placeholders}
    query: dict[str, str] = field(default_factory=dict)     # static query params
    params: list[ParamSpec] = field(default_factory=list)   # router-bindable params
    result_path: str = ""                                   # dotted path to the record array
    field_mappings: dict[str, str] = field(default_factory=dict)  # out_col -> dotted source path
    pagination: PaginationSpec = field(default_factory=PaginationSpec)
    # Example bindings for the freshness/live smoke audit (phase 4): a minimal
    # query that should return at least one row. Empty ⇒ the entry is not
    # live-smoke-tested.
    smoke_bindings: dict[str, str] = field(default_factory=dict)

    def param(self, name: str) -> Optional[ParamSpec]:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "query": dict(self.query),
            "params": [p.to_dict() for p in self.params],
            "result_path": self.result_path,
            "field_mappings": dict(self.field_mappings),
            "pagination": self.pagination.to_dict(),
        }
        if self.smoke_bindings:
            out["smoke_bindings"] = dict(self.smoke_bindings)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EndpointSpec":
        query = {
            _as_str(k): _as_str(v)
            for k, v in (d.get("query") or {}).items()
        }
        mappings = {
            _as_str(k): _as_str(v)
            for k, v in (d.get("field_mappings") or {}).items()
        }
        smoke = {
            _as_str(k): _as_str(v)
            for k, v in (d.get("smoke_bindings") or {}).items()
        }
        return cls(
            name=_as_str(d.get("name"), "default").strip() or "default",
            method=_as_str(d.get("method"), "GET").strip().upper() or "GET",
            path=_as_str(d.get("path")).strip(),
            query=query,
            params=[ParamSpec.from_dict(p) for p in (d.get("params") or []) if isinstance(p, dict)],
            result_path=_as_str(d.get("result_path")).strip(),
            field_mappings=mappings,
            pagination=PaginationSpec.from_dict(d.get("pagination")),
            smoke_bindings=smoke,
        )


# --------------------------------------------------------------------------- #
# The top-level entry
# --------------------------------------------------------------------------- #
@dataclass
class ApiSourceSpec:
    # Identity
    slug: str
    title: str = ""
    publisher: str = ""
    description: str = ""
    docs_url: str = ""
    # Coverage (router prefilter)
    coverage: Coverage = field(default_factory=Coverage)
    # Authority / trust
    authority_level: AuthorityLevel = AuthorityLevel.authoritative
    # Call spec
    base_url: str = ""
    auth: AuthSpec = field(default_factory=AuthSpec)
    endpoints: list[EndpointSpec] = field(default_factory=list)
    # Cost / limits
    cost_per_call: float = 0.0
    rate_limit_per_min: int = 0            # 0 = unspecified
    # Governance
    persist_ok: bool = True
    tos_note: str = ""
    enabled: bool = True
    entitlement: Entitlement = Entitlement.free
    # Freshness audit (phase 4): ISO date (YYYY-MM-DD) the entry's call spec was
    # last hand-verified against the live API. Empty ⇒ never verified. The
    # catalog-freshness audit flags an empty/stale stamp so an entry that has
    # silently rotted (endpoint moved, auth changed) is surfaced, not trusted.
    verified_at: str = ""
    # Provenance layer this entry came from (set by the loader; not authored).
    layer: str = "global_public"

    # -- convenience -------------------------------------------------------- #
    def endpoint(self, name: Optional[str] = None) -> Optional[EndpointSpec]:
        if not self.endpoints:
            return None
        if name is None:
            return self.endpoints[0]
        for ep in self.endpoints:
            if ep.name == name:
                return ep
        return None

    @property
    def is_paid(self) -> bool:
        return self.entitlement is Entitlement.paid or self.cost_per_call > 0.0

    # -- (de)serialization -------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "publisher": self.publisher,
            "description": self.description,
            "docs_url": self.docs_url,
            "coverage": self.coverage.to_dict(),
            "authority_level": self.authority_level.value,
            "base_url": self.base_url,
            "auth": self.auth.to_dict(),
            "endpoints": [e.to_dict() for e in self.endpoints],
            "cost_per_call": self.cost_per_call,
            "rate_limit_per_min": self.rate_limit_per_min,
            "persist_ok": self.persist_ok,
            "tos_note": self.tos_note,
            "enabled": self.enabled,
            "entitlement": self.entitlement.value,
            "verified_at": self.verified_at,
            "layer": self.layer,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApiSourceSpec":
        if not isinstance(d, dict):
            raise SpecError(f"entry must be a JSON object, got {type(d).__name__}")

        raw_authority = _as_str(d.get("authority_level"), "authoritative").strip() or "authoritative"
        try:
            authority = AuthorityLevel(raw_authority)
        except ValueError:
            authority = AuthorityLevel.authoritative

        raw_ent = _as_str(d.get("entitlement"), "free").strip() or "free"
        try:
            entitlement = Entitlement(raw_ent)
        except ValueError:
            entitlement = Entitlement.free

        try:
            cost = float(d.get("cost_per_call", 0.0) or 0.0)
        except (TypeError, ValueError):
            cost = 0.0

        return cls(
            slug=_as_str(d.get("slug")).strip(),
            title=_as_str(d.get("title")).strip(),
            publisher=_as_str(d.get("publisher")).strip(),
            description=_as_str(d.get("description")).strip(),
            docs_url=_as_str(d.get("docs_url")).strip(),
            coverage=Coverage.from_dict(d.get("coverage")),
            authority_level=authority,
            base_url=_as_str(d.get("base_url")).strip(),
            auth=AuthSpec.from_dict(d.get("auth")),
            endpoints=[
                EndpointSpec.from_dict(e) for e in (d.get("endpoints") or []) if isinstance(e, dict)
            ],
            cost_per_call=cost,
            rate_limit_per_min=_as_int(d.get("rate_limit_per_min"), 0),
            persist_ok=bool(d.get("persist_ok", True)),
            tos_note=_as_str(d.get("tos_note")).strip(),
            enabled=bool(d.get("enabled", True)),
            entitlement=entitlement,
            verified_at=_as_str(d.get("verified_at")).strip(),
            layer=_as_str(d.get("layer"), "global_public").strip() or "global_public",
        )

    # -- validation --------------------------------------------------------- #
    def validate(self) -> None:
        """Raise ``SpecError`` on the first structural problem.

        This is the schema check the CI catalog test runs on every entry. It is
        deliberately strict: a malformed entry must never ship.
        """
        errs = validate_spec(self)
        if errs:
            raise SpecError(f"{self.slug or '<no-slug>'}: " + "; ".join(errs))


# --------------------------------------------------------------------------- #
# URL lint + full validation (importable so the CI test and loader share them)
# --------------------------------------------------------------------------- #
_BLOCKED_HOST_RE = re.compile(
    r"(?i)^(localhost|.*\.local|.*\.internal|metadata\.google\.internal)$"
)


def url_lint_errors(url: str, *, field_name: str) -> list[str]:
    """Return lint errors for a catalog URL.

    Requires https, a real host, and refuses private/link-local/reserved hosts
    and blocked names — the static half of the executor's SSRF guard, applied at
    author/CI time so a bad base_url is caught before it can ever be fetched.
    """
    errs: list[str] = []
    raw = (url or "").strip()
    if not raw:
        errs.append(f"{field_name} is empty")
        return errs
    try:
        parsed = urlparse(raw)
    except ValueError:
        return [f"{field_name} is not a parseable URL: {raw!r}"]
    if parsed.scheme != "https":
        errs.append(f"{field_name} must use https (got {parsed.scheme or 'no scheme'!r})")
    host = parsed.hostname or ""
    if not host:
        errs.append(f"{field_name} has no host: {raw!r}")
        return errs
    if _BLOCKED_HOST_RE.match(host.rstrip(".")):
        errs.append(f"{field_name} points at a blocked host: {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_loopback or ip.is_link_local or ip.is_private
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        errs.append(f"{field_name} points at a non-public IP: {host!r}")
    return errs


def validate_spec(spec: ApiSourceSpec) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errs: list[str] = []

    if not spec.slug:
        errs.append("slug is required")
    elif not _SLUG_RE.match(spec.slug):
        errs.append(f"slug {spec.slug!r} must match {_SLUG_RE.pattern}")

    if not spec.title:
        errs.append("title is required")
    if len(spec.description) > _MAX_DESCRIPTION:
        errs.append(f"description exceeds {_MAX_DESCRIPTION} chars")
    for label, value in (("title", spec.title), ("publisher", spec.publisher), ("tos_note", spec.tos_note)):
        if len(value) > _MAX_TEXT:
            errs.append(f"{label} exceeds {_MAX_TEXT} chars")

    # URL lint (base_url + docs_url; docs_url only if present)
    errs.extend(url_lint_errors(spec.base_url, field_name="base_url"))
    if spec.docs_url:
        errs.extend(url_lint_errors(spec.docs_url, field_name="docs_url"))

    # Coverage text caps (prompt-injection hygiene)
    for lst_name, lst in (
        ("coverage.entity_kinds", spec.coverage.entity_kinds),
        ("coverage.attributes", spec.coverage.attributes),
        ("coverage.example_asks", spec.coverage.example_asks),
    ):
        if len(lst) > _MAX_LIST:
            errs.append(f"{lst_name} has more than {_MAX_LIST} items")
        for item in lst:
            if len(item) > _MAX_DESCRIPTION:
                errs.append(f"{lst_name} item exceeds {_MAX_DESCRIPTION} chars")

    if spec.cost_per_call < 0:
        errs.append("cost_per_call must be >= 0")

    # verified_at, if present, must be an ISO calendar date (YYYY-MM-DD).
    if spec.verified_at:
        try:
            date.fromisoformat(spec.verified_at)
        except ValueError:
            errs.append(f"verified_at {spec.verified_at!r} is not an ISO date (YYYY-MM-DD)")

    # Auth — a keyed mode needs EXACTLY ONE secret reference: an env-var name
    # (curated global form) OR a per-tenant secret_ref (tenant_custom form).
    mode = spec.auth.mode
    has_env = bool(spec.auth.key_env)
    has_ref = bool(spec.auth.secret_ref)
    if mode is not AuthMode.none:
        if not has_env and not has_ref:
            errs.append(
                f"auth.mode={mode.value} requires auth.key_env (env var name) "
                f"or auth.secret_ref (per-tenant secret name)"
            )
        elif has_env and has_ref:
            errs.append(
                "auth may set only one of auth.key_env / auth.secret_ref, not both"
            )
    elif has_ref:
        # secret_ref with mode=none is meaningless (nothing consumes it).
        errs.append("auth.secret_ref set but auth.mode=none (no auth to apply it to)")
    if spec.auth.key_env and not _KEY_ENV_RE.match(spec.auth.key_env):
        errs.append(
            f"auth.key_env {spec.auth.key_env!r} must be an UPPER_SNAKE env-var name"
        )
    if spec.auth.secret_ref and not _SECRET_REF_RE.match(spec.auth.secret_ref):
        errs.append(
            f"auth.secret_ref {spec.auth.secret_ref!r} must match {_SECRET_REF_RE.pattern}"
        )
    if mode is AuthMode.api_key_header and not spec.auth.header_name:
        errs.append("auth.mode=api_key_header requires auth.header_name")
    if mode is AuthMode.api_key_query and not spec.auth.query_key:
        errs.append("auth.mode=api_key_query requires auth.query_key")

    # A paid entry should reference a key (it's meant to be dormant without one).
    if spec.entitlement is Entitlement.paid and mode is AuthMode.none:
        errs.append("entitlement=paid but auth.mode=none (paid entries must gate on a key)")

    # Endpoints
    if not spec.endpoints:
        errs.append("at least one endpoint is required")
    seen_names: set[str] = set()
    for i, ep in enumerate(spec.endpoints):
        prefix = f"endpoints[{i}]"
        if not ep.name:
            errs.append(f"{prefix}.name is required")
        elif ep.name in seen_names:
            errs.append(f"{prefix}.name {ep.name!r} is duplicated")
        else:
            seen_names.add(ep.name)
        if ep.method != "GET":
            errs.append(f"{prefix}.method must be GET in v1 (got {ep.method!r})")
        if not ep.path.startswith("/"):
            errs.append(f"{prefix}.path must start with '/' (got {ep.path!r})")
        if not ep.field_mappings:
            errs.append(f"{prefix}.field_mappings is required (at least one output column)")
        if ep.result_path and not is_valid_path(ep.result_path):
            errs.append(f"{prefix}.result_path {ep.result_path!r} is not a valid dotted path")
        for col, src in ep.field_mappings.items():
            if not is_valid_path(src):
                errs.append(f"{prefix}.field_mappings[{col!r}] path {src!r} is not a valid dotted path")
        errs.extend(_validate_endpoint_params(ep, prefix))
        errs.extend(_validate_pagination(ep.pagination, prefix))

    return errs


def _validate_endpoint_params(ep: EndpointSpec, prefix: str) -> list[str]:
    errs: list[str] = []
    seen: set[str] = set()
    placeholders = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", ep.path))
    for j, p in enumerate(ep.params):
        pp = f"{prefix}.params[{j}]"
        if not p.name:
            errs.append(f"{pp}.name is required")
        elif p.name in seen:
            errs.append(f"{pp}.name {p.name!r} is duplicated")
        else:
            seen.add(p.name)
        if p.location is ParamLocation.path and p.target not in placeholders:
            errs.append(
                f"{pp} is a path param but {{{p.target}}} is not in path {ep.path!r}"
            )
        if p.enrich_from not in ENRICH_FROM_VALUES:
            errs.append(
                f"{pp}.enrich_from {p.enrich_from!r} not in {sorted(ENRICH_FROM_VALUES)}"
            )
    # Every path placeholder must be filled by a declared path param.
    declared_targets = {p.target for p in ep.params if p.location is ParamLocation.path}
    for ph in placeholders:
        if ph not in declared_targets:
            errs.append(f"{prefix}.path placeholder {{{ph}}} has no matching path param")
    # Smoke bindings (phase 4) are passed to execute() keyed by param NAME, so
    # every key must reference a declared param — a typo would silently no-op.
    param_names = {p.name for p in ep.params if p.name}
    for key in ep.smoke_bindings:
        if key not in param_names:
            errs.append(f"{prefix}.smoke_bindings key {key!r} is not a declared param name")
    return errs


def _validate_pagination(pg: PaginationSpec, prefix: str) -> list[str]:
    errs: list[str] = []
    pp = f"{prefix}.pagination"
    if pg.max_pages < 1:
        errs.append(f"{pp}.max_pages must be >= 1")
    if pg.style is not PaginationStyle.none and pg.page_size < 1:
        errs.append(f"{pp}.page_size must be >= 1 for style={pg.style.value}")
    if pg.style is PaginationStyle.page and not pg.page_param:
        errs.append(f"{pp}.page_param required for style=page")
    if pg.style is PaginationStyle.offset and not pg.offset_param:
        errs.append(f"{pp}.offset_param required for style=offset")
    if pg.style is PaginationStyle.offset and not pg.limit_param:
        # The offset stride is next_index * page_size; without telling the server
        # the page size, real page size can diverge -> skipped/overlapping rows.
        errs.append(f"{pp}.limit_param required for style=offset (offset stride assumes page_size)")
    if pg.style is PaginationStyle.cursor and not (pg.cursor_param and pg.cursor_path):
        errs.append(f"{pp}.cursor_param and cursor_path required for style=cursor")
    if pg.style is PaginationStyle.next_link and not pg.next_link_path:
        errs.append(f"{pp}.next_link_path required for style=next_link")
    return errs


__all__ = [
    "ApiSourceSpec",
    "Coverage",
    "AuthSpec",
    "ParamSpec",
    "PaginationSpec",
    "EndpointSpec",
    "AuthorityLevel",
    "AuthMode",
    "PaginationStyle",
    "Entitlement",
    "ParamLocation",
    "SpecError",
    "validate_spec",
    "url_lint_errors",
]
