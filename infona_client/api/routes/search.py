"""Canonical semantic instance search — the ONE ``/search`` route (ONTA-178).

``POST /graphs/{tenant}/search`` is the single user surface of the semantic
instance index (ONTA-173): "which entities talk about X?" answered from the
derived chunk index with **no Neptune round-trip**. Since ONTA-421 it also
answers "which entity is CALLED X?": every named entity carries a small
identity doc (``semantic/extract.py``) that rides the LEXICAL leg, so an exact
name matches even though names are far too short to be marked as free text.
Substring and fuzzy name matching are still NOT this route's job — the
index-free ``POST /graphs/{tenant}/grep`` owns those (``routes/grep.py``). Per the
interface-convergence rule (CLAUDE.md), every client — the Explorer webapp,
the CLI, the MCP ``search`` tool, the ``cograph`` SDK — rides THIS route;
none may mint a bespoke endpoint or re-implement ranking client-side, because
divergent per-interface search paths are exactly the drift the rule exists to
prevent (the COG-128 lesson).

Division of labor (the ONTA-176 locked contract):

* **This route embeds the query** via the shared embed client
  (``nlp.embed_client``, ONTA-174) and passes the vector down as
  ``query_embedding``. The index itself NEVER calls an embedding API — it only
  consumes vectors. When the query cannot be embedded (no OpenRouter key
  configured, or the embed call fails), the route passes
  ``query_embedding=None`` and the backend runs **lexical-only**, returning
  ``degraded=True`` — surfaced verbatim in the response body so a UI can badge
  "reduced recall" instead of users mistaking a degraded answer for a complete
  one. An embed hiccup therefore degrades the answer, never 500s it.
* **The index ranks**: hybrid FTS + vector legs fused RRF-style, grouped by
  entity (see ``semantic/protocol.py``); this route adds auth, validation and
  the response envelope, nothing ranking-related.

Documented semantics (each is tested in ``tests/test_search_route.py``):

* **Semantic gate off → lexical degrade, NOT 503.** When
  ``COGRAPH_SEMANTIC_INDEX_ENABLED`` (the master gate for the write hook and
  the reconciler) is off, the vector leg simply never populates — but the
  backend's lexical/full-text leg still works, so this read path DEGRADES to
  keyword-only search and returns ``degraded=True`` (an honest "reduced
  recall" badge) instead of a hard 503 dead-end. Forcing the query embedding
  to ``None`` guarantees the lexical path even on a deployment that happens to
  have a stray query key configured. A gated-off deployment therefore still
  answers "which entities mention X?" from whatever the FTS leg can match
  (possibly empty, but always a 200), matching the degraded rendering the MCP
  ``search`` tool already shows. (Historically this route hard-503'd here; that
  removed a natural keyword fallback for no correctness benefit — the index is
  never *queried* semantically when the gate is off anyway.)
* **Empty/whitespace query → 400.** Chosen over "empty results" because a
  blank query is always a caller bug (a UI submitting an empty box), and an
  empty 200 would hide it; 400 with a clear message is actionable.
* **Unknown ``kg_name`` → empty results, NOT 404/500.** The index is the
  authority on what is indexed; a KG that doesn't exist and a KG with no
  indexed text are indistinguishable here by design (checking Neptune for
  existence would cost the round-trip this index exists to avoid).
* **``top_k`` clamped to [1, TOP_K_MAX(=50)]**, silently, with the effective
  value echoed back as ``top_k`` so clamping is observable. 50 matches the
  per-leg candidate budget (``_CANDIDATES_PER_LEG``) — the ranker never
  produces more than 50 entities per leg, so a larger ``top_k`` could not be
  honored anyway.
* **Type filter staleness caveat:** ``type`` matches the DENORMALIZED
  ``attrs["type"]`` written onto each chunk at index time. If an entity's type
  changes later without its marked text changing, the filter matches the stale
  value until the hourly ONTA-181 reconciler re-upserts the doc. Accepted cost
  of zero-join hits; documented here so clients don't treat the filter as
  transactionally consistent with Neptune.
* **``entity_uris`` allowlist (structured pre-filter):** callers that first
  resolve a candidate set via SPARQL / own logic can pass those entity URIs so
  hybrid ranking only considers that set. Filters apply **inside** FTS + vector
  legs before LIMIT (AND with ``kg_name`` / ``type``), not as a post-hoc top_k
  shrink. Semantics:

  * omit / ``null`` → no URI filter (unrestricted);
  * non-empty list → only those URIs may rank (blanks dropped, duplicates
    deduped server-side);
  * empty list ``[]`` → **zero hits, 200** (strict empty allowlist — not a
    400: the structured pre-filter produced no candidates);
  * more than ``ENTITY_URIS_MAX`` (=500) unique URIs after clean → **400**
    with a clear message (refuse oversized allowlists rather than silently
    clamp, so callers notice they need to narrow upstream).
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.config import settings
from infona_client.semantic.protocol import SemanticHit

logger = structlog.stdlib.get_logger("cograph.api.search")

router = APIRouter(prefix="/graphs/{tenant}")

#: Hard ceiling for ``top_k`` (entities per response). Matches the per-leg
#: candidate budget of both backends (``memory._CANDIDATES_PER_LEG`` — the SQL
#: backend's two top-50 CTEs): asking for more entities than a leg can supply
#: would silently under-fill, so the cap is honest as well as protective.
TOP_K_MAX = 50
TOP_K_DEFAULT = 10

#: Hard ceiling for ``entity_uris`` unique entries after blank-strip + dedupe.
#: Structured pre-filters that need more should narrow upstream (SPARQL LIMIT
#: / paging); refusing with 400 is observable, silent clamp is not.
ENTITY_URIS_MAX = 500


class SearchRequest(BaseModel):
    """Body of ``POST /graphs/{tenant}/search`` (the canonical search op)."""

    query: str = Field(
        ...,
        description=(
            "Free-text query. Must contain at least one non-whitespace "
            "character (blank queries are a 400)."
        ),
    )
    kg_name: Optional[str] = Field(
        None,
        description=(
            "Narrow the search to one knowledge graph. Omit/null/empty = every "
            "KG in the tenant. An unknown KG yields empty results (see module "
            "docs), never an error."
        ),
    )
    type: Optional[str] = Field(
        None,
        description=(
            "Filter hits to entities whose denormalized display type equals "
            "this value (e.g. 'Speech'). NOTE: the type is denormalized onto "
            "chunks at write time and repaired hourly by the reconciler, so a "
            "recent type change may match stale values (ONTA-178 docs)."
        ),
    )
    entity_uris: Optional[list[str]] = Field(
        None,
        description=(
            "Strict allowlist of entity URIs that may participate in ranking. "
            "Omit/null = no URI filter; empty list = zero hits (200); "
            f"more than {ENTITY_URIS_MAX} unique URIs after blank-strip + "
            "dedupe = 400. Combined with kg_name/type via AND; applied inside "
            "ranking legs before LIMIT."
        ),
    )
    top_k: int = Field(
        TOP_K_DEFAULT,
        description=(
            f"Maximum entities to return. Clamped server-side to "
            f"[1, {TOP_K_MAX}]; the effective value is echoed in the response."
        ),
    )


class SearchResponse(BaseModel):
    """Entity-grouped hits + the explicit degraded flag.

    ``hits`` reuses :class:`~infona_client.semantic.protocol.SemanticHit`
    directly (entity_uri, attrs, snippet, attr, score) — one shape from the
    index protocol to the wire, so route and backend cannot drift.
    """

    hits: list[SemanticHit] = Field(default_factory=list)
    #: Number of entities returned (== len(hits); explicit so thin clients can
    #: render "N results" without re-counting a streamed body).
    count: int = 0
    #: True when the query ran lexical-only (query embedding unavailable —
    #: embed service down/unconfigured, or a dimension mismatch downstream).
    #: Propagated verbatim from the index so no client mistakes reduced recall
    #: for a complete answer.
    degraded: bool = False
    #: The EFFECTIVE top_k after clamping to [1, TOP_K_MAX] — echoes what the
    #: ranker was actually asked for, making the documented clamp observable.
    top_k: int = TOP_K_DEFAULT


async def _embed_query(query: str) -> Optional[list[float]]:
    """Embed the query via the shared embed client; ``None`` on any failure.

    THE one place the search read-path touches an embedding API — the index
    never does (locked ONTA-176 contract). ``None`` (no key configured, or the
    call failed) makes the backend run lexical-only and flag ``degraded=True``:
    an embedding outage degrades recall, it must never 500 the search.
    """
    api_key = settings.openrouter_api_key
    if not api_key:
        # Lexical-only deployment — same loud-not-silent stance as the
        # reconciler's embed-fill sweep (semantic_embed_fill_no_api_key).
        logger.info("semantic_search_no_embed_key")
        return None
    try:
        # Lazy import mirrors the reconciler: keeps httpx/numpy paths out of
        # module import for deployments that never enable the index.
        from infona_client.nlp.embed_client import embed_texts

        vectors = await embed_texts([query], api_key=api_key)
        return list(vectors[0]) if vectors else None
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the search
        logger.warning(
            "semantic_search_query_embed_failed", error=str(exc)[:500]
        )
        return None


@router.post("/search", response_model=SearchResponse)
async def semantic_search(
    body: SearchRequest,
    tenant: TenantContext = Depends(get_tenant),
) -> SearchResponse:
    """Hybrid semantic search over the tenant's indexed free-text attributes.

    Auth is the same ``get_tenant`` dependency as every ``/graphs/{tenant}``
    route: the search is ALWAYS scoped to the resolved tenant (a multi-tenant
    key requesting an unowned path tenant is a 403 before this body runs), so
    the index's tenant-isolation contract starts here. See the module
    docstring for the full documented semantics (lexical-degrade when the gate
    is off, 400 on blank query, unknown-KG-empty, top_k clamp, type-staleness
    caveat).
    """
    # Master env gate — same knob (ONTA-181) that governs the write hook and
    # the reconciler. When it is OFF, the semantic (vector) leg is not
    # maintained, so we run the backend's lexical/full-text leg only and flag
    # the result ``degraded=True`` rather than 503'ing: a keyword fallback is
    # strictly more useful than a hard dead-end, and it matches the degraded
    # rendering the MCP ``search`` tool already surfaces.
    from infona_client.semantic.reconciler import semantic_index_enabled

    gate_on = semantic_index_enabled()

    query = body.query.strip()
    if not query:
        raise HTTPException(
            status_code=400,
            detail="query must contain at least one non-whitespace character",
        )

    top_k = max(1, min(body.top_k, TOP_K_MAX))

    # entity_uris: None = no filter; list = strict allowlist (empty → zero hits).
    # Strip blanks + dedupe (order-preserving); refuse oversized lists with 400
    # rather than silent clamp so callers see they need to narrow upstream.
    entity_uris: Optional[list[str]]
    if body.entity_uris is None:
        entity_uris = None
    else:
        seen: set[str] = set()
        cleaned: list[str] = []
        for u in body.entity_uris:
            if not u:
                continue
            s = u.strip()
            if not s or s in seen:
                continue
            seen.add(s)
            cleaned.append(s)
        if len(cleaned) > ENTITY_URIS_MAX:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"entity_uris exceeds max of {ENTITY_URIS_MAX} unique URIs "
                    f"(got {len(cleaned)} after blank-strip + dedupe)"
                ),
            )
        entity_uris = cleaned

    # Empty allowlist is a guaranteed empty result — skip embed + index work.
    if entity_uris is not None and len(entity_uris) == 0:
        return SearchResponse(
            hits=[],
            count=0,
            # Lexical would also be empty; flag degraded=False (honest zero set).
            degraded=False,
            top_k=top_k,
        )

    # Only embed the query when the semantic leg is actually maintained. With
    # the gate off, force ``None`` so the backend runs lexical-only (and
    # reports ``degraded=True``) instead of scoring against a stale/empty
    # vector index.
    query_embedding = await _embed_query(query) if gate_on else None

    from infona_client.semantic.registry import get_semantic_index

    result = await get_semantic_index().search(
        tenant.tenant_id,
        query,
        query_embedding=query_embedding,
        # Empty strings normalize to None ("all KGs" / "no filter") so thin
        # clients forwarding blank form fields don't accidentally filter to a
        # KG literally named "".
        kg_name=body.kg_name or None,
        type_filter=body.type or None,
        entity_uris=entity_uris,
        top_k=top_k,
    )
    return SearchResponse(
        hits=result.hits,
        count=len(result.hits),
        degraded=result.degraded,
        top_k=top_k,
    )
