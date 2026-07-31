"""Marker-driven extraction of semantic chunks from instance triples (ONTA-175).

The write path hands us the exact RDF triples it just inserted (the same input
``kg_writer._index_spatiotemporal`` receives); this module turns the values of
**marked free-text predicates** into :class:`SemanticChunk` rows for the
semantic instance index. Unlike the spatio-temporal extractor (datatype-driven:
a ``geo:wktLiteral`` *is* the signal), free text has no distinguishing
datatype — ``description`` and ``sku`` are both plain literals — so indexing is
**opt-in via a marker set**: the caller (the ONTA-181 write hook, consulting
its marker map; or the reconciler's backfill scan) says which predicates are
semantic-candidate attributes, and only those are extracted.

Canonicalization (the change-detection contract)
------------------------------------------------

All values of one marked attribute on one entity form ONE document:

1. values are stripped, empties dropped, exact duplicates removed;
2. multi-valued attributes are **sorted** and joined with a blank line —
   BEFORE hashing/chunking, so the doc (and therefore ``content_hash``) is
   deterministic regardless of triple order. This is load-bearing: the write
   hook skips unchanged hashes and the reconciler upserts by hash, so a
   spurious hash change (same values, different order) would re-chunk and
   re-embed a doc that didn't change.

``content_hash`` is the sha256 hex digest of that canonical doc, computed once
per (entity, attr); every chunk row of the doc carries the same hash.

Chunking (no tokenizer dependency)
----------------------------------

Chunks target ~256–512 tokens estimated at ~4 chars/token → 1024–2048 chars,
preferring to break on paragraph, then sentence, then whitespace boundaries
(hard mid-word cut only for pathological unbroken text). Edge cases are
explicit and tested: empty/whitespace docs → 0 chunks; anything short → exactly
1 chunk; identical docs within one entity are deduplicated (first attribute
wins); and a runaway entity is capped at :data:`MAX_CHUNKS_PER_ENTITY` chunks —
logged, never silent.

Object encoding: the write path emits URIs bare (``https://…``) and typed
literals as ``"<lexical>^^<type-uri>"`` — exactly what
:func:`cograph_client.graph.queries._escape_value` consumes. We mirror its
URI-vs-literal decision (URI objects are entity references, never text) and
strip the ``^^`` datatype tail the way ``spatiotemporal/extract.py`` does. The
small helpers are duplicated from there deliberately, so this leaf module stays
importable on its own without reaching into a sibling subsystem.

The identity arm (ONTA-421)
---------------------------

Marker-driven extraction alone left ``/search`` structurally unable to find an
entity **by its own name**: candidacy is decided from VALUE SHAPE
(``graph/text_markers.classify_text_candidacy``), and a name is short, so it is
never ``ValueShape.TEXT`` and therefore never markable — no amount of
reindexing helps. On top of the marked free-text docs this module therefore
emits ONE extra doc per entity under the reserved attr
:data:`~cograph_client.semantic.protocol.IDENTITY_ATTR`, built from the entity's
own name(s) (``rdfs:label`` plus the ``label`` / ``name`` / ``title`` locals —
the same values already harvested for the denormalized display ``attrs``). It is
marker-INDEPENDENT by construction: whether a thing has a name is not a
candidacy question.

Three properties keep it from disturbing free-text retrieval:

* it is emitted **after** every marked doc, so the intra-entity dedup drops it
  when it exactly mirrors a marked doc — a marked ``title`` keeps its own attr
  name AND its embedding; the identity arm never relocates an existing doc out
  of the semantic leg;
* it is exempt from :data:`MAX_CHUNKS_PER_ENTITY` (it is one short chunk, and a
  text-heavy entity must not become unfindable by name because its prose spent
  the budget);
* it is never embedded — enforced backend-side in ``fetch_pending`` — so the ANN
  leg's candidate pool is exactly what it was before.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Collection, Iterable, Optional

import structlog

from cograph_client.semantic.protocol import IDENTITY_ATTR, SemanticChunk

logger = structlog.stdlib.get_logger("cograph.semantic.extract")

Triple = tuple[str, str, str]

#: Chunk size bounds in characters, from the ~4 chars/token heuristic:
#: 256 tokens ≈ 1024 chars (the earliest a natural break is accepted) and
#: 512 tokens ≈ 2048 chars (the hard ceiling). No tokenizer dependency —
#: the pgvector backend and the embed model tolerate the estimation slack.
MIN_CHUNK_CHARS = 1024
MAX_CHUNK_CHARS = 2048

#: Hard cap on chunks per ENTITY (across all its marked attributes). A single
#: pathological entity (a 1 MB scraped page in ``notes``) must not swamp the
#: index or the embed-fill sweep. Overflow is truncated and logged — never
#: silent — so the cap is observable in ops before anyone wonders why an
#: entity's tail text doesn't match.
MAX_CHUNKS_PER_ENTITY = 200

#: Deterministic separator between the sorted values of a multi-valued
#: attribute. A blank line, so the chunker's paragraph-preference naturally
#: avoids splitting mid-value.
VALUE_SEPARATOR = "\n\n"

# Standard RDF predicates used only to denormalize small display fields onto
# the chunk (so a hit renders with no Neptune round-trip). Never used to decide
# whether to index — that is the marker set's job. Mirrors spatiotemporal/extract.
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_LABEL_LOCALS = {"label", "name", "title"}

_SENTENCE_END_RE = re.compile(r"[.!?][)\"”']*(?:\s|$)")

#: Opt-OUT kill switch for the identity arm (ONTA-421), default **on**. It sits
#: INSIDE the already-opt-in ``COGRAPH_SEMANTIC_INDEX_ENABLED`` gate, so it can
#: only ever narrow an index that is already running. Read per call so ops can
#: flip it without a re-import, and consulted HERE — inside the one extractor
#: that BOTH the write hook and the reconciler call — so the two can never
#: disagree about whether an identity doc is expected. (A disagreement would be
#: catastrophic in the ordinary way this subsystem is catastrophic: the
#: reconciler would ghost-delete every identity doc the hook just wrote, every
#: hour, forever.)
IDENTITY_INDEX_ENV = "COGRAPH_SEMANTIC_IDENTITY_INDEX"


def identity_index_enabled() -> bool:
    """Whether to emit the per-entity identity doc (:data:`IDENTITY_INDEX_ENV`)."""
    raw = os.environ.get(IDENTITY_INDEX_ENV, "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def is_identity_predicate(predicate: str) -> bool:
    """True when ``predicate`` carries one of an entity's own names.

    ``rdfs:label`` by exact URI, or a ``label`` / ``name`` / ``title`` local
    name — exactly the set :func:`extract_semantic_chunks` harvests into the
    identity doc. Exported so the write hook and the reconciler can decide which
    entities own an identity doc without re-deriving the rule.
    """
    return predicate == _RDFS_LABEL or _local_name(predicate) in _LABEL_LOCALS


def is_identity_value(obj: str) -> bool:
    """True when ``obj`` is usable as a name: a NON-EMPTY, UNTYPED plain literal.

    Mirrors the acceptance test inside :func:`extract_semantic_chunks` exactly —
    a URI object is an entity reference, and a typed literal under a label
    predicate is a date or a number, not a name.
    """
    if not isinstance(obj, str) or _is_uri_object(obj):
        return False
    lexical, type_uri = _split_typed(obj)
    return bool(lexical.strip()) and type_uri is None


def _local_name(uri: str, *, lower: bool = True) -> str:
    """Last path/fragment segment of a URI (``…/types/Event/description`` →
    ``description``). Lower-cased by default for case-insensitive predicate
    matching; returns the input unchanged when it is not a URI. (Duplicated
    from ``spatiotemporal/extract.py`` — leaf-module independence.)"""
    if not isinstance(uri, str):
        return ""
    tail = uri.rsplit("#", 1)[-1]
    tail = tail.rsplit("/", 1)[-1]
    return tail.lower() if lower else tail


def _split_typed(obj: str) -> tuple[str, Optional[str]]:
    """Split ``"<lexical>^^<type-uri>"`` into ``(lexical, type_uri)``.

    Only treats the tail as a datatype when it is an ``http`` URI, so a plain
    string literal that happens to contain ``^^`` is left intact (type ``None``).
    (Duplicated from ``spatiotemporal/extract.py`` — leaf-module independence.)"""
    if isinstance(obj, str) and "^^" in obj:
        lexical, type_uri = obj.rsplit("^^", 1)
        if type_uri.startswith("http"):
            return lexical, type_uri
    return obj, None


def _is_uri_object(obj: str) -> bool:
    """True when the write path would emit ``obj`` as a URI, not a literal.

    Mirrors :func:`cograph_client.graph.queries._escape_value`'s decision
    exactly: a bare ``http(s)://…`` or an already-wrapped ``<…>`` is an entity
    reference / URI — never free text, even under a marked predicate (a marked
    local name can collide with a relation predicate on another type)."""
    if not isinstance(obj, str):
        return True
    return obj.startswith(("http://", "https://")) or (
        obj.startswith("<") and obj.endswith(">")
    )


def canonicalize_values(values: Iterable[str]) -> str:
    """Canonicalize an attribute's values into ONE deterministic document.

    Strip each value, drop empty/whitespace-only ones, remove exact
    duplicates, **sort**, and join with :data:`VALUE_SEPARATOR`. Sorting before
    hashing/chunking is the whole point: triple order is not stable across
    writers/replays, and ``content_hash`` must only change when content does.
    Returns ``""`` for no (usable) values.
    """
    cleaned = sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})
    return VALUE_SEPARATOR.join(cleaned)


def content_hash(canonical_doc: str) -> str:
    """sha256 hex digest of a canonicalized (entity, attr) document — the
    change-detection currency shared by the write hook, the reconciler, and
    the embed-fill sweep (see the protocol module docstring)."""
    return hashlib.sha256(canonical_doc.encode("utf-8")).hexdigest()


def _best_break(window: str, min_break: int) -> int:
    """Best split position in ``window`` (which is exactly the max chunk size).

    Preference order, each accepted only at/after ``min_break`` so chunks never
    degenerate into slivers: paragraph boundary (blank line) → sentence end →
    any newline → any space → hard cut at the window end (pathological
    unbroken text — the only case a word may be split).
    """
    pos = window.rfind("\n\n", min_break)
    if pos != -1:
        return pos
    last_sentence = -1
    for m in _SENTENCE_END_RE.finditer(window, min_break):
        last_sentence = m.end()
    if last_sentence != -1:
        return last_sentence
    pos = window.rfind("\n", min_break)
    if pos != -1:
        return pos
    pos = window.rfind(" ", min_break)
    if pos != -1:
        return pos
    return len(window)


def chunk_text(
    text: str,
    *,
    max_chars: int = MAX_CHUNK_CHARS,
    min_break: int = MIN_CHUNK_CHARS,
) -> list[str]:
    """Split ``text`` into chunks of at most ``max_chars`` characters.

    * empty / whitespace-only → ``[]`` (0 chunks);
    * anything up to ``max_chars`` → exactly 1 chunk;
    * longer text is split greedily at the best natural boundary in each
      ``max_chars`` window (see :func:`_best_break`), so chunks land in the
      ``[min_break, max_chars]`` range ≈ 256–512 estimated tokens.

    Chunk edges are whitespace-stripped (the boundary whitespace carries no
    content and would only perturb embeddings).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chars:
            chunks.append(rest)
            break
        cut = _best_break(rest[:max_chars], min_break)
        piece = rest[:cut].rstrip()
        if piece:
            chunks.append(piece)
        rest = rest[cut:].lstrip()
    return chunks


class _EntityAccumulator:
    """Per-subject scratch state collected in a single pass over the triples."""

    __slots__ = ("values", "label", "type_name", "identity_values")

    def __init__(self) -> None:
        # attr local name -> raw values, both in first-seen order (output
        # ordering only — canonicalize_values sorts before hashing).
        self.values: dict[str, list[str]] = {}
        self.label: Optional[str] = None
        self.type_name: Optional[str] = None
        # EVERY name-shaped value (label / name / title), not just the first:
        # the identity doc (ONTA-421) should match on ANY of an entity's names,
        # whereas ``label`` is a single display field.
        self.identity_values: list[str] = []


def extract_semantic_chunks(
    triples: list[Triple],
    *,
    tenant_id: str,
    kg_name: str,
    marked_predicates: Collection[str],
) -> list[SemanticChunk]:
    """Build :class:`SemanticChunk` rows for every marked free-text attribute
    among ``triples``. Pure and side-effect free (the cap warning is a log,
    not a mutation); deterministic for a given input set.

    ``marked_predicates`` is the caller's marker set/map (a ``dict``'s keys
    work — membership is all we test): an entry may be a full predicate URI
    (exact match) or a bare attribute local name, matched case-insensitively
    against the predicate's local name. NOTE the deliberate conflation: a
    local-name entry (``"description"``) marks that attribute on EVERY type —
    the marker map's granularity (ONTA-181) is the attribute name, matching
    how the ontology names attributes tenant-wide.

    Per (entity, attr): values are canonicalized (:func:`canonicalize_values`),
    hashed (:func:`content_hash`), chunked (:func:`chunk_text`). Edge cases:

    * empty/whitespace-only doc → 0 chunks (the ONTA-181 hook turns "had
      chunks before, has 0 now" into a ``delete(..., attr=…)``);
    * identical canonical docs within one entity (e.g. ``summary`` mirroring
      ``description``) are emitted ONCE — first attribute in triple order wins
      (intra-entity dedup: double-indexing the same text would double-count it
      in every ranking);
    * at most :data:`MAX_CHUNKS_PER_ENTITY` chunks per entity across all its
      attributes — overflow truncated + logged (never silent).

    Plus, marker-INDEPENDENTLY, one identity doc per entity under
    :data:`~cograph_client.semantic.protocol.IDENTITY_ATTR` (ONTA-421, see the
    module docstring): the entity's own names, emitted last so a marked
    name-source attribute wins the dedup, exempt from the chunk cap, and never
    embedded. Suppressed wholesale by ``COGRAPH_SEMANTIC_IDENTITY_INDEX=0``. An
    entity that has a name but NO marked attribute now yields a chunk where it
    previously yielded none — that is the whole point of the fix.
    """
    # Normalize the marker set once: exact entries as given, plus each entry's
    # lowered local name (a non-URI entry's local name is itself).
    marked_exact = {m for m in marked_predicates if isinstance(m, str)}
    marked_locals = {_local_name(m) for m in marked_exact}

    acc: dict[str, _EntityAccumulator] = {}
    order: list[str] = []

    for s, p, o in triples:
        if not isinstance(s, str) or not isinstance(p, str):
            continue
        ent = acc.get(s)
        if ent is None:
            ent = acc[s] = _EntityAccumulator()
            order.append(s)

        # rdf:type -> denormalized type display name (no effect on indexing).
        if p == _RDF_TYPE:
            if ent.type_name is None:
                ent.type_name = _local_name(o, lower=False)
            continue

        if not isinstance(o, str):
            continue
        lexical, type_uri = _split_typed(o)

        # Label / name for the denormalized display fields (plain literals
        # only) AND for the identity doc (ONTA-421). ``label`` keeps its
        # first-wins semantics — it is ONE display field — while
        # ``identity_values`` collects every name the entity carries.
        if p == _RDFS_LABEL or _local_name(p) in _LABEL_LOCALS:
            if lexical and type_uri is None and not _is_uri_object(o):
                if ent.label is None:
                    ent.label = lexical
                ent.identity_values.append(lexical)
            # NOT `continue`: a label predicate may itself be marked (e.g.
            # `title` on an Article) — it still contributes text below.

        if p not in marked_exact and _local_name(p) not in marked_locals:
            continue
        if _is_uri_object(o):
            continue  # entity reference, not free text — never index a URI
        # Any datatype's lexical form is accepted: marking is the gate, and a
        # marked predicate is free text by declaration.
        ent.values.setdefault(_local_name(p), []).append(lexical)

    chunks: list[SemanticChunk] = []
    emit_identity = identity_index_enabled()
    for uri in order:
        ent = acc[uri]
        identity_doc = (
            canonicalize_values(ent.identity_values) if emit_identity else ""
        )
        if not ent.values and not identity_doc:
            continue
        display: dict[str, str] = {}
        if ent.label:
            display["label"] = ent.label
        if ent.type_name:
            display["type"] = ent.type_name

        seen_hashes: set[str] = set()  # intra-entity doc dedup
        entity_chunk_count = 0
        for attr, values in ent.values.items():
            doc = canonicalize_values(values)
            if not doc:
                continue  # empty/whitespace-only -> 0 chunks
            doc_hash = content_hash(doc)
            if doc_hash in seen_hashes:
                logger.debug(
                    "semantic_extract_duplicate_doc",
                    entity_uri=uri,
                    attr=attr,
                    content_hash=doc_hash,
                )
                continue
            seen_hashes.add(doc_hash)

            pieces = chunk_text(doc)
            budget = MAX_CHUNKS_PER_ENTITY - entity_chunk_count
            if len(pieces) > budget:
                logger.warning(
                    "semantic_extract_chunk_cap",
                    entity_uri=uri,
                    attr=attr,
                    cap=MAX_CHUNKS_PER_ENTITY,
                    produced=len(pieces),
                    dropped=len(pieces) - budget,
                )
                pieces = pieces[:budget]
            for ix, piece in enumerate(pieces):
                chunks.append(
                    SemanticChunk(
                        tenant_id=tenant_id,
                        kg_name=kg_name,
                        entity_uri=uri,
                        attr=attr,
                        chunk_ix=ix,
                        chunk_text=piece,
                        content_hash=doc_hash,
                        attrs=dict(display),
                    )
                )
            entity_chunk_count += len(pieces)

        # The identity doc, LAST (ONTA-421). Last so that a marked attribute
        # which happens to BE a name source (a marked ``title``) wins the
        # intra-entity dedup and keeps both its own attr name and its
        # embedding: the identity arm adds findability, it never relocates an
        # existing free-text doc out of the semantic leg. Exempt from the
        # per-entity chunk budget — it is one short chunk, and an entity must
        # not become unfindable BY NAME because its prose used up the cap.
        if identity_doc:
            doc_hash = content_hash(identity_doc)
            if doc_hash in seen_hashes:
                logger.debug(
                    "semantic_extract_identity_mirrors_marked_doc",
                    entity_uri=uri,
                    content_hash=doc_hash,
                )
            else:
                # A handful of names — chunk_text always returns exactly one.
                for ix, piece in enumerate(chunk_text(identity_doc)):
                    chunks.append(
                        SemanticChunk(
                            tenant_id=tenant_id,
                            kg_name=kg_name,
                            entity_uri=uri,
                            attr=IDENTITY_ATTR,
                            chunk_ix=ix,
                            chunk_text=piece,
                            content_hash=doc_hash,
                            attrs=dict(display),
                        )
                    )
    return chunks
