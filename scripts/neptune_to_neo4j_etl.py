#!/usr/bin/env python3
"""One-shot Neptune → Neo4j offline ETL (migration tooling, not product surface).

Maps RDF-era triples to the **ADR 0013 Assertion model** (unit of truth), not
Entity-properties-only SoT. Instance triples become :class:`Fact` rows via the
shared B1–B5 sanitizers / ``classify_triple`` mapper, then
:func:`fact_to_assertion_fact` + the store path of :func:`insert_facts` (which
dual-writes :Assertion nodes, Class/Property catalog, and derived cache).

Ontology / catalog triples in the same dump (``rdfs:subClassOf``,
``rdfs:subPropertyOf``) become ``SUBCLASS_OF`` / ``SUBPROPERTY_OF`` edges on
Class / Property nodes with **original RDF IRIs kept as node ids**.

**Validation after cutover is golden answers (result sets), not SPARQL→Cypher
string translation.** See ``docs/plans/neo4j-golden-queries.md`` and ADR 0013.

Hermetic usage (no live DBs)::

    python scripts/neptune_to_neo4j_etl.py \\
        --fixture path/to/triples.nt --tenant demo-tenant --kg bookstore --dry-run

Live dry-run from Neptune::

    export NEPTUNE_ENDPOINT=https://…:8182   # or INFONA_NEPTUNE_ENDPOINT
    python scripts/neptune_to_neo4j_etl.py --tenant demo-tenant --kg bookstore --dry-run

Commit to Neo4j::

    export NEO4J_URI=bolt://… NEO4J_USER=neo4j NEO4J_PASSWORD=…
    python scripts/neptune_to_neo4j_etl.py --tenant demo-tenant --kg bookstore

See parent ``docs/runbooks/neo4j-cutover.md`` and
``docs/plans/neo4j-rdf-semantic-model.md`` §13 (ETL cheat sheet).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence
from urllib.parse import unquote

# Allow ``python scripts/neptune_to_neo4j_etl.py`` from a checkout without install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infona_client.graph.assertion_model import (  # noqa: E402
    AssertionFact,
    property_uri,
    type_membership_property_id,
)
from infona_client.graph.facts import (  # noqa: E402
    Fact,
    classify_triple,
    group_facts_by_subject,
    sanitize_prop_key,
    sanitize_rel_type,
    triples_to_facts,
)
from infona_client.graph.labels import sanitize_domain_label  # noqa: E402
from infona_client.graph.predicates import RDFS_NS  # noqa: E402
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri  # noqa: E402
from infona_client.graph.rdf_model import (  # noqa: E402
    class_iri,
    fact_to_assertion_fact,
)
from infona_client.graph.scope import GraphScopeError  # noqa: E402

# ---------------------------------------------------------------------------
# Catalog predicates (ontology triples → Class/Property hierarchy)
# ---------------------------------------------------------------------------

RDFS_SUBCLASS_OF = f"{RDFS_NS}subClassOf"
RDFS_SUBPROPERTY_OF = f"{RDFS_NS}subPropertyOf"

# N-Triples: <iri> <iri> <iri> .  |  <iri> <iri> "literal" .  |  typed/lang tags
_NT_LINE = re.compile(
    r"^\s*(<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+"
    r"(<[^>]+>|_:[^\s]+|\"(?:[^\"\\]|\\.)*\"(?:@[\w-]+|\^\^<[^>]+>)?)\s*\.\s*$"
)
_KG_GRAPH_ANY_HOST = re.compile(
    r"^https?://[^/]+/graphs/(?P<tenant>[^/]+)/kg/(?P<kg>[^/]+)/?$"
)
_ONTOLOGY_GRAPH_ANY_HOST = re.compile(
    r"^https?://[^/]+/graphs/(?P<tenant>[^/]+)/?$"
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogEdge:
    """One catalog hierarchy edge (original IRIs preserved as node ids)."""

    kind: Literal["subclass_of", "subproperty_of"]
    child_id: str
    parent_id: str


@dataclass(frozen=True, slots=True)
class EtlStats:
    """Aggregate counts printed in dry-run and write modes (ADR 0013).

    Instance SoT counts are **assertions** (not Entity-props-only). Catalog
    counts cover Class / Property nodes implied by type Assertions, property
    leaves, and explicit ``rdfs:subClassOf`` / ``rdfs:subPropertyOf`` edges.
    """

    triples_in: int = 0
    facts_out: int = 0  # bridge Fact rows (legacy key; == assertions when mapped)
    assertions: int = 0
    classes: int = 0
    properties: int = 0
    entities: int = 0
    subclass_of: int = 0
    subproperty_of: int = 0
    skipped: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    b3_literal_conflicts: int = 0
    subjects: int = 0
    graphs: int = 0
    written_facts: int = 0
    written_assertions: int = 0
    written_catalog_edges: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "triples_in": self.triples_in,
            "facts_out": self.facts_out,
            "assertions": self.assertions,
            "classes": self.classes,
            "properties": self.properties,
            "entities": self.entities,
            "subclass_of": self.subclass_of,
            "subproperty_of": self.subproperty_of,
            "skipped": self.skipped,
            "kind_counts": dict(self.kind_counts),
            "b3_literal_conflicts": self.b3_literal_conflicts,
            "subjects": self.subjects,
            "graphs": self.graphs,
            "written_facts": self.written_facts,
            "written_assertions": self.written_assertions,
            "written_catalog_edges": self.written_catalog_edges,
        }


def strip_nt_term(term: str) -> str:
    """Unwrap an N-Triples IRI or plain string literal (minimal)."""
    term = term.strip()
    if term.startswith("<") and term.endswith(">"):
        return term[1:-1]
    if term.startswith('"'):
        # Drop language tag / datatype, unescape common sequences.
        m = re.match(r'^"((?:[^"\\]|\\.)*)"', term)
        if not m:
            return term
        body = m.group(1)
        return (
            body.replace(r"\"", '"')
            .replace(r"\\", "\\")
            .replace(r"\n", "\n")
            .replace(r"\t", "\t")
            .replace(r"\r", "\r")
        )
    if term.startswith("_:"):
        return term
    return term


def parse_ntriples(text: str) -> list[tuple[str, str, str]]:
    """Parse a subset of N-Triples into ``(s, p, o)`` string triples.

    Blank nodes are kept as ``_:…`` strings (mapper typically skips them).
    Lines that do not match are ignored (comments / empty).
    """
    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _NT_LINE.match(line)
        if not m:
            continue
        s, p, o = m.group(1), m.group(2), m.group(3)
        out.append((strip_nt_term(s), strip_nt_term(p), strip_nt_term(o)))
    return out


def parse_json_triples(payload: Any) -> list[tuple[str, str, str]]:
    """Parse JSON triple dumps: list of objects, list of triples, or ``{triples: …}``."""
    if isinstance(payload, dict) and "triples" in payload:
        payload = payload["triples"]
    if not isinstance(payload, list):
        raise ValueError("JSON fixture must be a list or {\"triples\": [...]}")
    out: list[tuple[str, str, str]] = []
    for item in payload:
        if isinstance(item, dict):
            s = item.get("s") or item.get("subject")
            p = item.get("p") or item.get("predicate")
            o = item.get("o") or item.get("object")
            if s is None or p is None:
                continue
            out.append((str(s), str(p), "" if o is None else str(o)))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            out.append((str(item[0]), str(item[1]), str(item[2])))
        else:
            raise ValueError(f"Unrecognized triple row: {item!r}")
    return out


def load_triples_from_file(path: str | Path) -> list[tuple[str, str, str]]:
    """Load N-Triples (``.nt`` / ``.ttl`` text) or JSON triples from disk."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            rows: list[Any] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
            return parse_json_triples(rows)
        return parse_json_triples(json.loads(raw))
    # Default: N-Triples-ish (also works for simple .ttl dumps of triples).
    return parse_ntriples(raw)


def parse_instance_graph_uri(graph_uri: str) -> tuple[str, str] | None:
    """``…/graphs/{tenant}/kg/{kg}`` → ``(tenant, kg)`` for any host (legacy + live)."""
    if not isinstance(graph_uri, str):
        return None
    m = _KG_GRAPH_ANY_HOST.match(graph_uri.strip())
    if not m:
        return None
    return m.group("tenant"), m.group("kg")


def parse_ontology_graph_uri(graph_uri: str) -> str | None:
    """``…/graphs/{tenant}`` (no ``/kg/``) → tenant id, or None."""
    if not isinstance(graph_uri, str):
        return None
    raw = graph_uri.strip().rstrip("/")
    # Reject instance graphs.
    if parse_instance_graph_uri(raw) is not None:
        return None
    if "/kg/" in raw:
        return None
    m = _ONTOLOGY_GRAPH_ANY_HOST.match(raw + "/") or _ONTOLOGY_GRAPH_ANY_HOST.match(
        raw
    )
    if not m:
        # Try without trailing slash pattern
        m2 = re.match(r"^https?://[^/]+/graphs/(?P<tenant>[^/]+)$", raw)
        if not m2:
            return None
        return m2.group("tenant")
    return m.group("tenant")


def _is_http_iri(value: str) -> bool:
    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://")
    )


def classify_catalog_triple(
    s: str, p: str, o: str
) -> CatalogEdge | None:
    """Map ontology hierarchy triples; keep child/parent IRIs verbatim."""
    if not s or not p or not o:
        return None
    if not _is_http_iri(s) or not _is_http_iri(o):
        return None
    if p == RDFS_SUBCLASS_OF:
        return CatalogEdge(kind="subclass_of", child_id=s, parent_id=o)
    if p == RDFS_SUBPROPERTY_OF:
        return CatalogEdge(kind="subproperty_of", child_id=s, parent_id=o)
    return None


def count_b3_literal_conflicts(facts: Sequence[Fact]) -> int:
    """Count entity-scoped literal leaf collisions (model B3 last-write-wins).

    When the same ``(subject_id, key)`` appears more than once among
    ``kind=literal`` facts with differing non-null values, each extra write
    after the first counts as one collapsed conflict.
    """
    seen: dict[tuple[str, str], Any] = {}
    conflicts = 0
    for f in facts:
        if f.kind != "literal":
            continue
        k = (f.subject_id, f.key)
        if k not in seen:
            seen[k] = f.value
            continue
        prev = seen[k]
        if f.value is not None and prev is not None and f.value != prev:
            conflicts += 1
        if f.value is not None:
            seen[k] = f.value
    return conflicts


def fact_to_assertion_or_none(fact: Fact) -> AssertionFact | None:
    """Bridge Fact → AssertionFact; skip rows that cannot become Assertions."""
    try:
        return fact_to_assertion_fact(
            subject_id=fact.subject_id,
            kind=fact.kind,
            key=fact.key,
            value=fact.value,
            source=fact.source,
        )
    except GraphScopeError:
        return None


def class_id_from_type_fact(fact: Fact) -> str:
    """Resolve Class node id from a type Fact (prefer original RDF Class IRI)."""
    if isinstance(fact.value, str) and fact.value.startswith("http"):
        return fact.value
    return class_iri(fact.key)


def map_triples(
    triples: Iterable[tuple[str, str, str]],
) -> tuple[list[Fact], list[AssertionFact], list[CatalogEdge], EtlStats]:
    """Map RDF triples → Facts + AssertionFacts + catalog edges (pure; no I/O).

    * Instance: ``classify_triple`` → Fact → AssertionFact (ADR 0013 SoT).
    * Catalog: ``rdfs:subClassOf`` / ``rdfs:subPropertyOf`` → CatalogEdge.
    * Entity / Class / Property **ids stay the RDF IRIs** from the dump
      (no reminting to opaque Neo4j keys).
    """
    triple_list = list(triples)
    facts: list[Fact] = []
    catalog: list[CatalogEdge] = []
    skipped = 0
    for t in triple_list:
        if not t or len(t) < 3:
            skipped += 1
            continue
        s, p, o = t[0], t[1], t[2]
        edge = classify_catalog_triple(s, p, o if o is not None else "")
        if edge is not None:
            catalog.append(edge)
            continue
        fact = classify_triple(s, p, o if o is not None else "")
        if fact is None:
            skipped += 1
        else:
            facts.append(fact)

    assertion_facts: list[AssertionFact] = []
    for f in facts:
        af = fact_to_assertion_or_none(f)
        if af is not None:
            assertion_facts.append(af)

    # --- Dry-run identity sets (ADR 0013 counts) ---------------------------
    entity_ids: set[str] = set()
    class_ids: set[str] = set()
    property_ids: set[str] = set()
    # Always include the well-known type-membership Property when any type
    # Assertion exists (assert_fact MERGEs it on write).
    for f in facts:
        entity_ids.add(f.subject_id)
        if f.kind == "type":
            class_ids.add(class_id_from_type_fact(f))
            property_ids.add(type_membership_property_id())
        elif f.kind == "rel":
            property_ids.add(property_uri(f.key))
            if isinstance(f.value, str) and f.value:
                entity_ids.add(f.value)
        elif f.kind == "literal":
            property_ids.add(property_uri(f.key))

    subclass_n = 0
    subprop_n = 0
    for edge in catalog:
        if edge.kind == "subclass_of":
            subclass_n += 1
            class_ids.add(edge.child_id)
            class_ids.add(edge.parent_id)
        else:
            subprop_n += 1
            property_ids.add(edge.child_id)
            property_ids.add(edge.parent_id)

    # Assertion kind_counts use AssertionFact.kind (literal|object|type).
    kinds = Counter(af.kind for af in assertion_facts)
    grouped = group_facts_by_subject(facts)
    stats = EtlStats(
        triples_in=len(triple_list),
        facts_out=len(facts),
        assertions=len(assertion_facts),
        classes=len(class_ids),
        properties=len(property_ids),
        entities=len(entity_ids),
        subclass_of=subclass_n,
        subproperty_of=subprop_n,
        skipped=skipped,
        kind_counts=dict(kinds),
        b3_literal_conflicts=count_b3_literal_conflicts(facts),
        subjects=len(grouped),
        graphs=0,
        written_facts=0,
        written_assertions=0,
        written_catalog_edges=0,
    )
    return facts, assertion_facts, catalog, stats


def sparql_bindings_to_triples(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Convert SPARQL SELECT JSON (``?s ?p ?o``) into string triples."""
    bindings = (result.get("results") or {}).get("bindings") or []
    out: list[tuple[str, str, str]] = []
    for row in bindings:
        if not isinstance(row, dict):
            continue
        s_b, p_b, o_b = row.get("s"), row.get("p"), row.get("o")
        if not s_b or not p_b:
            continue
        s = str((s_b or {}).get("value") or "")
        p = str((p_b or {}).get("value") or "")
        o = "" if not o_b else str(o_b.get("value") or "")
        if s and p:
            out.append((s, p, o))
    return out


def neptune_endpoint_from_env() -> str | None:
    """Prefer ``NEPTUNE_ENDPOINT``, fall back to ``INFONA_NEPTUNE_ENDPOINT``."""
    for key in ("NEPTUNE_ENDPOINT", "INFONA_NEPTUNE_ENDPOINT"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return None


def neo4j_env_ready() -> bool:
    return bool(
        (os.environ.get("NEO4J_URI") or "").strip()
        and (os.environ.get("NEO4J_PASSWORD") or "") != ""
    )


# ---------------------------------------------------------------------------
# I/O: Neptune read + Neo4j write
# ---------------------------------------------------------------------------


async def fetch_triples_from_neptune(
    endpoint: str,
    *,
    graph_uri: str | None = None,
    backend: str = "neptune",
    limit: int | None = None,
) -> list[tuple[str, str, str]]:
    """SELECT ?s ?p ?o from one named graph or the default union."""
    from infona_client.graph.client import NeptuneClient

    client = NeptuneClient(endpoint, backend=backend)
    try:
        if graph_uri:
            # GRAPH keyword — avoid interpolating untrusted IRIs with ``>``.
            if ">" in graph_uri or "<" in graph_uri:
                raise ValueError(f"Refusing graph_uri with angle brackets: {graph_uri!r}")
            sparql = (
                "SELECT ?s ?p ?o WHERE {\n"
                f"  GRAPH <{graph_uri}> {{ ?s ?p ?o }}\n"
                "}\n"
            )
        else:
            sparql = "SELECT ?s ?p ?o WHERE { ?s ?p ?o }\n"
        if limit is not None and limit > 0:
            sparql = sparql.rstrip() + f"\nLIMIT {int(limit)}\n"
        result = await client.query(sparql)
        return sparql_bindings_to_triples(result)
    finally:
        await client._client.aclose()


async def list_instance_graphs_from_neptune(
    endpoint: str,
    *,
    backend: str = "neptune",
    tenant: str | None = None,
    kg: str | None = None,
) -> list[str]:
    """Discover ``…/graphs/{tenant}/kg/{kg}`` named graphs on the endpoint."""
    from infona_client.graph.client import NeptuneClient

    client = NeptuneClient(endpoint, backend=backend)
    try:
        sparql = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }\n"
        result = await client.query(sparql)
        bindings = (result.get("results") or {}).get("bindings") or []
        graphs: list[str] = []
        for row in bindings:
            g = ((row.get("g") or {}).get("value") or "").strip()
            if not g:
                continue
            parsed = parse_instance_graph_uri(g)
            if parsed is None:
                continue
            t, k = parsed
            if tenant and t != tenant:
                continue
            if kg and k != kg:
                continue
            graphs.append(g)
        return sorted(set(graphs))
    finally:
        await client._client.aclose()


async def write_facts_to_store(
    facts: Sequence[Fact],
    *,
    tenant_id: str,
    kg: str,
    batch_size: int = 500,
) -> int:
    """Write Facts through ``insert_facts`` store path (dual-writes Assertions).

    ``pg_ops.apply_facts`` MERGEs Entity cache + calls ``assert_fact`` so
    :Assertion / Class / Property / INSTANCE_OF land on the ADR 0013 model.
    """
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.store import get_graph_store

    store = get_graph_store()
    await store.bootstrap_schema()
    graph_uri = kg_graph_uri(tenant_id, kg)
    written = 0
    batch: list[Fact] = []
    for f in facts:
        batch.append(f)
        if len(batch) >= batch_size:
            await insert_facts(None, graph_uri, facts=batch, store=store)
            written += len(batch)
            batch = []
    if batch:
        await insert_facts(None, graph_uri, facts=batch, store=store)
        written += len(batch)
    return written


async def write_catalog_to_store(
    catalog: Sequence[CatalogEdge],
    *,
    tenant_id: str,
    kg: str,
) -> int:
    """Write SUBCLASS_OF / SUBPROPERTY_OF via rdf_model helpers (scoped session)."""
    if not catalog:
        return 0
    from infona_client.graph.rdf_model import (
        merge_property_node,
        set_subclass_of,
        set_subproperty_of,
    )
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import get_graph_store

    store = get_graph_store()
    await store.bootstrap_schema()
    # Catalog hierarchy for a tenant KG lives on the same instance scope as
    # type Assertions in Wave 1 (session injects tenant_id+kg). Class IRIs are
    # the RDF subjects/objects from the dump.
    scope = GraphScope.for_instance(tenant_id, kg)
    session = store.session(scope)
    written = 0
    for edge in catalog:
        if edge.kind == "subclass_of":
            await set_subclass_of(session, edge.child_id, edge.parent_id)
            written += 1
        else:
            await merge_property_node(session, edge.child_id, kind="datatype")
            await merge_property_node(session, edge.parent_id, kind="datatype")
            await set_subproperty_of(session, edge.child_id, edge.parent_id)
            written += 1
    return written


def print_stats(label: str, stats: EtlStats) -> None:
    print(f"=== ETL {label} ===")
    for key, val in stats.as_dict().items():
        print(f"  {key}: {val}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Neptune → Neo4j ETL (ADR 0013 Assertion model; dual-write via "
            "insert_facts store path). Validate cutover with golden answers, "
            "not SPARQL→Cypher translation."
        )
    )
    p.add_argument(
        "--fixture",
        type=str,
        default=None,
        help="N-Triples (.nt) or JSON triples file — hermetic path, no live DB",
    )
    p.add_argument(
        "--tenant",
        type=str,
        default=None,
        help="Tenant id for scope / graph filter (required with --fixture for write)",
    )
    p.add_argument(
        "--kg",
        type=str,
        default=None,
        help="KG name for scope / graph filter (required with --fixture for write)",
    )
    p.add_argument(
        "--graph",
        type=str,
        default=None,
        help="Explicit instance graph IRI (overrides --tenant/--kg for Neptune read)",
    )
    p.add_argument(
        "--include-ontology",
        action="store_true",
        help=(
            "Also pull the tenant ontology graph (…/graphs/{tenant}) for "
            "rdfs:subClassOf / rdfs:subPropertyOf when reading from Neptune"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Map and print counts only; never open Neo4j write path",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional SPARQL LIMIT when reading from Neptune",
    )
    p.add_argument(
        "--backend",
        type=str,
        default="neptune",
        choices=("neptune", "fuseki"),
        help="SPARQL client backend path layout (default neptune)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Fact batch size for insert_facts when writing (default 500)",
    )
    p.add_argument(
        "--json-stats",
        action="store_true",
        help="Emit final stats as a single JSON object on stdout (after human lines)",
    )
    return p


async def async_main(args: argparse.Namespace) -> int:
    # Each job: (tenant, kg, instance_and_optional_ontology triples)
    jobs: list[tuple[str | None, str | None, list[tuple[str, str, str]]]] = []

    if args.fixture:
        triples = load_triples_from_file(args.fixture)
        tenant = args.tenant
        kg = args.kg
        if not args.dry_run and (not tenant or not kg):
            print(
                "ERROR: --tenant and --kg are required when writing from --fixture",
                file=sys.stderr,
            )
            return 2
        jobs.append((tenant, kg, triples))
    else:
        endpoint = neptune_endpoint_from_env()
        if not endpoint:
            print(
                "ERROR: provide --fixture or set NEPTUNE_ENDPOINT "
                "(or INFONA_NEPTUNE_ENDPOINT)",
                file=sys.stderr,
            )
            return 2
        if args.graph:
            g = args.graph
            parsed = parse_instance_graph_uri(g)
            triples = await fetch_triples_from_neptune(
                endpoint,
                graph_uri=g,
                backend=args.backend,
                limit=args.limit,
            )
            t, k = (parsed if parsed else (args.tenant, args.kg))
            if args.include_ontology and t:
                try:
                    onto_g = tenant_graph_uri(t)
                except Exception:
                    onto_g = None
                if onto_g:
                    onto_triples = await fetch_triples_from_neptune(
                        endpoint,
                        graph_uri=onto_g,
                        backend=args.backend,
                        limit=args.limit,
                    )
                    triples = list(triples) + list(onto_triples)
            jobs.append((t, k, triples))
        elif args.tenant and args.kg:
            # Prefer mint via live IRI_BASE; also try any-host discovery.
            try:
                g = kg_graph_uri(args.tenant, args.kg)
            except Exception:
                g = None
            if g:
                triples = await fetch_triples_from_neptune(
                    endpoint,
                    graph_uri=g,
                    backend=args.backend,
                    limit=args.limit,
                )
                if triples:
                    if args.include_ontology:
                        try:
                            onto_g = tenant_graph_uri(args.tenant)
                        except Exception:
                            onto_g = None
                        if onto_g:
                            onto_triples = await fetch_triples_from_neptune(
                                endpoint,
                                graph_uri=onto_g,
                                backend=args.backend,
                                limit=args.limit,
                            )
                            triples = list(triples) + list(onto_triples)
                    jobs.append((args.tenant, args.kg, triples))
            if not jobs:
                graphs = await list_instance_graphs_from_neptune(
                    endpoint,
                    backend=args.backend,
                    tenant=args.tenant,
                    kg=args.kg,
                )
                for guri in graphs:
                    parsed = parse_instance_graph_uri(guri)
                    triples = await fetch_triples_from_neptune(
                        endpoint,
                        graph_uri=guri,
                        backend=args.backend,
                        limit=args.limit,
                    )
                    t, k = parsed if parsed else (args.tenant, args.kg)
                    if args.include_ontology and t:
                        try:
                            onto_g = tenant_graph_uri(t)
                        except Exception:
                            onto_g = None
                        if onto_g:
                            onto_triples = await fetch_triples_from_neptune(
                                endpoint,
                                graph_uri=onto_g,
                                backend=args.backend,
                                limit=args.limit,
                            )
                            triples = list(triples) + list(onto_triples)
                    jobs.append((t, k, triples))
        else:
            graphs = await list_instance_graphs_from_neptune(
                endpoint,
                backend=args.backend,
                tenant=args.tenant,
                kg=args.kg,
            )
            if not graphs:
                print(
                    "WARNING: no instance graphs discovered; "
                    "try --tenant/--kg/--graph or --fixture",
                    file=sys.stderr,
                )
            for guri in graphs:
                parsed = parse_instance_graph_uri(guri)
                triples = await fetch_triples_from_neptune(
                    endpoint,
                    graph_uri=guri,
                    backend=args.backend,
                    limit=args.limit,
                )
                t, k = parsed if parsed else (None, None)
                if args.include_ontology and t:
                    try:
                        onto_g = tenant_graph_uri(t)
                    except Exception:
                        onto_g = None
                    if onto_g:
                        onto_triples = await fetch_triples_from_neptune(
                            endpoint,
                            graph_uri=onto_g,
                            backend=args.backend,
                            limit=args.limit,
                        )
                        triples = list(triples) + list(onto_triples)
                jobs.append((t, k, triples))

    if not jobs:
        print("No triples loaded.", file=sys.stderr)
        return 1

    total = EtlStats()
    kind_acc: Counter[str] = Counter()
    all_written = 0
    all_written_assertions = 0
    all_written_catalog = 0
    graphs_done = 0
    class_acc = 0
    prop_acc = 0
    ent_acc = 0
    assert_acc = 0
    subclass_acc = 0
    subprop_acc = 0

    for tenant, kg, triples in jobs:
        facts, assertion_facts, catalog, stats = map_triples(triples)
        label = f"tenant={tenant!r} kg={kg!r}"
        print_stats(label, stats)
        # Optional: sanity-call sanitizers so reserved collisions fail early
        # on write path; dry-run only samples distinct keys.
        for f in facts:
            if f.kind == "type":
                sanitize_domain_label(f.key)
            elif f.kind == "rel":
                sanitize_rel_type(f.key)
            elif f.kind == "literal" and f.key not in (
                "name",
                "source",
                "ingested_at",
                "batch_id",
                "coreSlot",
                "aliasOf",
                "lambda_refreshed_at",
            ):
                try:
                    sanitize_prop_key(f.key)
                except Exception as exc:
                    print(
                        f"  WARN prop key {f.key!r}: {exc}",
                        file=sys.stderr,
                    )

        written = 0
        written_catalog = 0
        if not args.dry_run:
            if not neo4j_env_ready():
                print(
                    "ERROR: NEO4J_URI and NEO4J_PASSWORD required to write "
                    "(or pass --dry-run)",
                    file=sys.stderr,
                )
                return 2
            if not tenant or not kg:
                print(
                    "ERROR: cannot write without tenant/kg scope",
                    file=sys.stderr,
                )
                return 2
            # Ensure product writers use GraphStore for this process.
            os.environ.setdefault("INFONA_GRAPH_BACKEND", "neo4j")
            written = await write_facts_to_store(
                facts,
                tenant_id=tenant,
                kg=kg,
                batch_size=max(1, args.batch_size),
            )
            written_catalog = await write_catalog_to_store(
                catalog,
                tenant_id=tenant,
                kg=kg,
            )
            print(f"  written_facts: {written}")
            print(f"  written_assertions: {written}")  # 1:1 with Fact batch
            print(f"  written_catalog_edges: {written_catalog}")

        graphs_done += 1
        kind_acc.update(stats.kind_counts)
        all_written += written
        all_written_assertions += written
        all_written_catalog += written_catalog
        assert_acc += stats.assertions
        class_acc += stats.classes
        prop_acc += stats.properties
        ent_acc += stats.entities
        subclass_acc += stats.subclass_of
        subprop_acc += stats.subproperty_of
        total = EtlStats(
            triples_in=total.triples_in + stats.triples_in,
            facts_out=total.facts_out + stats.facts_out,
            assertions=assert_acc,
            classes=class_acc,
            properties=prop_acc,
            entities=ent_acc,
            subclass_of=subclass_acc,
            subproperty_of=subprop_acc,
            skipped=total.skipped + stats.skipped,
            kind_counts=dict(kind_acc),
            b3_literal_conflicts=total.b3_literal_conflicts + stats.b3_literal_conflicts,
            subjects=total.subjects + stats.subjects,
            graphs=graphs_done,
            written_facts=all_written,
            written_assertions=all_written_assertions,
            written_catalog_edges=all_written_catalog,
        )

    if len(jobs) > 1 or args.json_stats:
        print_stats("TOTAL", total)
    if args.json_stats:
        print(json.dumps(total.as_dict(), sort_keys=True))

    # Touch unquote so URL-encoded IRIs in fixtures can be normalized by callers
    # without a second import (kept for scripting convenience).
    _ = unquote
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
