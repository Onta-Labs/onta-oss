"""LLM judge / question-generation prompts for the eval harness.

Implementation sibling of :mod:`infona_client.eval`. Public names are
re-exported from that facade.
"""
from __future__ import annotations


ONTOLOGY_JUDGE_PROMPT = """\
You are an expert knowledge graph ontologist evaluating the quality of an \
automatically-generated ontology. You will receive the ontology schema and a \
sample of the source data that was ingested.

Score each dimension 0-10. Be strict. A 10 means perfect, production-ready. \
A 5 means "works but has clear structural problems." Below 5 means "needs \
significant rework."

Dimensions:

1. DECOMPOSITION (0-10)
   Are composite values broken into separate entities?
   Bad: Property.address = "123 Main St, Austin, TX" (flat string)
   Good: Property → located_in → City → located_in → State
   Score 10 if ALL composite values are properly decomposed.
   Score 5 if some are decomposed but others remain flat.
   Score 0 if everything is flat string attributes.

2. REUSABILITY (0-10)
   Are entities created that other datasets would naturally share?
   Cities, states, countries, people, organizations should be their own entities.
   Score 10 if all shareable concepts are entities.
   Score 5 if some are entities but others are buried as string attributes.

3. HIERARCHY (0-10)
   Are types connected via subClassOf relationships?
   Broker should be subClassOf Person. Condo subClassOf Property.
   Score 10 if all natural hierarchies exist.
   Score 5 if some exist but obvious ones are missing.
   Score 0 if all types are flat/orphaned.

4. PREDICATE_CONSISTENCY (0-10)
   Is there exactly one predicate per semantic relationship?
   Bad: both "located_in" and "is_located_in" exist.
   Score 10 if no duplicates. Score 5 if a few duplicates exist.

5. ENTITY_FIRST (0-10)
   Are real-world things modeled as entities, not string literals?
   Bad: Property.agent_name = "John Smith" (dead-end string)
   Good: Property → listed_by → Person("John Smith")
   Score 10 if all real-world references are entities.

6. TYPE_NAMING (0-10)
   Are type names PascalCase, singular, descriptive?
   Bad: "properties", "LISTING", "real_estate_listing"
   Good: "Property", "Listing", "RealEstateBroker"

For each dimension, provide:
- The score (integer 0-10)
- A one-sentence explanation
- Specific issues found (if score < 10)

Also list the top 3-5 "weak points" — the most impactful improvements that \
would make this ontology significantly better.

Respond with valid JSON only:
{
  "dimensions": [
    {"name": "decomposition", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "reusability", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "hierarchy", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "predicate_consistency", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "entity_first", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "type_naming", "score": N, "explanation": "...", "issues": ["..."]}
  ],
  "weak_points": ["...", "...", "..."]
}"""

QUESTION_GEN_PROMPT = """\
You are generating test questions for a knowledge graph query system. Given the \
ontology schema and a sample of the source data, generate exactly {num_questions} \
questions distributed across 4 difficulty tiers.

Distribution:
- Tier 1 (count/lookup): {t1} questions — basic COUNT, simple retrieval
- Tier 2 (filter): {t2} questions — WHERE clauses with comparisons
- Tier 3 (join): {t3} questions — relationship traversal across entities
- Tier 4 (multi-hop): {t4} questions — chained joins + aggregation

Rules:
- Each question must be answerable from the data that was ingested
- Include the expected answer derived from the DATASET STATISTICS provided \
  (these cover the FULL dataset, not just a sample)
- Tier 1 questions should have exact numeric answers
- Tier 2 questions should test numeric/string/date filters
- Tier 3 questions should require traversing at least one relationship
- Tier 4 questions should combine filtering + joins + aggregation
- Questions should feel natural, like a human analyst would ask them
- Vary the entity types and attributes tested across questions

Respond with valid JSON only:
[
  {{
    "tier": 1,
    "question": "How many properties are there?",
    "expected_answer": "1000",
    "reasoning": "COUNT of all Property entities"
  }},
  ...
]"""

QUERY_JUDGE_PROMPT = """\
You are evaluating whether a knowledge graph query system answered a question \
correctly. You will receive:
1. The question asked
2. The generated SPARQL query
3. The system's answer
4. The ontology schema (types, attributes, predicates available in the graph)
5. Dataset statistics computed from the FULL source data (use these for ground truth)
6. A sample of raw source data rows

IMPORTANT: The expected answer was computed deterministically from the full source \
CSV using pandas. Trust it as ground truth. Compare the system's answer against \
this expected answer. The dataset statistics and sample rows are provided for \
context only.

Evaluate the answer and respond with valid JSON:
{{
  "verdict": "correct" | "partial" | "wrong" | "error",
  "expected": "the correct answer based on dataset statistics",
  "explanation": "one sentence explaining your judgment",
  "failure_category": "none" | "bad_predicate_uri" | "missing_join" | "wrong_filter" | "wrong_aggregation" | "empty_result" | "uri_instead_of_value" | "other",
  "corrected_sparql": "if verdict is not correct, write the SPARQL that WOULD produce the correct answer using the ontology schema provided. Use exact predicate URIs from the ontology. If verdict is correct, leave empty."
}}

Failure categories:
- "bad_predicate_uri": SPARQL uses wrong predicate URIs (e.g., <price> instead of <https://graph.infona.ai/types/Property/attrs/price>)
- "missing_join": Query doesn't traverse a relationship that's needed
- "wrong_filter": Filter condition is malformed or uses wrong comparison
- "wrong_aggregation": COUNT/AVG/SUM is wrong or applied to wrong variable
- "empty_result": Query returns no results when data exists
- "uri_instead_of_value": Returns entity URIs instead of human-readable attribute values
- "none": Answer is correct
- "other": Doesn't fit the above categories

Scoring:
- "correct": Answer matches expected value (within 2% for counts, within 5% for averages/sums)
- "partial": Answer is in the right direction but imprecise or incomplete
- "wrong": Answer is factually incorrect
- "error": System returned an error, empty result, or nonsensical response

For counts, allow a tolerance of up to 2% (to account for minor data normalization \
differences between the CSV and the knowledge graph, such as case sensitivity or \
whitespace). For averages and sums, allow up to 5%. Be lenient on text answers \
(paraphrasing is fine if facts are correct)."""


GROUND_TRUTH_PROMPT = """\
You are generating a pandas expression to compute the exact answer to a question \
about a CSV dataset. The DataFrame is already loaded as `df` with these columns:

{columns}

Column dtypes (auto-detected):
{dtypes}

Rules:
- Return ONLY valid Python that evaluates to a single scalar value (int, float, or str)
- Use pandas operations on `df`
- For counts, return an int
- For averages/sums, return a float rounded to 2 decimal places
- For text answers, return a string
- Handle NaN/empty values (use .dropna() when needed)
- String comparisons should be case-insensitive where appropriate
- Numeric columns may have been loaded as strings — cast if needed
- When the question asks "which X has the most Y", return the name/label, not a ratio
- When asked for a count, always return len(...) or .count(), never .mean() or ratios
- Match column values exactly as shown in the sample data (case-sensitive unless question implies otherwise)

Example questions and expressions:
- "How many properties are there?" → len(df)
- "How many have 4+ bedrooms?" → len(df[df['bedrooms'] >= 4])
- "Average price of SINGLE_FAMILY in Austin?" → round(df[(df['home_type']=='SINGLE_FAMILY') & (df['city'].str.upper()=='AUSTIN')]['price'].mean(), 2)
- "How many listings by broker X?" → len(df[df['broker'].str.contains('X', case=False, na=False)])
- When using str.contains with values that have special regex chars like (), always add regex=False

Respond with ONLY the Python expression, nothing else. No markdown, no explanation."""
