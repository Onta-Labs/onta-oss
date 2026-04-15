"""Lambda function endpoints — tier-2 HTTP lambdas and invoke/materialize.

Delivers two capabilities:
1. Concrete tier-2 lambda endpoints (e.g. SEC EDGAR latest-filing lookup)
2. A generic invoke endpoint that runs a registered function against an entity
   and materializes the output as triples on that entity in the KG.
"""

import datetime
import time

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from omnix.api.deps import get_neptune_client
from omnix.auth.api_keys import TenantContext, get_tenant
from omnix.functions.executor import FunctionExecutor
from omnix.graph.client import NeptuneClient
from omnix.graph.ontology_queries import insert_attribute
from omnix.graph.parser import parse_sparql_results
from omnix.graph.queries import (
    insert_triples,
    kg_graph_uri,
    list_functions_query,
    tenant_graph_uri,
)
from omnix.models.function import FunctionRef, FunctionTier

logger = structlog.stdlib.get_logger("omnix.lambda_functions")

router = APIRouter()

# ---------------------------------------------------------------------------
# Tier-2 Lambda: SEC EDGAR latest-filing
# ---------------------------------------------------------------------------

SEC_USER_AGENT = "cograph-demo smoeenmh@gmail.com"


class SECFilingRequest(BaseModel):
    cik: str


class SECFilingResponse(BaseModel):
    latest_filing_date: str | None
    latest_filing_type: str | None
    days_since_last_filing: int | None
    source_url: str


@router.post("/functions/sec-latest-filing", response_model=SECFilingResponse)
async def sec_latest_filing(
    body: SECFilingRequest,
    _tenant: TenantContext = Depends(get_tenant),
):
    """Fetch a company's most recent SEC filing from EDGAR.

    Input: CIK (Central Index Key) as a string.
    Output: latest_filing_date, latest_filing_type, days_since_last_filing, source_url.
    """
    # Zero-pad CIK to 10 digits as required by SEC
    padded_cik = body.cik.lstrip("0").zfill(10)
    source_url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(
                source_url,
                headers={"User-Agent": SEC_USER_AGENT},
            )
        except httpx.RequestError as exc:
            logger.warning("sec_edgar_request_error", cik=padded_cik, error=str(exc))
            return SECFilingResponse(
                latest_filing_date=None,
                latest_filing_type=None,
                days_since_last_filing=None,
                source_url=source_url,
            )

    if resp.status_code == 404:
        return SECFilingResponse(
            latest_filing_date=None,
            latest_filing_type=None,
            days_since_last_filing=None,
            source_url=source_url,
        )

    resp.raise_for_status()
    data = resp.json()

    # Parse filings.recent — arrays of form, filingDate, etc.
    recent = data.get("filings", {}).get("recent", {})
    dates = recent.get("filingDate", [])
    forms = recent.get("form", [])

    if not dates:
        return SECFilingResponse(
            latest_filing_date=None,
            latest_filing_type=None,
            days_since_last_filing=None,
            source_url=source_url,
        )

    # The first entry is the most recent filing
    latest_date_str = dates[0]
    latest_form = forms[0] if forms else None

    try:
        latest_date = datetime.date.fromisoformat(latest_date_str)
        days_since = (datetime.date.today() - latest_date).days
    except ValueError:
        days_since = None

    return SECFilingResponse(
        latest_filing_date=latest_date_str,
        latest_filing_type=latest_form,
        days_since_last_filing=days_since,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Generic function invoke + materialize
# ---------------------------------------------------------------------------

class InvokeRequest(BaseModel):
    entity_uri: str
    kg_name: str


class InvokeResponse(BaseModel):
    entity_uri: str
    function: str
    output: dict
    duration_ms: float


# Shared executor instance
_executor: FunctionExecutor | None = None


def _get_executor() -> FunctionExecutor:
    global _executor
    if _executor is None:
        _executor = FunctionExecutor()
    return _executor


@router.post("/graphs/{tenant}/functions/{function_name}/invoke", response_model=InvokeResponse)
async def invoke_function(
    function_name: str,
    body: InvokeRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Invoke a registered function for one entity and materialize the result as triples.

    Steps:
    1. Look up FunctionRef in the tenant ontology graph
    2. Resolve the entity's filing_cik attribute from the KG
    3. Invoke the function via FunctionExecutor
    4. Write result attributes back as triples on the entity
    """
    start = time.monotonic()
    ontology_graph = tenant_graph_uri(tenant.tenant_id)
    instance_graph = kg_graph_uri(tenant.tenant_id, body.kg_name)

    # --- Step 1: Look up the function definition ---
    sparql = list_functions_query(ontology_graph, entity_type=None)
    raw = await client.query(sparql)
    _, bindings = parse_sparql_results(raw)

    func_ref = None
    for row in bindings:
        if row.get("name") == function_name:
            func_ref = FunctionRef(
                name=row["name"],
                entity_type=row.get("type", "").split("/")[-1],
                endpoint_url=row.get("endpoint"),
                description=row.get("desc", ""),
                tier=FunctionTier.CUSTOM,
            )
            break

    if func_ref is None:
        raise HTTPException(status_code=404, detail=f"Function '{function_name}' not registered")

    # --- Step 2: Resolve the entity's filing_cik from the KG ---
    entity_type = func_ref.entity_type  # e.g. "Company"
    cik_attr_uri = f"https://omnix.dev/types/{entity_type}/attrs/filing_cik"

    # Try direct attribute on the entity
    cik_query = (
        f"SELECT ?cik FROM <{instance_graph}>\n"
        f"WHERE {{\n"
        f"  <{body.entity_uri}> <{cik_attr_uri}> ?cik .\n"
        f"}}"
    )
    raw_cik = await client.query(cik_query)
    _, cik_bindings = parse_sparql_results(raw_cik)

    cik_value = None
    if cik_bindings:
        cik_value = cik_bindings[0].get("cik")

    # Fallback 1: check linked FundingRound entities for a filing_cik attribute
    if not cik_value:
        fallback_query = (
            f"SELECT ?cik FROM <{instance_graph}>\n"
            f"WHERE {{\n"
            f"  ?round ?rel <{body.entity_uri}> .\n"
            f"  ?round <https://omnix.dev/types/FundingRound/attrs/filing_cik> ?cik .\n"
            f"}}"
        )
        raw_fallback = await client.query(fallback_query)
        _, fb_bindings = parse_sparql_results(raw_fallback)
        if fb_bindings:
            cik_value = fb_bindings[0].get("cik")

    # Fallback 2: FundingRound entity label IS the CIK (pear-backyard data pattern)
    if not cik_value:
        label_query = (
            f"SELECT ?label FROM <{instance_graph}>\n"
            f"WHERE {{\n"
            f"  ?round <https://omnix.dev/onto/company_name> <{body.entity_uri}> .\n"
            f"  ?round <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FundingRound> .\n"
            f"  ?round <http://www.w3.org/2000/01/rdf-schema#label> ?label .\n"
            f"}}\n"
            f"LIMIT 1"
        )
        raw_label = await client.query(label_query)
        _, label_bindings = parse_sparql_results(raw_label)
        if label_bindings:
            candidate = label_bindings[0].get("label", "")
            # Verify it looks like a CIK (all digits, possibly zero-padded)
            if candidate.lstrip("0").isdigit():
                cik_value = candidate

    if not cik_value:
        raise HTTPException(
            status_code=422,
            detail=f"Could not resolve filing_cik for entity {body.entity_uri}",
        )

    # --- Step 3: Invoke the function ---
    executor = _get_executor()
    payload = {"cik": cik_value}
    # Pass the caller's API key so the tier-2 endpoint can authenticate
    invoke_headers = {"X-API-Key": tenant.api_key}
    result = await executor.invoke(func_ref, payload, headers=invoke_headers)
    output = result.output

    # --- Step 4: Materialize result as triples on the entity ---
    new_triples: list[tuple[str, str, str]] = []
    for key, value in output.items():
        if value is None:
            continue
        attr_pred = f"https://omnix.dev/types/{entity_type}/attrs/{key}"
        new_triples.append((body.entity_uri, attr_pred, str(value)))

        # Ensure the attribute exists in the ontology
        # (use DELETE+INSERT pattern: first delete old value if any, then insert new)
        datatype = "string"
        if isinstance(value, int):
            datatype = "integer"
        elif isinstance(value, float):
            datatype = "float"
        attr_sparql = insert_attribute(
            ontology_graph, entity_type, key,
            description=f"Lambda-computed by {function_name}",
            datatype=datatype,
        )
        try:
            await client.update(attr_sparql)
        except Exception:
            pass  # attribute may already exist

    # Add provenance triple
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_triples.append((
        body.entity_uri,
        "https://omnix.dev/onto/lambda_refreshed_at",
        now_iso,
    ))

    # Delete any existing lambda-computed attributes for this entity first
    # to avoid duplicates on re-invoke
    for key, value in output.items():
        if value is None:
            continue
        attr_pred = f"https://omnix.dev/types/{entity_type}/attrs/{key}"
        delete_sparql = (
            f"DELETE {{ GRAPH <{instance_graph}> {{ <{body.entity_uri}> <{attr_pred}> ?old }} }}\n"
            f"WHERE {{ GRAPH <{instance_graph}> {{ <{body.entity_uri}> <{attr_pred}> ?old }} }}"
        )
        try:
            await client.update(delete_sparql)
        except Exception:
            pass  # no existing value — fine

    # Also delete old provenance
    delete_prov = (
        f"DELETE {{ GRAPH <{instance_graph}> {{ <{body.entity_uri}> <https://omnix.dev/onto/lambda_refreshed_at> ?old }} }}\n"
        f"WHERE {{ GRAPH <{instance_graph}> {{ <{body.entity_uri}> <https://omnix.dev/onto/lambda_refreshed_at> ?old }} }}"
    )
    try:
        await client.update(delete_prov)
    except Exception:
        pass

    # Insert the new triples into the KG-specific graph
    if new_triples:
        sparql_insert = insert_triples(instance_graph, new_triples)
        await client.update(sparql_insert)

    duration_ms = (time.monotonic() - start) * 1000

    # TODO(lambda-scheduler): re-invoke stale entries every N seconds
    # A scheduler would scan for entities with lambda_refreshed_at older
    # than a configurable threshold and re-invoke the attached functions.

    logger.info(
        "lambda_invoked",
        function=function_name,
        entity=body.entity_uri,
        duration_ms=round(duration_ms, 1),
        output_keys=list(output.keys()),
    )

    return InvokeResponse(
        entity_uri=body.entity_uri,
        function=function_name,
        output=output,
        duration_ms=round(duration_ms, 1),
    )
