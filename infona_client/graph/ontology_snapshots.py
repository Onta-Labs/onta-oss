"""Ontology snapshots, structural diff, and restore (ONTA-406).

Version the **graph name**, never the type IRI (plan §5). Published A/B releases
live at ``graphs/global/public/v{N}`` / ``graphs/global/enhanced/v{N}``; C
revisions materialize at ``graphs/{tenant}/revisions/r{N}``. Release/revision
**metadata** is RDF on the versions companion graph
(:func:`~infona_client.graph.ontology_commit.versions_graph_uri`) — no Postgres
migration.

Diff produces the frozen :class:`~infona_client.models.ontology.ChangeRecord`
vocabulary shared with ONTA-404. Symmetric by construction:
``diff(a, a) == []`` and ``invert(diff(a, b))`` multiset-equals ``diff(b, a)``.

Schema graphs only — not instance data — so this module is outside
``kg_writer`` (justified on the write-path allowlist).

Implementation lives in sibling ``ontology_snapshots_*.py`` modules. Every
previously importable name is re-exported here.
"""

from __future__ import annotations

from infona_client.graph.ontology_snapshots_models import (  # noqa: F401
    LayerName,
    ReleaseRecord,
    RestorePlan,
    SnapshotKind,
    SnapshotPlan,
    _LITERAL_DATATYPES,
    _REL_COMPAT,
    _REL_DELTA,
    _REL_FINGERPRINT,
    _REL_KIND,
    _REL_LAYER,
    _REL_NS,
    _REL_OF,
    _REL_PARENT,
    _REL_PUBLISHER,
    _REL_SNAPSHOT,
    _REL_SUMMARY,
    _REL_TIMESTAMP,
    _REL_TYPE,
    _REL_VERSION,
    _release_subject,
    layer_for_graph,
    live_graph_from_snapshot,
)
from infona_client.graph.ontology_snapshots_diff import (  # noqa: F401
    _add_slot_record,
    _is_literal_datatype,
    _record_key,
    _remove_slot_record,
    diff_graphs,
    diff_shapes,
    diffs_symmetric,
    invert_change,
    invert_diff,
)
from infona_client.graph.ontology_snapshots_sparql import (  # noqa: F401
    _clear_graph_sparql,
    _copy_graph_sparql,
    _drop_graph_sparql,
    _release_metadata_triples,
)
from infona_client.graph.ontology_snapshots_list import (  # noqa: F401
    _list_snapshots_graph_store,
    _parse_change_records,
    _parse_int_lit,
    get_snapshot,
    list_snapshots,
    read_snapshot_shape,
)
from infona_client.graph.ontology_snapshots_exec import (  # noqa: F401
    _current_revision_counter,
    _next_release_version,
    _write_snapshot_graph_store,
    execute_snapshot,
    plan_snapshot,
    snapshot_ontology,
)
from infona_client.graph.ontology_snapshots_restore import (  # noqa: F401
    _cleanup_version_artifacts_graph_store,
    _execute_restore_graph_store,
    cleanup_tenant_version_artifacts,
    cleanup_version_artifacts,
    execute_restore,
    list_version_artifact_uris,
    plan_cleanup_version_artifacts,
    plan_restore,
    restore_ontology,
)
