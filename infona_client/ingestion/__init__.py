"""3rd-party SaaS / DB / API extract (ONTA-553).

dlt is the read-only extract layer. Infona still decides facts
(``SchemaResolver.ingest_structured_rows``) and still writes
(``insert_facts`` / ``refresh_after_write``). There is no dlt destination.

``import dlt`` lives only in :mod:`infona_client.ingestion.dlt_source`.
"""

from infona_client.ingestion.dlt_source import (
    ExtractedResource,
    dlt_available,
    extract_records,
    require_dlt,
)
from infona_client.ingestion.errors import (
    DltExtractError,
    DltNotInstalled,
    DltSecretMissing,
)
from infona_client.ingestion.models import (
    DltAuthSpec,
    DltExtractSource,
    DltIngestRequest,
    DltResourceMap,
    DltSourceKind,
    DltSourceSpec,
)

__all__ = [
    "DltAuthSpec",
    "DltExtractError",
    "DltExtractSource",
    "DltIngestRequest",
    "DltNotInstalled",
    "DltResourceMap",
    "DltSecretMissing",
    "DltSourceKind",
    "DltSourceSpec",
    "ExtractedResource",
    "dlt_available",
    "extract_records",
    "require_dlt",
]
