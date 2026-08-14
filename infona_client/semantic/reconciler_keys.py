"""Doc-key helpers + Assertion→triple projection for the semantic reconciler."""

from __future__ import annotations

from typing import Sequence

from infona_client.semantic.extract import (
    _is_uri_object,
    _local_name,
    identity_index_enabled,
    is_identity_predicate,
    is_identity_value,
)
from infona_client.semantic.protocol import IDENTITY_ATTR
from infona_client.semantic.reconciler_const import Triple, _RDF_TYPE


def marked_doc_keys(
    triples: Sequence[Triple], marked_predicates: set[str]
) -> set[tuple[str, str]]:
    """(entity_uri, attr-local-name) pairs carrying a marked LITERAL value.

    The write hook diffs this against what ``extract_semantic_chunks`` actually
    emitted to find docs that must be DELETED for this write: a marked attr
    whose canonicalized doc came out empty (values all blank), or one deduped
    away because it mirrors another attr's doc (extract indexes identical docs
    once). Matching mirrors the extractor exactly — exact predicate URI OR
    lower-cased local name — via the extractor's own helpers, so the two can
    never disagree about what "marked" means.
    """
    marked_locals = {_local_name(m) for m in marked_predicates}
    keys: set[tuple[str, str]] = set()
    for s, p, o in triples:
        if not isinstance(s, str) or not isinstance(p, str) or not isinstance(o, str):
            continue
        if p not in marked_predicates and _local_name(p) not in marked_locals:
            continue
        if _is_uri_object(o):
            continue  # entity reference, never a text doc
        keys.add((s, _local_name(p)))
    return keys


def identity_doc_keys(triples: Sequence[Triple]) -> set[tuple[str, str]]:
    """``(entity_uri, IDENTITY_ATTR)`` for every entity carrying a NAME here.

    The identity-arm counterpart of :func:`marked_doc_keys` (ONTA-421), and
    marker-independent for the same reason the doc itself is: whether a thing
    has a name is not a candidacy question. Acceptance is delegated to the
    extractor's own exported predicates (``is_identity_predicate`` /
    ``is_identity_value``) so the two can never disagree about which entities
    own an identity doc — a disagreement would make the write hook delete the
    docs it just wrote.
    """
    keys: set[tuple[str, str]] = set()
    for s, p, o in triples:
        if not isinstance(s, str) or not isinstance(p, str) or not isinstance(o, str):
            continue
        if is_identity_predicate(p) and is_identity_value(o):
            keys.add((s, IDENTITY_ATTR))
    return keys


def indexable_doc_keys(
    triples: Sequence[Triple], marked_predicates: set[str]
) -> set[tuple[str, str]]:
    """Every doc key these triples imply: marked free-text docs PLUS identity
    docs (when the identity arm is on).

    This — not :func:`marked_doc_keys` — is what the write hook diffs, on BOTH
    sides: it decides which entities a write TOUCHED, and which of a re-read
    entity's docs came out EMPTY and must be deleted. Using the marked-only set
    on either side would be a bug in a different direction: on the touched side
    a name-only write would index nothing (the ONTA-421 symptom itself); on the
    emptied side every identity doc the extractor had just written would
    immediately be deleted again as an unexpected key.
    """
    keys = marked_doc_keys(triples, marked_predicates) if marked_predicates else set()
    if identity_index_enabled():
        keys |= identity_doc_keys(triples)
    return keys


def _assertion_row_to_semantic_triples(row: dict) -> list[Triple]:
    """Project one Assertion SoT row into RDF-shaped triples for extraction.

    Shared by the write-hook re-read (``kg_writer``) and the reconciler scan so
    the two never disagree on how GraphStore facts map into the extractor's
    triple vocabulary (ONTA-533). Lives here (not in ``kg_writer``) to avoid a
    circular import: the hook already imports schedule helpers from this module.
    """
    from infona_client.graph.assertion_model import type_membership_property_id

    s = row.get("subject_id") or ""
    if not s:
        return []
    prop = row.get("property_id") or ""
    out: list[Triple] = []
    type_prop = type_membership_property_id()
    if prop == type_prop or (
        isinstance(prop, str) and prop.endswith("/properties/rdf_type")
    ):
        class_id = row.get("object_class_id") or ""
        if class_id:
            out.append((s, _RDF_TYPE, str(class_id)))
        return out
    if not prop:
        return out
    lit = row.get("literal_value")
    if lit is not None:
        if isinstance(lit, list):
            for v in lit:
                if v is not None:
                    out.append((s, prop, str(v)))
        else:
            out.append((s, prop, str(lit)))
        return out
    obj = row.get("object_id")
    if obj:
        out.append((s, prop, str(obj)))
    return out
