"""Score-time entity alignment and restated-add drop.

Gold entity URIs are author-stable and must not appear in task input or the
schema hint. When the model mints a different slug, ``graph_delta_prf`` on
raw strings is a miss even if type_id / attr / value are right. This module
rewrites the predicted delta before those exact-set metrics run.

Alignment (first match wins; no gold URI is written into the prompt):
1. legalName value (case-insensitive) predicted → gold
2. input mention / record.name matching a gold legalName, one leftover pred
3. single predicted entity + single gold entity

Adds that only restate a type_assertion or literal on the same subject are
dropped so they cannot zero precision when gold.adds is empty.
"""

from __future__ import annotations

from typing import Any, Mapping

from .graph_delta import (
    GraphDelta,
    LiteralSet,
    Merge,
    Triple,
    TypeAssertion,
    TypeExtension,
)
from .models import Ontology

ENT_PREFIX = "https://graph.infona.ai/bench/ent/"


def prepare_for_score(
    predicted: GraphDelta,
    gold: GraphDelta,
    task_input: Mapping[str, Any] | None = None,
    ontology: Ontology | None = None,
) -> GraphDelta:
    """Drop restated adds and ancestor types, then align minted entities."""
    cleaned = drop_restated_adds(predicted)
    if ontology is not None:
        extras = (*predicted.type_extensions, *gold.type_extensions)
        cleaned = keep_leaf_types(cleaned, ontology, extras)
    mapping = entity_alignment(cleaned, gold, task_input or {})
    return rewrite_delta(cleaned, mapping)


def keep_leaf_types(
    delta: GraphDelta,
    ontology: Ontology,
    extra_extensions: tuple[TypeExtension, ...] = (),
) -> GraphDelta:
    """Keep only the most specific type_ids per entity.

    If Supplier and Company are both asserted, Company is an ancestor in the
    fixture ontology and is dropped. Sibling types (Person + Supplier) stay.
    Unknown ids are not treated as ancestors.
    """
    parents = {tid: typ.parent_ids for tid, typ in ontology.types.items()}
    for ext in extra_extensions:
        if ext.type_id and ext.parent_id:
            parents[ext.type_id] = (ext.parent_id,)
    cache: dict[str, frozenset[str]] = {}

    def ancestors(type_id: str) -> frozenset[str]:
        if type_id in cache:
            return cache[type_id]
        found: set[str] = set()
        stack = list(parents.get(type_id, ()))
        while stack:
            nid = stack.pop()
            if nid in found:
                continue
            found.add(nid)
            stack.extend(parents.get(nid, ()))
        cache[type_id] = frozenset(found)
        return cache[type_id]

    by_entity: dict[str, list[TypeAssertion]] = {}
    for item in delta.type_assertions:
        by_entity.setdefault(item.entity, []).append(item)
    kept: list[TypeAssertion] = []
    for assertions in by_entity.values():
        ids = {item.type_id for item in assertions}
        for item in assertions:
            if any(item.type_id in ancestors(other) for other in ids if other != item.type_id):
                continue
            kept.append(item)
    return GraphDelta(
        adds=delta.adds,
        deletes=delta.deletes,
        type_assertions=tuple(kept),
        literals=delta.literals,
        merges=delta.merges,
        type_extensions=delta.type_extensions,
        constraint_repairs=delta.constraint_repairs,
    )


def drop_restated_adds(delta: GraphDelta) -> GraphDelta:
    """Remove adds whose object is already a structured fact on the subject."""
    values: dict[str, set[str]] = {}
    for item in delta.literals:
        values.setdefault(item.entity, set()).add(item.value)
    types: dict[str, set[str]] = {}
    for item in delta.type_assertions:
        types.setdefault(item.entity, set()).add(item.type_id)
    kept: list[Triple] = []
    for triple in delta.adds:
        restated = values.get(triple.subject, set()) | types.get(
            triple.subject, set()
        )
        if triple.object in restated:
            continue
        kept.append(triple)
    return GraphDelta(
        adds=tuple(kept),
        deletes=delta.deletes,
        type_assertions=delta.type_assertions,
        literals=delta.literals,
        merges=delta.merges,
        type_extensions=delta.type_extensions,
        constraint_repairs=delta.constraint_repairs,
    )


def entity_alignment(
    predicted: GraphDelta,
    gold: GraphDelta,
    task_input: Mapping[str, Any],
) -> dict[str, str]:
    """Map predicted entity URIs onto gold URIs. Does not mutate gold."""
    mapping: dict[str, str] = {}
    gold_by_name = _legal_names(gold)
    pred_by_name = _legal_names(predicted)
    for name, pred_ent in pred_by_name.items():
        gold_ent = gold_by_name.get(name)
        if gold_ent and pred_ent != gold_ent:
            mapping[pred_ent] = gold_ent
    gold_ents = _entities(gold)
    pred_ents = _entities(predicted)
    unused_pred = [e for e in pred_ents if e not in mapping]
    unused_gold = [e for e in gold_ents if e not in mapping.values()]
    mention_names = {_norm(s) for s in _mention_strings(task_input)}
    for name, gold_ent in gold_by_name.items():
        if gold_ent not in unused_gold or name not in mention_names:
            continue
        if len(unused_pred) == 1:
            pred_ent = unused_pred[0]
            if pred_ent != gold_ent:
                mapping[pred_ent] = gold_ent
            unused_pred = []
            unused_gold = [e for e in unused_gold if e != gold_ent]
            break
    if len(unused_pred) == 1 and len(unused_gold) == 1:
        pred_ent, gold_ent = unused_pred[0], unused_gold[0]
        if pred_ent != gold_ent:
            mapping[pred_ent] = gold_ent
    return mapping


def rewrite_delta(delta: GraphDelta, mapping: Mapping[str, str]) -> GraphDelta:
    def ent(uri: str) -> str:
        return mapping.get(uri, uri)

    return GraphDelta(
        adds=tuple(
            Triple(ent(t.subject), t.predicate, ent(t.object)) for t in delta.adds
        ),
        deletes=tuple(
            Triple(ent(t.subject), t.predicate, ent(t.object))
            for t in delta.deletes
        ),
        type_assertions=tuple(
            TypeAssertion(ent(t.entity), t.type_id) for t in delta.type_assertions
        ),
        literals=tuple(
            LiteralSet(ent(item.entity), item.attr, item.value)
            for item in delta.literals
        ),
        merges=tuple(
            Merge(ent(m.absorbed), ent(m.survivor)) for m in delta.merges
        ),
        type_extensions=delta.type_extensions,
        constraint_repairs=delta.constraint_repairs,
    )


def _entities(delta: GraphDelta) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(uri: str) -> None:
        if uri and uri not in seen:
            seen.add(uri)
            found.append(uri)

    for item in delta.type_assertions:
        add(item.entity)
    for item in delta.literals:
        add(item.entity)
    for triple in (*delta.adds, *delta.deletes):
        if _looks_like_entity(triple.subject):
            add(triple.subject)
        if _looks_like_entity(triple.object):
            add(triple.object)
    for merge in delta.merges:
        add(merge.absorbed)
        add(merge.survivor)
    return found


def _looks_like_entity(uri: str) -> bool:
    return uri.startswith(ENT_PREFIX)


def _legal_names(delta: GraphDelta) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in delta.literals:
        if item.attr == "legalName" and item.value:
            out[_norm(item.value)] = item.entity
    return out


def _mention_strings(task_input: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    mention = task_input.get("mention")
    if isinstance(mention, str) and mention:
        out.append(mention)
    record = task_input.get("record")
    if isinstance(record, dict):
        name = record.get("name")
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())
