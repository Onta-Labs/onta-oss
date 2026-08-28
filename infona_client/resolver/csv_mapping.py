"""AppliedMapping result + small CSV mapping helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from infona_client.resolver.models import ExtractedEntity, ExtractedRelationship

@dataclass
class AppliedMapping:
    """Result of ``CSVResolver.apply_mapping``: the extracted entities and
    relationships plus row-conservation accounting (ADR 0003 §2 — input rows
    are never silently dropped; a mapping with no TYPE_ID column uses a
    synthetic key per valued row instead of dropping the table).

    Iterates as the legacy ``(entities, relationships)`` pair, so existing
    two-value unpacking call sites keep working unchanged; new callers read
    ``rows_in`` / ``rows_dropped`` / ``drops_by_entity`` off the object.
    """

    entities: list[ExtractedEntity]
    relationships: list[ExtractedRelationship]
    #: Number of input rows this call received.
    rows_in: int = 0
    #: Rows that produced no entity at all. Only possible when every owned
    #: value in the row is empty — a principled skip (nothing to assert),
    #: never a silent drop: it is always counted and logged.
    rows_dropped: int = 0
    #: Skipped entity-instances per mapping entity. Keyed by entity_type in
    #: single-entity mode, by EntitySpec.name in multi-entity mode (one row
    #: can mint some entities while skipping an all-empty one without the
    #: row itself counting as dropped).
    drops_by_entity: dict[str, int] = field(default_factory=dict)

    def __iter__(self):
        yield self.entities
        yield self.relationships


def _v2_enabled() -> bool:
    """ADR 0003 feature flag: ``INFONA_CSV_INFERENCE_V2`` defaults ON; set it
    to 0 (or false/no/off) to run the legacy single-call inference verbatim."""
    return os.environ.get("INFONA_CSV_INFERENCE_V2", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# Output-token budget for v2 passes, scaled to the column count.
_V2_BASE_MAX_TOKENS = 8192
_V2_MAX_TOKENS_CAP = 32768
_V2_TOKENS_PER_COLUMN = 100

# Bound on concurrent column-assignment chunk calls in the wide path.
_WIDE_CHUNK_CONCURRENCY = 5


def _v2_max_tokens(n_columns: int) -> int:
    """Per-pass output budget scaled to column count (COG-58)."""
    return min(
        _V2_MAX_TOKENS_CAP,
        max(_V2_BASE_MAX_TOKENS, 1024 + n_columns * _V2_TOKENS_PER_COLUMN),
    )


def _chunked(seq: list, size: int) -> list[list]:
    """Split a list into consecutive chunks of at most ``size``."""
    return [seq[i : i + size] for i in range(0, len(seq), size)]

