"""Cross-workspace ingestion isolation check — the QC guarantee that two discovery
runs in DIFFERENT workspaces, sharing one store (and, in the wild, one process and even
one resolver), never cross-contaminate: no fact, edge, or node authored by workspace A
ever lands in workspace B's graphs, and each workspace's instance data lands in its OWN
declared target graph — never a sibling's graph, and never (the ONTA-198 empty-``kg_name``
class) leaked into its own tenant BASE graph where it is invisible in Explorer.

Why a sibling module and not another per-graph ``Invariant``: the invariants in
``cograph_client.qc.invariants`` ask "is THIS one graph well-formed?" Isolation is
inherently CROSS-graph and CROSS-tenant — "did workspace A's data stay inside A's graphs
while B ingested concurrently?" — so it needs the whole store plus a per-workspace
ownership spec, not a single ``graph_uri``. Same shape as ``scenario.py``: a reusable OSS
helper that composes the store + the graph model, layered beside the invariant library.

The un-gameable discriminator is PROVENANCE. Every instance entity the resolver writes is
stamped ``onto/source`` = the caller's source string (``schema_resolver`` ~L3166). Give
each workspace's ingest a DISTINCT source and that source value identifies the AUTHOR of a
fact regardless of which named graph the triple physically lands in. So a leak — A's write
mis-directed into B's graph — is detectable as "an A-sourced entity found inside B's graph"
even though the mis-directed triple otherwise looks native to B. Edges are attributed
through their endpoint entities' source (a relationship triple carries no source of its own).

This is the exact failure surface of the known SchemaResolver non-reentrancy: a single
resolver keeps the live target graph on the INSTANCE — ``self._instance_graph``, set at the
top of ``ingest()`` (``schema_resolver`` L1011) and read on the whole insert path
(``getattr(self, "_instance_graph", ...)``). Two INTERLEAVED ingests on ONE shared resolver
clobber that field, so the later target wins and the earlier run's triples are written into
the wrong workspace's graph. This check turns that latent hazard into a hard, observable
pass/fail. It does NOT fix the resolver (out of scope — see ``tests/test_qc_isolation.py``).

Scope note: this is a GRAPH-resident check — it covers the facts, nodes, and edges that
land in the store. Discovery *plans* (the plan→confirm staging) are session state, not
graph triples, so they are outside a store-level structural invariant; the graph outputs a
confirmed plan produces ARE covered here.

OSS: stdlib + ``cograph_client.*`` only. No endpoint baked in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri

# URI scheme (mirrors qc.invariants).
ENTITY_PREFIX = "https://cograph.tech/entities/"
ONTO_PREFIX = "https://cograph.tech/onto/"
ONTO_SOURCE = "https://cograph.tech/onto/source"


@dataclass(frozen=True)
class WorkspaceScope:
    """One workspace participating in an interleaved run.

    ``source`` is the provenance tag its ingest stamps on every entity (``onto/source``) —
    the discriminator that identifies facts it authored. ``kg`` names its target KG, so its
    instances land in ``kg_graph_uri(tenant, kg)`` with the ontology in the tenant base
    graph; ``kg=None`` targets the base graph directly (no per-KG split). Sources MUST be
    distinct across the workspaces under test, or the check cannot attribute a fact.
    """

    tenant: str
    source: str
    kg: Optional[str] = None
    label: Optional[str] = None

    @property
    def base_graph(self) -> str:
        return tenant_graph_uri(self.tenant)

    @property
    def instance_graph(self) -> str:
        """Where this workspace's INSTANCE data is supposed to land."""
        return kg_graph_uri(self.tenant, self.kg) if self.kg else tenant_graph_uri(self.tenant)

    @property
    def display(self) -> str:
        if self.label:
            return self.label
        return f"{self.tenant}/{self.kg}" if self.kg else self.tenant


@dataclass(frozen=True)
class IsolationViolation:
    """One cross-workspace leak. Always ``error`` severity — a workspace boundary crossed is
    never a mere warning."""

    kind: str  # cross_workspace_fact | fact_leaked_to_base | cross_workspace_edge
    severity: str
    detail: str
    author: str  # display of the workspace that AUTHORED the leaked data
    landed_in: str  # display (or graph URI) of where it WRONGLY landed
    binding: dict = field(default_factory=dict)


def _cell(binding: dict, key: str) -> str:
    cell = binding.get(key)
    return cell.get("value", "") if isinstance(cell, dict) else ""


async def _entities_by_graph(neptune) -> list[tuple[str, str, str]]:
    """Every ``(graph, entity, source)``: which instance entities carry an ``onto/source``
    tag, and in which named graph the tag physically lives. The graph binding is what
    exposes a mis-direction — the source says who authored it, the graph says where it
    ended up."""
    result = await neptune.query(
        f"SELECT ?g ?e ?src WHERE {{ GRAPH ?g {{ ?e <{ONTO_SOURCE}> ?src }} }}"
    )
    return [
        (_cell(b, "g"), _cell(b, "e"), _cell(b, "src"))
        for b in result.get("results", {}).get("bindings", [])
    ]


async def _edges_by_graph(neptune) -> list[tuple[str, str, str, str]]:
    """Every relationship edge ``(graph, s, p, o)`` on an ``onto/<leaf>`` predicate whose
    subject and object are both entity nodes — the edges a leak could strand in the wrong
    workspace's graph."""
    result = await neptune.query(
        "SELECT ?g ?s ?p ?o WHERE { "
        "GRAPH ?g { ?s ?p ?o . "
        f'FILTER(STRSTARTS(STR(?p), "{ONTO_PREFIX}") '
        f'&& isIRI(?s) && STRSTARTS(STR(?s), "{ENTITY_PREFIX}") '
        f'&& isIRI(?o) && STRSTARTS(STR(?o), "{ENTITY_PREFIX}")) '
        "} }"
    )
    return [
        (_cell(b, "g"), _cell(b, "s"), _cell(b, "p"), _cell(b, "o"))
        for b in result.get("results", {}).get("bindings", [])
    ]


async def check_isolation(
    neptune, workspaces: list[WorkspaceScope]
) -> list[IsolationViolation]:
    """Assert that every workspace in ``workspaces`` kept its data inside its own graphs.

    ``neptune`` is any client exposing ``async query(sparql) -> dict`` returning SPARQL-1.1
    JSON (the production ``NeptuneClient``, the harness store, and the pyoxigraph test shim
    all satisfy this). Returns every leak found, empty when perfectly isolated. Three leak
    classes:

    * ``cross_workspace_fact`` — an entity carrying workspace X's source found inside a
      graph owned by a DIFFERENT workspace (or any graph that is not X's target): the direct
      cross-tenant contamination signal, and the one the resolver-reentrancy leak trips.
    * ``fact_leaked_to_base`` — an X-sourced instance entity found in X's own BASE graph while
      X declared a KG target: instance data leaked to the ontology graph, invisible in
      Explorer (ONTA-198 empty-``kg_name`` class). Correct-target-graph enforcement.
    * ``cross_workspace_edge`` — a relationship edge sitting in workspace Y's graph but
      referencing an entity authored by workspace X: a stranded edge across the boundary.

    Data whose source is not one of the workspaces under test is IGNORED (unrelated tenants
    on a shared store never false-positive). Intended for DISTINCT workspaces (distinct
    tenants / base graphs); that is the "two-workspace" contract."""
    by_source = {w.source: w for w in workspaces}
    inst_owner = {w.instance_graph: w for w in workspaces}
    base_owner = {w.base_graph: w for w in workspaces}

    ent_rows = await _entities_by_graph(neptune)

    # entity URI -> the set of authoring-workspace sources it carries (by VALUE, wherever
    # the tag physically sits). A well-isolated entity carries exactly one; >1 is itself a
    # leak the fact checks below already surface.
    home: dict[str, set[str]] = {}
    for _g, e, src in ent_rows:
        if src in by_source:
            home.setdefault(e, set()).add(src)

    violations: list[IsolationViolation] = []

    # --- fact / node placement -------------------------------------------------------- #
    for g, e, src in ent_rows:
        author = by_source.get(src)
        if author is None:
            continue  # a source not under test — unrelated data in a shared store
        if g == author.instance_graph:
            continue  # correct target — the happy path
        if g == author.base_graph and author.kg is not None:
            violations.append(
                IsolationViolation(
                    kind="fact_leaked_to_base",
                    severity="error",
                    detail=(
                        f"{e} (source {src!r}) landed in {author.display}'s BASE graph "
                        f"instead of its KG target {author.instance_graph} — instance data "
                        "in the ontology graph is invisible in Explorer (ONTA-198)"
                    ),
                    author=author.display,
                    landed_in=g,
                    binding={"e": e, "src": src, "g": g},
                )
            )
            continue
        into = inst_owner.get(g) or base_owner.get(g)
        into_label = into.display if into else g
        violations.append(
            IsolationViolation(
                kind="cross_workspace_fact",
                severity="error",
                detail=(
                    f"{e} authored by {author.display} (source {src!r}) leaked into "
                    f"{into_label} — cross-workspace contamination"
                ),
                author=author.display,
                landed_in=into_label,
                binding={"e": e, "src": src, "g": g},
            )
        )

    # --- edge placement --------------------------------------------------------------- #
    # A relationship triple carries no source of its own, so attribute it through its
    # endpoint entities: an edge living in Y's graph but touching an X-authored entity is a
    # stranded cross-workspace edge. Emit at most one violation per edge.
    for g, s, p, o in await _edges_by_graph(neptune):
        g_owner = inst_owner.get(g) or base_owner.get(g)
        if g_owner is None:
            continue  # edge in an untracked graph — not ours to judge
        foreign_author: Optional[WorkspaceScope] = None
        for endpoint in (s, o):
            authors = home.get(endpoint)
            if not authors or len(authors) != 1:
                continue  # unattributable or ambiguous endpoint — skip
            author = by_source[next(iter(authors))]
            if author != g_owner:
                foreign_author = author
                break
        if foreign_author is not None:
            violations.append(
                IsolationViolation(
                    kind="cross_workspace_edge",
                    severity="error",
                    detail=(
                        f"edge {s} --[{p}]--> {o} sits in {g_owner.display}'s graph but "
                        f"references an entity authored by {foreign_author.display} — "
                        "cross-workspace edge"
                    ),
                    author=foreign_author.display,
                    landed_in=g_owner.display,
                    binding={"s": s, "p": p, "o": o, "g": g},
                )
            )

    return violations


def isolated(violations: list[IsolationViolation]) -> bool:
    """True when no leak was found — the pass condition an assertion or gate keys off."""
    return not violations


def format_isolation(violations: list[IsolationViolation]) -> str:
    """Human-readable rendering of a :func:`check_isolation` result."""
    if not violations:
        return "workspace isolation: OK (no cross-workspace leakage)"
    lines = [f"workspace isolation: {len(violations)} leak(s)"]
    for v in violations:
        lines.append(f"  x [{v.kind}] {v.detail}")
    return "\n".join(lines)
