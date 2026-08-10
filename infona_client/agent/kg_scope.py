"""The ONE knowledge-graph scope gate for ``/agent`` (ONTA-426, ONTA-428).

Why this exists
---------------
``kg_graph_uri()`` happily mints an IRI for ANY syntactically legal name, and
SPARQL against a named graph that does not exist returns ZERO ROWS rather than an
error. ONTA-413 (onta-oss#263) fixed the consequence for the READ path only:
``QueryCapability.answer`` probes with :func:`~infona_client.graph.kg_status.kg_data_status`
and turns a typo into a clarify instead of a confident "No results found.". Two
holes were left open, and both are worse on an action turn than on a question:

* **ONTA-428**: a typo'd ``kg_name`` on an **enrich / dedup / clean / subscribe /
  discover** turn still planned and RAN work against a graph that does not exist.
  Nothing errored: the scope query matched nothing, the job reported success over
  zero rows, or (on a create-capable rail) a brand-new graph was implicitly minted
  under the typo'd name. The user is told the work happened.

* **ONTA-426**: an OMITTED ``kg_name`` was never handled as its own case, and the
  three rails disagreed about what it meant. ``enrich_cap`` resolved its scope
  against the tenant BASE graph (``kg_graph_uri(...) if ctx.kg_name else
  onto_graph``), so the turn silently operated on a dataset the user never named
  and cannot select in the Explorer's KG dropdown; ``dedup_cap`` and
  ``subscribe_cap`` returned ``[]``, which the planner rendered as a vague "I
  couldn't determine the specifics"; and the normalization / enrichment executors
  call ``kg_graph_uri(tenant, "")``, which RAISES ``InvalidKGName``. Three
  different wrong answers, none of which says the one true thing: you did not tell
  me which graph. Naming nothing is not the same as naming the default, and an
  unscoped read is the shape ONTA-424 is separately closing at the SPARQL layer
  (Neptune's default graph is the UNION of all named graphs). This gate makes the
  omission EXPLICIT at the turn boundary rather than leaving it to be caught
  downstream.

Where the check lives, and why not in ``kg_writer``
--------------------------------------------------
The write-path convergence rule (ADR 0007) says instance writes funnel through
``graph/kg_writer.py``, so that seam is the obvious candidate. It is the wrong
one here, for three reasons:

1. **Too late to be honest.** By the time a write reaches ``insert_facts`` the
   ``/agent`` turn has already returned a plan ack and the work is running as a
   background job. The user's complaint is a silent failure in the CONVERSATION;
   the answer has to arrive in the turn that proposed the work, not in a job
   record nobody re-reads.
2. **It cannot tell a typo from a cold start.** ``ensure_kg_registered`` mints the
   registration record for whatever name it is handed precisely so a brand-new KG
   created by discovery / CLI / MCP becomes visible. That is correct behaviour and
   must stay. Only the planner knows the user's INTENT: "add records I don't have
   yet" (create is right) versus "enrich the records already in graph X" (a missing
   X is a typo).
3. **Reads would stay unguarded.** A dedup preview, a normalize sample and an
   enrich scope resolution all READ a nonexistent graph before anything is
   written, and ``kg_writer`` never sees those.

So the gate sits at the planner, which is the single dispatch point every
``/agent`` turn crosses, and it reuses the existing shared probe rather than
introducing a second validator. ``kg_writer`` keeps doing its job unchanged.

Policy, not a hardcoded capability list
---------------------------------------
Each capability declares its own :data:`KG_SCOPE_ATTR` policy so a downstream /
premium capability registered at boot is covered without editing this module:

* ``"require"``: operates on data that must ALREADY be in the named graph
  (enrich, dedup, clean, subscribe). A missing graph is a typo → clarify.
* ``"create"``: may legitimately mint the graph it targets (web discovery). A
  missing graph is NOT refused; the fact is surfaced on the plan the user confirms.
* ``"none"``: not KG-scoped, or does its own richer probe (query does, ONTA-413;
  ontology edits are tenant-scoped; web research never touches the KG).

The default for a capability that declares nothing is ``"require"``: the
conservative verdict is the one that asks a question instead of silently acting.
"""

from __future__ import annotations

import structlog

from infona_client.graph.kg_status import (
    KG_MISSING,
    kg_data_status,
    list_kg_names,
    missing_kg_message,
)

logger = structlog.stdlib.get_logger("cograph.agent.kg_scope")

# The attribute a capability sets to declare its policy (see the module docstring).
KG_SCOPE_ATTR = "kg_scope_policy"

SCOPE_REQUIRE = "require"
SCOPE_CREATE = "create"
SCOPE_NONE = "none"

# Unknown / undeclared capability → the conservative policy. A premium capability
# that operates on existing data (the common case) is covered without opting in;
# one that legitimately creates graphs opts out explicitly, the same way it opts
# into every other registry contract.
DEFAULT_SCOPE_POLICY = SCOPE_REQUIRE

# Where the resolved verdict is stashed for capabilities that want it (the
# discovery rail reads `kg_status` so its plan card can say the graph will be
# created). Kept on ctx.extras rather than AgentContext so an older/bare context
# built by a test or a downstream caller keeps working.
CTX_KG_STATUS = "kg_status"
CTX_KG_RESOLVED = "kg_scope_resolved"
CTX_KG_AVAILABLE = "kg_scope_available"

# Machine-readable reason on the returned clarify, so a client (or an MCP agent
# deciding how to retry) can tell "you named a graph that isn't there" from "you
# named none and there are several" without parsing the prose.
CODE_KG_MISSING = "kg_missing"
CODE_KG_AMBIGUOUS = "kg_ambiguous"


def scope_policy(cap) -> str:
    """The KG-scope policy declared by ``cap`` (a capability instance or a name).

    Read defensively: a capability object that predates this contract, or a name
    that no longer resolves in the registry, falls back to
    :data:`DEFAULT_SCOPE_POLICY`.
    """
    if isinstance(cap, str):
        from infona_client.agent.registry import get_capability

        cap = get_capability(cap)
    if cap is None:
        return DEFAULT_SCOPE_POLICY
    value = getattr(cap, KG_SCOPE_ATTR, DEFAULT_SCOPE_POLICY)
    if value not in (SCOPE_REQUIRE, SCOPE_CREATE, SCOPE_NONE):
        return DEFAULT_SCOPE_POLICY
    return value


def ambiguous_kg_message(names: list[str]) -> str:
    """The clarify text for an omitted ``kg_name`` with several graphs to choose from."""
    return (
        "No knowledge graph was specified for this request, and this workspace "
        f"has {len(names)}. Which one should I use? Available knowledge graphs: "
        f"{', '.join(names)}."
    )


def missing_kg_mixed_message(kg_name: str) -> str:
    """Clarify text for a turn that mixes a ``create`` rail with a ``require`` one.

    "Find X from the web and clean up the names" against a graph that does not
    exist yet. Discovery alone would be fine (it mints the graph), but the clean /
    dedup / enrich half has nothing to operate on, and ``_INTENT_PLAN_ORDER`` runs
    cleaning BEFORE discovery anyway. Refusing is right; using the plain
    "does not exist, here are the real ones" text would not be, because it reads
    as if the user asked for something impossible when half their request is
    perfectly runnable. Name the actual split instead.
    """
    return (
        f"Knowledge graph '{kg_name}' does not exist yet. I can create it by "
        "adding data, but the rest of this request (cleaning, merging, enriching "
        "or watching existing records) needs data that is already there. Ask me "
        "to add the data first, then follow up with the rest."
    )


def resolved_kg_note(kg_name: str) -> str:
    """The note shown when an omitted ``kg_name`` resolved to the only graph there is."""
    return (
        "No knowledge graph was specified, so this applies to "
        f"'{kg_name}', the only knowledge graph in this workspace."
    )


async def check_kg_scope(
    ctx, capabilities: list, *, resolve_omitted: bool = True
) -> dict | None:
    """Resolve / validate the KG scope of one ``/agent`` turn.

    ``capabilities`` are the capability instances (or names) this turn is about to
    run. Returns ``None`` when the turn may proceed, or a ``{"kind": "clarify"}``
    payload the caller returns verbatim (the ``/agent`` contract is
    ``{kind: answer|clarify|plan|result}``, so a clarify is both in-contract and
    directly actionable, exactly as ONTA-413 chose for the read path).

    ``resolve_omitted=False`` turns this into a pure VALIDATOR: an omitted
    ``kg_name`` is left exactly as it is. The confirm path passes it, because a
    plan proposed with no KG scope was already gated when it was proposed, and
    re-inferring at confirm time against a workspace that has since grown a graph
    would silently retarget a plan the user already approved.

    Side effects on ``ctx`` when the turn proceeds:

    * ``ctx.extras[CTX_KG_STATUS]``: the probe verdict, so a create-capable
      capability can say "this graph does not exist yet and will be created" on
      the plan card instead of creating one silently.
    * ``ctx.extras[CTX_KG_AVAILABLE]``: the workspace's other graphs, looked up
      once on the missing path and shared by the refusal message and the discovery
      rail (which withholds auto-confirm when a MISSING target sits in a workspace
      that already has graphs, i.e. the typo shape).
    * ``ctx.kg_name``: set when an omitted name resolved to the workspace's ONLY
      graph, with ``ctx.extras[CTX_KG_RESOLVED]`` recording that it was inferred
      rather than asked for.

    Fails OPEN throughout, inheriting :func:`kg_data_status`'s own rule: a probe
    that cannot run must degrade to today's behaviour rather than invent a "your
    graph does not exist" claim.
    """
    policies = {scope_policy(c) for c in capabilities}
    if not policies - {SCOPE_NONE}:
        # Nothing on this turn is KG-scoped (or the capability runs its own,
        # richer probe). Do not spend a round trip and do not second-guess it.
        return None

    tenant_id = getattr(ctx, "tenant_id", "")
    neptune = getattr(ctx, "neptune", None)
    extras = getattr(ctx, "extras", None)
    if extras is None:  # pragma: no cover - AgentContext always has one
        extras = {}

    kg_name = getattr(ctx, "kg_name", "") or ""
    if kg_name:
        status = await kg_data_status(neptune, tenant_id, kg_name)
        extras[CTX_KG_STATUS] = status
        if status != KG_MISSING:
            return None
        # ONE lookup, shared by the refusal message below and by the discovery
        # rail's auto-confirm decision. A workspace that already HAS graphs and
        # was handed a name that is not one of them is the typo shape; an empty
        # list is a genuine cold start.
        available = await list_kg_names(neptune, tenant_id)
        extras[CTX_KG_AVAILABLE] = list(available)
        if SCOPE_REQUIRE in policies:
            # ONTA-428. The named graph does not exist and at least one rail on
            # this turn needs data that would already be in it, so there is
            # nothing for that rail to act on. Refuse with the real graph names so
            # an MCP/CLI agent can retry in one hop instead of "succeeding" over
            # zero rows.
            logger.info(
                "agent_kg_scope_missing",
                tenant=tenant_id,
                kg_name=kg_name,
                mixed=SCOPE_CREATE in policies,
            )
            question = (
                missing_kg_mixed_message(kg_name)
                if SCOPE_CREATE in policies
                else missing_kg_message(kg_name, available)
            )
            return {
                "kind": "clarify",
                "code": CODE_KG_MISSING,
                "question": question,
                "options": list(available),
            }
        # A purely create-capable turn against a missing graph is NOT refused:
        # "the KG does not exist yet" is a legitimate cold start for discovery. It
        # is no longer SILENT either. The verdict and the workspace's other graphs
        # are on ctx.extras, so the plan card says the graph will be created and
        # withholds auto-confirm when the name looks like a typo.
        return None

    if not resolve_omitted:
        return None

    # ONTA-426: nothing was named. Resolve it or ask; never act unscoped.
    names = await list_kg_names(neptune, tenant_id)
    if len(names) == 1:
        ctx.kg_name = names[0]
        extras[CTX_KG_RESOLVED] = names[0]
        logger.info(
            "agent_kg_scope_resolved", tenant=tenant_id, kg_name=names[0]
        )
        return None
    if len(names) > 1:
        logger.info(
            "agent_kg_scope_ambiguous", tenant=tenant_id, candidates=len(names)
        )
        return {
            "kind": "clarify",
            "code": CODE_KG_AMBIGUOUS,
            "question": ambiguous_kg_message(names),
            "options": list(names),
        }
    # Zero registered graphs: there is no candidate to disambiguate, and a
    # workspace can legitimately keep its instance data in the tenant BASE graph
    # (``api/routes/ingest.py`` writes there whenever ``kg_name`` is absent, the
    # same do-no-harm case ONTA-413 protects on the read path). Proceed exactly as
    # before rather than block a cold start on a question with no answers.
    return None


__all__ = [
    "CODE_KG_AMBIGUOUS",
    "CODE_KG_MISSING",
    "CTX_KG_AVAILABLE",
    "CTX_KG_RESOLVED",
    "CTX_KG_STATUS",
    "DEFAULT_SCOPE_POLICY",
    "KG_SCOPE_ATTR",
    "SCOPE_CREATE",
    "SCOPE_NONE",
    "SCOPE_REQUIRE",
    "ambiguous_kg_message",
    "check_kg_scope",
    "missing_kg_mixed_message",
    "resolved_kg_note",
    "scope_policy",
]
