"""Semantic ontology retrieval via embeddings.

Embeds ontology types as chunks, stores in-memory with optional S3 persistence.
At query time, retrieves the top-K most relevant types via cosine similarity
and expands 1 hop on the ontology graph for relationship neighbors.
"""

from cograph_client.graph.iri import IRI_BASE, TYPE_URI_PREFIX
import asyncio
import io
import json
import logging
import re
from dataclasses import dataclass, field

import numpy as np

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.ontology_queries import (
    TYPE_URI_PREFIX,
    attr_uri,
    get_full_ontology_query,
    type_uri,
)
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import skip_invalid_type_name

# Shared embed client (ONTA-174) — model/batching/errors live in ONE place.
# EmbeddingError and the embedding constants are re-exported here so existing
# importers (tests, callers) keep working unchanged.
from cograph_client.nlp.embed_client import (  # noqa: F401 — re-exports
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    OPENROUTER_EMBEDDINGS_URL,
    EmbeddingError,
    embed_texts,
)
from cograph_client.nlp.embed_client import cosine_similarity as _cosine_similarity  # noqa: F401

logger = logging.getLogger(__name__)

LARGE_TYPE_ATTR_THRESHOLD = 200
LARGE_TYPE_ATTR_KEEP = 50

# Type-level mark for "declared in the tenant ontology, carries no data in the KG
# being queried". Identical to the mark the full-ontology path emits
# (`pipeline._fetch_ontology`) so the SPARQL prompt has ONE rule to learn.
NO_INSTANCES_MARK = "[no instances]"

# Rank penalty applied to a type that has no instances in the target KG
# (ONTA-411). Larger than the cosine range [-1, 1] on purpose: every in-KG type
# outranks every out-of-KG one, while the relative order WITHIN each group is
# still the cosine order, so out-of-KG types fill leftover slots instead of
# being hard-dropped.
INACTIVE_TYPE_DEMOTION = 10.0


def _singularize(word: str) -> str:
    """Lowercase + strip a simple English plural suffix for loose token matching.

    Deliberately conservative (no external NLP dep): handles the common
    regular-plural cases so "Sprockets" in a question surfaces the declared
    "Sprocket" type. Not exhaustive — false negatives just fall back to the
    embedding ranking, which is the pre-existing behavior.
    """
    w = word.lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 1 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _types_named_in_question(question: str, type_names) -> set[str]:
    """Declared type names the question explicitly mentions.

    Matches each declared type against the question's word tokens with loose
    singular/plural normalization (so "list all Sprockets" surfaces "Sprocket"),
    plus a verbatim WORD match for compound/multi-word type names. Used to
    force-include a named type into the retrieved subset even when it ranks below
    top-K or has no instances (ONTA-258) — a declared type dropped from the
    subset is invisible to the SPARQL LLM, which then wrongly claims it "does not
    exist".

    The verbatim arm is word-BOUNDED (ONTA-450). A bare substring test matched a
    short declared type inside an unrelated word — `Age` in "manages", `Ion` in
    "medication", `Cat` in "categories" — which was harmless while a spurious
    match only force-INCLUDED a type into the context, but no longer is now that
    "the question named this type" also gates the zero-row escalation guard. A
    trailing `s?` keeps the plural of a compound name ("clinicaltrials"). The
    boundaries are lookarounds rather than `\b` so a type name that ENDS in a
    non-word character ("U.S.", "(Draft)") keeps the arm at all: a trailing
    `\b` cannot match after a dot when the next character is a space, which
    dropped such a name from the match entirely.
    """
    ql = question.lower()
    q_singulars = {_singularize(t) for t in re.findall(r"[a-z0-9]+", ql)}
    named: set[str] = set()
    for tn in type_names:
        nl = tn.lower()
        if _singularize(nl) in q_singulars:
            named.add(tn)
        elif len(nl) > 2 and re.search(rf"(?<!\w){re.escape(nl)}s?(?!\w)", ql):
            named.add(tn)  # compound / multi-word type named verbatim
    return named


@dataclass
class TypeChunk:
    type_name: str
    chunk_text: str
    embedding: np.ndarray  # shape: (EMBEDDING_DIM,), dtype float32
    attributes: list[str] = field(default_factory=list)
    relationship_targets: list[str] = field(default_factory=list)


@dataclass
class TenantEmbeddingStore:
    chunks: dict[str, TypeChunk] = field(default_factory=dict)
    dirty: bool = False


class OntologyEmbeddingService:
    def __init__(self, openrouter_api_key: str, s3_bucket: str = "", s3_prefix: str = "omnix/embeddings"):
        self._api_key = openrouter_api_key
        self._s3_bucket = s3_bucket
        self._s3_prefix = s3_prefix
        self._stores: dict[str, TenantEmbeddingStore] = {}

    # ── Full rebuild ──────────────────────────────────────────────────

    async def build_from_ontology(self, graph_uri: str, neptune: NeptuneClient) -> int:
        """Fetch all types from Neptune, embed them, store. Returns number of types embedded."""
        raw = await neptune.query(get_full_ontology_query(graph_uri))
        _, bindings = parse_sparql_results(raw)

        types = _parse_ontology_bindings(bindings)
        if not types:
            return 0

        # Build chunk texts
        chunk_texts: list[str] = []
        type_names: list[str] = []
        type_infos: list[dict] = []
        for type_name, info in types.items():
            chunk_text = _format_chunk_text(type_name, info)
            chunk_texts.append(chunk_text)
            type_names.append(type_name)
            type_infos.append(info)

        # Embed all chunks
        embeddings = await self._embed_texts(chunk_texts)

        # Build store
        store = TenantEmbeddingStore()
        for i, type_name in enumerate(type_names):
            store.chunks[type_name] = TypeChunk(
                type_name=type_name,
                chunk_text=chunk_texts[i],
                embedding=np.array(embeddings[i], dtype=np.float32),
                attributes=type_infos[i]["attributes"],
                relationship_targets=type_infos[i]["relationship_target_types"],
            )
        store.dirty = True
        self._stores[graph_uri] = store

        await self._save_to_s3(graph_uri)
        return len(store.chunks)

    # ── Incremental update ────────────────────────────────────────────

    async def embed_types(self, graph_uri: str, type_names: list[str], neptune: NeptuneClient) -> None:
        """Embed only the specified types and merge into the existing store."""
        if not type_names:
            return

        # Fetch full ontology to get attribute info for these types
        raw = await neptune.query(get_full_ontology_query(graph_uri))
        _, bindings = parse_sparql_results(raw)
        all_types = _parse_ontology_bindings(bindings)

        chunk_texts: list[str] = []
        names: list[str] = []
        infos: list[dict] = []
        for tn in type_names:
            info = all_types.get(tn)
            if not info:
                continue
            chunk_texts.append(_format_chunk_text(tn, info))
            names.append(tn)
            infos.append(info)

        if not chunk_texts:
            return

        embeddings = await self._embed_texts(chunk_texts)

        store = self._stores.get(graph_uri, TenantEmbeddingStore())
        for i, tn in enumerate(names):
            store.chunks[tn] = TypeChunk(
                type_name=tn,
                chunk_text=chunk_texts[i],
                embedding=np.array(embeddings[i], dtype=np.float32),
                attributes=infos[i]["attributes"],
                relationship_targets=infos[i]["relationship_target_types"],
            )
        store.dirty = True
        self._stores[graph_uri] = store

        await self._save_to_s3(graph_uri)

    # ── Query-time retrieval ──────────────────────────────────────────

    async def retrieve(
        self,
        graph_uri: str,
        question: str,
        top_k: int = 15,
        active_types: set[str] | None = None,
    ) -> str | None:
        """Retrieve the most relevant ontology subset for a question.

        Returns formatted ontology text (same format as _fetch_ontology),
        or None if no embeddings are available (triggers fallback).

        ``active_types`` (ONTA-411) is the set of type names that actually carry
        instances in the KG being queried. The embedding store is TENANT-WIDE
        (one ontology graph per tenant, shared by every KG) while a question is
        answered against ONE KG's instance graph, so cosine ranking alone lets a
        SIBLING KG's schema win the top-K outright: the model then writes a
        perfectly valid query against types the target graph does not carry and
        gets zero rows. Types outside the set are rank-DEMOTED and, if they still
        survive, annotated ``[no instances]``, never hard-dropped, because
        hiding a declared type is exactly the ONTA-258 regression (the model
        asserts the type "does not exist", or silently substitutes a populated
        one). ``None`` (the default) means "not KG-scoped" and behaves exactly as
        before.
        """
        store = self._stores.get(graph_uri)

        # Try S3 cold start
        if store is None:
            loaded = await self._load_from_s3(graph_uri)
            if loaded:
                store = self._stores.get(graph_uri)
        if store is None or not store.chunks:
            return None

        # Embed the question
        q_embedding = (await self._embed_texts([question]))[0]
        q_vec = np.array(q_embedding, dtype=np.float32)

        # Cosine similarity against all type embeddings
        type_names = list(store.chunks.keys())
        matrix = np.stack([store.chunks[tn].embedding for tn in type_names])
        similarities = _cosine_similarity(q_vec, matrix)

        def _out_of_kg(tn: str) -> bool:
            return active_types is not None and tn not in active_types

        # Scope-aware ranking (ONTA-411): in-KG types first, cosine order within
        # each group. With active_types=None the ranking is the plain cosine one.
        ranking = similarities
        if active_types is not None:
            penalty = np.array(
                [INACTIVE_TYPE_DEMOTION if _out_of_kg(tn) else 0.0 for tn in type_names],
                dtype=np.float32,
            )
            ranking = similarities - penalty

        # Top-K
        top_indices = np.argsort(ranking)[::-1][:top_k]
        selected = {type_names[i] for i in top_indices}

        # 1-hop expansion
        expansion: set[str] = set()
        for tn in list(selected):
            chunk = store.chunks[tn]
            for target in chunk.relationship_targets:
                if target not in selected and target in store.chunks:
                    expansion.add(target)

        # Ordering key shared by the expansion cap and the output assembly:
        # in-KG types first, then alphabetical. Alphabetical is not cosmetic:
        # `selected`/`expansion` are SETS, so without it the prompt's type order
        # (and which neighbours survive the cap) varied with set-iteration order
        # between processes.
        def _scope_rank(tn: str) -> tuple[int, str]:
            return (1 if _out_of_kg(tn) else 0, tn)

        # Cap expansion. A sibling KG's neighbour is the last thing that should
        # consume a slot, so in-KG neighbours are admitted first.
        max_total = top_k * 2
        for target in sorted(expansion, key=_scope_rank):
            if len(selected) >= max_total:
                break
            selected.add(target)

        # ONTA-258: always surface a type the question names by name, even if it
        # ranked below top-K or carries no instances. A declared type dropped
        # from the retrieved subset is invisible to the SPARQL LLM, which then
        # claims the type "does not exist" instead of returning an honest
        # zero-row answer. Matched against the FULL declared set (all chunks),
        # and force-included past the expansion cap on purpose.
        for tn in _types_named_in_question(question, type_names):
            selected.add(tn)

        # Assemble ontology text
        lines: list[str] = []
        for tn in sorted(selected, key=_scope_rank):
            chunk = store.chunks[tn]
            # Safety valve: filter attributes if too many
            if len(chunk.attributes) > LARGE_TYPE_ATTR_THRESHOLD:
                filtered_attrs = await self._filter_attributes(chunk.attributes, q_vec)
                text = _format_output_text(tn, filtered_attrs, chunk.relationship_targets)
            else:
                text = chunk.chunk_text
            # A type that survived the demotion (or was force-included by name)
            # but carries no data in THIS KG is marked, not hidden. The prompt
            # rule tells the model what the mark means.
            lines.append(_mark_no_instances(text) if _out_of_kg(tn) else text)

        if not lines:
            return None
        return "\n".join(lines)

    async def type_names(self, graph_uri: str) -> set[str]:
        """Declared type names in this tenant's store (S3 cold start included).

        The store is built from the tenant's ontology, so its keys ARE the
        declared type set. Exposed because the KG-scoping probe needs candidate
        type names BEFORE any schema read (ONTA-411 + ONTA-427): with them the
        probe stays on its bounded LIMIT-1 form, without them the caller would
        have to scan the whole instance graph on every ask. Empty set when no
        embeddings exist, which is the same condition under which
        :meth:`retrieve` returns None.
        """
        store = self._stores.get(graph_uri)
        if store is None:
            await self._load_from_s3(graph_uri)
            store = self._stores.get(graph_uri)
        return set(store.chunks) if store else set()

    # ── Embedding API ─────────────────────────────────────────────────

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Delegate to the shared embed client (kept as a method: test seam)."""
        return await embed_texts(texts, api_key=self._api_key)

    async def _filter_attributes(self, attributes: list[str], question_vec: np.ndarray) -> list[str]:
        """For types with 200+ attributes, keep only the top-50 most relevant."""
        attr_texts = [a.split(" — ")[0] if " — " in a else a for a in attributes]
        embeddings = await self._embed_texts(attr_texts)
        attr_matrix = np.array(embeddings, dtype=np.float32)
        similarities = _cosine_similarity(question_vec, attr_matrix)
        top_indices = np.argsort(similarities)[::-1][:LARGE_TYPE_ATTR_KEEP]
        return [attributes[i] for i in sorted(top_indices)]

    # ── S3 persistence ────────────────────────────────────────────────

    async def _save_to_s3(self, graph_uri: str) -> None:
        """Persist embedding store to S3. Non-blocking on failure."""
        if not self._s3_bucket:
            return
        store = self._stores.get(graph_uri)
        if not store or not store.dirty:
            return

        tenant_id = _extract_tenant_id(graph_uri)
        key = f"{self._s3_prefix}/{tenant_id}/ontology.json"

        serialized: dict[str, dict] = {}
        for tn, chunk in store.chunks.items():
            serialized[tn] = {
                "chunk_text": chunk.chunk_text,
                "embedding": chunk.embedding.tolist(),
                "attributes": chunk.attributes,
                "relationship_targets": chunk.relationship_targets,
            }

        try:
            body = json.dumps(serialized).encode()
            await asyncio.get_event_loop().run_in_executor(
                None, self._s3_put, key, body,
            )
            store.dirty = False
            logger.info("Saved embeddings to S3", extra={"key": key, "types": len(serialized)})
        except Exception:
            logger.warning("Failed to save embeddings to S3", exc_info=True)

    def _s3_put(self, key: str, body: bytes) -> None:
        import boto3
        s3 = boto3.client("s3")
        s3.put_object(Bucket=self._s3_bucket, Key=key, Body=body)

    async def _load_from_s3(self, graph_uri: str) -> bool:
        """Try to load embedding store from S3. Returns True on success."""
        if not self._s3_bucket:
            return False

        tenant_id = _extract_tenant_id(graph_uri)
        key = f"{self._s3_prefix}/{tenant_id}/ontology.json"

        try:
            body = await asyncio.get_event_loop().run_in_executor(
                None, self._s3_get, key,
            )
            serialized = json.loads(body)
            store = TenantEmbeddingStore()
            for tn, data in serialized.items():
                store.chunks[tn] = TypeChunk(
                    type_name=tn,
                    chunk_text=data["chunk_text"],
                    embedding=np.array(data["embedding"], dtype=np.float32),
                    attributes=data.get("attributes", []),
                    relationship_targets=data.get("relationship_targets", []),
                )
            self._stores[graph_uri] = store
            logger.info("Loaded embeddings from S3", extra={"key": key, "types": len(store.chunks)})
            return True
        except Exception:
            logger.debug("Could not load embeddings from S3", exc_info=True)
            return False

    def _s3_get(self, key: str) -> bytes:
        import boto3
        s3 = boto3.client("s3")
        response = s3.get_object(Bucket=self._s3_bucket, Key=key)
        return response["Body"].read()

    # ── Cache management ──────────────────────────────────────────────

    def invalidate(self, graph_uri: str) -> None:
        """Clear in-memory store for a graph. S3 is overwritten on next build."""
        self._stores.pop(graph_uri, None)


# ── Helpers ───────────────────────────────────────────────────────────


def _parse_ontology_bindings(bindings: list[dict]) -> dict[str, dict]:
    """Parse SPARQL ontology bindings into per-type structures."""
    types: dict[str, dict] = {}
    for row in bindings:
        tl = row.get("typeLabel", "")
        if not tl:
            continue
        # Fail SOFT (ONTA-425): these labels are stored literals, and
        # `_format_chunk_text` / `attr_uri` mint IRIs from them. Raising on one
        # corrupt row would lose EVERY typef's embedding chunk, which is the
        # semantic-retrieval half of the same all-or-nothing failure the NL
        # ontology summary avoids.
        if skip_invalid_type_name(tl, "ontology_embeddings"):
            continue
        if tl not in types:
            types[tl] = {
                "attributes": [],
                "relationships": [],
                "relationship_target_types": [],
                "functions": set(),
            }
        if row.get("attrLabel") and not skip_invalid_type_name(
            row["attrLabel"], "ontology_embeddings_attr"
        ):
            attr_name = row["attrLabel"]
            range_str = row.get("range", "")
            if range_str.startswith(TYPE_URI_PREFIX):
                target_type = range_str[len(TYPE_URI_PREFIX):]
                onto_uri = f"{IRI_BASE}/onto/{attr_name}"
                entry = f"{attr_name} \u2192 {target_type} \u2014 predicate URI: <{onto_uri}>"
                if entry not in types[tl]["relationships"]:
                    types[tl]["relationships"].append(entry)
                if target_type not in types[tl]["relationship_target_types"]:
                    types[tl]["relationship_target_types"].append(target_type)
            else:
                dtype = range_str.split("#")[-1] if "#" in range_str else "string"
                entry = f"{attr_name} ({dtype}) \u2014 URI: <{attr_uri(tl, attr_name)}>"
                if entry not in types[tl]["attributes"]:
                    types[tl]["attributes"].append(entry)
        if row.get("funcName"):
            types[tl]["functions"].add(row["funcName"])
    return types


def _mark_no_instances(chunk_text: str) -> str:
    """Append the ``[no instances]`` mark to a chunk's ``Type:`` header line.

    Same annotation the full-ontology path puts on a declared-but-unpopulated
    type, so both retrieval paths hand the LLM one consistent vocabulary. Rides
    on the header line only (attributes/relationships keep their own marks) and
    is idempotent.
    """
    if not chunk_text:
        return chunk_text
    head, sep, rest = chunk_text.partition("\n")
    if NO_INSTANCES_MARK in head:
        return chunk_text
    return f"{head} {NO_INSTANCES_MARK}{sep}{rest}"


def _format_chunk_text(type_name: str, info: dict) -> str:
    """Format a type into embeddable chunk text (also used as LLM ontology output)."""
    lines = [f"Type: {type_name} \u2014 URI: <{type_uri(type_name)}>"]
    if info["attributes"]:
        lines.append(f"  Attributes: {', '.join(sorted(info['attributes']))}")
    if info["relationships"]:
        lines.append(f"  Relationships: {', '.join(sorted(info['relationships']))}")
    funcs = info.get("functions", set())
    if funcs:
        lines.append(f"  Functions: {', '.join(sorted(funcs))}")
    return "\n".join(lines)


def _format_output_text(type_name: str, attributes: list[str], relationship_targets: list[str]) -> str:
    """Format output text with a filtered attribute list."""
    lines = [f"Type: {type_name} \u2014 URI: <{type_uri(type_name)}>"]
    if attributes:
        lines.append(f"  Attributes: {', '.join(sorted(attributes))}")
    if relationship_targets:
        lines.append(f"  Relationships: (see related types)")
    return "\n".join(lines)


def _extract_tenant_id(graph_uri: str) -> str:
    """Extract tenant ID from a graph URI like ``…/graphs/{tenant_id}``."""
    # Handle both base and KG-specific URIs
    parts = graph_uri.rstrip("/").split("/")
    # https://graph.onta.sh/graphs/{tenant_id} → tenant_id is at index 4
    # https://graph.onta.sh/graphs/{tenant_id}/kg/{kg_name} → still index 4
    if len(parts) >= 5:
        return parts[4]
    return "unknown"
