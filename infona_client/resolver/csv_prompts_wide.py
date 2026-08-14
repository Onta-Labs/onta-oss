"""Wide-table (COG-58) entity-decomposition + column-assignment prompts."""

from __future__ import annotations

# --- COG-58 Pass B split: ENTITY (global) -----------------------------------
# Same entity-decomposition rules as REASON_SYSTEM, but the model returns ONLY
# the entity list + inter-entity edges — never a per-column array. Output is
# therefore bounded by the (small) entity count, not the column count, so this
# pass is safe at any table width.

ENTITY_SYSTEM = """\
You decompose a CSV table into the knowledge-graph ENTITIES it describes (not
the columns yet). You are given a STATISTICAL PROFILE of every column computed
over the full table. Reason from that evidence, not from column names.

ENTITY DECOMPOSITION
- Columns that travel together (mutual functional dependency) and include an
  id-like member describe ONE entity; a code column paired with its label/title
  column are the SAME entity (code = its key, title = its label).
- A column with low card_ratio that repeats across rows (distinct far below row
  count) is a DIMENSION: a shared entity referenced by many rows. Model it as
  its own entity, NOT a literal attribute of the row's primary entity.
- Wide/denormalized exports usually bundle SEVERAL distinct entities per row — a
  person, a transaction, a place, an organization, a product. Default to
  multi-entity unless the row genuinely describes ONE thing.
- SAME TYPE TWICE: two column-clusters of the same base type in different roles
  (buyer & seller, patient & provider, applicant & co_applicant) are TWO
  separate entities with distinct names — NEVER merge them.

KEYS (row conservation is mandatory)
- An entity key must be a column that is BOTH ~100% complete AND unique.
- If none qualifies, use a composite of identifying columns or a synthetic id.
  NEVER key on an incomplete column: it silently drops every row missing it.

TYPE REUSE
- The user message lists the tenant's EXISTING ontology (types with attributes
  and relationships). Reuse a type ONLY when your entity is genuinely the SAME
  real-world concept. Otherwise propose a NEW accurate PascalCase type name —
  never force-fit a different concept.

Output strict JSON ONLY (no columns array):
{"entities":[{"name","type_name","key_strategy":"column|composite|synthetic",
"key_columns":[...],"why","confidence"}],
"relationships":[{"subject","predicate","object","why"}]}
An edge predicate names the RELATIONSHIP (a role/verb) between two entity
names, never a source column name. JSON only."""

ENTITY_USER = """\
COLUMN PROFILE (computed over {rows_profiled} of {total_rows} rows):
{profile}

SAMPLE ROWS ({n} highest-density rows — value context only; trust the profile for statistics):
{sample_rows}

EXISTING ONTOLOGY (types with attributes and relationships):
{existing_types}

This table has {n_columns} columns — return ONLY the entity decomposition
(entities + inter-entity relationships). Column assignment happens separately.
Return the JSON now."""


# --- COG-58 Pass B split: COLUMN ASSIGNMENT (chunked) -----------------------
# Given the already-decided entities, assign a BATCH of columns to them. Output
# is bounded by the batch size, so arbitrarily wide tables are handled by
# running this pass once per chunk and merging the column arrays.

COLUMN_ASSIGN_SYSTEM = """\
You assign CSV columns to an ALREADY-DECIDED set of knowledge-graph entities.
The entities (with their types and keys) are fixed — do NOT invent new entities.
For EACH column in the batch, decide:
- role: "attribute" (a literal property), "relationship" (a reference to a
  shared OUT-OF-ROW entity that is NOT one of the decided entities — carry a
  "target_type"), or "key" (an identifying column of its owner entity).
- entity: the NAME of the decided entity this column belongs to.
- predicate_or_attr: a snake_case attribute name, or the edge predicate for a
  relationship (a role/verb, never the raw column name).
- text_kind: for a column whose profiled shape is "text", set "free_text" when
  its values are free-running PROSE (descriptions, reviews, notes, transcripts);
  set null EXPLICITLY for structured strings (addresses, person/organization
  names, titles used as labels — an explicit null records a decided NO, while
  omitting the field leaves it undecided). Omit for every non-text-shaped column.
Datatypes are derived later from the profile — do not emit them.
Tag EVERY column in the batch exactly once. Output strict JSON ONLY:
{"columns":[{"column","role":"attribute|relationship|key","entity",
"predicate_or_attr","target_type":null|"<T>","why","confidence",
"text_kind":null|"free_text"}]}
JSON only."""

COLUMN_ASSIGN_USER = """\
DECIDED ENTITIES (fixed — assign each column to one of these by name):
{entities}

COLUMN PROFILE for THIS BATCH (computed over {rows_profiled} of {total_rows} rows):
{profile}

SAMPLE ROWS ({n} highest-density rows — value context only):
{sample_rows}

Assign every column in this batch to one of the decided entities and return the
columns JSON now."""

