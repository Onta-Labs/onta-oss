"""Semantic NL type / relationship-leaf resolution over an ontology embed index.

ONTA-537 (P-F1a): deterministic string-only type matching is insufficient when
tenants carry empty leftover types from sibling KGs. Ask-time resolution must
use **semantic similarity** of ontology nodes (entity types + relationship
leaves), informed by **hierarchy** (parent/child) and **in-KG instance
counts**, with **fail-closed** embed config for the production path.

Reuses the shared OpenRouter embed client (:mod:`infona_client.nlp.embed_client`)
— no third embedding stack. Hermetic tests inject a FakeEmbedder; production
requires an API key (``INFONA_OPENROUTER_API_KEY`` / ``OPENROUTER_API_KEY``) and
optional ``INFONA_EMBED_MODEL`` override (default
``openai/text-embedding-3-small``).

Scoring (documented for tests):

1. **Primary** — cosine similarity of mention ↔ catalog entry embed text.
2. **Secondary** — hierarchy expand/boost: if the best match is a parent with
   populated descendants, prefer the parent (callers expand via
   ``type_names_with_subclasses``); children with instances get a small boost
   when the mention is close to a parent.
3. **Tertiary** — instance prior: ``entity_count > 0`` in the active KG boosts;
   known-empty (0) demotes so leftovers never win a silent typed 0.

Ambiguous top-2 scores → ``None`` (clarify / LLM fallthrough), never invent.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterator, Iterable, Mapping, Sequence

import numpy as np

from infona_client.nlp.embed_client import (
    EMBEDDING_MODEL,
    cosine_similarity,
    embed_texts,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config / fail-closed
# ---------------------------------------------------------------------------

# Documented default — cheap quality-sufficient OpenRouter embed id.
# Override with INFONA_EMBED_MODEL (honored by embed_client too).
DEFAULT_EMBED_MODEL = EMBEDDING_MODEL  # openai/text-embedding-3-small

# Cosine thresholds (unit vectors assumed after L2 normalize at store time).
MIN_ACCEPT_SIM = 0.42
AMBIGUITY_MARGIN = 0.04  # top-2 within this band → ambiguous
INSTANCE_BOOST = 0.12
EMPTY_PENALTY = 0.28
HIERARCHY_CHILD_BOOST = 0.06  # when mention near parent and child has instances

EmbedFn = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


class EmbedConfigError(RuntimeError):
    """NL type/rel semantic resolution requires embed configuration.

    Fail-closed for the production ask path: never silently fall back to
    string-only matching that binds empty leftover types.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "NL type/relationship resolution requires an embedding client. "
                "Set INFONA_OPENROUTER_API_KEY (or OPENROUTER_API_KEY) and "
                "optionally INFONA_EMBED_MODEL "
                f"(default: {DEFAULT_EMBED_MODEL}). "
                "See docs: nlp/embed_client.py / ONTA-537. "
                "Hermetic tests may inject a FakeEmbedder via OntologyMentionIndex."
            )
        )


def embed_api_key_from_env() -> str:
    """Resolve OpenRouter (or compatible) API key from env / settings."""
    key = (
        os.environ.get("INFONA_OPENROUTER_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or ""
    ).strip()
    if key:
        return key
    try:
        from infona_client.config import settings

        return (settings.openrouter_api_key or "").strip()
    except Exception:  # noqa: BLE001 — settings optional in pure unit tests
        return ""


def require_embed_config(*, api_key: str | None = None) -> str:
    """Return a usable API key or raise :class:`EmbedConfigError` (fail closed)."""
    key = (api_key if api_key is not None else embed_api_key_from_env()).strip()
    if not key:
        raise EmbedConfigError()
    return key


def current_embed_model() -> str:
    return os.environ.get("INFONA_EMBED_MODEL", DEFAULT_EMBED_MODEL)


# ---------------------------------------------------------------------------
# Index entries
# ---------------------------------------------------------------------------


@dataclass
class OntologyMentionEntry:
    """One embeddable ontology node (entity type or relationship leaf)."""

    kind: str  # "type" | "rel"
    name: str
    embed_text: str
    embedding: np.ndarray | None = None
    parents: list[str] = field(default_factory=list)
    domain: str = ""
    range: str = ""
    description: str = ""


def format_type_embed_text(
    name: str,
    *,
    description: str = "",
    parents: Sequence[str] = (),
) -> str:
    """Minimum embed text for an entity type (name + description + parents)."""
    parts = [f"Entity type: {name}"]
    if description:
        parts.append(f"Description: {description.strip()}")
    if parents:
        parts.append(f"Parent types: {', '.join(parents)}")
    # Also include spaced CamelCase so embed models see head nouns.
    spaced = _space_camel(name)
    if spaced.lower() != name.lower():
        parts.append(f"Also known as: {spaced}")
    return "\n".join(parts)


def format_rel_embed_text(
    leaf: str,
    *,
    domain: str = "",
    range_type: str = "",
    description: str = "",
) -> str:
    """Minimum embed text for a relationship (object-property) leaf."""
    parts = [f"Relationship: {leaf}"]
    if domain:
        parts.append(f"Domain type: {domain}")
    if range_type:
        parts.append(f"Range type: {range_type}")
    if description:
        parts.append(f"Description: {description.strip()}")
    spaced = leaf.replace("_", " ")
    if spaced != leaf:
        parts.append(f"Also known as: {spaced}")
    return "\n".join(parts)


def _space_camel(name: str) -> str:
    s = re.sub(r"[_\-]+", " ", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return s.strip()


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return v
    return v / n


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class OntologyMentionIndex:
    """In-memory semantic index of ontology types + relationship leaves.

    Build incrementally as the ontology expands (ingest / A8 / type upsert).
    Ask-time resolve embeds the NL mention once and ranks catalog entries.
    """

    def __init__(self) -> None:
        self._entries: dict[str, OntologyMentionEntry] = {}  # key: "type:Name" | "rel:leaf"
        self._child_to_parent: dict[str, str] = {}
        self._activity: dict[str, int] = {}  # type name → instance count (-1 unknown)

    # -- identity helpers ---------------------------------------------------

    @staticmethod
    def _type_key(name: str) -> str:
        return f"type:{name}"

    @staticmethod
    def _rel_key(leaf: str) -> str:
        return f"rel:{leaf}"

    def clear(self) -> None:
        self._entries.clear()
        self._child_to_parent.clear()
        self._activity.clear()

    def set_hierarchy(self, child_to_parent: Mapping[str, str]) -> None:
        self._child_to_parent = {str(c): str(p) for c, p in child_to_parent.items() if c and p}

    def set_activity(self, activity: Mapping[str, int]) -> None:
        """Replace activity map: type leaf → instance count (0 / >0 / -1)."""
        self._activity = {str(k): int(v) for k, v in activity.items()}

    def merge_activity(self, activity: Mapping[str, int]) -> None:
        """Merge instance counts into the existing activity map."""
        for k, v in activity.items():
            self._activity[str(k)] = int(v)

    def update_activity(self, type_name: str, count: int) -> None:
        self._activity[type_name] = int(count)

    def hierarchy(self) -> dict[str, str]:
        return dict(self._child_to_parent)

    def activity(self) -> dict[str, int]:
        return dict(self._activity)

    # -- upsert (structure; embeddings filled separately) -------------------

    def upsert_type(
        self,
        name: str,
        *,
        description: str = "",
        parents: Sequence[str] = (),
        embedding: Sequence[float] | np.ndarray | None = None,
    ) -> OntologyMentionEntry:
        parents_list = [p for p in parents if p]
        for p in parents_list:
            self._child_to_parent[name] = p  # last parent wins if multi
        text = format_type_embed_text(name, description=description, parents=parents_list)
        emb = _l2_normalize(np.asarray(embedding, dtype=np.float32)) if embedding is not None else None
        entry = OntologyMentionEntry(
            kind="type",
            name=name,
            embed_text=text,
            embedding=emb,
            parents=parents_list,
            description=description,
        )
        self._entries[self._type_key(name)] = entry
        return entry

    def upsert_rel(
        self,
        leaf: str,
        *,
        domain: str = "",
        range_type: str = "",
        description: str = "",
        embedding: Sequence[float] | np.ndarray | None = None,
    ) -> OntologyMentionEntry:
        text = format_rel_embed_text(
            leaf, domain=domain, range_type=range_type, description=description
        )
        emb = _l2_normalize(np.asarray(embedding, dtype=np.float32)) if embedding is not None else None
        entry = OntologyMentionEntry(
            kind="rel",
            name=leaf,
            embed_text=text,
            embedding=emb,
            domain=domain,
            range=range_type,
            description=description,
        )
        self._entries[self._rel_key(leaf)] = entry
        return entry

    def type_names(self) -> list[str]:
        return sorted(e.name for e in self._entries.values() if e.kind == "type")

    def rel_names(self) -> list[str]:
        return sorted(e.name for e in self._entries.values() if e.kind == "rel")

    def is_healthy(self) -> bool:
        """True when at least one type has an embedding (sim index ready)."""
        return any(
            e.kind == "type" and e.embedding is not None for e in self._entries.values()
        )

    # -- batch embed --------------------------------------------------------

    async def embed_missing(self, embed_fn: EmbedFn) -> int:
        """Embed entries that lack vectors. Returns number newly embedded."""
        pending = [e for e in self._entries.values() if e.embedding is None]
        if not pending:
            return 0
        texts = [e.embed_text for e in pending]
        vectors = await embed_fn(texts)
        if len(vectors) != len(pending):
            raise RuntimeError(
                f"embed_fn returned {len(vectors)} vectors for {len(pending)} texts"
            )
        for e, vec in zip(pending, vectors):
            e.embedding = _l2_normalize(np.asarray(vec, dtype=np.float32))
        return len(pending)

    async def reembed_all(self, embed_fn: EmbedFn) -> int:
        """Force re-embed every entry (model change). Returns count."""
        for e in self._entries.values():
            e.embedding = None
        return await self.embed_missing(embed_fn)

    # -- resolve ------------------------------------------------------------

    def resolve_type(
        self,
        mention: str,
        *,
        query_embedding: Sequence[float] | np.ndarray,
        activity: Mapping[str, int] | None = None,
        type_names: Sequence[str] | None = None,
    ) -> str | None:
        """Rank type entries for ``mention``; return leaf or None if ambiguous/miss.

        ``activity`` overrides the index's stored activity map when provided
        (per-KG instance prior). ``type_names`` restricts candidates (ontology
        known to the fixture); when None, all indexed types compete.
        """
        return self._resolve_kind(
            mention,
            kind="type",
            query_embedding=query_embedding,
            activity=activity,
            name_filter=type_names,
        )

    def resolve_rel(
        self,
        mention: str,
        *,
        query_embedding: Sequence[float] | np.ndarray,
        rel_names: Sequence[str] | None = None,
    ) -> str | None:
        """Rank relationship leaves for ``mention``."""
        return self._resolve_kind(
            mention,
            kind="rel",
            query_embedding=query_embedding,
            activity=None,
            name_filter=rel_names,
        )

    def _resolve_kind(
        self,
        mention: str,
        *,
        kind: str,
        query_embedding: Sequence[float] | np.ndarray,
        activity: Mapping[str, int] | None,
        name_filter: Sequence[str] | None,
    ) -> str | None:
        if not mention or not mention.strip():
            return None
        q = _l2_normalize(np.asarray(query_embedding, dtype=np.float32))
        act = dict(self._activity)
        if activity is not None:
            act.update({str(k): int(v) for k, v in activity.items()})

        allowed: set[str] | None = None
        if name_filter is not None:
            allowed = {n for n in name_filter}

        # Exact name fast path (index healthy): case-insensitive exact leaf.
        # Still applies instance prior so empty leftover loses when a synonym
        # exists — exact match on empty type with populated near-synonym is
        # handled below only when exact is unambiguous and not demoted.
        needle = mention.strip()
        exact_hits = [
            e
            for e in self._entries.values()
            if e.kind == kind
            and e.embedding is not None
            and (allowed is None or e.name in allowed)
            and e.name.lower() == needle.lower()
        ]
        if len(exact_hits) == 1:
            hit = exact_hits[0]
            if kind != "type" or act.get(hit.name, -1) != 0:
                return hit.name
            # exact empty type: fall through to semantic so populated synonym can win

        candidates: list[OntologyMentionEntry] = [
            e
            for e in self._entries.values()
            if e.kind == kind
            and e.embedding is not None
            and (allowed is None or e.name in allowed)
        ]
        if not candidates:
            return None

        matrix = np.stack([e.embedding for e in candidates])  # type: ignore[misc]
        sims = cosine_similarity(q, matrix)

        scored: list[tuple[str, float]] = []
        parent_names = set(self._child_to_parent.values())
        # Mention closeness to any parent (for child boost).
        parent_sim: dict[str, float] = {}
        for i, e in enumerate(candidates):
            if e.name in parent_names or e.name in self._child_to_parent.values():
                parent_sim[e.name] = float(sims[i])

        for i, e in enumerate(candidates):
            sim = float(sims[i])
            score = sim
            if kind == "type":
                count = act.get(e.name, -1)
                if count == 0:
                    score -= EMPTY_PENALTY
                elif count > 0:
                    score += INSTANCE_BOOST
                # Hierarchy: child of a parent the mention is near → slight boost
                parent = self._child_to_parent.get(e.name)
                if parent and count != 0:
                    psim = parent_sim.get(parent)
                    if psim is None:
                        # parent may not be in candidate list; check entry
                        pkey = self._type_key(parent)
                        pe = self._entries.get(pkey)
                        if pe is not None and pe.embedding is not None:
                            psim = float(
                                cosine_similarity(q, pe.embedding.reshape(1, -1))[0]
                            )
                    if psim is not None and psim >= MIN_ACCEPT_SIM:
                        score += HIERARCHY_CHILD_BOOST
            scored.append((e.name, score))

        scored.sort(key=lambda kv: kv[1], reverse=True)
        top_name, top_score = scored[0]
        # Recover raw sim for threshold (score may be demoted below MIN).
        top_entry = next(e for e in candidates if e.name == top_name)
        top_idx = candidates.index(top_entry)
        top_sim = float(sims[top_idx])

        if top_sim < MIN_ACCEPT_SIM and top_score < MIN_ACCEPT_SIM:
            return None

        if len(scored) > 1:
            second_name, second_score = scored[1]
            if (
                second_name != top_name
                and abs(top_score - second_score) < AMBIGUITY_MARGIN
            ):
                return None

        # Empty leftover: if winner is known-empty and a high-sim populated
        # alternative exists, prefer the populated one or None (never silent 0).
        if kind == "type" and act.get(top_name, -1) == 0:
            for name, sc in scored[1:]:
                if act.get(name, -1) > 0 and sc >= MIN_ACCEPT_SIM - EMPTY_PENALTY:
                    # populated alternative that survived demotion of empty top
                    return name
            return None

        return top_name

    async def resolve_type_async(
        self,
        mention: str,
        *,
        embed_fn: EmbedFn,
        activity: Mapping[str, int] | None = None,
        type_names: Sequence[str] | None = None,
    ) -> str | None:
        vecs = await embed_fn([mention.strip()])
        if not vecs:
            return None
        return self.resolve_type(
            mention,
            query_embedding=vecs[0],
            activity=activity,
            type_names=type_names,
        )

    async def resolve_rel_async(
        self,
        mention: str,
        *,
        embed_fn: EmbedFn,
        rel_names: Sequence[str] | None = None,
    ) -> str | None:
        vecs = await embed_fn([mention.strip()])
        if not vecs:
            return None
        return self.resolve_rel(
            mention, query_embedding=vecs[0], rel_names=rel_names
        )


# ---------------------------------------------------------------------------
# OpenRouter embed_fn factory + process registry
# ---------------------------------------------------------------------------


def openrouter_embed_fn(api_key: str | None = None) -> EmbedFn:
    """Return an async embed_fn bound to the shared OpenRouter client."""
    key = require_embed_config(api_key=api_key)

    async def _fn(texts: Sequence[str]) -> list[list[float]]:
        return await embed_texts(list(texts), api_key=key)

    return _fn


_process_index: OntologyMentionIndex | None = None


def get_process_mention_index() -> OntologyMentionIndex | None:
    """Process-scoped index (set after ontology build / reindex hook)."""
    return _process_index


def set_process_mention_index(index: OntologyMentionIndex | None) -> None:
    global _process_index
    _process_index = index


def invalidate_process_mention_index() -> None:
    set_process_mention_index(None)


@dataclass
class ResolveContext:
    """Ask-time context for :func:`~infona_client.nlp.cypher_generate.resolve_type_name`.

    Fixtures remain sync; callers (pipeline / tests) bind a healthy index and
    optional precomputed mention→vector map so string-only is not the sole path.
    """

    mention_index: OntologyMentionIndex | None = None
    # lowercased mention → embedding vector (from FakeEmbedder or live embed)
    query_embeddings: dict[str, list[float]] = field(default_factory=dict)
    require_semantic: bool = False


_resolve_ctx: ContextVar[ResolveContext | None] = ContextVar(
    "infona_nl_type_resolve_ctx", default=None
)


def get_resolve_context() -> ResolveContext | None:
    return _resolve_ctx.get()


@contextmanager
def semantic_resolve_context(
    index: OntologyMentionIndex | None = None,
    *,
    query_embeddings: Mapping[str, Sequence[float]] | None = None,
    require_semantic: bool = False,
) -> Iterator[ResolveContext]:
    """Bind semantic resolve context for the current task (fixtures / tests)."""
    ctx = ResolveContext(
        mention_index=index,
        query_embeddings={
            str(k).strip().lower(): list(v)
            for k, v in (query_embeddings or {}).items()
        },
        require_semantic=require_semantic,
    )
    token = _resolve_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _resolve_ctx.reset(token)


def lookup_query_embedding(mention: str, ctx: ResolveContext | None = None) -> list[float] | None:
    """Find a precomputed query vector for ``mention`` (exact then stripped)."""
    c = ctx if ctx is not None else get_resolve_context()
    if c is None or not c.query_embeddings:
        return None
    key = (mention or "").strip().lower()
    if key in c.query_embeddings:
        return c.query_embeddings[key]
    # try first alphanumeric token group
    compact = re.sub(r"\s+", " ", key)
    return c.query_embeddings.get(compact)


async def reindex_types(
    types: Iterable[Mapping[str, object]],
    *,
    embed_fn: EmbedFn | None = None,
    api_key: str | None = None,
    relationships: Iterable[Mapping[str, object]] = (),
    activity: Mapping[str, int] | None = None,
    child_to_parent: Mapping[str, str] | None = None,
    replace: bool = True,
) -> OntologyMentionIndex:
    """Build / merge an index from catalog dicts and embed missing rows.

    Each type mapping: ``name`` (required), ``description``, ``parents`` (list).
    Each rel mapping: ``name``/``leaf``, ``domain``, ``range``/``range_type``,
    ``description``.

    Documented reindex hook for ingest / ontology upsert (ONTA-537 AC).
    """
    fn = embed_fn or openrouter_embed_fn(api_key)
    index = OntologyMentionIndex() if replace else (get_process_mention_index() or OntologyMentionIndex())
    if child_to_parent:
        index.set_hierarchy(child_to_parent)
    if activity:
        index.set_activity(activity)

    for t in types:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        parents = t.get("parents") or t.get("parent") or []
        if isinstance(parents, str):
            parents = [parents] if parents else []
        index.upsert_type(
            name,
            description=str(t.get("description") or ""),
            parents=list(parents),  # type: ignore[arg-type]
        )
    for r in relationships:
        leaf = str(r.get("name") or r.get("leaf") or "").strip()
        if not leaf:
            continue
        index.upsert_rel(
            leaf,
            domain=str(r.get("domain") or ""),
            range_type=str(r.get("range") or r.get("range_type") or ""),
            description=str(r.get("description") or ""),
        )
    await index.embed_missing(fn)
    set_process_mention_index(index)
    logger.info(
        "ontology_mention_index_rebuilt",
        extra={
            "types": len(index.type_names()),
            "rels": len(index.rel_names()),
            "model": current_embed_model(),
        },
    )
    return index


__all__ = [
    "AMBIGUITY_MARGIN",
    "DEFAULT_EMBED_MODEL",
    "EMPTY_PENALTY",
    "EmbedConfigError",
    "EmbedFn",
    "HIERARCHY_CHILD_BOOST",
    "INSTANCE_BOOST",
    "MIN_ACCEPT_SIM",
    "OntologyMentionEntry",
    "OntologyMentionIndex",
    "ResolveContext",
    "current_embed_model",
    "embed_api_key_from_env",
    "format_rel_embed_text",
    "format_type_embed_text",
    "get_process_mention_index",
    "get_resolve_context",
    "invalidate_process_mention_index",
    "lookup_query_embedding",
    "openrouter_embed_fn",
    "reindex_types",
    "require_embed_config",
    "semantic_resolve_context",
    "set_process_mention_index",
]
