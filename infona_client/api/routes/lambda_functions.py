"""Lambda function endpoints — tier-2 HTTP lambdas and invoke/materialize.

Delivers two capabilities:
1. Concrete tier-2 lambda endpoints (e.g. SEC EDGAR latest-filing lookup)
2. A generic invoke endpoint that runs a registered function against an entity
   and materializes the output as triples on that entity in the KG.
"""

from infona_client.graph.iri import IRI_BASE
import datetime
import time

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.lambda_functions_investor import (
    run_investor_portfolio,
    run_invoke_investor_portfolio,
)
from infona_client.api.routes.lambda_functions_reads import (
    safe_query as _safe_query,
    store_discover_investors,
    store_entity_name,
    store_function_refs,
    store_portfolio,
    store_resolve_cik,
)
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.auth.access import require_tenant_write
from infona_client.config import settings
from infona_client.functions.executor import FunctionExecutor
from infona_client.graph.client import NeptuneClient
from infona_client.graph.kg_writer import delete_facts, insert_facts, refresh_after_write
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.models.ontology import OntologyMutation, OntologyOpKind
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.provenance import (
    attr_provenance_companion_uri,
    legacy_attr_companion_uri,
)
from infona_client.graph.queries import (
    kg_graph_uri,
    list_functions_query,
    tenant_graph_uri,
)
from infona_client.models.function import FunctionRef, FunctionTier
from infona_client.resolver.validator import _typed_value

logger = structlog.stdlib.get_logger("infona.lambda_functions")

router = APIRouter()

# ---------------------------------------------------------------------------
# Tier-2 Lambda: SEC EDGAR latest-filing
# ---------------------------------------------------------------------------

#: Fallback User-Agent when the deployment has not declared its own contact.
#: Impersonal by design (a published OSS build must never issue EDGAR requests
#: under an individual's identity) but still a WORKING contact, because SEC's
#: fair-access policy throttles clients that declare none. Same
#: ``project/version (+repo; role@domain)`` shape the Wikidata adapter uses.
DEFAULT_SEC_USER_AGENT = (
    "infona-client/0.1 (+https://github.com/infona-ai/infona-oss; ops@infona.ai)"
)

_sec_ua_warned = False


def sec_user_agent() -> str:
    """The User-Agent for SEC EDGAR requests.

    SEC's fair-access policy asks automated clients to identify themselves with
    a working contact and throttles those that do not, so every deployment
    should set ``INFONA_SEC_USER_AGENT`` to its own address. Unset falls back to
    :data:`DEFAULT_SEC_USER_AGENT` and warns once per process rather than
    borrowing anyone else's contact.
    """
    global _sec_ua_warned

    configured = (settings.sec_user_agent or "").strip()
    if configured:
        return configured

    if not _sec_ua_warned:
        _sec_ua_warned = True
        logger.warning(
            "sec_user_agent_unset",
            fallback=DEFAULT_SEC_USER_AGENT,
            hint="set INFONA_SEC_USER_AGENT to your own contact (SEC EDGAR "
            "throttles clients that do not declare one)",
        )
    return DEFAULT_SEC_USER_AGENT


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
    _tenant: TenantContext = Depends(require_tenant_write),
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
                headers={"User-Agent": sec_user_agent()},
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


class DiscoveredEntity(BaseModel):
    """An entity found by cascade discovery, plus the functions attached to its type.

    `functions` holds callable FUNCTION names (e.g. ``"filings()"``) registered for
    the entity's type — executable code, not prose.

    DEPRECATED: `skills` is a legacy alias of `functions`, kept only so existing
    clients of the `/graphs/{tenant}/functions/{name}/invoke` response keep working.
    It is populated identically to `functions` and will be removed in a future
    release — read `functions` instead. The name is a misnomer: in this product a
    "skill" is now type-attached human-authored markdown PROSE consumed by LM agents
    (``infona_client.skills``), which is the opposite of an executable function.
    """

    uri: str
    type: str
    name: str
    functions: list[str]
    skills: list[str]


class InvokeResponse(BaseModel):
    entity_uri: str
    function: str
    output: dict
    discovered_entities: list[DiscoveredEntity] = []
    duration_ms: float


# Hardcoded FUNCTION-name mapping per entity type (mirrors frontend TypeNode
# METHOD_MAP). These are callable function names — note the parentheses — not
# "skills" in the type-attached-markdown-prose sense (infona_client.skills).
FUNCTIONS_BY_TYPE: dict[str, list[str]] = {
    "Company": ["filings()", "patents()", "headcount()", "news()"],
    "Investor": ["portfolio()", "coInvestors()"],
    "Person": ["publications()", "bio()", "trajectory()"],
    "FundingRound": ["coInvestors()", "capTable()"],
}


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
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Invoke a registered function for one entity and materialize the result as triples.

    Steps:
    1. Look up FunctionRef in the tenant ontology graph
    2. Resolve the entity's filing_cik attribute from the KG
    3. Invoke the function via FunctionExecutor
    4. Write result attributes back as triples on the entity
    """
    # Dispatch to specific invoke endpoints for functions that have their
    # own resolution logic (e.g., investor-portfolio resolves by name, not CIK)
    if function_name == "investor-portfolio":
        return await invoke_investor_portfolio(body, tenant, client)

    start = time.monotonic()
    ontology_graph = tenant_graph_uri(tenant.tenant_id)
    instance_graph = kg_graph_uri(tenant.tenant_id, body.kg_name)

    # --- Step 1: Look up the function definition ---
    # Function store FIRST (ONTA-534) — the same registry `GET
    # /graphs/{t}/functions` lists from, so invoke and list cannot disagree
    # about what is registered. The residual SPARQL arm still supplements it
    # (dual-arm tests), and its failure is swallowed: a retired client must
    # leave this as the 404 it always was, not turn it into a 500.
    func_ref = None
    for rec in await store_function_refs(tenant.tenant_id):
        if rec.name == function_name:
            func_ref = FunctionRef(
                name=rec.name,
                entity_type=rec.entity_type,
                endpoint_url=rec.endpoint_url,
                description=rec.description,
                tier=FunctionTier.CUSTOM,
            )
            break

    if func_ref is None:
        sparql = list_functions_query(ontology_graph, entity_type=None)
        for row in await _safe_query(client, sparql):
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

    # --- Step 2: Resolve the entityf's filing_cik from the KG ---
    # GraphStore ladder FIRST (ONTA-534) — the same three rungs, over the same
    # entity read the Explorer panel renders. Every rung below was a bare
    # `client.query`, so on the shipped backend this 500'd before the 422 it
    # was supposed to reach. The SPARQL rungs stay as the residual arm and their
    # failures are swallowed: an unresolvable CIK is a 422, never a 500.
    entity_type = func_ref.entity_type  # e.g. "Company"
    cik_attr_uri = f"{IRI_BASE}/types/{entity_type}/attrs/filing_cik"

    cik_value = await store_resolve_cik(
        tenant.tenant_id, body.kg_name, body.entity_uri
    )

    # Try direct attribute on the entity
    if not cik_value:
        cik_query = (
            f"SELECT ?cik FROM <{instance_graph}>\n"
            f"WHERE {{\n"
            f"  <{body.entity_uri}> <{cik_attr_uri}> ?cik .\n"
            f"}}"
        )
        cik_bindings = await _safe_query(client, cik_query)
        if cik_bindings:
            cik_value = cik_bindings[0].get("cik")

    # Fallback 1: check linked FundingRound entities for a filing_cik attribute
    if not cik_value:
        fallback_query = (
            f"SELECT ?cik FROM <{instance_graph}>\n"
            f"WHERE {{\n"
            f"  ?round ?rel <{body.entity_uri}> .\n"
            f"  ?round <{IRI_BASE}/types/FundingRound/attrs/filing_cik> ?cik .\n"
            f"}}"
        )
        fb_bindings = await _safe_query(client, fallback_query)
        if fb_bindings:
            cik_value = fb_bindings[0].get("cik")

    # Fallback 2: FundingRound entity label IS the CIK (pear-backyard data pattern)
    if not cik_value:
        label_query = (
            f"SELECT ?label FROM <{instance_graph}>\n"
            f"WHERE {{\n"
            f"  ?round <{IRI_BASE}/onto/company_name> <{body.entity_uri}> .\n"
            f"  ?round <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{IRI_BASE}/types/FundingRound> .\n"
            f"  ?round <http://www.w3.org/2000/01/rdf-schema#label> ?label .\n"
            f"}}\n"
            f"LIMIT 1"
        )
        label_bindings = await _safe_query(client, label_query)
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
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_triples: list[tuple[str, str, str]] = []
    replaced_preds: list[str] = []
    # ONTA-236 value history: the NEW value each PRIMARY attribute predicate is
    # replaced WITH, so delete_facts can version a genuine old→new change (gated by
    # INFONA_VALUE_HISTORY_ENABLED). Only PRIMARY attributes are tracked — the
    # per-fact `_verified_at` / per-entity `lambda_refreshed_at` companions advance
    # every run by design, so treating their clock ticks as "value changes" would
    # be noise, not signal.
    new_value_by_pred: dict[tuple[str, str], str] = {}
    for key, value in output.items():
        if value is None:
            continue
        attr_pred = f"{IRI_BASE}/types/{entity_type}/attrs/{key}"
        new_triples.append((body.entity_uri, attr_pred, str(value)))
        replaced_preds.append(attr_pred)
        new_value_by_pred[(body.entity_uri, attr_pred)] = str(value)

        # Per-fact freshness stamp: a QUERYABLE `verified_at` companion on the
        # attr_meta metadata namespace, mirroring enrichmentf's _provenance_triples
        # (shared shape via attr_provenance_companion_uri — ONTA-262: companions
        # are metadata OF the attribute, never ontology-declared attributes). This
        # is per-FACT (each lambda-computed attribute gets its own stamp), unlike
        # the per-ENTITY, system-hidden `onto/lambda_refreshed_at` below — so the
        # query layer can filter "verified in the last N days" per attribute.
        # Written as a TYPED xsd:dateTime literal (via _typed_value): the NL
        # planner emits typed comparisons, so an untyped string would be
        # type-incompatible and the row would be silently dropped. Both the
        # current attr_meta shape and the legacy `attrs/<key>_verified_at` shape
        # are cleared on replace, so a re-run on a pre-migration entity retires
        # its stale legacy stamp instead of accreting beside it. Full value
        # history is out of scope (a separate deferred ticket); just freshness.
        verified_at_pred = attr_provenance_companion_uri(
            entity_type, key, "verified_at"
        )
        new_triples.append(
            (body.entity_uri, verified_at_pred, _typed_value(now_iso, "datetime"))
        )
        replaced_preds.append(verified_at_pred)
        replaced_preds.append(
            legacy_attr_companion_uri(entity_type, key, "verified_at")
        )

        # Ensure the attribute exists in the ontology (schema graph — unrelated to
        # the instance-fact write below; left as-is). Only the PRIMARY attribute
        # is declared — the freshness companion is attr_meta metadata (ONTA-262),
        # deliberately absent from the ontology so it never renders as a column.
        datatype = "string"
        if isinstance(value, int):
            datatype = "integer"
        elif isinstance(value, float):
            datatype = "float"
        try:
            await commit_ontology(
                client,
                ontology_graph,
                [OntologyMutation(
                    op=OntologyOpKind.UPSERT_ATTRIBUTE,
                    type_name=entity_type,
                    slot_name=key,
                    datatype=datatype,
                    description=f"Lambda-computed by {function_name}",
                )],
            )
        except Exception:
            pass  # attribute may already exist

    # Add provenance triple
    lambda_ts_pred = f"{IRI_BASE}/onto/lambda_refreshed_at"
    new_triples.append((body.entity_uri, lambda_ts_pred, now_iso))
    replaced_preds.append(lambda_ts_pred)

    # Persist via the shared write path (ADR 0007): an attribute update = clear the
    # old value + insert the new. delete_facts with object=None is a predicate-scoped
    # delete (drops any prior value of each replaced predicate, no-op when absent),
    # insert_facts writes the new values batched, and ONE refresh_after_write carries
    # the touched type — so this write fans out (index/cache/stats) like every other.
    if new_triples:
        await delete_facts(
            client,
            instance_graph,
            triples=[(body.entity_uri, pred, None) for pred in replaced_preds],
            new_values=new_value_by_pred,
            touched_types=[entity_type],
            reason=f"lambda re-invoke: {function_name}",
        )
        await insert_facts(client, instance_graph, new_triples)
        await refresh_after_write(
            client,
            tenant_id=tenant.tenant_id,
            kg_name=body.kg_name,
            affected_types=[entity_type],
        )

    # --- Step 5: Discover linked entities for cascade ---
    discovered: list[DiscoveredEntity] = []

    if function_name == "sec-latest-filing":
        # Find Investor entities linked via FundingRound → lead_investor
        discover_query = (
            f"SELECT DISTINCT ?investor ?investorName FROM <{instance_graph}>\n"
            f"WHERE {{\n"
            f"  ?round <{IRI_BASE}/onto/company_name> <{body.entity_uri}> .\n"
            f"  ?round <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{IRI_BASE}/types/FundingRound> .\n"
            f"  ?round <{IRI_BASE}/onto/lead_investor> ?investor .\n"
            f"  ?investor <http://www.w3.org/2000/01/rdf-schema#label> ?investorName .\n"
            f"}}"
        )
        # Store cascade first, SPARQL supplement second; both already fail-soft.
        seen_investors: set[str] = set()
        try:
            rows = await store_discover_investors(
                tenant.tenant_id, body.kg_name, body.entity_uri
            )
            rows += [
                (r.get("investor", ""), r.get("investorName", ""))
                for r in await _safe_query(client, discover_query)
            ]
            for inv_uri, inv_name in rows:
                if not inv_uri or not inv_name or inv_uri in seen_investors:
                    continue
                seen_investors.add(inv_uri)
                inv_type = "Investor"
                inv_functions = FUNCTIONS_BY_TYPE.get(inv_type, [])
                discovered.append(DiscoveredEntity(
                    uri=inv_uri,
                    type=inv_type,
                    name=inv_name,
                    functions=inv_functions,
                    # Deprecated alias — populated identically to `functions`
                    # so existing invoke-response clients keep working.
                    skills=inv_functions,
                ))
        except Exception as exc:
            logger.warning("discover_entities_failed", error=str(exc))

    duration_ms = (time.monotonic() - start) * 1000

    logger.info(
        "lambda_invoked",
        function=function_name,
        entity=body.entity_uri,
        duration_ms=round(duration_ms, 1),
        output_keys=list(output.keys()),
        discovered_count=len(discovered),
    )

    return InvokeResponse(
        entity_uri=body.entity_uri,
        function=function_name,
        output=output,
        discovered_entities=discovered,
        duration_ms=round(duration_ms, 1),
    )


# ---------------------------------------------------------------------------
# Tier-2 Lambda: Investor Portfolio (no external API)
#
# Bodies live in ``lambda_functions_investor`` (ONTA-534 — this file is at its
# size pin); the ROUTES stay here so the router and its registration order are
# unchanged. ``invoke_function`` above dispatches ``investor-portfolio`` to
# ``invoke_investor_portfolio`` before the generic path runs.
# ---------------------------------------------------------------------------

class PortfolioRequest(BaseModel):
    investor_name: str


class PortfolioResponse(BaseModel):
    portfolio_count: int
    companies: list[str]
    total_invested_usd: int | None


@router.post("/functions/investor-portfolio", response_model=PortfolioResponse)
async def investor_portfolio(
    body: PortfolioRequest,
    _tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Query the KG for all companies in an investor's portfolio.

    Looks up FundingRound entities where lead_investor matches this investor,
    then follows company_name relationships to get Company names and sums
    amounts. Returns 503 rather than an empty portfolio when the graph cannot
    be read, so a zero count always means a zero count.
    """
    return PortfolioResponse(
        **await run_investor_portfolio(client, _tenant.tenant_id, body.investor_name)
    )


@router.post(
    "/graphs/{tenant}/functions/investor-portfolio/invoke",
    response_model=InvokeResponse,
)
async def invoke_investor_portfolio(
    body: InvokeRequest,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Invoke investor-portfolio for an Investor entity.

    Resolves the investor name from the entity URI, walks the KG for portfolio
    data, and materializes the results as triples through the shared write path.
    """
    return InvokeResponse(
        **await run_invoke_investor_portfolio(
            client, tenant.tenant_id, body.entity_uri, body.kg_name
        )
    )
