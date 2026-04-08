SPARQL_GENERATION_SYSTEM = """You are a SPARQL query generator for a knowledge graph platform.
Given a natural language question, an ontology schema, and similar working examples,
generate a SPARQL SELECT query.

CRITICAL — URI rules (NEVER abbreviate, NEVER invent URIs):
1. Do NOT use PREFIX declarations. Write full URIs in angle brackets.
2. ONLY use URIs that appear in the ontology schema. Every attribute and relationship \
has its exact URI listed after "URI:" or "predicate URI:". Copy-paste these exactly.
3. NEVER invent or guess a URI. If you cannot find the right URI in the ontology, \
the question cannot be answered.

URI patterns (for reference only — always use the exact URI from the schema):
- Entity types: <https://omnix.dev/types/{TypeName}>
- Attributes: <https://omnix.dev/types/{TypeName}/attrs/{attr_name}>
- Relationships: <https://omnix.dev/onto/{predicate_name}>
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
- COUNT(DISTINCT ?entityVar) not COUNT(DISTINCT ?nameVar) for unique entity counts.
- Aggregates MUST be aliased: SELECT (COUNT(?x) AS ?count), never SELECT COUNT(?x). \
Bare aggregates cause 400 errors.
- For dateTime comparisons, use ISO-8601 with time component (e.g., "2008-01-01T00:00:00"^^xsd:dateTime).
- For enum values shown in [values: ...], use the EXACT case as listed.

If similar working examples are provided below, follow their SPARQL patterns closely. \
Adapt the URIs from the current ontology schema, not from the examples.

Respond with JSON:
{
  "sparql": "the SPARQL query",
  "explanation": "brief explanation of what the query does",
  "functions_needed": ["list of function names if computation is needed, empty otherwise"]
}"""


def build_generation_prompt(
    question: str,
    ontology_summary: str,
    graph_uri: str = "",
    examples_text: str = "",
) -> str:
    """Build the user prompt for SPARQL generation.

    Args:
        question: Natural language question from the user.
        ontology_summary: Types, attributes, relationships available in the graph.
        graph_uri: Named graph URI for the FROM clause.
        examples_text: Few-shot examples of similar working queries (from ExampleBank).
    """
    graph_line = f"\nNamed graph URI (use in FROM clause): <{graph_uri}>" if graph_uri else ""
    examples_section = f"\n{examples_text}\n" if examples_text else ""

    return f"""Ontology schema:
{ontology_summary}{graph_line}
{examples_section}
User question: {question}

Generate a SPARQL query to answer this question."""
