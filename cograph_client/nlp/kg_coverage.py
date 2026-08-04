"""Say so when the KG the user NAMED contributed nothing to the answer (ONTA-454).

The defect
----------
A generated query's dataset is a UNION, not one graph. ``/ask`` builds it from the
KG the caller named plus the tenant BASE graph plus the shared Global layers::

    FROM <.../graphs/demo-tenant/kg/maral>
    FROM <.../graphs/demo-tenant>
    FROM <.../graphs/global/public>

Per SPARQL 1.1 the default graph is the union of all three, so a question asked
"about maral" is answered from whichever of them happens to hold matching data.
Reproduced on production: ``kg_name="maral"`` (registered, 96 triples, 8 subjects,
all of ONE type) asked "how many product recalls are there?" and answered
**4229** — every row of it from the tenant base graph, none from ``maral``. The
number is confidently wrong and nothing in the response says so.

Why the existing guards do not cover it
---------------------------------------
* ONTA-453 (``graph/kg_status.py``) catches the KG that does NOT EXIST. This one
  exists and is registered, so the probe passes cleanly and the union still
  answers from elsewhere. This is also the LIKELIER real-user case: the name came
  from the Explorer dropdown, not from a typo.
* ONTA-450 / ONTA-258 (``nlp/empty_type_guard.py``) both key on ZERO ROWS. Here
  the query returned a row. That is the whole problem.

Why the obvious fix is wrong
----------------------------
Dropping the tenant base graph from the ``FROM`` list is not available. That graph
IS the instance graph for every ``kg_name``-less ingest (18,515 typed instance
subjects on demo-tenant, measured 2026-08-03), and it also carries the 28
``rdfs:subClassOf`` edges the generated ``rdf:type/rdfs:subClassOf*`` closure
walks. Narrowing the dataset would zero out those workspaces and break subclass
closure for every tenant-declared type. The plausibility is STRUCTURAL, so the
answer cannot be a narrower dataset and must not be a refusal either.

What this module does instead
-----------------------------
A per-query **coverage caveat**. The answer is still returned; a sentence beside
it says how the named graph relates to the number, so the reader is never left to
assume the graph they picked is what answered.

Two signals, because two different query shapes produce the same hidden failure:

**A. The type-ANCHORED query.** It constrains ``rdf:type`` to types the named
graph holds none of, so the rows came from elsewhere in the union. This is the
reported bug. Free on the common path (see below).

**B. The type-UNANCHORED query.** It constrains no type at all, so it reads the
whole union and the number is a workspace number rather than a per-graph one.
Measured on production the same day: "how many rows of data are there in total?"
against the same 8-subject ``maral`` generated
``SELECT (COUNT(DISTINCT ?s)) ... WHERE { ?s rdf:type ?type }`` and answered
**19582**. Signal A is structurally blind to this: there is no type to compare.
The two say deliberately different things — A is about PROVENANCE ("not an answer
about this graph"), B is about SCOPE ("this counts the workspace, not just your
graph") — because for shape B the named graph usually did contribute, just
negligibly, and claiming otherwise would be the same defect one level up.

**Known remaining shape, NOT covered here, and why (ONTA-455).** A query anchored
on a type that BOTH the named graph AND the base graph hold returns a count
summed across them, and neither signal fires: the type IS present here, and the
query IS anchored. Live and measured read-only on demo-tenant 2026-08-03:

* "how many organizations are there?" with ``kg_name="clinical-trials"`` answers
  **6854**, while the KG's own graph holds 1,839 ``Organization`` subjects and
  the base graph holds 3,184 (closure).
* 4 of the 11 registered KGs share at least one POPULATED type with the base
  graph, so this is common, not exotic.

The mechanism that would close it is a small extension of
:func:`kg_subtype_presence_query` — add ``FROM NAMED <base graph>`` and one block
per (type, graph) pair, and a type present in BOTH earns a "this count spans the
two" caveat. It is correct (verified against production: the probe reports
``Organization`` in both graphs and ``ProductRecall`` in the base graph only).
What stops it landing here is COST, measured rather than assumed: that probe must
run on EVERY typed question, not just the rare caveat candidate, and end-to-end
from outside the VPC it took 895 ms for one type and 1623 ms for six against a
345 to 506 ms round-trip floor. Roughly half a second added to every typed /ask
is a hot-path regression on the exact path ONTA-427 was an entire PR about making
cheaper, so it is a deliberate decision to take, not one to smuggle into this
change. The same measurement is why the signal-A probe below stays on the rare
candidate path.

The signal is ALREADY ON THE HOT PATH and costs nothing extra
--------------------------------------------------------------
``[no instances]`` (ONTA-258) is computed PER INSTANCE GRAPH: ``_fetch_ontology``
resolves ``active_types`` against the KG's own graph and marks every declared type
absent from it, and the semantic retriever marks its chunks the same way
(``ontology_embeddings._mark_no_instances``, ONTA-411). So the ontology summary
the planner was handed already knows ``ProductRecall`` has no instances in
``maral``; nothing downstream ever compared that against the query that ran.
:func:`empty_types_for_kg` + :func:`referenced_types` are pure string analysis over
two values ``ask()`` already holds. No second execution of the answer query, and
no extra round-trip on the common path.

The one round-trip, and why it is not optional
----------------------------------------------
The ``[no instances]`` probe matches DIRECT ``rdf:type`` only, while generated
queries walk ``rdf:type/rdfs:subClassOf*``. On demo-tenant ``Facility`` and
``University`` are subclasses of ``Organization``, so a KG holding only
``Facility`` rows would be marked "no ``Organization`` instances" while the
closure query legitimately answers from that very KG. Emitting the caveat there
would be a confidently wrong caveat on a correct answer — the same defect class,
pointed the other way. :func:`kg_subtype_presence_query` settles it with ONE
LIMIT-1-per-type probe, and it runs ONLY when a caveat is otherwise about to be
emitted (rare: the semantic subset is already scoped to active types, so the
planner rarely targets an empty one). It can only SUPPRESS a caveat, never create
one, so a probe failure degrades to the direct-type verdict the planner was
already shown rather than to silence.

Deliberately NOT a refusal, and deliberately NOT fired on zero rows
-------------------------------------------------------------------
* A ``kg_name``-less workspace legitimately reads the base graph. There
  ``instance_graph == ontology_graph``, ``active_types`` is ``None``, no type is
  marked, and this whole path is structurally unreachable.
* Zero rows stays entirely with ONTA-450 / ONTA-258. This fires only on a
  NON-EMPTY result set, so the two notes are mutually exclusive by construction
  and neither can dilute the other.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from cograph_client.graph.iri import IRI_BASE
from cograph_client.nlp.empty_type_guard import empty_declared_types

RDF_TYPE_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_SUBCLASS_OF_URI = "http://www.w3.org/2000/01/rdf-schema#subClassOf"

#: How many uncovered types one caveat will probe and speak about. A query that
#: targets more than this is already so far outside the named graph that naming
#: the first few makes the point; the cap keeps the confirmation probe bounded.
MAX_UNCOVERED_TYPES = 6

#: How many type names the sentence itself lists before summarising the rest.
MAX_NAMED_IN_CAVEAT = 3

# Same shape as `empty_type_guard._TYPE_URI_RE` (bare type, attribute, or layered
# form). Kept separate because this one needs the matched URI ITSELF, not just the
# name: the confirmation probe has to seek on the exact URIs the query used.
_TYPE_URI_RE = re.compile(rf"<({re.escape(IRI_BASE)}/types/[^>\s]+)>")


def _bare_type_uri(uri: str) -> str:
    """``types/Person/attrs/name`` -> ``types/Person``; anything else unchanged.

    Layered forms (``types/public/Person/attrs/name``) reduce the same way, since
    the split is on the ``/attrs/`` segment rather than on a path depth.
    """
    marker = "/attrs/"
    index = uri.find(marker)
    return uri[:index] if index != -1 else uri


def referenced_types(sparql: str) -> dict[str, list[str]]:
    """``{type_name: [type URIs the query spells]}`` for an executed query.

    Distinct from :func:`~cograph_client.nlp.empty_type_guard.types_referenced`,
    which returns names only. The URIs are kept because the confirmation probe
    must seek on exactly what the query named — re-minting a URI from the name
    would guess a layer namespace the query may not have used.
    """
    from cograph_client.graph.layers import type_name_from_uri

    out: dict[str, list[str]] = {}
    for match in _TYPE_URI_RE.finditer(sparql or ""):
        bare = _bare_type_uri(match.group(1))
        name = type_name_from_uri(bare)
        if not name:
            continue
        uris = out.setdefault(name, [])
        if bare not in uris:
            uris.append(bare)
    return out


def empty_types_for_kg(
    ontology: str,
    *,
    declared_names: Iterable[str] | None = None,
    active_types: Iterable[str] | None = None,
) -> set[str]:
    """Declared type names with NO instances in the KG this question targets.

    Two sources, unioned, because the two ontology paths surface the same fact
    differently and neither alone is complete:

    * ``ontology`` — the summary the planner actually saw. On the FULL path every
      declared type appears, so its ``[no instances]`` marks are exhaustive. On
      the SEMANTIC path only the retrieved top-K chunks appear, so the marks
      cover the subset.
    * ``declared_names`` minus ``active_types`` — the semantic path's own
      per-KG probe (ONTA-411), which covers every declared type including the
      ones retrieval left out of the subset.

    ``active_types is None`` means "nothing to scope by" (no KG graph, or the
    probe failed) and contributes nothing, so this degrades to the marks alone
    and, when there are none, to an empty set — i.e. to silence, never to a
    guess.
    """
    empty = empty_declared_types(ontology)
    if active_types is not None and declared_names is not None:
        active = set(active_types)
        empty |= {name for name in declared_names if name and name not in active}
    return empty


def uncovered_types(
    sparql_or_referenced: str | dict[str, list[str]], empty_in_kg: set[str]
) -> tuple[dict[str, list[str]], bool]:
    """``({name: uris}, every_referenced_type_is_uncovered)``.

    The flag distinguishes "this answer has nothing to do with the named graph"
    from "one leg of this answer does not", which are different sentences.

    Accepts either the raw query or an already-computed
    :func:`referenced_types` map, so a caller that has to test "did this query
    name any type at all?" first does not pay for a second scan.
    """
    referenced = (
        sparql_or_referenced
        if isinstance(sparql_or_referenced, dict)
        else referenced_types(sparql_or_referenced)
    )
    flagged = {
        name: uris for name, uris in referenced.items() if name in empty_in_kg
    }
    return flagged, bool(referenced) and len(flagged) == len(referenced)


def kg_subtype_presence_query(
    kg_graph: str, ontology_graphs: Sequence[str], type_uris: Sequence[str]
) -> str:
    """Which of ``type_uris`` have an instance IN ``kg_graph``, subclasses included.

    Scoped by construction: every graph it names is one the route already resolved
    for this request, so there is no caller-supplied IRI to confine. Registered as
    such in ``tests/test_generated_sparql_scoping.py``.

    Shape, and why it is this shape:

    * ``FROM`` the ontology graphs and ``FROM NAMED`` the KG graph. The subclass
      edges live in the tenant base / Global layers while the INSTANCES must come
      from the KG alone, and a plain union dataset could not tell the two apart —
      it would find the base graph's own instances and suppress every caveat,
      which is the bug this module exists to report.
    * The closure factor is written FIRST, with the searched type BOUND, so the
      engine enumerates the (tiny, 28-edge on demo-tenant) subclass set and then
      does a bound ``rdf:type`` seek per candidate, instead of scanning the KG's
      whole type index with ``?sub`` unbound.
    * One ``LIMIT 1`` subselect per type, UNIONed — the same first-match-seek
      shape ``_active_type_probe_query`` uses, so cost is O(types asked about),
      not O(entities).
    """
    froms = " ".join(f"FROM <{g}>" for g in ontology_graphs)
    blocks = " UNION ".join(
        f"{{ SELECT (<{u}> AS ?type) WHERE {{ "
        f"?sub <{RDFS_SUBCLASS_OF_URI}>* <{u}> . "
        f"GRAPH <{kg_graph}> {{ ?s <{RDF_TYPE_URI}> ?sub }} "
        f"}} LIMIT 1 }}"
        for u in type_uris
    )
    return (
        f"SELECT DISTINCT ?type {froms} FROM NAMED <{kg_graph}> "
        f"WHERE {{ {blocks} }}"
    )


def _name_list(names: Sequence[str]) -> str:
    shown = list(names[:MAX_NAMED_IN_CAVEAT])
    rest = len(names) - len(shown)
    joined = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    if rest > 0:
        joined += f" (and {rest} other type{'s' if rest > 1 else ''})"
    return joined


def coverage_caveat(kg_name: str, uncovered: Sequence[str], *, all_types: bool) -> str:
    """The one sentence. Empty string when there is nothing to say.

    States only what was measured — "this graph holds no instances of these types"
    — and where the rows therefore came from. It does not claim the answer is
    wrong (it may be exactly what the user wanted from the workspace as a whole),
    and it does not withhold it.
    """
    names = sorted(uncovered)
    if not names or not kg_name:
        return ""
    subject = _name_list(names)
    plural = len(names) > 1
    if all_types:
        # Phrased as "not an answer ABOUT this graph" rather than "no row came
        # from it". The stronger claim would be a shade too strong: a pattern can
        # carry an unconstrained leg (a bare `?o rdfs:label ?l` join) whose triple
        # could still be resolved out of the named graph. What IS airtight, and
        # what the reader actually needs, is that every type the query anchors on
        # is absent here.
        return (
            f"Knowledge graph '{kg_name}' contains no instances of {subject}, "
            f"{'which are' if plural else 'which is'} the only "
            f"{'types' if plural else 'type'} this query reads. This result is "
            f"therefore not an answer about '{kg_name}'; it was computed from "
            "other data in this workspace (the workspace base graph and any "
            "shared layers)."
        )
    return (
        f"Knowledge graph '{kg_name}' contains no instances of {subject}, so any "
        f"part of this result that depends on {'them' if plural else 'it'} came "
        f"from other data in this workspace, not from '{kg_name}'."
    )


def unscoped_caveat(kg_name: str) -> str:
    """The caveat for a query that constrains NO type at all (ONTA-454 signal B).

    Measured on production 2026-08-03: "how many rows of data are there in
    total?" against ``kg_name="maral"`` (8 subjects) generated
    ``SELECT (COUNT(DISTINCT ?s)) ... WHERE { ?s rdf:type ?type }`` over the
    three-graph union and answered **19582**. The type-based signal above cannot
    speak here, because there is no type to speak about; the query is simply not
    restricted to anything the named graph holds.

    This says exactly that, and no more. It does NOT claim the named graph
    contributed nothing — for this shape it usually contributed a little, 8 rows
    of 19582 in the measured case — so the honest statement is about SCOPE, not
    provenance: the number is a workspace-wide number, not a per-graph one.
    """
    if not kg_name:
        return ""
    return (
        "This query was not restricted to any type, so it read the whole "
        f"workspace, not just knowledge graph '{kg_name}': the result counts "
        f"data from '{kg_name}' together with the workspace base graph and any "
        "shared layers. Ask about a specific type to scope it."
    )


__all__ = [
    "MAX_NAMED_IN_CAVEAT",
    "MAX_UNCOVERED_TYPES",
    "coverage_caveat",
    "empty_types_for_kg",
    "kg_subtype_presence_query",
    "referenced_types",
    "uncovered_types",
    "unscoped_caveat",
]
