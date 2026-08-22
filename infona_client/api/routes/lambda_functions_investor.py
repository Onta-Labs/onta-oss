"""The investor-portfolio lambda's two operations (ONTA-534).

Extracted from :mod:`infona_client.api.routes.lambda_functions` — which sits at
its file-size pin — so the ONTA-534 GraphStore arms could land without growing
it. The ROUTES still live there (same router, same registration order, same
paths); only the bodies moved, and both are plain coroutines returning dicts so
this module never has to import the route models back and close a cycle.

Two operations, one shape:

* :func:`run_investor_portfolio` — read-only lookup by investor NAME
  (``POST /functions/investor-portfolio``).
* :func:`run_invoke_investor_portfolio` — the same walk from an entity URI,
  materializing the result onto that entity
  (``POST /graphs/{t}/functions/investor-portfolio/invoke``).

**What ONTA-534 changed.** Both were SPARQL-only over the retired
the retired SPARQL HTTP read. The invoke path's first read had no ``try`` and
500'd;
the read-only path had ``except: pass`` and answered ``portfolio_count=0,
companies=[]`` for every investor — a confident wrong answer produced without a
single row being read, which is worse than the 500. Both now walk the
GraphStore first (:mod:`.lambda_functions_reads`) with the SPARQL kept as a
residual supplement, and the read-only path tracks whether ANY arm actually ran:
a genuine 0 is still 0, but when neither arm could read it returns **503**
instead of asserting an emptiness it never checked.
"""

from __future__ import annotations

import datetime
import time

import structlog
from fastapi import HTTPException

from infona_client.api.routes.lambda_functions_reads import (
    safe_query,
    store_entity_name,
    store_portfolio,
)
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import delete_facts, insert_facts, refresh_after_write
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.models.ontology import OntologyMutation, OntologyOpKind

logger = structlog.stdlib.get_logger("infona.lambda_functions")

#: KGs the read-only lookup searches. Hardcoded demo scope, unchanged by the
#: ONTA-534 port — a real KG selector is a separate product decision.
PORTFOLIO_KGS = ["pear-backyard"]


def _portfolio_sparql(instance_graph: str, investor_name: str) -> str:
    """The residual portfolio traversal, identical for both operations."""
    return (
        f"SELECT ?companyName ?amount FROM <{instance_graph}>\n"
        f"WHERE {{\n"
        f"  ?investor <{IRI_BASE}/types/Investor/attrs/name> \"{investor_name}\" .\n"
        f"  ?investor <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{IRI_BASE}/types/Investor> .\n"
        f"  ?round <{IRI_BASE}/onto/lead_investor> ?investor .\n"
        f"  ?round <{IRI_BASE}/onto/company_name> ?company .\n"
        f"  ?company <{IRI_BASE}/types/Company/attrs/name> ?companyName .\n"
        f"  OPTIONAL {{ ?round <{IRI_BASE}/types/FundingRound/attrs/amount_usd> ?amount }}\n"
        f"}}"
    )


def _absorb_rows(rows, companies: list[str]) -> int:
    """Fold SPARQL rows into ``companies``; return the amount they contribute."""
    total = 0
    for row in rows:
        cname = row.get("companyName", "")
        if cname and cname not in companies:
            companies.append(cname)
        amt_str = row.get("amount", "")
        if amt_str:
            try:
                total += int(float(amt_str))
            except (ValueError, TypeError):
                pass
    return total


async def run_investor_portfolio(client, tenant_id: str, investor_name: str) -> dict:
    """Companies in an investor's portfolio, looked up by NAME.

    Looks up FundingRound entities where lead_investor matches this investor,
    then follows company_name relationships to get Company names and sums
    amounts. GraphStore first, SPARQL as the residual supplement.

    Raises 503 when NEITHER arm could read the graph — see the module docstring
    for why an empty portfolio would be the wrong answer there.
    """
    companies: list[str] = []
    total_invested = 0
    answered = False

    for kg_name in PORTFOLIO_KGS:
        from_store = await store_portfolio(
            tenant_id, kg_name, investor_name=investor_name
        )
        if from_store is not None:
            answered = True
            for cname in from_store[0]:
                if cname and cname not in companies:
                    companies.append(cname)
            total_invested += from_store[1]

        sparql = _portfolio_sparql(kg_graph_uri(tenant_id, kg_name), investor_name)
        try:
            from infona_client.graph.parser import parse_sparql_results

            _, rows = parse_sparql_results(await client.query(sparql))
        except Exception:  # noqa: BLE001 — this arm simply could not answer
            continue
        answered = True
        total_invested += _absorb_rows(rows, companies)

    if not answered:
        raise HTTPException(
            status_code=503,
            detail=(
                "Portfolio is unavailable: neither the graph store nor the "
                "SPARQL client could be read (ONTA-534). Returning an empty "
                "portfolio here would assert an emptiness that was never "
                "checked."
            ),
        )

    return {
        "portfolio_count": len(companies),
        "companies": companies,
        "total_invested_usd": total_invested if total_invested > 0 else None,
    }


async def run_invoke_investor_portfolio(
    client, tenant_id: str, entity_uri: str, kg_name: str
) -> dict:
    """Resolve one Investor's portfolio and materialize it onto the entity.

    Returns the ``InvokeResponse`` field map (the route builds the model, so
    this module never imports it back). Raises 422 when the entity has no
    resolvable name — the same failure this route always had.
    """
    start = time.monotonic()
    instance_graph = kg_graph_uri(tenant_id, kg_name)
    ontology_graph = tenant_graph_uri(tenant_id)

    # Resolve investor name from entity — prefer the Investor/attrs/name
    # attribute (which uses spaces) over rdfs:label (which uses underscores).
    # GraphStore first (ONTA-534): this was a bare `client.query` and therefore
    # a 500 on the shipped backend. The SPARQL arm is retained as a supplement;
    # an unresolvable name is still the 422 it always was.
    investor_name = await store_entity_name(tenant_id, kg_name, entity_uri)
    if not investor_name:
        name_query = (
            f"SELECT ?name FROM <{instance_graph}>\n"
            f"WHERE {{\n"
            f"  {{ <{entity_uri}> <{IRI_BASE}/types/Investor/attrs/name> ?name }}\n"
            f"  UNION\n"
            f"  {{ <{entity_uri}> <http://www.w3.org/2000/01/rdf-schema#label> ?name }}\n"
            f"}}"
        )
        rows = await safe_query(client, name_query)
        investor_name = rows[0].get("name", "") if rows else ""

    if not investor_name:
        raise HTTPException(
            status_code=422,
            detail=f"Could not resolve name for entity {entity_uri}",
        )

    # Portfolio walk, run directly rather than by calling the read-only endpoint
    # function (avoids FastAPI Depends() / connection-state issues internally).
    companies: list[str] = []
    total_invested = 0
    from_store = await store_portfolio(tenant_id, kg_name, investor_uri=entity_uri)
    if from_store is not None:
        companies = list(from_store[0])
        total_invested = from_store[1]
    total_invested += _absorb_rows(
        await safe_query(client, _portfolio_sparql(instance_graph, investor_name)),
        companies,
    )

    output = {
        "portfolio_count": len(companies),
        "companies": ", ".join(companies),
    }
    if total_invested > 0:
        output["total_invested_usd"] = str(total_invested)

    await _materialize(
        client,
        tenant_id=tenant_id,
        kg_name=kg_name,
        instance_graph=instance_graph,
        ontology_graph=ontology_graph,
        entity_uri=entity_uri,
        output=output,
    )

    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "lambda_invoked",
        function="investor-portfolio",
        entity=entity_uri,
        duration_ms=round(duration_ms, 1),
        portfolio_count=len(companies),
    )
    return {
        "entity_uri": entity_uri,
        "function": "investor-portfolio",
        "output": output,
        "discovered_entities": [],
        "duration_ms": round(duration_ms, 1),
    }


async def _materialize(
    client,
    *,
    tenant_id: str,
    kg_name: str,
    instance_graph: str,
    ontology_graph: str,
    entity_uri: str,
    output: dict,
) -> None:
    """Write the computed portfolio onto the Investor via the shared write path."""
    entity_type = "Investor"
    new_triples: list[tuple[str, str, str]] = []
    replaced_preds: list[str] = []
    for key, value in output.items():
        if value is None:
            continue
        attr_pred = f"{IRI_BASE}/types/{entity_type}/attrs/{key}"
        new_triples.append((entity_uri, attr_pred, str(value)))
        replaced_preds.append(attr_pred)

        # Ensure attribute in ontology (schema graph — unrelated to the instance
        # write below; left as-is).
        datatype = "integer" if key == "portfolio_count" else "string"
        try:
            await commit_ontology(
                client,
                ontology_graph,
                [OntologyMutation(
                    op=OntologyOpKind.UPSERT_ATTRIBUTE,
                    type_name=entity_type,
                    slot_name=key,
                    datatype=datatype,
                    description="Lambda-computed by investor-portfolio",
                )],
            )
        except Exception:
            pass

    # Provenance timestamp
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    lambda_ts_pred = f"{IRI_BASE}/onto/lambda_refreshed_at"
    new_triples.append((entity_uri, lambda_ts_pred, now_iso))
    replaced_preds.append(lambda_ts_pred)

    # Persist via the shared write path (ADR 0007): clear each replaced predicate's
    # prior value, insert the new values, then ONE refresh carrying the touched type.
    if not new_triples:
        return
    await delete_facts(
        client,
        instance_graph,
        triples=[(entity_uri, pred, None) for pred in replaced_preds],
        touched_types=[entity_type],
        reason="lambda re-invoke: investor-portfolio",
    )
    await insert_facts(client, instance_graph, new_triples)
    await refresh_after_write(
        client,
        tenant_id=tenant_id,
        kg_name=kg_name,
        affected_types=[entity_type],
    )


__all__ = ["run_investor_portfolio", "run_invoke_investor_portfolio"]
