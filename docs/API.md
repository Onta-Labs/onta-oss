# Omnix API Reference

**Version:** 0.1.0

Living Knowledge Graph Platform

Auto-generated from the OpenAPI spec. Do not edit manually.

## Interactive Docs

When the server is running, visit:
- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## Ask

### `POST /graphs/{tenant}/ask`

Ask Question

**Request body:** `NLQuery`

**200:** Successful Response
**422:** Validation Error

---

## Functions

### `POST /graphs/{tenant}/functions`

Register Function

**Request body:** `FunctionRegister`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/functions`

List Functions

**200:** Successful Response
**422:** Validation Error

---

## Health

### `GET /health`

Health

**200:** Successful Response

---

## Ingest

### `POST /graphs/{tenant}/ingest`

Ingest

Ingest raw content into the knowledge graph.

Runs LLM extraction, schema resolution (type matching, attribute
resolution, validation), and inserts validated triples into Neptune.

**Request body:** `IngestRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ingest/csv/schema`

Infer Csv Schema

Step 1: Infer column mapping from CSV headers + sample rows. Single LLM call.

**Request body:** `CSVSchemaRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ingest/csv/rows`

Ingest Csv Rows

Step 2: Insert rows using a pre-inferred mapping. No LLM call.

**Request body:** `CSVRowsRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/embeddings/build`

Build Embeddings

Trigger a full embedding build for all ontology types in this tenant.

**200:** Successful Response

---

## Knowledge_Graphs

### `GET /graphs/{tenant}/kgs`

List Kgs

List all knowledge graphs for a tenant.

**200:** Successful Response

---

### `POST /graphs/{tenant}/kgs`

Create Kg

Create a new knowledge graph for a tenant.

**Request body:** `KGCreate`

**201:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/kgs/{kg_name}`

Delete Kg

Delete a knowledge graph and all its data.

**200:** Successful Response
**422:** Validation Error

---

## Ontology

### `GET /graphs/{tenant}/ontology/types`

List Types

**200:** Successful Response

---

### `POST /graphs/{tenant}/ontology/types`

Create Type

**Request body:** `TypeCreate`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/types/{type_name}`

Get Type

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/types/{type_name}/attributes`

Add Attributes

**Request body:** `AttributeAdd`

**201:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/types/{type_name}/subtypes`

Add Subtype

**Request body:** `SubtypeAdd`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/schema`

Get Full Schema

Get the complete ontology schema. Used by the NL pipeline.

**200:** Successful Response

---

## Query

### `POST /graphs/{tenant}/query`

Execute Query

**Request body:** `SPARQLQuery`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/update`

Execute Update

**Request body:** `SPARQLUpdate`

**200:** Successful Response
**422:** Validation Error

---

## Triples

### `POST /graphs/{tenant}/triples`

Create Triples

**Request body:** `TripleCreate`

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/triples`

Get Triples

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/triples`

Remove Triples

**Request body:** `TripleDelete`

**200:** Successful Response
**422:** Validation Error

---

## Schemas

### AttributeAdd

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `attributes` | array | Yes |  |

### AttributeDefinition

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `datatype` | string | No | string, integer, float, boolean, datetime, uri, or a type name for relationships |

### CSVRowsRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mapping` | #/components/schemas/CSVSchemaMapping-Input | Yes |  |
| `rows` | array | Yes |  |
| `source` | string | No |  |
| `kg_name` | object | No |  |

### CSVSchemaMapping-Input

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_type` | string | Yes |  |
| `columns` | array | Yes |  |

### CSVSchemaMapping-Output

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_type` | string | Yes |  |
| `columns` | array | Yes |  |

### CSVSchemaRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `headers` | array | Yes |  |
| `sample_rows` | array | Yes |  |
| `total_rows` | integer | No |  |

### ColumnMapping

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `column_name` | string | Yes |  |
| `role` | #/components/schemas/ColumnRole | Yes |  |
| `target_type` | object | No |  |
| `datatype` | string | No |  |
| `attribute_name` | object | No |  |

### ColumnRole

### FunctionRef

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `entity_type` | string | Yes |  |
| `description` | string | No |  |
| `endpoint_url` | object | No |  |
| `tier` | #/components/schemas/FunctionTier | No |  |

### FunctionRegister

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `entity_type` | string | Yes |  |
| `endpoint_url` | string | Yes | HTTPS endpoint for the function |
| `description` | string | No |  |

### FunctionTier

### HTTPValidationError

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `detail` | array | No |  |

### IngestRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | Yes | Raw text, JSON, or CSV to ingest |
| `content_type` | string | No | text, json, or csv |
| `source` | string | No | Source identifier for provenance |
| `kg_name` | object | No | Knowledge graph name. If set, data goes into a KG-specific graph. |

### IngestResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `batch_id` | string | No | Batch ID for rollback support |
| `entities_extracted` | integer | No |  |
| `entities_resolved` | integer | No |  |
| `triples_inserted` | integer | No |  |
| `types_created` | array | No |  |
| `attributes_added` | array | No |  |
| `rejections` | array | No |  |
| `flagged_types` | array | No | Types needing user review |
| `chunks_processed` | integer | No |  |
| `entities_deduplicated` | integer | No |  |

### KGCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |

### KGInfo

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `triple_count` | integer | No |  |

### NLQuery

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `question` | string | Yes |  |
| `kg_name` | object | No | Query a specific knowledge graph |
| `model` | object | No | Override the query generation model (OpenRouter model ID) |

### NLResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `answer` | string | Yes |  |
| `sparql` | string | Yes |  |
| `explanation` | string | Yes |  |
| `ontology` | string | No | Ontology summary text passed to the LLM for SPARQL generation |
| `functions_invoked` | array | No |  |
| `timing` | object | No | Stage latencies in ms and metadata |

### RejectedValue

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_id` | string | Yes |  |
| `attribute` | string | Yes |  |
| `value` | string | Yes |  |
| `expected_datatype` | string | Yes |  |
| `reason` | string | Yes |  |

### SPARQLQuery

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | SPARQL 1.1 query string |

### SPARQLResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bindings` | array | No |  |
| `vars` | array | No |  |

### SPARQLUpdate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `update` | string | Yes | SPARQL 1.1 Update string |

### SubtypeAdd

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subtype` | string | Yes | Name of the child type |

### Triple

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | Yes | RDF subject URI or blank node |
| `predicate` | string | Yes | RDF predicate URI |
| `object` | string | Yes | RDF object (URI, literal, or blank node) |

### TripleBatch

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `inserted` | integer | No |  |
| `deleted` | integer | No |  |

### TripleCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `triples` | array | Yes |  |

### TripleDelete

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `triples` | array | Yes |  |

### TypeCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `parent_type` | object | No | Parent type name for subtype relationship |
| `attributes` | array | No |  |

### TypeResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `parent_type` | object | No |  |
| `attributes` | array | No |  |
| `subtypes` | array | No |  |
| `functions` | array | No |  |

### ValidationError

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `loc` | array | Yes |  |
| `msg` | string | Yes |  |
| `type` | string | Yes |  |
| `input` | object | No |  |
| `ctx` | object | No |  |
