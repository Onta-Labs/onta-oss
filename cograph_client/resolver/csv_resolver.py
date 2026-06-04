"""CSV schema inference — one LLM call to infer column mapping, then
deterministic mapping for all rows. No LLM per row."""

from __future__ import annotations

import json
import os
import re

import anthropic
import httpx
import structlog
from pydantic import ValidationError

from cograph_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    EntityRelationSpec,
    EntitySpec,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
)

logger = structlog.stdlib.get_logger("cograph.resolver.csv")

CSV_SCHEMA_SYSTEM = """\
You are a knowledge graph schema inference engine. Given CSV column names and
sample rows, decide how to turn the table into entities, attributes, and
relationships.

STEP 1 — How many real-world entities does ONE ROW describe?
Wide/denormalized exports usually bundle SEVERAL distinct entities per row — a
person, a transaction, a place, an organization, a product. Read the column-name
clusters AND the sample values: each distinct real-world "noun" that has its own
identity is a separate entity. This is the COMMON case for exports across every
domain (orders, claims, encounters, bookings, rosters, listings…). Default to
multi-entity unless the row genuinely describes ONE thing.

MULTI-ENTITY output (the usual case) — return:
- `entities`: one object per entity, each with `name` (a local handle),
  `type_name` (PascalCase singular), and an id — either `id_column` (a natural
  key column like order_id / patient_id / sku) OR `id_from` (the columns that
  together identify it, e.g. ["customer_email"] or ["first_name","last_name",
  "phone"]) when there is no single id column.
- every column tagged with `entity` = the entity `name` it belongs to.
- `relationships`: {{subject, predicate, object}} edges between entity `name`s;
  predicate is a snake_case verb (order `placed_by` customer, order `contains`
  product, encounter `treated_by` provider, claim `filed_against` policy).
- SAME TYPE TWICE: if two column-clusters are the same base type in different
  roles (buyer & seller, sender & receiver, patient & provider, applicant &
  co_applicant) make them TWO separate entities with distinct names and
  role-distinct relationships — NEVER merge them into one.

SINGLE-ENTITY output — ONLY when the row describes one thing (a product catalog,
a transactions ledger, a lab result, a sensor reading): OMIT `entities` and
`relationships`, return entity_type + columns with exactly one column = type_id.
Do not invent entities for a genuinely flat row.

Type naming & reuse: the user message lists the tenant's EXISTING ontology
types. Reuse one ONLY when your entity is genuinely the SAME real-world concept
(another order → Order; another guest → Person). If none genuinely matches,
propose a NEW accurate PascalCase type name — NEVER force-fit a different concept
onto an available type just because it exists (a hospital is a Facility, not a
Property; a drug is a Drug, not a Product; an airport is an Airport, not a City).

Column roles & datatypes (both modes):
- role = type_id (single-entity only) | attribute | relationship.
- IN-ROW entities are expressed via the `entities` array — NOT via relationship
  columns. Use a `relationship` column (with `target_type`) only for a shared
  out-of-row dimension that is NOT one of your in-row entities (e.g. a bare
  country or category name with no other columns describing it).
- Datatype from the VALUE, not its JSON type (values may arrive as numbers or
  strings): numbers → integer/float, dates → datetime, true/false → boolean,
  URLs → uri, else string.
- NEVER use a date, timestamp, or a non-unique label as an id. If no unique key
  column exists, use `id_from` (a composite of the columns that identify it).

Respond with valid JSON only. No markdown."""

CSV_SCHEMA_USER = """\
Column names: {columns}

Sample rows (first {n} of {total}):
{sample_rows}

Existing ontology types:
{existing_types}

Follow these two worked examples (different domains — generalize the pattern,
do not copy the type names).

EXAMPLE A — a WIDE multi-entity row. Columns: order_id, order_date,
customer_id, customer_email, sku, product_name, qty, ship_country
{{
  "entity_type": "Order",
  "columns": [
    {{"column_name": "order_id", "role": "attribute", "datatype": "string", "attribute_name": "order_id", "entity": "order"}},
    {{"column_name": "order_date", "role": "attribute", "datatype": "datetime", "attribute_name": "order_date", "entity": "order"}},
    {{"column_name": "qty", "role": "attribute", "datatype": "integer", "attribute_name": "qty", "entity": "order"}},
    {{"column_name": "customer_id", "role": "attribute", "datatype": "string", "attribute_name": "customer_id", "entity": "customer"}},
    {{"column_name": "customer_email", "role": "attribute", "datatype": "string", "attribute_name": "email", "entity": "customer"}},
    {{"column_name": "sku", "role": "attribute", "datatype": "string", "attribute_name": "sku", "entity": "product"}},
    {{"column_name": "product_name", "role": "attribute", "datatype": "string", "attribute_name": "name", "entity": "product"}},
    {{"column_name": "ship_country", "role": "relationship", "target_type": "Country", "datatype": "string", "attribute_name": "ship_country", "entity": "order"}}
  ],
  "entities": [
    {{"name": "order", "type_name": "Order", "id_column": "order_id"}},
    {{"name": "customer", "type_name": "Customer", "id_column": "customer_id"}},
    {{"name": "product", "type_name": "Product", "id_column": "sku"}}
  ],
  "relationships": [
    {{"subject": "order", "predicate": "placed_by", "object": "customer"}},
    {{"subject": "order", "predicate": "contains", "object": "product"}}
  ]
}}

EXAMPLE B — a FLAT single-entity row (omit entities/relationships). Columns:
isbn, title, author_name, price, published_date
{{
  "entity_type": "Book",
  "columns": [
    {{"column_name": "isbn", "role": "type_id", "datatype": "string", "attribute_name": "isbn"}},
    {{"column_name": "title", "role": "attribute", "datatype": "string", "attribute_name": "title"}},
    {{"column_name": "author_name", "role": "relationship", "target_type": "Author", "datatype": "string", "attribute_name": "author_name"}},
    {{"column_name": "price", "role": "attribute", "datatype": "float", "attribute_name": "price"}},
    {{"column_name": "published_date", "role": "attribute", "datatype": "datetime", "attribute_name": "published_date"}}
  ]
}}

Now return the JSON for the columns above — tag EVERY column. Use the
multi-entity shape (with `entities`) whenever the row bundles more than one
real-world entity."""


class CSVResolver:
    EXTRACT_MODEL = os.environ.get("OMNIX_EXTRACT_MODEL", "deepseek/deepseek-v3.2")
    EXTRACT_PROVIDER = os.environ.get("OMNIX_EXTRACT_PROVIDER", "openrouter")

    def __init__(self, client: anthropic.AsyncAnthropic, openrouter_key: str = ""):
        self._client = client
        self._openrouter_key = openrouter_key or os.environ.get("OPENROUTER_API_KEY", "")

    async def infer_schema(
        self,
        headers: list[str],
        sample_rows: list[dict[str, str]],
        existing_types: dict[str, str],
        total_rows: int = 0,
    ) -> CSVSchemaMapping:
        """Infer column-to-ontology mapping from sample rows. Single LLM call,
        with one retry at higher temperature if the response fails validation."""
        types_str = "\n".join(f"- {name}" for name in existing_types) if existing_types else "(none)"

        # Prefer rows with the most non-empty fields. CSVs whose leading rows
        # are mostly-empty (e.g. `status=deleted` records with only slug+url)
        # otherwise feed the LLM a near-blank sample, which reliably produces
        # malformed JSON keys (observed: `column118 name`).
        ranked_samples = _rank_sample_rows(sample_rows)[:10]
        sample_str = "\n".join(
            json.dumps(row, default=str) for row in ranked_samples
        )

        user_content = CSV_SCHEMA_USER.format(
            columns=", ".join(headers),
            n=len(ranked_samples),
            total=total_rows or len(sample_rows),
            sample_rows=sample_str,
            existing_types=types_str,
        )

        try:
            data = await self._call_llm(user_content, temperature=0.0)
            mapping = self._build_mapping(data)
        except (ValidationError, KeyError, json.JSONDecodeError) as e:
            logger.warning("csv_schema_validation_retry", error=str(e))
            data = await self._call_llm(user_content, temperature=0.3)
            mapping = self._build_mapping(data)

        # In multi-entity mode, ids come from the EntitySpec specs (not a
        # type_id column), so the single-entity type_id enforcement below is
        # skipped. The geographic/entity promotion pass still runs (columns keep
        # their `entity` owner).
        multi = mapping.entities is not None

        # Validate: must have exactly one type_id (single-entity mode only)
        if not multi:
            id_cols = [c for c in mapping.columns if c.role == ColumnRole.TYPE_ID]
            if len(id_cols) != 1:
                logger.warning("csv_schema_no_id", id_cols=len(id_cols))
                # Fallback: use first column as ID
                if mapping.columns:
                    mapping.columns[0].role = ColumnRole.TYPE_ID

        # Post-processing: if the chosen type_id is numeric, prefer a string
        # column with a name-like label (institution, title, name, etc.)
        # Numeric IDs cause deduplication when values repeat.
        id_col = None if multi else next((c for c in mapping.columns if c.role == ColumnRole.TYPE_ID), None)
        if id_col and id_col.datatype in ("integer", "float"):
            NAME_HINTS = {"name", "title", "institution", "series_title", "label", "id"}
            for col in mapping.columns:
                col_key = (col.attribute_name or col.column_name).lower().replace(" ", "_")
                if col_key in NAME_HINTS and col.role != ColumnRole.TYPE_ID:
                    logger.info(
                        "csv_type_id_override",
                        old=id_col.column_name,
                        new=col.column_name,
                        reason="numeric ID replaced with name-like column",
                    )
                    id_col.role = col.role
                    col.role = ColumnRole.TYPE_ID
                    break

        # Post-processing: enforce entity-first for known geographic/entity columns
        # The LLM sometimes ignores the prompt and treats these as string attributes
        FORCE_RELATIONSHIP = {
            # Geographic
            "city": "City",
            "state": "State",
            "country": "Country",
            "region": "Region",
            "zipcode": "ZipCode",
            "zip_code": "ZipCode",
            "zip": "ZipCode",
            "postal_code": "PostalCode",
            "county": "County",
            "district": "District",
            "neighborhood": "Neighborhood",
            "area": "Area",
            # People
            "owner": "Person",
            "agent": "Person",
            "broker": "Person",
            "manager": "Person",
            "seller": "Person",
            "buyer": "Person",
            "author": "Person",
            "creator": "Person",
            # Organizations
            "company": "Company",
            "brokerage": "Company",
            "firm": "Company",
            "agency": "Company",
            "school": "School",
            "university": "University",
        }
        for col in mapping.columns:
            col_key = (col.attribute_name or col.column_name).lower().replace(" ", "_")
            if col.role == ColumnRole.ATTRIBUTE and col_key in FORCE_RELATIONSHIP:
                col.role = ColumnRole.RELATIONSHIP
                col.target_type = FORCE_RELATIONSHIP[col_key]
                col.datatype = "string"
                logger.info("csv_column_promoted", column=col.column_name, target_type=col.target_type)

        logger.info(
            "csv_schema_inferred",
            entity_type=mapping.entity_type,
            columns=len(mapping.columns),
            relationships=sum(1 for c in mapping.columns if c.role == ColumnRole.RELATIONSHIP),
        )
        return mapping

    async def _call_llm(self, user_content: str, temperature: float = 0.0) -> dict:
        if self.EXTRACT_PROVIDER == "openrouter" and self._openrouter_key:
            return await self._infer_via_openrouter(user_content, temperature)
        return await self._infer_via_anthropic(user_content, temperature)

    def _build_mapping(self, data: dict) -> CSVSchemaMapping:
        # Gemini Flash occasionally emits `datatype: null` for a column it
        # can't classify. Coerce to "string" so the pydantic model doesn't
        # reject the whole inference — callers can always retry the
        # downstream resolver pass if the string guess turns out wrong.
        for col in data.get("columns", []):
            if col.get("datatype") is None:
                col["datatype"] = "string"
            if col.get("role") is None:
                col["role"] = "attribute"
        # Multi-entity mode is opt-in: the model returns a non-empty `entities`
        # array only for wide CSVs that bundle several entities. Absent → legacy.
        entities = data.get("entities") or None
        relationships = data.get("relationships") or None
        # `entity_type` is required in single-entity mode — its absence signals a
        # malformed LLM response and (by raising KeyError) triggers the retry. In
        # multi-entity mode it's ignored, so a placeholder is fine.
        entity_type = data.get("entity_type")
        if entity_type is None:
            if entities is None:
                raise KeyError("entity_type")
            entity_type = "Entity"
        return CSVSchemaMapping(
            entity_type=entity_type,
            columns=[ColumnMapping(**col) for col in data["columns"]],
            entities=[EntitySpec(**e) for e in entities] if entities else None,
            relationships=(
                [EntityRelationSpec(**r) for r in relationships] if relationships else None
            ),
        )

    async def _infer_via_openrouter(self, user_content: str, temperature: float = 0.0) -> dict:
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.EXTRACT_MODEL,
                    "messages": [
                        {"role": "system", "content": CSV_SCHEMA_SYSTEM},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": 2048,
                    "temperature": temperature,
                },
            )
            res.raise_for_status()
            text = res.json()["choices"][0]["message"]["content"]
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
                stripped = "\n".join(lines)
            return json.loads(stripped)

    async def _infer_via_anthropic(self, user_content: str, temperature: float = 0.0) -> dict:
        msg = await self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            temperature=temperature,
            system=CSV_SCHEMA_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "entity_type": {"type": "string"},
                            "columns": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "column_name": {"type": "string"},
                                        "role": {"type": "string", "enum": ["type_id", "attribute", "relationship"]},
                                        "target_type": {"type": ["string", "null"]},
                                        "datatype": {"type": "string"},
                                        "attribute_name": {"type": ["string", "null"]},
                                        "entity": {"type": ["string", "null"]},
                                    },
                                    "required": ["column_name", "role", "datatype"],
                                    "additionalProperties": False,
                                },
                            },
                            "entities": {
                                "type": ["array", "null"],
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "type_name": {"type": "string"},
                                        "id_column": {"type": ["string", "null"]},
                                        "id_from": {"type": ["array", "null"], "items": {"type": "string"}},
                                    },
                                    "required": ["name", "type_name"],
                                    "additionalProperties": False,
                                },
                            },
                            "relationships": {
                                "type": ["array", "null"],
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "subject": {"type": "string"},
                                        "predicate": {"type": "string"},
                                        "object": {"type": "string"},
                                    },
                                    "required": ["subject", "predicate", "object"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["entity_type", "columns"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        return json.loads(msg.content[0].text)

    @staticmethod
    def apply_mapping(
        mapping: CSVSchemaMapping,
        rows: list[dict[str, str]],
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelationship]]:
        """Deterministically convert all CSV rows to entities + relationships. No LLM."""
        # Multi-entity mode: one row expands into several fully-attributed,
        # linked entities. Legacy single-entity path below is untouched.
        if mapping.entities:
            return CSVResolver._apply_multi_entity(mapping, rows)

        id_col = next((c for c in mapping.columns if c.role == ColumnRole.TYPE_ID), None)
        if not id_col:
            return [], []

        entities: list[ExtractedEntity] = []
        relationships: list[ExtractedRelationship] = []
        seen_rel_entities: dict[str, str] = {}  # safe_id → type for relationship targets
        rel_entity_names: dict[str, str] = {}  # safe_id → original value for name attr

        for row in rows:
            entity_id = row.get(id_col.column_name, "").strip()
            if not entity_id:
                continue

            safe_id = _safe_id(entity_id)
            attrs: list[ExtractedAttribute] = []
            entity_rels: list[ExtractedRelationship] = []

            for col in mapping.columns:
                if col.role == ColumnRole.TYPE_ID:
                    continue
                raw_value = row.get(col.column_name, "")
                if isinstance(raw_value, str):
                    raw_value = raw_value.strip()
                if not raw_value:
                    continue

                attr_name = col.attribute_name or _snake_case(col.column_name)

                # Handle JSON arrays, pipe-delimited, and comma-delimited strings
                # by expanding into multiple values for relationships
                if col.role == ColumnRole.RELATIONSHIP and col.target_type:
                    values: list[str] = []
                    if isinstance(raw_value, list):
                        values = [v.strip() for v in raw_value if isinstance(v, str) and v.strip()]
                    elif "|" in raw_value:
                        values = [v.strip() for v in raw_value.split("|") if v.strip()]
                    elif ", " in raw_value:
                        # Comma-delimited: split if parts are short (not addresses)
                        parts = [v.strip() for v in raw_value.split(", ") if v.strip()]
                        if all(len(p) < 30 for p in parts) and len(parts) >= 2:
                            values = parts
                        else:
                            values = [raw_value]
                    else:
                        values = [raw_value]

                    for value in values:
                        target_id = _safe_id(value)
                        entity_rels.append(ExtractedRelationship(
                            source_id=safe_id,
                            predicate=attr_name,
                            target_id=target_id,
                        ))
                        if target_id not in seen_rel_entities:
                            seen_rel_entities[target_id] = col.target_type
                            rel_entity_names[target_id] = value

                elif col.role == ColumnRole.ATTRIBUTE:
                    value = str(raw_value) if not isinstance(raw_value, str) else raw_value
                    # Split pipe-delimited attribute values into multiple triples.
                    # "PHASE1|PHASE2" becomes two separate attribute triples so that
                    # exact-match SPARQL filters work without CONTAINS.
                    if "|" in value and col.datatype == "string":
                        for v in value.split("|"):
                            v = v.strip()
                            if v:
                                attrs.append(ExtractedAttribute(
                                    name=attr_name,
                                    value=v,
                                    datatype=col.datatype,
                                ))
                    else:
                        attrs.append(ExtractedAttribute(
                            name=attr_name,
                            value=value,
                            datatype=col.datatype,
                        ))

            entities.append(ExtractedEntity(
                type_name=mapping.entity_type,
                id=safe_id,
                attributes=attrs,
            ))
            relationships.extend(entity_rels)

        # Create stub entities for relationship targets (so they exist in the graph)
        for target_id, target_type in seen_rel_entities.items():
            entities.append(ExtractedEntity(
                type_name=target_type,
                id=target_id,
                attributes=[ExtractedAttribute(name="name", value=rel_entity_names.get(target_id, target_id.replace("_", " ")), datatype="string")],
            ))

        return entities, relationships

    @staticmethod
    def _entity_key(spec, row: dict) -> str | None:
        """Deterministic key for one in-row entity: its id_column value, or a
        composite of id_from columns. None when the key resolves empty."""
        if spec.id_column:
            v = (row.get(spec.id_column) or "").strip()
            return _safe_id(v) if v else None
        if spec.id_from:
            parts = [(row.get(c) or "").strip() for c in spec.id_from]
            if not any(parts):
                return None
            return _safe_id("|".join(parts))
        return None

    @staticmethod
    def _apply_multi_entity(
        mapping: CSVSchemaMapping,
        rows: list[dict[str, str]],
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelationship]]:
        """Multi-entity mode: one row → several fully-attributed, linked entities.

        Each `EntitySpec` is keyed by its id_column or an id_from composite.
        Columns route to their owner entity (`ColumnMapping.entity`). Inter-entity
        relationships reference the same deterministic ids the entities are minted
        from, so edges resolve to real URIs (not stubs). Entities dedup across
        rows by (type, id) with attribute union — collapsing repeated keys (e.g.
        many reservations → 5 Properties) into one entity. ER fires per
        ER-enabled type downstream (schema_resolver); nothing ER-specific here.
        """
        specs = {e.name: e for e in (mapping.entities or [])}

        # Route columns to their owner entity; drop (and log) unowned columns.
        cols_by_entity: dict[str, list[ColumnMapping]] = {name: [] for name in specs}
        for col in mapping.columns:
            if col.role == ColumnRole.TYPE_ID:
                continue  # in multi-entity mode, ids come from EntitySpec
            owner = col.entity
            if owner is None or owner not in specs:
                logger.warning(
                    "csv_multi_unowned_column", column=col.column_name, entity=owner,
                )
                continue
            cols_by_entity[owner].append(col)

        entities_by_key: dict[tuple[str, str], ExtractedEntity] = {}
        relationships: list[ExtractedRelationship] = []

        def add_entity(type_name: str, key: str, attrs: list[ExtractedAttribute]) -> None:
            ekey = (type_name, key)
            ent = entities_by_key.get(ekey)
            if ent is None:
                entities_by_key[ekey] = ExtractedEntity(
                    type_name=type_name, id=key, attributes=list(attrs),
                )
                return
            seen = {(a.name, a.value) for a in ent.attributes}
            for a in attrs:
                if (a.name, a.value) not in seen:
                    ent.attributes.append(a)
                    seen.add((a.name, a.value))

        for row in rows:
            row_ids: dict[str, str] = {}
            for name, spec in specs.items():
                key = CSVResolver._entity_key(spec, row)
                if key is None:
                    continue
                row_ids[name] = key
                attrs: list[ExtractedAttribute] = []
                for col in cols_by_entity[name]:
                    raw = row.get(col.column_name, "")
                    if isinstance(raw, str):
                        raw = raw.strip()
                    if not raw:
                        continue
                    attr_name = col.attribute_name or _snake_case(col.column_name)
                    if col.role == ColumnRole.RELATIONSHIP and col.target_type:
                        # Out-of-row reference (e.g. country) → stub target + edge.
                        for value in _rel_values(raw):
                            tid = _safe_id(value)
                            relationships.append(ExtractedRelationship(
                                source_id=key, predicate=attr_name, target_id=tid,
                            ))
                            add_entity(col.target_type, tid, [ExtractedAttribute(
                                name="name", value=value, datatype="string",
                            )])
                    elif col.role == ColumnRole.ATTRIBUTE:
                        value = str(raw)
                        if "|" in value and col.datatype == "string":
                            for v in value.split("|"):
                                v = v.strip()
                                if v:
                                    attrs.append(ExtractedAttribute(
                                        name=attr_name, value=v, datatype=col.datatype,
                                    ))
                        else:
                            attrs.append(ExtractedAttribute(
                                name=attr_name, value=value, datatype=col.datatype,
                            ))
                add_entity(spec.type_name, key, attrs)

            # Inter-entity edges — only when both endpoints exist this row.
            for rel in (mapping.relationships or []):
                s = row_ids.get(rel.subject)
                o = row_ids.get(rel.object)
                if s and o:
                    relationships.append(ExtractedRelationship(
                        source_id=s, predicate=rel.predicate, target_id=o,
                    ))

        return list(entities_by_key.values()), relationships


def _rank_sample_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Sort by descending non-empty field count; stable on ties (preserves
    original order). Used so the LLM gets the most informative rows when the
    head of the CSV is sparse (deleted/empty records). Does not mutate input."""
    def score(row: dict) -> int:
        return sum(
            1 for v in row.values()
            if v is not None and (not isinstance(v, str) or v.strip() != "")
        )
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda t: (-score(t[1]), t[0]))
    return [r for _, r in indexed]


def _rel_values(raw_value) -> list[str]:
    """Split a relationship cell into one or more target labels (JSON array,
    pipe-delimited, or short comma-delimited). Mirrors the legacy single-entity
    splitting so multi-entity and legacy paths behave identically."""
    if isinstance(raw_value, list):
        return [v.strip() for v in raw_value if isinstance(v, str) and v.strip()]
    raw_value = str(raw_value)
    if "|" in raw_value:
        return [v.strip() for v in raw_value.split("|") if v.strip()]
    if ", " in raw_value:
        parts = [v.strip() for v in raw_value.split(", ") if v.strip()]
        if all(len(p) < 30 for p in parts) and len(parts) >= 2:
            return parts
    return [raw_value.strip()] if raw_value.strip() else []


def _safe_id(raw: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())
    return safe[:200] if safe else "unknown"


def _snake_case(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]", "_", name.strip())
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s or "unnamed"
