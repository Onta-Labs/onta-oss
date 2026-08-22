"""Lambda-invoke's graph reads, on the GraphStore (ONTA-534).

``api/routes/lambda_functions.py`` reads the graph four times before it can
invoke anything, and every one of them was a bare SPARQL ``client.query``:

* the FUNCTION REGISTRY lookup (which function is registered under this name);
* the CIK resolution ladder (the entity's ``filing_cik``, then a linked
  ``FundingRound``'s, then a ``FundingRound`` label that IS the CIK);
* the Investor's display NAME for ``investor-portfolio``;
* the PORTFOLIO traversal itself (``Investor`` ← ``lead_investor`` ―
  ``FundingRound`` → ``company_name`` → ``Company``).

That SPARQL HTTP read is RETIRED under the shipped Neo4j GraphStore, so the
first three raised ``SparqlClientRetired`` out of the route as a **500**. The
fourth was worse: ``POST /functions/investor-portfolio`` wrapped its query in a
bare ``except: pass`` and returned ``portfolio_count=0, companies=[]`` — a
CONFIDENT WRONG ANSWER, indistinguishable from "this investor has no
portfolio", produced without a single row ever being read.

**Same reads, one source.** The registry answer comes from the same
``functions.store`` that ``GET /graphs/{t}/functions`` already lists from; the
entity reads come from :func:`infona_client.graph.explore_store.get_entity_detail`,
the same read the Explorer entity panel renders. Attribute leaves land as Entity
property keys and relationship leaves as ``r.attr`` on the edge, so the
traversals below are the Cypher-shaped mirror of the SPARQL they replace.

**Tri-state, deliberately.** Every function returns ``None`` for "the store
could not be consulted at all" and a real value (possibly empty) when it was.
That distinction is what lets the portfolio route keep 0/[] honest: it answers
0 only when the store genuinely searched and found nothing, and reports
"unavailable" when neither arm could run — instead of asserting an emptiness it
never checked.
"""

from __future__ import annotations

from typing import Any

import structlog

from infona_client.graph.parser import parse_sparql_results

logger = structlog.stdlib.get_logger("infona.lambda_functions")

#: Attribute leaf every CIK lookup keys on.
CIK_LEAF = "filing_cik"

#: Cap on incident edges any one walk expands. Each SPARQL read these replace
#: was ONE query; the store walk is one read per hop, so a hub entity with
#: thousands of incident rounds would otherwise turn a bounded lookup into an
#: unbounded fan-out. The demo shapes these routes serve are far below this.
MAX_INCIDENT_HOPS = 200


async def safe_query(client, sparql: str) -> list[dict]:
    """Run one residual SPARQL read; ``[]`` instead of raising (ONTA-534).

    Every read in the lambda routes now has a GraphStore arm ahead of it, so the
    SPARQL client is a SUPPLEMENT — the arm that can still see a legacy triple
    store, and the arm the dual-arm unit tests drive. On the shipped Neo4j
    backend it raises ``SparqlClientRetired`` unconditionally, and letting that
    escape is exactly what turned "no CIK for this entity" (a 422) and "no such
    function" (a 404) into 500s. Degrading to no rows preserves each caller's
    own failure semantics.
    """
    try:
        _, bindings = parse_sparql_results(await client.query(sparql))
        return bindings
    except Exception as exc:  # noqa: BLE001 — retired/failing client, not a 500
        logger.debug("lambda_sparql_read_failed", error=str(exc))
        return []


async def _entity_detail(tenant_id: str, kg_name: str, entity_uri: str):
    """``EntityDetail`` for one entity, or ``None`` when the store can't answer."""
    if not (tenant_id and kg_name and entity_uri):
        return None
    try:
        from infona_client.graph.explore_store import get_entity_detail

        return await get_entity_detail(
            tenant_id=tenant_id, kg_name=kg_name, entity_id=entity_uri
        )
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "lambda_entity_detail_failed",
            tenant=tenant_id,
            kg=kg_name,
            entity=entity_uri,
            error=str(exc),
        )
        return None


async def store_function_refs(tenant_id: str) -> list[Any]:
    """Every function the tenant has registered, from the shared function store.

    The SAME source ``GET /graphs/{tenant}/functions`` lists from, so invoke and
    list cannot disagree about what is registered. Returns ``[]`` on any store
    failure — the caller still consults its residual SPARQL arm and only then
    decides 404.
    """
    try:
        from infona_client.functions.store import make_function_store

        return list(await make_function_store().list_for_tenant(tenant_id) or ())
    except Exception as exc:  # noqa: BLE001
        logger.debug("lambda_function_store_failed", tenant=tenant_id, error=str(exc))
        return []


async def store_resolve_cik(
    tenant_id: str, kg_name: str, entity_uri: str
) -> str | None:
    """The entity's ``filing_cik``, mirroring the SPARQL ladder's three rungs.

    1. the attribute directly on the entity;
    2. a ``FundingRound`` incident on it that carries the attribute;
    3. a ``FundingRound`` whose display NAME *is* a CIK (the pear-backyard data
       shape) — accepted only when it is all digits, exactly as the SPARQL arm
       checked.

    ``None`` = not resolvable here; the caller falls through to SPARQL and then
    to its existing 422.
    """
    detail = await _entity_detail(tenant_id, kg_name, entity_uri)
    if detail is None:
        return None

    direct = detail.properties.get(CIK_LEAF)
    if direct not in (None, ""):
        return str(direct)

    # Rungs 2 and 3 both look at entities incident on this one. The SPARQL arm
    # matched `?round ?rel <entity>` (any predicate, incoming) for rung 2 and
    # `?round onto/company_name <entity>` for rung 3; incoming edges cover both.
    for rel in detail.incoming[:MAX_INCIDENT_HOPS]:
        other = await _entity_detail(tenant_id, kg_name, rel.other_id)
        if other is None:
            continue
        linked = other.properties.get(CIK_LEAF)
        if linked not in (None, ""):
            return str(linked)
        if other.primary_type == "FundingRound" and rel.attr == "company_name":
            candidate = (other.name or "").strip()
            if candidate and candidate.lstrip("0").isdigit():
                return candidate
    return None


async def store_entity_name(
    tenant_id: str, kg_name: str, entity_uri: str
) -> str | None:
    """The entity's display name — the ``name`` attribute, else the node's name.

    Mirrors the SPARQL UNION that preferred ``Investor/attrs/name`` (spaces)
    over ``rdfs:label`` (underscores).
    """
    detail = await _entity_detail(tenant_id, kg_name, entity_uri)
    if detail is None:
        return None
    attr_name = detail.properties.get("name")
    if attr_name not in (None, ""):
        return str(attr_name)
    return (detail.name or "").strip() or None


async def store_portfolio(
    tenant_id: str,
    kg_name: str,
    *,
    investor_uri: str | None = None,
    investor_name: str | None = None,
) -> tuple[list[str], int] | None:
    """``(company_names, total_invested_usd)`` for one investor, or ``None``.

    Walks ``Investor`` ← ``lead_investor`` ― ``FundingRound`` →
    ``company_name`` → ``Company``, summing each round's ``amount_usd`` — the
    same shape as the SPARQL, one hop at a time.

    ``investor_uri`` is used when the caller already has the entity (the invoke
    route); otherwise the investor is looked up BY NAME with a bounded literal
    scan on the ``name`` attribute, matching the SPARQL arm's
    ``?investor types/Investor/attrs/name "<name>"``.

    ``None`` means the store could not be consulted — the caller must NOT
    report an empty portfolio on the strength of that.
    """
    uri = investor_uri
    if not uri:
        uri = await _investor_uri_by_name(tenant_id, kg_name, investor_name or "")
        if uri is None:
            return None
        if not uri:
            # The store searched and no Investor carries that name: a real,
            # empty answer rather than an unreadable one.
            return [], 0

    detail = await _entity_detail(tenant_id, kg_name, uri)
    if detail is None:
        return None

    companies: list[str] = []
    total = 0
    for rel in detail.incoming[:MAX_INCIDENT_HOPS]:
        if rel.attr != "lead_investor":
            continue
        round_detail = await _entity_detail(tenant_id, kg_name, rel.other_id)
        if round_detail is None:
            continue
        amount_counted = False
        for out in round_detail.outgoing:
            if out.attr != "company_name":
                continue
            company = await _entity_detail(tenant_id, kg_name, out.other_id)
            cname = None
            if company is not None:
                cname = company.properties.get("name") or company.name
            cname = str(cname or out.other_name or "").strip()
            if cname and cname not in companies:
                companies.append(cname)
            if not amount_counted:
                amount_counted = True
                total += _amount(round_detail.properties.get("amount_usd"))
    return companies, total


async def store_discover_investors(
    tenant_id: str, kg_name: str, entity_uri: str
) -> list[tuple[str, str]]:
    """``(investor_uri, investor_name)`` reachable from a Company, for the cascade.

    Walks ``Company`` ← ``company_name`` ― ``FundingRound`` → ``lead_investor``
    → ``Investor``, the same two hops the ``discover_query`` SPARQL made.
    Already-fail-soft at the call site, so this returns ``[]`` rather than a
    tri-state.
    """
    detail = await _entity_detail(tenant_id, kg_name, entity_uri)
    if detail is None:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rel in detail.incoming[:MAX_INCIDENT_HOPS]:
        if rel.attr != "company_name":
            continue
        round_detail = await _entity_detail(tenant_id, kg_name, rel.other_id)
        if round_detail is None or round_detail.primary_type != "FundingRound":
            continue
        for edge in round_detail.outgoing:
            if edge.attr != "lead_investor" or not edge.other_id:
                continue
            if edge.other_id in seen:
                continue
            investor = await _entity_detail(tenant_id, kg_name, edge.other_id)
            label = None
            if investor is not None:
                label = investor.name or investor.properties.get("name")
            label = str(label or edge.other_name or "").strip()
            if not label:
                continue
            seen.add(edge.other_id)
            out.append((edge.other_id, label))
    return out


async def _investor_uri_by_name(
    tenant_id: str, kg_name: str, investor_name: str
) -> str | None | Any:
    """Investor entity URI for ``investor_name``: URI, ``""``, or ``None``.

    ``""`` means "searched, no such Investor" (a real answer); ``None`` means
    the store could not be consulted.
    """
    name = (investor_name or "").strip()
    if not name:
        return ""
    try:
        from infona_client.graph.explore_store import grep_literals

        result = await grep_literals(
            tenant_id=tenant_id,
            kg_name=kg_name,
            needle=name,
            type_name="Investor",
            predicate_leaf="name",
            limit=25,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "lambda_investor_lookup_failed",
            tenant=tenant_id,
            kg=kg_name,
            error=str(exc),
        )
        return None
    if result is None:
        return None
    hits, _ = result
    # `grep` is a substring scan; the SPARQL arm bound the name EXACTLY, so keep
    # exact matching here rather than widening who counts as this investor.
    for hit in hits:
        if (hit.value or "").strip() == name and hit.entity_uri:
            return hit.entity_uri
    return ""


def _amount(raw: Any) -> int:
    """One round's ``amount_usd`` as an int; 0 for anything unparseable."""
    if raw in (None, ""):
        return 0
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "safe_query",
    "store_discover_investors",
    "store_entity_name",
    "store_function_refs",
    "store_portfolio",
    "store_resolve_cik",
]
