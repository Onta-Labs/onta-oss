"""Marker-driven semantic chunk extraction — canonicalization, hashing,
chunking, and every edge case in the ONTA-175 contract (empty/whitespace → 0
chunks, short → 1, multi-value sort-before-hash, intra-entity dedup, the
per-entity chunk cap logged-never-silent).

Plus the ONTA-421 IDENTITY ARM: the extra, marker-independent per-entity doc
that makes an entity findable by its own name. Its contract is narrow on
purpose — emitted last (so a marked name-source attribute wins the dedup),
exempt from the chunk cap, never embedded — and every clause of it is pinned
below, because each one exists to keep the fix from disturbing the free-text
index it sits beside.
"""

from __future__ import annotations

import hashlib

import structlog

from infona_client.semantic.extract import (
    MAX_CHUNK_CHARS,
    MAX_CHUNKS_PER_ENTITY,
    MAX_IDENTITY_CHUNKS,
    MIN_CHUNK_CHARS,
    VALUE_SEPARATOR,
    canonicalize_values,
    chunk_text,
    content_hash,
    extract_semantic_chunks,
    is_identity_predicate,
    is_identity_value,
)
from infona_client.semantic.protocol import IDENTITY_ATTR

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"

TENANT = "demo-tenant"
KG = "EventsSF"
MARKED = {"description", "notes", "bio"}


def _desc(uri: str, text: str, attr: str = "description") -> tuple:
    return (uri, f"https://graph.infona.ai/types/Event/{attr}", text)


def _extract(triples, marked=MARKED):
    return extract_semantic_chunks(
        triples, tenant_id=TENANT, kg_name=KG, marked_predicates=marked
    )


# ---------------------------------------------------------------------------
# chunk_text — the char-estimation chunker
# ---------------------------------------------------------------------------


def test_chunk_empty_and_whitespace_yield_zero_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\t  \n") == []


def test_chunk_short_text_is_exactly_one_chunk():
    assert chunk_text("A short description.") == ["A short description."]
    # Right at the ceiling still fits in one chunk.
    exact = "x" * MAX_CHUNK_CHARS
    assert chunk_text(exact) == [exact]


def test_chunk_long_text_splits_within_bounds():
    text = " ".join(f"Sentence number {i} says something useful." for i in range(200))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    for c in chunks:
        assert 0 < len(c) <= MAX_CHUNK_CHARS
    # Natural (sentence/whitespace) breaks: no word is ever split, so the
    # multiset of words survives the round-trip.
    assert " ".join(chunks).split() == text.split()


def test_chunk_prefers_paragraph_boundaries():
    # Two paragraphs, each individually under the max but jointly over it: the
    # split must land on the blank line, not mid-paragraph.
    para1 = "First paragraph. " * 80  # ~1360 chars
    para2 = "Second paragraph. " * 80
    chunks = chunk_text(f"{para1.strip()}\n\n{para2.strip()}")
    assert len(chunks) == 2
    assert chunks[0] == para1.strip()
    assert chunks[1] == para2.strip()


def test_chunk_unbroken_text_hard_cuts():
    """Pathological text with no whitespace at all must still terminate, via
    hard cuts at the window edge (the only case a 'word' is split)."""
    blob = "a" * (MAX_CHUNK_CHARS * 3 + 100)
    chunks = chunk_text(blob)
    assert "".join(chunks) == blob
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)


def test_chunk_no_sliver_chunks():
    """Natural breaks are only accepted at/after MIN_CHUNK_CHARS, so no chunk
    except the final remainder can be a tiny sliver."""
    text = "Word. " * 2000
    chunks = chunk_text(text)
    assert all(len(c) >= MIN_CHUNK_CHARS for c in chunks[:-1])


# ---------------------------------------------------------------------------
# canonicalize_values / content_hash
# ---------------------------------------------------------------------------


def test_canonicalize_sorts_dedups_and_strips():
    assert canonicalize_values(["  b value ", "a value", "b value"]) == (
        f"a value{VALUE_SEPARATOR}b value"
    )
    assert canonicalize_values(["", "   ", "\n"]) == ""


def test_content_hash_is_sha256_of_canonical_doc():
    doc = "a value"
    assert content_hash(doc) == hashlib.sha256(doc.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# extract_semantic_chunks — basics
# ---------------------------------------------------------------------------


def test_extracts_marked_predicate_into_keyed_chunk():
    chunks = _extract(
        [
            ("e:1", RDF_TYPE, "https://graph.infona.ai/types/Event"),
            ("e:1", RDFS_LABEL, "Solar Expo"),
            _desc("e:1", "An expo about solar panels."),
        ]
    )
    # Two docs: the marked free-text one, plus the ONTA-421 identity doc built
    # from the label. The marked doc is FIRST — ordering is contractual (the
    # identity doc is emitted last so it loses the intra-entity dedup).
    assert [c.attr for c in chunks] == ["description", IDENTITY_ATTR]
    c = chunks[0]
    assert c.key() == (TENANT, KG, "e:1", "description", 0)
    assert c.chunk_text == "An expo about solar panels."
    assert c.content_hash == content_hash("An expo about solar panels.")
    # Denormalized display attrs, mirroring the spatio-temporal facts.
    assert c.attrs == {"label": "Solar Expo", "type": "Event"}
    # Fresh rows are always pending-embed: the NULL embedding IS the queue.
    assert c.embedding is None and c.embed_model is None
    assert c.attempt_count == 0 and c.last_error is None


def test_unmarked_predicates_are_ignored():
    chunks = _extract(
        [
            ("e:1", "https://graph.infona.ai/types/Event/sku", "ABC-123"),
            ("e:1", "https://graph.infona.ai/types/Event/venue_name", "Moscone"),
        ]
    )
    assert chunks == []


def test_marker_matches_full_uri_and_local_name():
    pred = "https://graph.infona.ai/types/Event/description"
    by_uri = _extract([("e:1", pred, "Some text.")], marked={pred})
    by_local = _extract([("e:1", pred, "Some text.")], marked={"description"})
    by_local_cased = _extract([("e:1", pred, "Some text.")], marked={"Description"})
    assert len(by_uri) == len(by_local) == len(by_local_cased) == 1
    # attr is always the (lowered) local name regardless of marker form.
    assert {c.attr for c in by_uri + by_local + by_local_cased} == {"description"}


def test_uri_objects_are_never_indexed():
    """A relation predicate sharing a marked local name must not index the
    target URI as text (mirrors _escape_value's URI-vs-literal decision)."""
    chunks = _extract(
        [
            ("e:1", "https://graph.infona.ai/types/Event/description", "https://graph.infona.ai/entities/e2"),
            ("e:1", "https://graph.infona.ai/types/Event/description", "<https://graph.infona.ai/entities/e3>"),
        ]
    )
    assert chunks == []


def test_typed_literal_datatype_is_stripped():
    chunks = _extract([_desc("e:1", f"Plain text value.^^{XSD_STRING}")])
    assert len(chunks) == 1
    assert chunks[0].chunk_text == "Plain text value."
    assert chunks[0].content_hash == content_hash("Plain text value.")


# ---------------------------------------------------------------------------
# edge cases from the ONTA-175 contract
# ---------------------------------------------------------------------------


def test_empty_and_whitespace_values_yield_zero_chunks():
    assert _extract([_desc("e:1", "")]) == []
    assert _extract([_desc("e:1", "   \n\t ")]) == []


def test_short_value_yields_exactly_one_chunk():
    chunks = _extract([_desc("e:1", "Short.")])
    assert [c.chunk_ix for c in chunks] == [0]


def test_long_value_yields_contiguous_chunks_sharing_one_hash():
    text = " ".join(f"Sentence {i} of the very long description." for i in range(150))
    chunks = _extract([_desc("e:1", text)])
    assert len(chunks) > 1
    assert [c.chunk_ix for c in chunks] == list(range(len(chunks)))
    # Every chunk of the (entity, attr) doc carries the SAME doc-level hash.
    assert {c.content_hash for c in chunks} == {content_hash(text)}


def test_multivalued_attribute_is_sorted_before_hashing():
    """Triple order must not change the doc or its hash (sort-before-hash)."""
    forward = _extract([_desc("e:1", "beta value"), _desc("e:1", "alpha value")])
    backward = _extract([_desc("e:1", "alpha value"), _desc("e:1", "beta value")])
    expected_doc = f"alpha value{VALUE_SEPARATOR}beta value"
    assert forward[0].chunk_text == expected_doc
    assert [c.content_hash for c in forward] == [c.content_hash for c in backward]
    assert forward[0].content_hash == content_hash(expected_doc)


def test_intra_entity_duplicate_values_dedup():
    """The same value repeated (duplicate triples) hashes like a single value."""
    chunks = _extract([_desc("e:1", "same text"), _desc("e:1", "same text")])
    assert len(chunks) == 1
    assert chunks[0].content_hash == content_hash("same text")


def test_intra_entity_duplicate_docs_across_attrs_dedup():
    """Two marked attrs carrying the identical doc index ONCE (first attr in
    triple order wins) — double-indexing would double-count in every ranking."""
    chunks = _extract(
        [
            _desc("e:1", "mirrored text", attr="description"),
            _desc("e:1", "mirrored text", attr="notes"),
        ]
    )
    assert len(chunks) == 1
    assert chunks[0].attr == "description"
    # The same doc on ANOTHER entity is not deduped — the dedup is per entity.
    chunks2 = _extract(
        [
            _desc("e:1", "mirrored text", attr="description"),
            _desc("e:2", "mirrored text", attr="notes"),
        ]
    )
    assert {(c.entity_uri, c.attr) for c in chunks2} == {
        ("e:1", "description"),
        ("e:2", "notes"),
    }


def test_chunk_cap_truncates_and_logs_never_silent():
    # ~120k tiny sentences -> far beyond 200 chunks' worth of text.
    big = " ".join(f"word{i}." for i in range(120_000))
    with structlog.testing.capture_logs() as logs:
        chunks = _extract([_desc("e:1", big)])
    assert len(chunks) == MAX_CHUNKS_PER_ENTITY
    assert [c.chunk_ix for c in chunks] == list(range(MAX_CHUNKS_PER_ENTITY))
    cap_events = [l for l in logs if l["event"] == "semantic_extract_chunk_cap"]
    assert cap_events and cap_events[0]["log_level"] == "warning"
    assert cap_events[0]["entity_uri"] == "e:1"
    assert cap_events[0]["dropped"] > 0


def test_chunk_cap_spans_all_attrs_of_one_entity():
    """The cap is per ENTITY: a second attr only gets the remaining budget."""
    big = " ".join(f"word{i}." for i in range(120_000))
    with structlog.testing.capture_logs():
        chunks = _extract(
            [_desc("e:1", big, attr="description"), _desc("e:1", "tiny", attr="notes")]
        )
    assert len(chunks) == MAX_CHUNKS_PER_ENTITY
    assert all(c.attr == "description" for c in chunks)  # notes got budget 0


def test_cap_does_not_leak_across_entities():
    big = " ".join(f"word{i}." for i in range(120_000))
    with structlog.testing.capture_logs():
        chunks = _extract([_desc("e:1", big), _desc("e:2", "small doc")])
    by_entity = {c.entity_uri for c in chunks}
    assert by_entity == {"e:1", "e:2"}
    assert sum(1 for c in chunks if c.entity_uri == "e:2") == 1


# ---------------------------------------------------------------------------
# denormalized attrs + multi-entity behavior
# ---------------------------------------------------------------------------


def test_attrs_empty_when_no_label_or_type():
    chunks = _extract([_desc("e:1", "No display fields here.")])
    assert chunks[0].attrs == {}


# ---------------------------------------------------------------------------
# The identity arm (ONTA-421) — an entity must be findable by its own NAME
# ---------------------------------------------------------------------------


def _identity(chunks):
    return [c for c in chunks if c.attr == IDENTITY_ATTR]


def test_named_entity_with_no_marked_attribute_still_gets_a_chunk():
    """The ONTA-421 regression itself.

    Names are short, so they are LABEL/CODE_ID-shaped and can NEVER be marked
    free text — which meant this entity produced NO chunks at all and was
    permanently unfindable by its own name, no matter how often the index was
    rebuilt. It must now produce exactly one identity chunk.
    """
    chunks = _extract(
        [
            ("e:1", RDF_TYPE, "https://graph.infona.ai/types/Company"),
            ("e:1", RDFS_LABEL, "Acme Corporation"),
            ("e:1", "https://graph.infona.ai/types/Company/attrs/sku", "AC-1"),
        ]
    )
    assert len(chunks) == 1
    c = chunks[0]
    assert c.key() == (TENANT, KG, "e:1", IDENTITY_ATTR, 0)
    assert c.chunk_text == "Acme Corporation"
    assert c.content_hash == content_hash("Acme Corporation")
    assert c.attrs == {"label": "Acme Corporation", "type": "Company"}
    # Fresh row, like any other — but the backends' fetch_pending never hands
    # it to the embed sweep, so this NULL is permanent by contract.
    assert c.embedding is None and c.embed_model is None


def test_identity_doc_collects_every_name_predicate():
    """label / name / title all feed ONE doc, canonicalized like any other
    (stripped, deduped, SORTED, blank-line joined) so the hash is stable."""
    chunks = _extract(
        [
            ("e:1", RDFS_LABEL, "Acme Corp"),
            ("e:1", "https://graph.infona.ai/types/Company/attrs/name", "Acme Corporation"),
            ("e:1", "https://graph.infona.ai/types/Company/attrs/title", "ACME"),
            ("e:1", "https://graph.infona.ai/types/Company/attrs/title", "  Acme Corp  "),
        ]
    )
    (ident,) = _identity(chunks)
    assert ident.chunk_text == VALUE_SEPARATOR.join(
        ["ACME", "Acme Corp", "Acme Corporation"]
    )
    # `label` stays the FIRST name seen — it is one display field, not the doc.
    assert ident.attrs["label"] == "Acme Corp"


def test_identity_mirroring_a_marked_doc_is_deduped_and_the_marked_doc_wins():
    """A marked attribute that is ALSO a name source keeps its own attr name.

    This is what stops the identity arm from being a regression: if identity
    won the dedup instead, a marked `title` would move to a doc that is never
    embedded and would silently drop out of the vector leg.
    """
    chunks = _extract(
        [("e:1", "https://graph.infona.ai/types/Doc/title", "Solar Expo")],
        marked={"title"},
    )
    assert [c.attr for c in chunks] == ["title"]


def test_identity_ignores_uri_and_typed_label_values():
    """A URI object is an entity reference and a typed literal under a label
    predicate is a date/number — neither is a name."""
    chunks = _extract(
        [
            ("e:1", RDFS_LABEL, "https://graph.infona.ai/entities/Doc/e2"),
            (
                "e:1",
                "https://graph.infona.ai/types/Doc/attrs/name",
                f"2024-01-01^^{XSD_DATE}",
            ),
            ("e:1", "https://graph.infona.ai/types/Doc/attrs/title", "   "),
        ]
    )
    assert chunks == []


def test_identity_survives_the_per_entity_chunk_cap():
    """Exempt from MAX_CHUNKS_PER_ENTITY: a text-heavy entity must not become
    unfindable BY NAME because its prose spent the budget."""
    big = " ".join(f"word{i}." for i in range(120_000))
    with structlog.testing.capture_logs():
        chunks = _extract([("e:1", RDFS_LABEL, "Acme Corporation"), _desc("e:1", big)])
    assert len(chunks) == MAX_CHUNKS_PER_ENTITY + 1
    (ident,) = _identity(chunks)
    assert ident.chunk_text == "Acme Corporation"


def test_identity_doc_has_its_own_cap_so_exempt_never_means_unbounded():
    """Being exempt from MAX_CHUNKS_PER_ENTITY leaves the identity doc with no
    budget at all unless it carries its own. A pathological entity (an ER merge
    collapsing thousands of subjects onto one URI) must not emit unbounded
    identity rows — truncated and LOGGED, never silent."""
    names = [f"Acme Corporation subsidiary number {i}" for i in range(20_000)]
    triples = [
        ("e:1", "https://graph.infona.ai/types/Company/attrs/name", n) for n in names
    ]
    with structlog.testing.capture_logs() as logs:
        chunks = _extract(triples)
    assert len(chunks) == MAX_IDENTITY_CHUNKS
    assert [c.chunk_ix for c in chunks] == list(range(MAX_IDENTITY_CHUNKS))
    assert all(c.attr == IDENTITY_ATTR for c in chunks)
    cap_events = [l for l in logs if l["event"] == "semantic_extract_identity_cap"]
    assert cap_events and cap_events[0]["log_level"] == "warning"
    assert cap_events[0]["entity_uri"] == "e:1"
    assert cap_events[0]["dropped"] > 0


def test_identity_arm_can_be_switched_off(monkeypatch):
    """Opt-OUT kill switch — and when it is off, the named-but-unmarked entity
    goes back to producing nothing (i.e. the switch really governs the arm)."""
    monkeypatch.setenv("INFONA_SEMANTIC_IDENTITY_INDEX", "0")
    assert _extract([("e:1", RDFS_LABEL, "Acme Corporation")]) == []
    # ...and a marked doc is completely unaffected by the switch.
    chunks = _extract([("e:1", RDFS_LABEL, "Acme"), _desc("e:1", "Some prose.")])
    assert [c.attr for c in chunks] == ["description"]


def test_identity_predicate_and_value_helpers_mirror_the_extractor():
    """The write hook and the reconciler decide which entities own an identity
    doc through THESE helpers rather than re-deriving the rule; if they ever
    disagreed with the extractor the hook would delete the docs it just
    wrote."""
    assert is_identity_predicate(RDFS_LABEL)
    assert is_identity_predicate("https://graph.infona.ai/types/Doc/attrs/Name")
    assert not is_identity_predicate("https://graph.infona.ai/types/Doc/attrs/venue_name")
    assert is_identity_value("Acme Corporation")
    assert not is_identity_value("https://graph.infona.ai/entities/Doc/e2")
    assert not is_identity_value(f"2024-01-01^^{XSD_DATE}")
    assert not is_identity_value("   ")


def test_label_from_name_local_and_first_type_wins():
    chunks = _extract(
        [
            ("e:1", "https://graph.infona.ai/types/Person/name", "Ada Lovelace"),
            ("e:1", RDF_TYPE, "https://graph.infona.ai/types/Person"),
            ("e:1", RDF_TYPE, "https://graph.infona.ai/types/Author"),
            ("e:1", "https://graph.infona.ai/types/Person/bio", "Wrote the first program."),
        ]
    )
    assert chunks[0].attrs == {"label": "Ada Lovelace", "type": "Person"}


def test_marked_label_predicate_contributes_text_and_display():
    """A predicate can be BOTH the display label and a marked text attr
    (e.g. `title` on an Article) — it must serve both roles."""
    chunks = _extract(
        [("e:1", "https://graph.infona.ai/types/Article/title", "A Grand Title")],
        marked={"title"},
    )
    assert len(chunks) == 1
    assert chunks[0].attr == "title"
    assert chunks[0].chunk_text == "A Grand Title"
    assert chunks[0].attrs == {"label": "A Grand Title"}


def test_multiple_entities_extracted_independently():
    chunks = _extract(
        [
            _desc("e:1", "First entity text."),
            _desc("e:2", "Second entity text."),
        ]
    )
    assert {(c.entity_uri, c.chunk_ix) for c in chunks} == {("e:1", 0), ("e:2", 0)}
    assert all(c.tenant_id == TENANT and c.kg_name == KG for c in chunks)


def test_marker_map_keys_work_as_marker_set():
    """The ONTA-181 hook holds a marker MAP; membership over its keys must be
    enough (no dedicated map handling in the extractor)."""
    marker_map = {"description": {"marked": True, "source": "auto"}}
    chunks = _extract([_desc("e:1", "Map-marked text.")], marked=marker_map)
    assert len(chunks) == 1


def test_extract_is_deterministic():
    triples = [
        ("e:1", RDF_TYPE, "https://graph.infona.ai/types/Event"),
        _desc("e:1", "beta"),
        _desc("e:1", "alpha"),
        _desc("e:2", "other"),
    ]
    a = _extract(triples)
    b = _extract(triples)
    assert [(c.key(), c.chunk_text, c.content_hash) for c in a] == [
        (c.key(), c.chunk_text, c.content_hash) for c in b
    ]
