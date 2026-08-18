"""Allowlist-only payload construction (ONTA-548).

A job event may contain ONLY:

* ``event`` (always ``job``)
* ``install_id`` (random UUID; not a tenant / user id)
* ``job_type`` (``ingest`` / ``ask`` / ``er rebuild`` / ``export``)
* ``row_count_bucket`` (never an exact count)
* ``source_type`` (``csv`` / ``json`` / … — never a filename)
* ``error_class`` (exception type or HTTP family — never message text)
* ``use_case`` (optional coarse self-declared enum)

Everything else is dropped. Message text, paths, prompts, answers, Cypher,
tenant ids, emails, column names, and graph content cannot enter the payload.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Mapping, Optional

JOB_TYPES = frozenset({"ingest", "ask", "er rebuild", "export"})
SOURCE_TYPES = frozenset({"csv", "json", "jsonl", "text", "http", "unknown"})
ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "event",
        "install_id",
        "job_type",
        "row_count_bucket",
        "source_type",
        "error_class",
        "use_case",
    }
)

# Names that must never survive sanitization, even if a caller passes them.
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "tenant",
        "tenant_id",
        "email",
        "prompt",
        "question",
        "answer",
        "cypher",
        "sparql",
        "filename",
        "file_name",
        "file",
        "path",
        "content",
        "column",
        "columns",
        "graph",
        "kg",
        "kg_name",
        "message",
        "detail",
        "error_message",
        "user",
        "subject",
        "api_key",
        "rows",
        "row_count",
        "distinct_id",
        "text",
        "body",
        "source",
    }
)

_EXC_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_HTTP_FAMILY = re.compile(r"^http_[1-5]xx$")
_PLATFORM_PREFIX = re.compile(r"^(?:file|source):", re.IGNORECASE)

# Order matters: first matching ceiling wins.
_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (10, "1-10"),
    (100, "11-100"),
    (1000, "101-1000"),
    (10000, "1001-10000"),
)


def row_count_bucket(n: Optional[int]) -> Optional[str]:
    if n is None:
        return None
    try:
        count = int(n)
    except (TypeError, ValueError):
        return None
    if count < 0:
        return None
    for ceiling, label in _BUCKETS:
        if count <= ceiling:
            return label
    return "10000+"


def normalize_job_type(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    key = " ".join(raw.strip().lower().replace("_", " ").replace("-", " ").split())
    return key if key in JOB_TYPES else None


def _leaf_source(raw: str) -> str:
    text = raw.strip().lower()
    text = _PLATFORM_PREFIX.sub("", text)
    # last path segment only, then strip a suffix — never keep a filename
    if "/" in text or "\\" in text:
        text = text.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in text and text.rsplit(".", 1)[-1] in SOURCE_TYPES:
        text = text.rsplit(".", 1)[-1]
    return text


def normalize_source_type(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        platforms = getattr(raw, "platforms", None)
        if isinstance(platforms, (list, tuple)) and platforms:
            return normalize_source_type(platforms[0])
        return None
    leaf = _leaf_source(raw)
    if leaf in SOURCE_TYPES:
        return leaf
    if leaf in {"application/json", "json"}:
        return "json"
    if leaf in {"text/csv", "csv"}:
        return "csv"
    if leaf in {"text/plain", "text"}:
        return "text"
    return "unknown" if leaf else None


def error_class(err: Any) -> Optional[str]:
    """Coarse class only — never exception message text."""
    if err is None or err is False:
        return None
    if err is True:
        exc = sys.exc_info()[1]
        return type(exc).__name__ if exc is not None else "Exception"
    if isinstance(err, BaseException):
        return type(err).__name__
    if isinstance(err, type) and issubclass(err, BaseException):
        return err.__name__
    if isinstance(err, int):
        if 100 <= err <= 599:
            return f"http_{err // 100}xx"
        return "Exception"
    if isinstance(err, str):
        token = err.strip()
        # "ValueError: boom with /secret.csv" → ValueError
        if ":" in token:
            token = token.split(":", 1)[0].strip()
        if _HTTP_FAMILY.match(token.lower()):
            return token.lower()
        if _EXC_TYPE.match(token) and token not in FORBIDDEN_FIELD_NAMES:
            return token
        return "Exception"
    return "Exception"


def build_payload(
    *,
    install_id: str,
    job_type: str,
    row_count: Optional[int] = None,
    source_type: Any = None,
    error: Any = None,
    use_case: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Return an allowlisted payload, or ``None`` if ``job_type`` is unknown."""
    jt = normalize_job_type(job_type)
    if jt is None:
        return None
    payload: dict[str, Any] = {
        "event": "job",
        "install_id": install_id,
        "job_type": jt,
    }
    bucket = row_count_bucket(row_count)
    if bucket is not None:
        payload["row_count_bucket"] = bucket
    src = normalize_source_type(source_type)
    if src is not None:
        payload["source_type"] = src
    klass = error_class(error)
    if klass is not None:
        payload["error_class"] = klass
    if use_case in {"research", "ops", "product", "other"}:
        payload["use_case"] = use_case
    if extra:
        # Extra keys are ignored unless they are already allowed *and* not set.
        # Values for forbidden names are discarded even if the key were allowed.
        for key, value in extra.items():
            if key in FORBIDDEN_FIELD_NAMES:
                continue
            if key not in ALLOWED_PAYLOAD_KEYS or key in payload:
                continue
            if value is None or value == "":
                continue
            payload[key] = value
    return {k: v for k, v in payload.items() if k in ALLOWED_PAYLOAD_KEYS}
