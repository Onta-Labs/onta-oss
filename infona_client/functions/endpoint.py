"""Shared CUSTOM-function endpoint classification (HTTPS URL vs Lambda ARN)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# arn:aws:lambda:<region>:<12-digit-account>:function:<name>
# plus optional :qualifier / :version / :$LATEST
_LAMBDA_ARN_RE = re.compile(
    r"^arn:aws:lambda:"
    r"[a-z0-9-]+:"
    r"\d{12}:"
    r"function:"
    r"[A-Za-z0-9_-]+"
    r"(?::(?:\$LATEST|[A-Za-z0-9_-]+))?"
    r"$"
)


def is_lambda_arn(value: str) -> bool:
    """True iff ``value`` is an AWS Lambda function ARN (optionally qualified)."""
    return isinstance(value, str) and _LAMBDA_ARN_RE.match(value) is not None


def is_https_endpoint(value: str) -> bool:
    """True iff ``value`` is a non-empty ``https://`` URL with a host."""
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def validate_function_endpoint(value: str) -> str:
    """Accept an HTTPS URL or Lambda function ARN; reject empty / garbage."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "endpoint_url must be a non-empty HTTPS URL or Lambda function ARN"
        )
    value = value.strip()
    if is_lambda_arn(value) or is_https_endpoint(value):
        return value
    raise ValueError(
        "endpoint_url must be an HTTPS URL or a Lambda function ARN "
        "(arn:aws:lambda:<region>:<account-id>:function:<name>[:qualifier])"
    )
