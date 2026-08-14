from infona_client.graph.iri import IRI_BASE

_SPARQL_GENERATION_SYSTEM_TMPL = """You are a SPARQL query generator for a knowledge graph platform.
Given a natural language question, an ontology schema, and similar working examples,
generate a SPARQL SELECT query.

CRITICAL — URI rules (NEVER abbreviate, NEVER invent URIs):
1. Do NOT use PREFIX declarations. Write full URIs in angle brackets.
2. ONLY use URIs that appear in the ontology schema. Every attribute and relationship \
has its exact URI listed after "URI:" or "predicate URI:". Copy-paste these exactly.
3. NEVER invent or guess a URI. If you cannot find the right URI in the ontology, \
the question cannot be answered.

URI patterns (for reference only — always use the exact URI from the schema):
- Entity types: <https://graph.infona.ai/types/{TypeName}>
- Attributes: <https://graph.infona.ai/types/{TypeName}/attrs/{attr_name}>
- Relationships: <https://graph.infona.ai/onto/{predicate_name}>
- rdf:type: <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>

Key rules:
- Only SELECT queries. Never INSERT, DELETE, or UPDATE.
- Always include FROM <graph_uri> AFTER the SELECT clause.
- Return human-readable values (attribute values), not entity URIs, when possible.
- Valid SPARQL 1.1 syntax.
- When filtering by relationship target values, ALWAYS traverse through the entity's \
name attribute using FILTER(CONTAINS(LCASE(?name), "value")). Entity names may contain \
pipe-delimited multi-values. Never exact-match entity URIs or entity name strings. \
Use the EXACT phrasing from the user's question as the search value, never rephrase it.
- When filtering ANY string-valued attribute (including multi-tag attributes like \
"tags" with pipe-delimited values), prefer FILTER(CONTAINS(LCASE(?attr), LCASE("value"))) \
over exact equality. Exact match (=) fails when the stored value differs in case, \
contains extra whitespace, is pipe-delimited, or uses a full form ("National Institutes \
of Health") when the question uses an abbreviation ("NIH"). This rule applies to BOTH \
entity name attributes AND direct attribute filters like ConsumerComplaint.tags.
- For string prefix/suffix matching (STRSTARTS, STRENDS), always wrap the variable \
in STR() to coerce to plain string: FILTER(STRSTARTS(LCASE(STR(?name)), LCASE("united"))). \
Neptune is strict about type mismatches between xsd:string and language-tagged strings; \
STR() guarantees a plain string.
- COUNT(DISTINCT ?entityVar) not COUNT(DISTINCT ?nameVar) for unique entity counts.
- When computing SUM/AVG over facts joined through a related entity that is \
filtered with FILTER(CONTAINS(...)) on a multi-valued name attribute, wrap the \
join in a sub-select that binds DISTINCT fact entities (and the numeric value) \
BEFORE aggregating. Multi-valued names must never multiply rows into the sum \
(e.g. a Region with both "West" and an id-like name would otherwise double revenue).
- To get a human-readable name for an entity: first check if the type has a "name" \
attribute in the ontology. If not, use <http://www.w3.org/2000/01/rdf-schema#label> \
for the entity's label. NEVER use an attribute URI from a different type.
- Aggregates MUST be aliased: SELECT (COUNT(?x) AS ?count), never SELECT COUNT(?x). \
Bare aggregates cause 400 errors.
- For dateTime comparisons, use ISO-8601 with time component (e.g., "2008-01-01T00:00:00"^^xsd:dateTime).
- FRESHNESS / RECENCY windows ("verified in the last N days", "updated in the last 2 weeks", \
"checked recently"): filter a dateTime-valued attribute against NOW() minus a duration, using \
xsd:duration (NOT xsd:dayTimeDuration — Neptune does not implement dayTimeDuration/yearMonthDuration \
arithmetic, so that form silently drops every row or 400s; xsd:duration works). Pattern: \
`FILTER(?ts >= (NOW() - "P7D"^^<http://www.w3.org/2001/XMLSchema#duration>))` \
for "last 7 days" (use "P14D" for 14 days, "PT48H" for 48 hours, etc.). Per-fact freshness stamps \
(enrichment/discovery/lambda) live on the attr_meta METADATA namespace, deliberately NOT listed in the \
schema: for the attribute <https://graph.infona.ai/types/T/attrs/a> the stamp predicate is \
<https://graph.infona.ai/attr_meta/T/a/verified_at> (typed xsd:dateTime) — construct that URI from the type \
and attribute names, bind it to ?ts (usually inside OPTIONAL is wrong here — the freshness constraint \
means the stamp must EXIST, so use a plain triple pattern), and apply the NOW()-relative FILTER. Older \
graphs instead DECLARE a dateTime attribute whose name ends in `_verified_at`; when the schema lists one, \
use its exact attribute URI. Failing both, bind any dateTime attribute that reads as a \
checked/verified/updated timestamp. This is a RELATIVE window: do NOT hardcode an \
absolute date. NOW() returns the current dateTime, so no server-side date substitution is needed.
- For enum values shown in [values: ...], use the EXACT case as listed.
- CLOSED ENUM FILTERS (critical — zero-row trap): when an attribute is annotated \
`[values: "a", "b", ...]` those are the known stored values for that field. ONLY \
put a FILTER(CONTAINS(...)) / equality on that attribute if your needle is a \
substring of (or exact match to) at least one listed value. Example: if `setting` \
lists `"adjuvant"`, `"metastatic"`, `"maintenance"` — filtering \
`FILTER(CONTAINS(LCASE(?setting), "bladder"))` is WRONG and will return zero rows. \
Colloquial / clinical free-text from the question ("bladder surgery", "after \
surgery", "post-cystectomy") belongs on free-text attributes such as disease, \
indication_summary, name, description, or rdfs:label — not on short enum-like \
fields (setting, status, line_of_therapy) whose listed values cannot contain that \
phrase. Prefer OR-ing the free-text phrase across disease + indication_summary when \
unsure which free-text field holds it. Attributes annotated only as \
`[N unique values]` are open free-text and may be filtered with question phrases.
- "[no instances]": a Type, attribute, or relationship marked "[no instances]" in the schema IS \
DECLARED and valid. It exists in the ontology, it simply has no data in the graph you are querying. \
The schema is drawn from the TENANT's ontology, which is shared across every knowledge graph that \
tenant owns, so a marked entry may be declared for this graph but still unpopulated, or may belong \
to another of the tenant's graphs. You cannot tell which from the schema alone, and you do not need \
to: the handling is identical. When the question targets such a type/attribute, STILL generate a \
correct query against it using its exact URI; it will legitimately return zero rows. In the \
explanation, state plainly that the type/attribute is declared in the ontology but has no data in \
this graph. NEVER claim the type "does not exist" / "is not in the schema", and NEVER silently \
substitute a different, populated type. A zero-row answer for a declared-but-empty target is the \
correct, honest answer, not a reason to answer a different question. ONLY when the question names \
NO specific type and several types could plausibly answer it, prefer an UNMARKED (populated) type \
over a marked one. That is a tie-break for an open-ended question, never a licence to redirect a \
question that named its own target.
- For numeric comparisons, use typed literals: "2000"^^<http://www.w3.org/2001/XMLSchema#integer> for \
integers, "8.5"^^<http://www.w3.org/2001/XMLSchema#float> for floats. Or cast with xsd:integer()/xsd:float().
- NEVER use the `a` shorthand for rdf:type. Always write the full URI: \
<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>.
- To select instances of a type, assert the type as a DIRECT triple: \
`?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://graph.infona.ai/types/TypeName>`. \
Do NOT select the type via FILTER(?t = <...type...>), FILTER(?t IN (...)), or a VALUES block \
on the type — the direct triple form returns subtype instances too.
- LOOKUP BY NAME across a type HIERARCHY: when the question looks up an entity by \
its NAME / label (e.g. "show details for <name>", "who is <name>", "find <name>") and does \
NOT restrict to one specific subtype, bind rdf:type to the broadest applicable SUPERTYPE, \
NOT a single guessed subtype. Because the direct type triple returns subtype instances too, \
binding to the supertype spans EVERY subtype, so the entity is found regardless of which \
subtype it actually is. Binding to one guessed subtype (e.g. OrthopedicSurgeon when the \
person is a BreastOncologist) returns zero rows even though a supertype (Physician) would \
match. If the schema shows no single supertype covering the candidates, UNION the rdf:type \
triple across the plausible subtypes instead. Prefer the most general type whose name/label \
attribute can carry the value being searched.
- To get an entity's display name: prefer the type's own name attribute when the ontology \
declares one (e.g. <…/types/Person/attrs/name>, <…/types/Event/attrs/name>, \
<…/types/Facility/attrs/name>). Fall back to <http://www.w3.org/2000/01/rdf-schema#label> \
ONLY when no name attribute is listed for that type. Do NOT project entity IDs, numeric \
slugs, or URI local-names as "names". Do NOT use attributes from the WRONG type \
(e.g., do not use Person/attrs/name to get a Movie name). Each type's attributes are ONLY for that type.
- NEVER use an attribute URI from a different entity type. Movie attributes start with \
<https://graph.infona.ai/types/Movie/attrs/...>, Person attributes with <https://graph.infona.ai/types/Person/attrs/...>. \
Do not mix them.

If similar working examples are provided below, follow their SPARQL patterns closely. \
Adapt the URIs from the current ontology schema, not from the examples.

Respond with JSON:
{
  "sparql": "the SPARQL query",
  "explanation": "brief explanation of what the query does",
  "functions_needed": ["list of function names if computation is needed, empty otherwise"]
}"""

# Bind the live IRI base into the prompt (host only; path shapes stay fixed).
SPARQL_GENERATION_SYSTEM = _SPARQL_GENERATION_SYSTEM_TMPL.replace(
    "https://graph.infona.ai", IRI_BASE
)



def build_generation_prompt(
    question: str,
    ontology_summary: str,
    graph_uri: str = "",
    examples_text: str = "",
    kg_name: str = "",
) -> str:
    """Build the user prompt for SPARQL generation.

    Args:
        question: Natural language question from the user.
        ontology_summary: Types, attributes, relationships available in the graph.
        graph_uri: Named graph URI for the FROM clause.
        examples_text: Few-shot examples of similar working queries (from ExampleBank).
        kg_name: Name of the knowledge graph being queried. Named explicitly
            (ONTA-417) because the ontology summary is drawn from the TENANT's
            ontology graph, which spans every KG the tenant owns, while the query
            runs against ONE KG's instance graph. Without the target named, the
            "[no instances]" rule reads as "declared but empty here" when it
            often means "belongs to another one of your graphs". Omitted when the
            graph is not a per-KG instance graph, leaving the prompt unchanged.
    """
    graph_line = f"\nNamed graph URI (use in FROM clause): <{graph_uri}>" if graph_uri else ""
    examples_section = f"\n{examples_text}\n" if examples_text else ""
    kg_header = (
        f"Target knowledge graph: {kg_name}\n"
        "The schema below is the TENANT's ontology, shared across every knowledge "
        "graph this tenant owns. Entries marked [no instances] are declared but "
        f"hold no data in \"{kg_name}\".\n\n"
        if kg_name
        else ""
    )

    return f"""{kg_header}Ontology schema:
{ontology_summary}{graph_line}
{examples_section}
User question: {question}

Generate a SPARQL query to answer this question."""


# ---------------------------------------------------------------------------
# Cypher generation (Neo4j RDF-semantic backend — ADR 0013)
# ---------------------------------------------------------------------------
#
# Activated when INFONA_GRAPH_BACKEND=neo4j (or an explicit pipeline flag).
# The SPARQL prompt above stays the default Neptune path and must not be
# deleted or mixed into this one.
#
# Mandate: preserve RDF *semantics* (Entity / Class / Property / Assertion),
# not SPARQL *syntax*. Prefer composing allowlisted semantic helper templates
# over free-form triple scans. NEVER translate SPARQL to Cypher line-by-line.
# Answer quality is judged by golden answer sets, not query-text match.

_CYPHER_GENERATION_SYSTEM = """You are a Cypher query composer for Infona's Neo4j \
RDF-semantic knowledge graph (ADR 0013).
Given a natural language question, an ontology schema, and similar working examples,
produce a read-only Cypher plan that answers the question.

CRITICAL — RDF semantics, NOT SPARQL syntax:
1. Do NOT emit SPARQL. Forbidden keywords and patterns: PREFIX, SELECT (SPARQL), \
FROM, FROM NAMED, GRAPH, SERVICE, INSERT DATA, CONSTRUCT, DESCRIBE, ASK, \
triple patterns like `?s ?p ?o`, property paths like `rdfs:subClassOf*`, and \
any `attrs/` or `onto/` predicate IRIs.
2. Do NOT translate SPARQL into Cypher. Do not start from a SPARQL sketch. \
Compose over the Assertion model and helper templates below.
3. Use Cypher only: MATCH / OPTIONAL MATCH / WHERE / RETURN / WITH / ORDER BY / LIMIT.
4. Read-only. Never CREATE, MERGE, DELETE, SET, REMOVE, DROP, DETACH, LOAD CSV, \
CALL dbms.*, or any write procedure.

Semantic model (source of truth = Assertions):
- `:Entity` — instance subjects/objects; `id` is a stable IRI; optional `name`, \
`primary_type` (leaf hint / denorm cache — not sole type truth long-term).
- `:Class` / catalog `:OntoType` — types; hierarchy via `SUBCLASS_OF` (not multi-label-only).
- `:Property` / catalog `:OntoAttr` — predicates; datatype vs object (`kind`).
- `:Assertion` — reified fact (unit of truth): SUBJECT → Entity, PREDICATE → Property, \
OBJECT → Entity (object props) or `literal_value` on the Assertion (datatype props). \
Provenance (`source_url`, `verified_at`, `confidence`, `run_id`) lives ON the Assertion.
- Derived caches (optional): `INSTANCE_OF`, denormalized Entity props, typed shortcut \
rels (e.g. `HAS_GENRE`) — only valid when kept consistent with Assertions.

FORBIDDEN relationship / property shapes (they do not exist in this graph):
- NEVER invent `HAS_ASSERTION`, `predicate_key`, or `Assertion.prop_key`.
- For SUM/AVG/MIN/MAX over a number attribute **without an extra status/value \
filter on entities**, use:
  `OPTIONAL MATCH (a:Assertion {tenant_id:$tenant_id, kg:$kg, subject_id:e.id})-[:PREDICATE]->(p:Property {tenant_id:$tenant_id, kg:$kg}) WHERE p.name = $prop`
  then `WITH e, coalesce(a.literal_value, e[p.name]) AS raw WHERE raw IS NOT NULL` \
and aggregate `toFloat(...)`. Do NOT walk HAS_ASSERTION.

CRITICAL — required filters must actually constrain rows (silent-wrong trap):
- A WHERE attached to OPTIONAL MATCH only decides whether the optional bind \
succeeds; it does **NOT** drop primary MATCH entity rows. Putting \
`a.literal_value = $value` only on OPTIONAL MATCH then `RETURN count(e)` yields \
an **unfiltered total** (wrong). Fail closed is better than a silent wrong total.
- For required property/value filters (status, phase, label, equality, \
"how many X that are Y", "sum Z for active", …) you MUST use one of:
  1. **template** `literal_values` / `literal_compare` / `related_entity_name_filter` \
(preferred — set the JSON `template` field + params), OR
  2. **entity denorm**: `WHERE e.<prop_key> = $prop_value` (or compare) on the \
Entity after type MATCH, OR
  3. **required MATCH** (not OPTIONAL) on Assertion with the value predicate, OR
  4. OPTIONAL MATCH for Assertion **only if** followed by \
`WITH e, a WHERE a IS NOT NULL` (or `coalesce → WHERE raw IS NOT NULL AND raw = $v`).
- Filtered aggregates: first constrain entities with a required filter, then \
aggregate the measure attribute. Never OPTIONAL-filter the status/phase predicate.
- If the question filters (for/in/where/with/status/quoted values, multi-constraint \
NL), Cypher MUST constrain those values — never emit an unfiltered sum/avg/count \
of a measure as a silent total. Template `literal_aggregate` alone is ONLY for \
unfiltered measure aggregates; when a dimension filter is required, use \
literal_values / literal_compare / related_entity_name_filter first (or free-form \
with a required filter) then aggregate. If you cannot tell which field a filter \
token binds to, prefer an honest constrained plan or fail closed over inventing \
a field or returning a silent unfiltered total.

- Correct datatype read pattern:
  `(a:Assertion {tenant_id:$tenant_id, kg:$kg, subject_id:e.id})-[:PREDICATE]->(p:Property)`
  with `a.literal_value` (or denorm `e[p.name]` / `e.price`).
- Correct object-rel read pattern:
  `(a:Assertion)-[:SUBJECT]->(from)-[:OBJECT]->(to)` + PREDICATE Property,
  or the dual-written typed rel `HAS_*` when present.

Prefer allowlisted semantic helper templates (set the JSON ``template`` field when \
the shape matches; params must match the template). Helpers include:
- entities_of_type / entities_of_type_count — type membership ONLY when the \
question has **no** property/value filter; pass expanded `$type_names` \
(include subclasses when the question means "type T and subtypes")
- literal_values — datatype property equality (`$type_names`, `$prop_key`, `$prop_value`) \
— use for "how many X with status/phase/label = Y", equality filters, filtered counts
- literal_compare — numeric inequality (`$prop_key`, `$op` in lt/le/gt/ge/eq, `$threshold`)
- related_entities — 1-hop object relationships (`$from_types`, `$to_types`, optional `$rel_attr`)
- related_entity_name_filter — subjects linked to a related entity by display name \
  (`$rel_attr`, `$target_name`) e.g. books with genre "Classic Fiction"
- assertions_for_subject — all (or filtered) Assertions for one Entity
- subclass_of_closure — Class/OntoType descendant names for a root type
Only fall back to free-form scoped Cypher when no helper fits. Never open-scan all \
Assertions without a subject or type constraint as the default plan.
For numeric free-form compares, use `toFloat(...)` on the value; if a legacy string \
still contains `^^`, split off the suffix first: `split(toString(raw),'^^')[0]`.

Isolation (HARD — never invent scope values):
- Every instance node/rel/Assertion is multi-tenant. Scope ALWAYS via parameters:
  `$tenant_id` and `$kg`.
- On every MATCH of instance data, put those as map properties, e.g.:
  `MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})`
- NEVER hardcode a tenant id, kg name, or database name as a string literal.
- NEVER invent or guess `$tenant_id` / `$kg` values — the session injects them.

Key rules:
- **BUILD, do not guess.** Use Graph build notes (live entity counts) and \
populated schema leaves first. Prefer types with count > 0 that match the \
question; never default to empty pollution types (e.g. Product/Book shells from \
other KGs) when a populated type fits.
- If Graph build notes list **dim values**, equality-filter with those exact \
stored strings (do not invent enum values). If **money / measure leaf candidates** \
are listed for cost/price/tuition cues, use that prop_key (e.g. assay_cost / \
unit_cost / list_price) — never invent a bare price/cost leaf that is not \
declared. Multi-constraint questions MUST constrain **all** listed dims before \
SUM/COUNT/AVG.
- Parameterize user filters: string/number needles as `$param`, not concatenated.
- Prefer `count(*)` with an alias: `RETURN count(*) AS n`.
- Return human-readable fields (`name`, literal values) not only internal ids \
when the question asks for entities.
- Type filters: prefer `$type_names` lists (subclass-expanded) over a single \
hardcoded leaf when hierarchy is relevant — but only types that hold data in \
THIS kg unless the question forces otherwise.
- "[no instances]": a type, attribute, or relationship marked empty in the schema \
is still valid — generate a correct scoped query; zero rows is an honest answer. \
Prefer UNMARKED (populated) attributes and relationships for planning when the \
question does not require a specific declared-empty leaf. Instance-populated \
leaves are listed first in each type block; declared-but-empty leaves trail them.
- Only use type names and attribute keys that appear in the ontology schema or \
Graph build notes.
- Filtered aggregates: constrain entities first, then SUM/AVG/COUNT the measure.
- Success is correct *answers*, not SPARQL look-alikes.

If similar working examples are provided, follow their helper / Cypher SHAPE closely. \
Adapt type / property names from the current ontology, not from foreign examples.

Respond with JSON:
{
  "cypher": "the Cypher query (or helper-shaped Cypher)",
  "template": "optional allowlisted helper name e.g. entities_of_type_count",
  "params": {"type_names": ["optional"], "prop_key": "optional", "...": "..."},
  "explanation": "brief explanation of what the query does",
  "functions_needed": ["list of function names if computation is needed, empty otherwise"]
}"""

CYPHER_GENERATION_SYSTEM = _CYPHER_GENERATION_SYSTEM


def build_cypher_generation_prompt(
    question: str,
    ontology_summary: str,
    *,
    tenant_id: str = "",
    kg_name: str = "",
    examples_text: str = "",
    error_feedback: str = "",
    grounding_text: str = "",
) -> str:
    """Build the user prompt for Cypher generation (Neo4j ADR 0013 path).

    Scope values are named only to orient the model; the generator must still
    emit ``$tenant_id`` / ``$kg`` parameters — the session overwrites any
    values. Never instruct the model to embed the real ids as Cypher literals.

    ``error_feedback`` is optional scrubbed store/generator error text for a
    single retry (mirrors SPARQL retry spirit).

    ``grounding_text`` is optional structured ontology-subgraph grounding from
    :func:`infona_client.nlp.ontology_subgraph_match.ground_ask_plan` — hints
    only; the model still produces the final Cypher (always-LLM product rule).
    """
    examples_section = f"\n{examples_text}\n" if examples_text else ""
    error_section = ""
    if error_feedback:
        error_section = (
            "\nPrevious Cypher attempt failed. Fix the query based on this "
            f"error (do not invent scope values; do not switch to SPARQL):\n"
            f"{error_feedback}\n"
        )
    grounding_section = ""
    if grounding_text and grounding_text.strip():
        grounding_section = f"\n{grounding_text.strip()}\n"
    scope_line = ""
    if tenant_id or kg_name:
        scope_line = (
            "\nSession scope (use ONLY as $tenant_id / $kg parameters — "
            "do not hardcode these strings):\n"
            f"  $tenant_id  # workspace\n"
            f"  $kg         # knowledge graph name"
            + (f' ("{kg_name}")' if kg_name else "")
            + "\n"
        )
    kg_header = (
        f"Target knowledge graph: {kg_name}\n"
        "The schema below is the TENANT's ontology, shared across every knowledge "
        "graph this tenant owns. Entries marked [no instances] are declared but "
        f'hold no data in "{kg_name}".\n\n'
        if kg_name
        else ""
    )
    return f"""{kg_header}Ontology schema:
{ontology_summary}{scope_line}
{examples_section}{grounding_section}{error_section}
User question: {question}

BUILD a read-only Cypher answer from the Graph build notes + ontology above \
(do not invent empty types). Prefer semantic helpers when possible \
(entities_of_type, literal_values, related_entities, …). Do not translate SPARQL. \
Scope every MATCH with {{tenant_id: $tenant_id, kg: $kg}}."""