"""SPARQL helpers for snapshot copy/clear/drop + release metadata triples."""

from __future__ import annotations

from datetime import datetime, timezone

from infona_client.graph.ontology_queries_uris import XSD
from infona_client.graph.ontology_snapshots_models import (
    SnapshotPlan,
    _REL_COMPAT,
    _REL_DELTA,
    _REL_FINGERPRINT,
    _REL_KIND,
    _REL_LAYER,
    _REL_OF,
    _REL_PARENT,
    _REL_PUBLISHER,
    _REL_SNAPSHOT,
    _REL_SUMMARY,
    _REL_TIMESTAMP,
    _REL_TYPE,
    _REL_VERSION,
    _release_subject,
)

import json


def _copy_graph_sparql(source: str, target: str) -> str:
    """INSERT-SELECT copy of every triple from ``source`` into ``target``."""
    return (
        f"INSERT {{ GRAPH <{target}> {{ ?s ?p ?o }} }}\n"
        f"WHERE {{ GRAPH <{source}> {{ ?s ?p ?o }} }}"
    )


def _clear_graph_sparql(graph_uri: str) -> str:
    return f"CLEAR SILENT GRAPH <{graph_uri}>"


def _drop_graph_sparql(graph_uri: str) -> str:
    return f"DROP SILENT GRAPH <{graph_uri}>"


def _release_metadata_triples(
    plan: SnapshotPlan,
    *,
    publisher: str | None,
    change_summary: str | None,
    compat_class: str | None,
    timestamp: str | None,
) -> list[tuple[str, str, str]]:
    subj = _release_subject(plan.live_graph_uri, plan.version, plan.kind)
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    triples: list[tuple[str, str, str]] = [
        (subj, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", _REL_TYPE),
        (subj, _REL_OF, plan.live_graph_uri),
        (subj, _REL_VERSION, f"{plan.version}^^{XSD}#integer"),
        (subj, _REL_LAYER, plan.layer),
        (subj, _REL_KIND, plan.kind),
        (subj, _REL_FINGERPRINT, plan.fingerprint),
        (subj, _REL_SNAPSHOT, plan.snapshot_graph_uri),
        (subj, _REL_TIMESTAMP, f"{ts}^^{XSD}#dateTime"),
    ]
    if plan.parent_version is not None:
        triples.append(
            (subj, _REL_PARENT, f"{plan.parent_version}^^{XSD}#integer")
        )
    if publisher:
        triples.append((subj, _REL_PUBLISHER, publisher))
    if change_summary:
        triples.append((subj, _REL_SUMMARY, change_summary))
    if compat_class:
        triples.append((subj, _REL_COMPAT, compat_class))
    if plan.change_records_vs_parent:
        delta = [
            {
                "kind": r.kind.value if hasattr(r.kind, "value") else str(r.kind),
                "type_name": r.type_name,
                "slot_name": r.slot_name,
                "parent_type": r.parent_type,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "from_name": r.from_name,
                "to_name": r.to_name,
            }
            for r in plan.change_records_vs_parent
        ]
        triples.append(
            (
                subj,
                _REL_DELTA,
                json.dumps(delta, separators=(",", ":"), sort_keys=True),
            )
        )
    return triples
