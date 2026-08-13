"""Semantic NL type / relationship-leaf resolution over an ontology embed index.

ONTA-537 (P-F1a): deterministic string-only type matching is insufficient when
tenants carry empty leftover types from sibling KGs. Ask-time resolution must
use **semantic similarity** of ontology nodes (entity types + relationship
leaves), informed by **hierarchy** (parent/child) and **in-KG instance
counts**.

Reuses the shared OpenRouter embed client (:mod:`infona_client.nlp.embed_client`)
— no third embedding stack. Hermetic tests inject a FakeEmbedder; production
uses an API key (``INFONA_OPENROUTER_API_KEY`` / ``OPENROUTER_API_KEY``) and
optional ``INFONA_EMBED_MODEL`` override (default
``openai/text-embedding-3-small``).

**Fail-closed is opt-in.** Production ask path only refuses string-only
fallback when ``INFONA_REQUIRE_SEMANTIC_RESOLVE=1`` (or
``require_semantic=True``). Until cold-start reindex is solid (full catalog
on process boot), the default is best-effort semantic when the index is ready
for the candidate set, else string heuristics. Do not claim always-fail-closed.

Scoring (documented for tests):

1. **Primary** — cosine similarity of mention ↔ catalog entry embed text.
2. **Secondary** — hierarchy: when the mention is a **parent concept** and that
   parent has **populated descendants**, prefer the **parent** so count/list
   fixtures expand subclasses via ``type_names_with_subclasses``. Child
   instance boost applies only when the parent is not also competing (or the
   mention is clearly child-specific).
3. **Tertiary** — instance prior: ``entity_count > 0`` in the active KG boosts;
   known-empty (0) demotes so leftovers never win a silent typed 0. Parents
   with populated descendants are not treated as empty leftovers.

**Partial index safety:** never rank only the embedded subset of
``type_names``. If any allowed candidate lacks an embedding, skip semantic
for that call (string fallback, or ``None`` / error when
``require_semantic``).

Ambiguous top-2 scores → ``None`` (clarify / LLM fallthrough), never invent.

**Ask-time purity:** resolve paths accept activity/hierarchy as call-local
overlays only — they must not mutate the process-global index.
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
# When parent is NOT competing as a candidate, slight boost for populated
# children near a parent-concept mention.
HIERARCHY_CHILD_BOOST = 0.06
# Parent preference for expand is a post-process (lexical parent-concept
# alignment), not a score boost — a boost made empty abstract parents
# (ProductLike) ambiguous with populated children (InventorySKU).

EmbedFn = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


class EmbedConfigError(RuntimeError):
    """NL type/rel semantic resolution requires embed configuration.

    Raised when ``require_semantic=True`` / ``INFONA_REQUIRE_SEMANTIC_RESOLVE``
    is set and the embed index or API key is unavailable. Opt-in fail-closed —
    not the default until cold-start reindex is reliable.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "NL type/relationship resolution requires an embedding client. "
                "Set INFONA_OPENROUTER_API_KEY (or OPENROUTER_API_KEY) and "
                "optionally INFONA_EMBED_MODEL "
                f"(default: {DEFAULT_EMBED_MODEL}). "
                "Fail-closed is opt-in via INFONA_REQUIRE_SEMANTIC_RESOLVE=1 "
                "or require_semantic=True (ONTA-537). "
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
    """One embeddable ontology node (entity type, relationship, or attr leaf)."""

    kind: str  # "type" | "rel" | "attr"
    name: str
    embed_text: str
    embedding: np.ndarray | None = None
    parents: list[str] = field(default_factory=list)
    domain: str = ""
    range: str = ""
    description: str = ""
    datatype: str = ""


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


def format_attr_embed_text(
    leaf: str,
    *,
    domain: str = "",
    datatype: str = "",
    description: str = "",
) -> str:
    """Minimum embed text for a datatype / literal attribute leaf.

    Used for money-ish synonym resolve (``price`` ↔ ``unit_cost``) when the
    mention index is fully embedded for the candidate set. Partial indexes
    must not invent leaves — callers gate on :meth:`attrs_fully_embedded`.
    """
    parts = [f"Attribute: {leaf}"]
    if domain:
        parts.append(f"Domain type: {domain}")
    if datatype:
        parts.append(f"Datatype: {datatype}")
    if description:
        parts.append(f"Description: {description.strip()}")
    spaced = leaf.replace("_", " ")
    if spaced != leaf:
        parts.append(f"Also known as: {spaced}")
    # Space camelCase for embed models.
    camel_spaced = _space_camel(leaf)
    if camel_spaced.lower() != leaf.lower() and camel_spaced != spaced:
        parts.append(f"Also known as: {camel_spaced}")
    return "\n".join(parts)


def _space_camel(name: str) -> str:
    s = re.sub(r"[_\-]+", " ", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return s.strip()


def _mention_aligns_with_type(mention: str, type_name: str) -> bool:
    """True when NL mention is a parent-concept word for ``type_name``.

    Used to prefer a parent type so count/list expand subclasses (e.g.
    ``animals`` → Animal, not CanineUnit). Strict: stem/plural of the type
    leaf only — does **not** match loose synonyms (``products`` ↛ ProductLike).
    """
    m = re.sub(r"[^a-z0-9]", "", (mention or "").lower())
    if not m:
        return False
    m_stem = m[:-1] if m.endswith("s") and len(m) > 3 else m
    t = re.sub(r"[^a-z0-9]", "", (type_name or "").lower())
    spaced = re.sub(r"[^a-z0-9]", "", _space_camel(type_name or "").lower())
    if not t:
        return False
    return (
        m == t
        or m_stem == t
        or m == t + "s"
        or m == spaced
        or m_stem == spaced
        or m == spaced + "s"
    )


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
    """In-memory semantic index of ontology types + relationship + attr leaves.

    Build incrementally as the ontology expands (ingest / A8 / type upsert).
    Ask-time resolve embeds the NL mention once and ranks catalog entries.
    """

    def __init__(self) -> None:
        # key: "type:Name" | "rel:leaf" | "attr:leaf"
        self._entries: dict[str, OntologyMentionEntry] = {}
        self._child_to_parent: dict[str, str] = {}
        self._activity: dict[str, int] = {}  # type name → instance count (-1 unknown)

    # -- identity helpers ---------------------------------------------------

    @staticmethod
    def _type_key(name: str) -> str:
        return f"type:{name}"

    @staticmethod
    def _rel_key(leaf: str) -> str:
        return f"rel:{leaf}"

    @staticmethod
    def _attr_key(leaf: str) -> str:
        return f"attr:{leaf}"

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

    def upsert_attr(
        self,
        leaf: str,
        *,
        domain: str = "",
        datatype: str = "",
        description: str = "",
        embedding: Sequence[float] | np.ndarray | None = None,
    ) -> OntologyMentionEntry:
        """Register a datatype / literal attribute leaf for semantic resolve."""
        text = format_attr_embed_text(
            leaf, domain=domain, datatype=datatype, description=description
        )
        emb = (
            _l2_normalize(np.asarray(embedding, dtype=np.float32))
            if embedding is not None
            else None
        )
        entry = OntologyMentionEntry(
            kind="attr",
            name=leaf,
            embed_text=text,
            embedding=emb,
            domain=domain,
            description=description,
            datatype=datatype,
        )
        self._entries[self._attr_key(leaf)] = entry
        return entry

    def get_attr_entry(self, leaf: str) -> OntologyMentionEntry | None:
        """Lookup an attr entry by leaf name (exact key)."""
        return self._entries.get(self._attr_key(leaf))

    def type_names(self) -> list[str]:
        return sorted(e.name for e in self._entries.values() if e.kind == "type")

    def rel_names(self) -> list[str]:
        return sorted(e.name for e in self._entries.values() if e.kind == "rel")

    def attr_names(self) -> list[str]:
        return sorted(e.name for e in self._entries.values() if e.kind == "attr")

    def is_healthy(self) -> bool:
        """Coarse readiness: at least one type has an embedding.

        Not sufficient for ask-time resolve — use
        :meth:`types_fully_embedded` / :meth:`rels_fully_embedded` so a
        partial index (restart + one small ingest) cannot rank only the
        embedded subset of the allowed candidate set.
        """
        return any(
            e.kind == "type" and e.embedding is not None for e in self._entries.values()
        )

    def types_fully_embedded(self, type_names: Sequence[str] | None) -> bool:
        """True iff every allowed type candidate has a stored embedding.

        When ``type_names`` is provided, **each** name must exist as a type
        entry with a non-None embedding. Missing catalog rows count as
        "lacks embedding" — partial indexes must not bind wrong leaves.
        When ``type_names`` is None, every indexed type must be embedded.
        """
        if type_names is None:
            type_entries = [e for e in self._entries.values() if e.kind == "type"]
            return bool(type_entries) and all(
                e.embedding is not None for e in type_entries
            )
        names = [str(n) for n in type_names if n]
        if not names:
            return False
        for name in names:
            entry = self._entries.get(self._type_key(name))
            if entry is None or entry.kind != "type" or entry.embedding is None:
                return False
        return True

    def rels_fully_embedded(self, rel_names: Sequence[str] | None) -> bool:
        """True iff every allowed relationship leaf candidate is embedded."""
        if rel_names is None:
            rel_entries = [e for e in self._entries.values() if e.kind == "rel"]
            return bool(rel_entries) and all(
                e.embedding is not None for e in rel_entries
            )
        names = [str(n) for n in rel_names if n]
        if not names:
            return False
        for name in names:
            entry = self._entries.get(self._rel_key(name))
            if entry is None or entry.kind != "rel" or entry.embedding is None:
                return False
        return True

    def attrs_fully_embedded(self, attr_names: Sequence[str] | None) -> bool:
        """True iff every allowed attribute leaf candidate has an embedding.

        Partial index safety (same class as types/rels): if any allowed
        candidate lacks an embedding, semantic attr resolve must not run.
        """
        if attr_names is None:
            attr_entries = [e for e in self._entries.values() if e.kind == "attr"]
            return bool(attr_entries) and all(
                e.embedding is not None for e in attr_entries
            )
        names = [str(n) for n in attr_names if n]
        if not names:
            return False
        for name in names:
            entry = self._entries.get(self._attr_key(name))
            if entry is None or entry.kind != "attr" or entry.embedding is None:
                return False
        return True

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
        hierarchy: Mapping[str, str] | None = None,
    ) -> str | None:
        """Rank type entries for ``mention``; return leaf or None if ambiguous/miss.

        ``activity`` and ``hierarchy`` are **call-local overlays** (never mutate
        the index). ``type_names`` restricts candidates; when None, all indexed
        types compete.

        Callers must only invoke this when
        :meth:`types_fully_embedded` is true for ``type_names`` — partial
        indexes must not rank a subset of allowed leaves.
        """
        return self._resolve_kind(
            mention,
            kind="type",
            query_embedding=query_embedding,
            activity=activity,
            name_filter=type_names,
            hierarchy=hierarchy,
        )

    def resolve_rel(
        self,
        mention: str,
        *,
        query_embedding: Sequence[float] | np.ndarray,
        rel_names: Sequence[str] | None = None,
    ) -> str | None:
        """Rank relationship leaves for ``mention``.

        Callers should gate on :meth:`rels_fully_embedded` for ``rel_names``.
        """
        return self._resolve_kind(
            mention,
            kind="rel",
            query_embedding=query_embedding,
            activity=None,
            name_filter=rel_names,
            hierarchy=None,
        )

    def resolve_attr(
        self,
        mention: str,
        *,
        query_embedding: Sequence[float] | np.ndarray,
        attr_names: Sequence[str] | None = None,
    ) -> str | None:
        """Rank datatype / literal attribute leaves for ``mention``.

        Callers **must** gate on :meth:`attrs_fully_embedded` for
        ``attr_names`` — partial indexes must not invent leaves.
        """
        if attr_names is not None and not self.attrs_fully_embedded(attr_names):
            return None
        if attr_names is None and not self.attrs_fully_embedded(None):
            return None
        return self._resolve_kind(
            mention,
            kind="attr",
            query_embedding=query_embedding,
            activity=None,
            name_filter=attr_names,
            hierarchy=None,
        )

    def _effective_hierarchy(
        self, hierarchy: Mapping[str, str] | None
    ) -> dict[str, str]:
        """Call-local hierarchy overlay on top of stored map (no mutation)."""
        base = dict(self._child_to_parent)
        if hierarchy:
            base.update({str(c): str(p) for c, p in hierarchy.items() if c and p})
        return base

    def _has_populated_descendant(
        self,
        name: str,
        *,
        child_to_parent: Mapping[str, str],
        activity: Mapping[str, int],
    ) -> bool:
        """True when any direct child of ``name`` has instance count > 0."""
        for child, parent in child_to_parent.items():
            if parent == name and activity.get(child, -1) > 0:
                return True
        return False

    def _resolve_kind(
        self,
        mention: str,
        *,
        kind: str,
        query_embedding: Sequence[float] | np.ndarray,
        activity: Mapping[str, int] | None,
        name_filter: Sequence[str] | None,
        hierarchy: Mapping[str, str] | None,
    ) -> str | None:
        if not mention or not mention.strip():
            return None
        q = _l2_normalize(np.asarray(query_embedding, dtype=np.float32))
        # Call-local activity only — do not merge into self._activity.
        act = dict(self._activity)
        if activity is not None:
            act = {**act, **{str(k): int(v) for k, v in activity.items()}}

        child_to_parent = self._effective_hierarchy(hierarchy)

        allowed: set[str] | None = None
        if name_filter is not None:
            allowed = {n for n in name_filter}

        # Exact name fast path: case-insensitive exact leaf.
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
        parent_names = set(child_to_parent.values())
        candidate_names = {e.name for e in candidates}
        # Mention closeness to any parent (for parent/child hierarchy scoring).
        parent_sim: dict[str, float] = {}
        for i, e in enumerate(candidates):
            if e.name in parent_names:
                parent_sim[e.name] = float(sims[i])

        sim_by_name: dict[str, float] = {
            e.name: float(sims[i]) for i, e in enumerate(candidates)
        }

        for i, e in enumerate(candidates):
            sim = float(sims[i])
            score = sim
            if kind == "type":
                count = act.get(e.name, -1)
                has_pop_desc = self._has_populated_descendant(
                    e.name, child_to_parent=child_to_parent, activity=act
                )
                # Parents with populated descendants are not "empty leftovers":
                # count/list expand subclasses via type_names_with_subclasses.
                if count == 0 and not has_pop_desc:
                    score -= EMPTY_PENALTY
                elif count > 0:
                    score += INSTANCE_BOOST
                parent = child_to_parent.get(e.name)
                if parent and count != 0:
                    psim = parent_sim.get(parent)
                    if psim is None:
                        pkey = self._type_key(parent)
                        pe = self._entries.get(pkey)
                        if pe is not None and pe.embedding is not None:
                            psim = float(
                                cosine_similarity(q, pe.embedding.reshape(1, -1))[0]
                            )
                    # Child boost only when parent is NOT also competing —
                    # otherwise parent-concept scoring is handled post-rank.
                    if (
                        psim is not None
                        and psim >= MIN_ACCEPT_SIM
                        and parent not in candidate_names
                    ):
                        score += HIERARCHY_CHILD_BOOST
            scored.append((e.name, score))

        scored.sort(key=lambda kv: kv[1], reverse=True)
        top_name, top_score = scored[0]
        # Recover raw sim for threshold (score may be demoted below MIN).
        top_sim = sim_by_name[top_name]

        if top_sim < MIN_ACCEPT_SIM and top_score < MIN_ACCEPT_SIM:
            return None

        if len(scored) > 1:
            second_name, second_score = scored[1]
            if (
                second_name != top_name
                and abs(top_score - second_score) < AMBIGUITY_MARGIN
            ):
                return None

        # Hierarchy AC: parent-concept mention + populated descendants → parent
        # so count/list expand subclasses. Only when the mention lexically
        # aligns with the parent (animals→Animal), not loose synonyms
        # (products→InventorySKU, not empty ProductLike).
        if kind == "type":
            parent = child_to_parent.get(top_name)
            if (
                parent
                and parent in candidate_names
                and sim_by_name.get(parent, 0.0) >= MIN_ACCEPT_SIM
                and self._has_populated_descendant(
                    parent, child_to_parent=child_to_parent, activity=act
                )
                and _mention_aligns_with_type(mention, parent)
            ):
                return parent

        # Empty leftover: if winner is known-empty (no populated descendants)
        # and a high-sim populated alternative exists, prefer that or None.
        if kind == "type" and act.get(top_name, -1) == 0:
            if not self._has_populated_descendant(
                top_name, child_to_parent=child_to_parent, activity=act
            ):
                for name, sc in scored[1:]:
                    if act.get(name, -1) > 0 and sc >= MIN_ACCEPT_SIM - EMPTY_PENALTY:
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
        hierarchy: Mapping[str, str] | None = None,
    ) -> str | None:
        vecs = await embed_fn([mention.strip()])
        if not vecs:
            return None
        return self.resolve_type(
            mention,
            query_embedding=vecs[0],
            activity=activity,
            type_names=type_names,
            hierarchy=hierarchy,
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
    "format_attr_embed_text",
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
