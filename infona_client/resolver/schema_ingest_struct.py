from __future__ import annotations

"""Structured-row projection helpers for the pre-structured ingest fast path.

Job: clip exhaustive-ceiling rows and build the fixed CSVSchemaMapping for
PRE-STRUCTURED records (ONTA-272 / ONTA-382). Do not invent a second
allowlist clip in discovery — call these.
"""

from infona_client.resolver.models import ColumnMapping, ColumnRole, CSVSchemaMapping

# Provenance / lineage cells that may ride on structured rows even when the
# user declared a closed attribute set (ONTA-382 ceiling). Never declared as
# ordinary ontology attributes by the mapping.
_STRUCTURED_PROVENANCE_COLS = frozenset({"source_url"})


def _project_structured_rows_to_attributes(
    rows: list[dict],
    *,
    key_field: str,
    attributes: list[str] | None,
    attributes_exhaustive: bool,
) -> list[dict]:
    """When ``attributes_exhaustive``, clip each row to the confirmed allowlist.

    Discovery often fetches rich provider payloads (``hint_columns``) for
    extraction quality, but a user-named closed field list is a WRITE ceiling
    (ONTA-382). The structured fast-path used to map *every* cell and silently
    invent ontology attributes (e.g. ``context_length`` when the user only asked
    for ``name, provider, modality, input_price``). Non-exhaustive keeps the
    full row (soft / open attribute set).

    Always retains ``key_field`` and ``source_url`` (citation) when present.
    """
    if not attributes_exhaustive or not attributes:
        return rows
    allow = {str(a) for a in attributes if a} | {key_field} | set(
        _STRUCTURED_PROVENANCE_COLS
    )
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({k: v for k, v in row.items() if k in allow})
    return out


def _structured_rows_mapping(
    rows: list[dict],
    type_name: str,
    key_field: str,
    *,
    attribute_allowlist: frozenset[str] | None = None,
) -> CSVSchemaMapping:
    """Build the fixed :class:`CSVSchemaMapping` for PRE-STRUCTURED rows (ONTA-272).

    Every distinct field (first-seen order across the rows) becomes a literal
    ATTRIBUTE column of ``type_name`` except the key field, which is the TYPE_ID
    (URI + label + key-as-attribute, per ADR 0003 §2). ``source_url`` is typed
    ``uri`` so its per-record citation renders as a link; every other field is a
    plain ``string`` literal — pre-structured sources deliver clean scalar cells,
    so there is no LLM datatype guessing. A degenerate ``key_field`` that never
    appears in the rows falls back to the first field so ``apply_mapping`` always
    has a TYPE_ID (an all-empty key still mints via its synthetic-key path).

    ``attribute_allowlist`` (ONTA-382 / structured ceiling): when set, only those
    column names (+ key / source_url already folded into the set by the caller)
    become mapping columns — a second belt if a row still carries extra keys.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k in row:
            if attribute_allowlist is not None and k not in attribute_allowlist:
                continue
            if k not in seen:
                seen.add(k)
                ordered.append(k)
    if key_field not in seen and ordered:
        key_field = ordered[0]
    columns = [
        ColumnMapping(
            column_name=k,
            role=ColumnRole.TYPE_ID if k == key_field else ColumnRole.ATTRIBUTE,
            target_type=type_name,
            datatype="uri" if k == "source_url" else "string",
        )
        for k in ordered
    ]
    return CSVSchemaMapping(entity_type=type_name, columns=columns)
