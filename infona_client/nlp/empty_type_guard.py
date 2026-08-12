"""Tell an HONEST zero-row answer apart from a semantic-retrieval miss.

Background (ONTA-450). Two very different situations produce the same
observable signal, "the generated SPARQL was valid and returned zero rows":

1. **Retrieval miss.** Semantic retrieval handed the planner a REDUCED ontology
   subset, and the planner built its query out of types/predicates that do not
   exist in this knowledge graph at all (infona-oss #273's Oliver demo:
   ``ClinicalTrial.interventions`` / ``conditions``). Widening to the full
   ontology and regenerating is the right recovery.
2. **Honest empty.** The question named a type that IS declared in the ontology
   and simply has no instances in this graph. Zero rows is the CORRECT, final
   answer (ONTA-258): the type stays visible, marked ``[no instances]``, and the
   model says so plainly rather than quietly answering about a populated type.

**The zero-row count alone cannot separate them** — it is identical in both.
Escalating on that signal alone is what makes case 2 unsafe: it hands the model
the full ontology (every populated type in the tenant) at the exact moment it
has just been told its query produced nothing, which is an invitation to find a
type that DOES have data and answer about that instead.

This module supplies the discriminator that the row count cannot: compare the
executed query and the question against the FULL ontology's own
``[no instances]`` marks. When the query targets a type that

* the QUESTION named explicitly (the same matcher ONTA-258's force-include uses),
* is DECLARED in the full ontology, and
* is marked ``[no instances]`` there,

then zero rows is provably the honest answer and escalation can only produce a
substitution. Everything else — including a query aimed at an unnamed empty type
(the ONTA-411 foreign-KG case) or at populated types whose filters missed — stays
eligible for escalation, so #273's recovery is preserved for the case it was
built for.

Pure string/regex analysis, no I/O; the caller has already fetched both inputs.
"""

from __future__ import annotations

from infona_client.graph.iri import IRI_BASE
import re

NO_INSTANCES_MARK = "[no instances]"

# Type URIs a query can reference, in either the bare-type or attribute form:
#   <https://graph.infona.ai/types/Sprocket>
#   <https://graph.infona.ai/types/Sprocket/attrs/name>
#   <https://graph.infona.ai/types/public/Person>   (layered — ONTA-397)
_TYPE_URI_RE = re.compile(rf"<({re.escape(IRI_BASE)}/types/[^>\s]+)>")


def _type_headers(ontology: str):
    """Yield ``(type_name, trailing_text)`` for each ``Type:`` header line.

    Header shape (``_fetch_ontology`` / the semantic retriever's chunk text):
    ``Type: Sprocket — URI: <https://graph.infona.ai/types/Sprocket> [no instances]``.
    Splitting on the em dash mirrors the existing ``types_in_summary`` parsing in
    ``pipeline.py`` and tolerates a type name containing spaces. A header with no
    URI part still parses.
    """
    for line in (ontology or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("Type:"):
            continue
        body = stripped[len("Type:"):]
        name, sep, rest = body.partition("—")
        if not sep:
            name, rest = body.replace(NO_INSTANCES_MARK, ""), body
        yield name.strip(), rest


def declared_types(ontology: str) -> set[str]:
    """Every type name with a ``Type:`` header in an ontology summary."""
    return {name for name, _ in _type_headers(ontology) if name}


def empty_declared_types(ontology: str) -> set[str]:
    """Type names whose header carries the ``[no instances]`` mark.

    Only the TYPE-level mark counts. An attribute- or relationship-level
    ``[no instances]`` (ONTA-248) sits on a later line of the same block and
    describes one field of a POPULATED type, which is not an honest-empty target.
    """
    return {
        name for name, rest in _type_headers(ontology)
        if name and NO_INSTANCES_MARK in rest
    }


def types_referenced(query: str, params: dict | None = None) -> set[str]:
    """Type names a SPARQL *or* Cypher query references.

    SPARQL: bare-type / attribute IRIs in ``<…/types/…>`` form.
    Cypher: ``params['type_names']`` / ``params['primary_type']`` (templates
    and confined generators) plus string literals next to ``primary_type``.
    """
    from infona_client.graph.layers import type_name_from_uri

    out: set[str] = set()
    for m in _TYPE_URI_RE.finditer(query or ""):
        # `type_name_from_uri` already reduces `…/types/Sprocket/attrs/name` and
        # the layered `…/types/public/Person` forms to the bare type name.
        name = type_name_from_uri(m.group(1))
        if name:
            out.add(name)
    if params:
        for tn in params.get("type_names") or []:
            if isinstance(tn, str) and tn.strip():
                out.add(tn.strip())
        pt = params.get("primary_type")
        if isinstance(pt, str) and pt.strip():
            out.add(pt.strip())
    # Cypher free-form: primary_type = 'Place' / = "Place"
    for m in re.finditer(
        r"""primary_type\s*=\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]""",
        query or "",
    ):
        out.add(m.group(1))
    return out


def honest_empty_targets(
    question: str,
    sparql: str,
    full_ontology: str,
    params: dict | None = None,
) -> set[str]:
    """Named-in-the-question, declared, empty types the query correctly targeted.

    A non-empty result means the zero-row answer is the ONTA-258 honest answer and
    the caller must NOT widen the ontology and regenerate. An empty result means
    the zero rows are unexplained by declared-but-empty targets, so a
    retrieval-miss escalation is still on the table.
    """
    empty = empty_declared_types(full_ontology)
    if not empty:
        return set()
    referenced = types_referenced(sparql, params)
    if not referenced:
        return set()
    # Match the question against every DECLARED type (not just the referenced
    # ones) so the matcher behaves identically to ONTA-258's force-include.
    from infona_client.nlp.ontology_embeddings import _types_named_in_question

    named = _types_named_in_question(question, declared_types(full_ontology))
    return named & referenced & empty


def zero_row_escalation_feedback(full_ontology_has_marks: bool) -> str:
    """Retry feedback for a zero-row escalation that does NOT license substitution.

    The pre-ONTA-450 text asserted the previous query "may have used types or
    predicates that are not in this knowledge graph's schema" and asked for a
    regeneration. Against a wider ontology full of populated types, that reads as
    permission to answer about a different type — exactly the ONTA-258 harm. The
    replacement states the observation neutrally, names the ONE legitimate reason
    to change the target, and restates the ``[no instances]`` rule verbatim at the
    moment of maximum substitution pressure.
    """
    text = (
        "The previous query was VALID but returned ZERO rows. You are now being "
        "shown the FULL ontology schema for this graph; the subset you saw before "
        "may have been incomplete. Change the types or predicates you target ONLY "
        "if the previous query used a URI that does not appear in the schema below, "
        "or if the schema below reveals the join path the question actually needs "
        "(e.g. an intermediate type you could not see before)."
    )
    if full_ontology_has_marks:
        text += (
            " If the type the question asks about is marked "
            f"\"{NO_INSTANCES_MARK}\" in the schema below, zero rows is the CORRECT "
            "and FINAL answer: keep targeting that same type and state plainly that "
            "it is declared in the ontology but currently has no instances. Do NOT "
            "substitute a different, populated type, and do NOT claim the type does "
            "not exist."
        )
    return text
