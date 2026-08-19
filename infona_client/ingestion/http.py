"""Map extract errors to HTTP (never 500 for missing extra / missing secret)."""

from __future__ import annotations

from fastapi import HTTPException

from infona_client.ingestion.errors import DltExtractError, DltNotInstalled, DltSecretMissing


def raise_dlt_http(exc: BaseException) -> None:
    if isinstance(exc, DltNotInstalled):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, DltSecretMissing):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, DltExtractError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise exc
