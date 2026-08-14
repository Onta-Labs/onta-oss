"""Ground an NL sketch + ranked paths into a structured :class:`GroundedAskPlan`.

Looked up on :mod:`infona_client.nlp.ontology_subgraph_match` at call time via
``_host()`` when a sibling needs a patchable name (``extract_nl_sketch``,
``candidate_ontology_paths``, ``rank_paths``, ``_unique_winner``,
``_safe_params``, ``ground_ask_plan``).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from infona_client.graph.rdfs_helpers import (
    TEMPLATE_ENTITIES_OF_TYPE,
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    type_names_with_subclasses,
)
from infona_client.nlp.cypher_generate import (
    DEFAULT_LIST_LIMIT,
    _SAFE_PROP_RE,
    extract_type_names_from_ontology,
    resolve_type_name,
)
from infona_client.nlp.ontology_subgraph_types import (
    GroundedAskPlan,
    RankedPath,
    _DEFAULT_MAX_HOPS,
    _LOCATIVE_CUES,
    _SAFE_PARAM_KEYS,
    _host,
)


def _safe_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only allowlisted keys; validate prop-like strings."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in _SAFE_PARAM_KEYS:
            continue
        if k in ("rel_attr", "prop_key") and isinstance(v, str):
            if not _SAFE_PROP_RE.match(v):
                continue
        if k == "type_names":
            if not isinstance(v, (list, tuple)):
                continue
            cleaned = [t for t in v if isinstance(t, str) and t]
            if not cleaned:
                continue
            out[k] = list(cleaned)
            continue
        if k == "target_name":
            if not isinstance(v, str) or not v.strip():
                continue
            # Value is a param, never interpolated into Cypher — allow free text.
            out[k] = v.strip()
            continue
        if k == "limit":
            try:
                n = int(v)
            except (TypeError, ValueError):
                n = DEFAULT_LIST_LIMIT
            out[k] = max(1, min(n, 200))
            continue
        out[k] = v
    return out


def ground_ask_plan(
    question: str,
    ontology_summary: str,
    *,
    type_names: Sequence[str] | None = None,
    mention_index: Any | None = None,
    query_embedding: Any | None = None,
    embedder: Any | None = None,
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> GroundedAskPlan | None:
    """End-to-end: sketch → paths → rank → safe plan (or fail-closed).

    Returns ``None`` only when the question/ontology is empty or no subject
    type can be resolved. Ambiguous path ranking still returns a plan with
    ``confidence="ambiguous"`` and ``path=None`` so the LLM can see the shortlist.

    Optional ``mention_index`` / ``query_embedding`` / sync ``embedder`` feed
    ranking only — never skip the LLM. Multi-hop unique winners are
    prompt-only (no multi-hop allowlisted template).
    """
    h = _host()
    q = (question or "").strip()
    if not q:
        return None
    if not (ontology_summary or "").strip():
        return None

    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )
    if not names:
        return None

    sketch = h.extract_nl_sketch(q)

    # Resolve subject type from first type mention (no invent).
    subject: str | None = None
    for mention in sketch.type_mentions:
        hit = resolve_type_name(mention, names, ontology_summary)
        if hit is not None:
            subject = hit
            break

    if subject is None and not sketch.type_mentions:
        # No type mention extracted — cannot ground.
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent,
            sketch=sketch,
            confidence="none",
            explanation="No type mention extracted from NL.",
        )
    if subject is None:
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent,
            sketch=sketch,
            confidence="none",
            explanation=(
                f"Type mention(s) {list(sketch.type_mentions)!r} did not resolve "
                f"to a known ontology type."
            ),
        )

    expanded = type_names_with_subclasses(
        subject, ontology_summary=ontology_summary, include_subclasses=True
    )
    value = sketch.value_mentions[0] if sketch.value_mentions else None

    # No relational cue / value → bare type count or list.
    if not sketch.rel_cues and not value:
        if sketch.intent == "count":
            return GroundedAskPlan(
                question=q,
                intent="count",
                sketch=sketch,
                subject_type=subject,
                path=None,
                value=None,
                template=TEMPLATE_ENTITIES_OF_TYPE_COUNT,
                params=h._safe_params({"type_names": expanded}),
                confidence="unique",
                explanation=f"Count entities of type {subject}.",
            )
        if sketch.intent == "list":
            return GroundedAskPlan(
                question=q,
                intent="list",
                sketch=sketch,
                subject_type=subject,
                path=None,
                value=None,
                template=TEMPLATE_ENTITIES_OF_TYPE,
                params=h._safe_params(
                    {
                        "type_names": expanded,
                        "after_id": None,
                        "limit": DEFAULT_LIST_LIMIT,
                    }
                ),
                confidence="unique",
                explanation=f"List entities of type {subject}.",
            )
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent,
            sketch=sketch,
            subject_type=subject,
            confidence="none",
            explanation="Subject type resolved but no intent/path to ground.",
        )

    # Path ranking for related-entity name filter shapes (1-hop + multi-hop).
    paths = h.candidate_ontology_paths(
        ontology_summary,
        names,
        domain_type=subject,
        max_hops=max_hops,
    )
    ranked = h.rank_paths(
        sketch,
        paths,
        subject_type=subject,
        embedder=embedder,
        mention_index=mention_index,
        query_embedding=query_embedding,
    )
    winner, confidence = h._unique_winner(ranked)

    # Explicit dim that failed to rank any path → do not invent.
    if sketch.dim_mentions and not ranked:
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent if sketch.intent != "unknown" else "list",
            sketch=sketch,
            subject_type=subject,
            path=None,
            value=value,
            confidence="none",
            ranked_paths=[],
            explanation=(
                f"Dim mention(s) {list(sketch.dim_mentions)!r} matched no "
                f"relationship on {subject}."
            ),
        )

    if confidence == "ambiguous":
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent if sketch.intent != "unknown" else "list",
            sketch=sketch,
            subject_type=subject,
            path=None,
            value=value,
            template=None,
            params={},
            confidence="ambiguous",
            ranked_paths=list(ranked[:5]),
            explanation=(
                f"Ambiguous ontology paths from {subject} for cues "
                f"{list(sketch.rel_cues)!r} / dims {list(sketch.dim_mentions)!r}; "
                f"no unique winner (fail closed)."
            ),
        )

    if winner is None or confidence == "none":
        # Locative + value but no scoring path: if exactly one outbound 1-hop
        # edge, accept it only when locative (unique edge is a strong signal).
        is_locative = bool(
            {c.lower() for c in sketch.rel_cues} & _LOCATIVE_CUES
        )
        one_hop = [p for p in paths if p.hop_count == 1]
        if is_locative and value and len(one_hop) == 1:
            only = one_hop[0]
            winner = RankedPath(
                path=only,
                score=2.0,
                reasons=("unique_outbound_edge",),
            )
            ranked = [winner]
            confidence = "unique"
        else:
            return GroundedAskPlan(
                question=q,
                intent=sketch.intent if sketch.intent != "unknown" else "list",
                sketch=sketch,
                subject_type=subject,
                path=None,
                value=value,
                confidence="none",
                ranked_paths=list(ranked[:5]),
                explanation=(
                    f"No ontology path ranked for {subject} with "
                    f"cues={list(sketch.rel_cues)!r}."
                ),
            )

    path = winner.path
    if not value:
        # Path without value — hop-ish; leave template unset for free-form LLM.
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent if sketch.intent != "unknown" else "list",
            sketch=sketch,
            subject_type=subject,
            path=path,
            value=None,
            template=None,
            params=h._safe_params({"type_names": expanded}),
            confidence="unique",
            ranked_paths=list(ranked[:5]),
            explanation=f"Preferred path {path.describe()} but no value to bind.",
        )

    intent = sketch.intent if sketch.intent != "unknown" else "list"

    # Multi-hop: prompt-only (no multi-hop ADR 0013 template). Do not invent
    # raw Cypher; do not stuff multi-hop into single-hop related_entity params.
    if path.hop_count > 1:
        expl = (
            f"{'Count' if intent == 'count' else 'Find'} {subject} entities "
            f"via multi-hop path {path.describe()} "
            f"with related name {value!r} on terminal "
            f"{path.terminal_range or '?'}. "
            f"(No multi-hop allowlisted template — follow preferred_path in Cypher.)"
        )
        return GroundedAskPlan(
            question=q,
            intent=intent,
            sketch=sketch,
            subject_type=subject,
            path=path,
            value=value,
            template=None,
            params=h._safe_params(
                {
                    "type_names": expanded,
                    "target_name": value,
                    "limit": DEFAULT_LIST_LIMIT,
                }
            ),
            confidence="unique",
            ranked_paths=list(ranked[:5]),
            explanation=expl,
        )

    # 1-hop: grounded related-entity name filter (list or count intent).
    params = h._safe_params(
        {
            "type_names": expanded,
            "rel_attr": path.rel_attr,
            "target_name": value,
            "limit": DEFAULT_LIST_LIMIT,
        }
    )
    expl = (
        f"{'Count' if intent == 'count' else 'Find'} {subject} entities "
        f"related via {path.rel_attr} to name {value!r}"
        + (f" (range {path.range_type})" if path.range_type else "")
        + "."
    )
    return GroundedAskPlan(
        question=q,
        intent=intent,
        sketch=sketch,
        subject_type=subject,
        path=path,
        value=value,
        template=TEMPLATE_RELATED_ENTITY_NAME_FILTER,
        params=params,
        confidence="unique",
        ranked_paths=list(ranked[:5]),
        explanation=expl,
    )


async def ground_ask_plan_async(
    question: str,
    ontology_summary: str,
    *,
    type_names: Sequence[str] | None = None,
    mention_index: Any | None = None,
    query_embedding: Any | None = None,
    embedder: Any | None = None,
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> GroundedAskPlan | None:
    """Async variant: materialize embeddings from async ``EmbedFn`` then ground.

    When ``embedder`` is an async batch function, embeds the sketch text and
    each candidate path signature once, then ranks with a sync cosine table
    (no further network). When embedder is sync or absent, delegates to
    :func:`ground_ask_plan`.
    """
    h = _host()
    q = (question or "").strip()
    if not q or not (ontology_summary or "").strip():
        return await _maybe_async_none(
            h.ground_ask_plan(
                question,
                ontology_summary,
                type_names=type_names,
                mention_index=mention_index,
                query_embedding=query_embedding,
                embedder=embedder if h._is_sync_embedder(embedder) else None,
                max_hops=max_hops,
            )
        )

    # Async EmbedFn path: precompute vectors for sketch + path signatures.
    if embedder is not None and h._is_async_embedder(embedder):
        names = (
            list(type_names)
            if type_names is not None
            else extract_type_names_from_ontology(ontology_summary)
        )
        sketch = h.extract_nl_sketch(q)
        subject: str | None = None
        for mention in sketch.type_mentions:
            hit = resolve_type_name(mention, names or [], ontology_summary)
            if hit is not None:
                subject = hit
                break
        paths = (
            h.candidate_ontology_paths(
                ontology_summary,
                names,
                domain_type=subject,
                max_hops=max_hops,
            )
            if subject
            else []
        )
        sketch_text = h._sketch_embed_text(sketch)
        texts = [sketch_text] + [p.signature() for p in paths]
        try:
            vectors = await embedder(texts)
        except Exception:  # noqa: BLE001
            vectors = None
        sync_map: dict[str, list[float]] = {}
        q_emb = query_embedding
        if (
            isinstance(vectors, (list, tuple))
            and vectors
            and len(vectors) == len(texts)
        ):
            q_emb = q_emb if q_emb is not None else vectors[0]
            for i, p in enumerate(paths):
                sync_map[p.signature()] = list(vectors[i + 1])
            sketch_vec = list(vectors[0])
            dim = len(sketch_vec) or 1

            def _sync_batch(batch: Sequence[str]) -> list[list[float]]:
                out: list[list[float]] = []
                for t in batch:
                    if t == sketch_text:
                        out.append(list(sketch_vec))
                    elif t in sync_map:
                        out.append(list(sync_map[t]))
                    else:
                        out.append([0.0] * dim)
                return out

            return h.ground_ask_plan(
                question,
                ontology_summary,
                type_names=type_names,
                mention_index=mention_index,
                query_embedding=q_emb,
                embedder=_sync_batch,
                max_hops=max_hops,
            )
        # Async embed failed — fall through without embedder.
        return h.ground_ask_plan(
            question,
            ontology_summary,
            type_names=type_names,
            mention_index=mention_index,
            query_embedding=query_embedding,
            embedder=None,
            max_hops=max_hops,
        )

    return h.ground_ask_plan(
        question,
        ontology_summary,
        type_names=type_names,
        mention_index=mention_index,
        query_embedding=query_embedding,
        embedder=embedder if h._is_sync_embedder(embedder) else None,
        max_hops=max_hops,
    )


async def _maybe_async_none(plan: GroundedAskPlan | None) -> GroundedAskPlan | None:
    return plan
