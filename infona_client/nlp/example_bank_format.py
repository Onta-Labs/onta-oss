"""Prompt formatting + tenant-graph sanitizers for the example bank.

``sanitize_example_sparql`` MUST keep rewriting ``FROM`` graphs to the caller
tenant (ONTA-420). ``sanitize_example_cypher`` rewrites literal tenant/kg
values to ``$tenant_id`` / ``$kg`` parameters.
"""

from __future__ import annotations

import re

from infona_client.nlp.example_bank_models import Example

# Placeholder used when a caller formats examples without naming a target graph.
# Never reaches production /ask (the pipeline always passes the target graph);
# it exists so a bank example can be rendered standalone (docs, tests, tooling)
# without carrying whatever tenant happened to answer it first.
TARGET_GRAPH_PLACEHOLDER = "TARGET_GRAPH"

# Matches the graph IRI of a dataset clause: `FROM <...>` / `FROM NAMED <...>`.
# Group 1 keeps the keyword (and its original spacing/case) so only the IRI is
# swapped. The lookbehind is load-bearing: without it a variable that merely ENDS
# in "from" (`?from <p> ?o`, `?validFrom <p> ?o`) or a prefixed name (`ex:from
# <...>`) would have its OBJECT eaten as if it were a dataset clause, silently
# teaching the model a nonsense triple.
_FROM_GRAPH_RE = re.compile(r"(?<![\w?$:-])(FROM\s+(?:NAMED\s+)?)<[^>]*>", re.IGNORECASE)

# Backstop for any graph IRI the keyword rule cannot see: `GRAPH <...>`,
# `SERVICE <...>`, or a bare mention. The bank has none today (all 262 examples
# scope with FROM), but it is REGENERATED from LLM-written SPARQL by
# `populate_from_eval_reports`, so a future model emitting a GRAPH block would
# quietly reopen the leak. Keyed on the `/graphs/` path segment that
# `graph/queries.py` mints, so type/attribute/entity IRIs are never touched.
#
# The `scheme://` anchor and the `[^<>\s]` body are load-bearing, not tidiness:
# `<` is also the SPARQL less-than operator, so a laxer `<[^>]*/graphs/` starts
# matching at the `<` of `FILTER(?y < 2000)` and swallows everything up to the
# next `>` (the whole filter plus a following GRAPH clause). A SPARQL IRI cannot
# contain whitespace, `<`, or `>`, so excluding all three costs nothing and makes
# a comparison operator unmatchable.
_ANY_GRAPH_IRI_RE = re.compile(r"<[a-zA-Z][\w+.-]*://[^<>\s]*/graphs/[^<>\s]*>")


def sanitize_example_sparql(sparql: str, target_graph_uri: str = "") -> str:
    """Rewrite an example's dataset clause onto the CURRENT caller's graph.

    ONTA-420. The example bank is scoped per PROCESS, not per tenant: one JSONL
    file, and ``Example`` has a ``kg_name`` but no tenant. Every stored example
    was answered against whichever graph produced it (the shipped bank is 262
    examples, all ``demo-tenant``), and ``format_examples_for_prompt`` used to
    emit the SPARQL verbatim. So every self-hosted or third-party tenant's
    NL->SPARQL prompt carried our ``demo-tenant`` graph IRIs, defended only by a
    prose "adapt the URIs" instruction in the system prompt.

    The graph IRI is the ONLY tenant-identifying token in a stored example, so it
    is the only thing rewritten here. Type and attribute IRIs are left ALONE on
    purpose: they are the pattern the examples exist to teach (correct
    ``types/<T>/attrs/<a>`` and ``onto/<leaf>`` shapes, aggregation, joins), they
    name public open-data schemas rather than customer data, and abstracting them
    into placeholders would delete the pedagogical value while adding no privacy.

    Rewriting to the caller's real target graph (rather than leaving a
    placeholder in the prompt) also means the model never sees a token it could
    echo into generated SPARQL, and a cross-KG example can no longer point the
    model at a DIFFERENT KG than the one being asked about.

    Two rules, deliberately overlapping: the dataset clause (which catches a graph
    IRI of ANY shape, including a self-hoster's custom one) and any surviving
    ``/graphs/`` IRI (which catches a graph scoped some other way, e.g. a GRAPH
    block). Neither alone is sufficient.
    """
    replacement = target_graph_uri or TARGET_GRAPH_PLACEHOLDER
    out = _FROM_GRAPH_RE.sub(lambda m: f"{m.group(1)}<{replacement}>", sparql)
    return _ANY_GRAPH_IRI_RE.sub(lambda _m: f"<{replacement}>", out)


def sanitize_example_cypher(cypher: str) -> str:
    """Strip literal tenant/kg values from a few-shot Cypher example.

    Cypher isolation uses ``$tenant_id`` / ``$kg`` parameters (not SPARQL
    ``FROM``). A stored example must never teach the model to hardcode a
    workspace id. We rewrite common literal forms to the parameter tokens.
    """
    text = cypher or ""
    # Map-form literals: tenant_id: 'demo-tenant' → tenant_id: $tenant_id
    text = re.sub(
        r"\btenant_id\s*:\s*(?:'[^']*'|\"[^\"]*\"|\$\w+)",
        "tenant_id: $tenant_id",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bkg\s*:\s*(?:'[^']*'|\"[^\"]*\"|\$\w+)",
        "kg: $kg",
        text,
        flags=re.IGNORECASE,
    )
    return text


def format_examples_for_prompt(
    examples: list[Example],
    target_graph_uri: str = "",
    *,
    language: str = "sparql",
) -> str:
    """Format retrieved examples for injection into the generation prompt.

    Args:
        examples: Retrieved examples, in prompt order.
        target_graph_uri: The graph the CURRENT question runs against. Every
            SPARQL example's ``FROM`` clause is rewritten to it (see
            :func:`sanitize_example_sparql`). When empty, a ``<TARGET_GRAPH>``
            placeholder is emitted instead and the header tells the model to
            substitute it. Ignored for ``language="cypher"`` (params instead).
        language: ``"sparql"`` (default Neptune path) or ``"cypher"`` (Neo4j).
            Cypher mode only includes examples that have a non-empty ``cypher``
            field; SPARQL-only rows are skipped so the model is not shown the
            wrong language.

    Output format (SPARQL):
        Similar queries that worked. ...
          SPARQL: SELECT ... FROM <graph> WHERE { ... }

    Output format (Cypher):
        Similar Cypher queries that worked. ...
          Cypher: MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) ...
    """
    if not examples:
        return ""

    lang = (language or "sparql").strip().lower()
    if lang == "cypher":
        return _format_cypher_examples(examples)

    if target_graph_uri:
        from_note = "Their FROM clause has been rewritten to your target graph."
    else:
        from_note = (
            f"Their FROM clause shows <{TARGET_GRAPH_PLACEHOLDER}> as a placeholder: "
            "substitute the named graph URI given above."
        )

    # Hedged ("Some may come from OTHER graphs") on purpose. Production /ask
    # passes no exclude_questions, so the same-KG filter + penalty at
    # `retrieve()` stay OFF and a near-identical prior answer on the SAME KG is
    # both common and the best available signal. Its type/attribute URIs are
    # exactly right, so an unconditional "these belong to a DIFFERENT ontology"
    # would tell the model to distrust correct URIs.
    lines = [
        "Similar queries that worked. Some may come from OTHER graphs, so reuse "
        "their SHAPE and check every type/attribute URI against the ontology "
        "schema above instead of copying it.",
        from_note,
    ]

    for i, ex in enumerate(examples, 1):
        if not (ex.sparql or "").strip():
            continue
        tag_str = " + ".join(ex.pattern_tags) if ex.pattern_tags else "basic"
        # Compact the SPARQL — collapse excessive whitespace but keep it readable
        sparql_compact = " ".join(sanitize_example_sparql(ex.sparql, target_graph_uri).split())
        lines.append("")
        lines.append(f"Example {i} ({tag_str}):")
        lines.append(f"  Q: {ex.question}")
        lines.append(f"  SPARQL: {sparql_compact}")

    # If every example was Cypher-only, return empty rather than a header alone.
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


def _format_cypher_examples(examples: list[Example]) -> str:
    """Format examples that carry a ``cypher`` field for the Cypher prompt.

    SPARQL-only rows are skipped entirely. If nothing usable remains, returns
    ``""`` (prefer empty few-shot over injecting SPARQL into a Cypher prompt).
    """
    usable = [ex for ex in examples if (ex.cypher or "").strip()]
    if not usable:
        return ""
    lines = [
        "Similar Cypher queries that worked. Some may come from OTHER graphs, so "
        "reuse their SHAPE and check every type/property name against the ontology "
        "schema above instead of copying it.",
        "Always scope MATCH with {tenant_id: $tenant_id, kg: $kg}; never hardcode "
        "workspace ids. Session parameters are injected at execution time.",
        "These examples are Cypher only — never emit SPARQL.",
        # Every stored example is the EXPANDED body of an allowlisted helper, so
        # none of them shows a `template` line. Without this reminder the model
        # imitates the rendering and stops emitting `template` at all — which is
        # what turned relationship questions into "No results found." once the
        # bank first shipped with Cypher rows.
        "Each example is the expanded body of an allowlisted helper. You must "
        "STILL set the JSON `template` field (plus its params) whenever a helper "
        "matches — the body is shown to teach the shape, not to replace the "
        "template field.",
        # The examples that traverse a relationship all go through :Assertion.
        # Say why, so the shape is not read as an incidental stylistic choice.
        "Relationship traversals go through :Assertion (SUBJECT / PREDICATE / "
        "OBJECT), never a typed edge named after the ontology leaf.",
    ]
    header_len = len(lines)
    for i, ex in enumerate(usable, 1):
        tag_str = " + ".join(ex.pattern_tags) if ex.pattern_tags else "basic"
        cypher_compact = " ".join(sanitize_example_cypher(ex.cypher).split())
        # Defense in depth: never surface a SPARQL body under a Cypher label.
        if re.search(r"(?i)\bSELECT\b|\bFROM\s*<|\bPREFIX\b", cypher_compact):
            continue
        lines.append("")
        lines.append(f"Example {i} ({tag_str}):")
        lines.append(f"  Q: {ex.question}")
        lines.append(f"  Cypher: {cypher_compact}")
    # Header-only → empty (no usable Cypher bodies after scrub). Counted from
    # the header list itself so adding a header line cannot silently start
    # emitting a body-less block.
    if len(lines) <= header_len:
        return ""
    return "\n".join(lines)
