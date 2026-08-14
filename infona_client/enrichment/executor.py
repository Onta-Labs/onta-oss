"""Async executor for enrichment jobs.

Public type: :class:`EnrichmentExecutor`. Implementation lives in sibling
``executor_*.py`` modules. Every previously importable name is re-exported here.

Writes go through ``insert_facts`` / ``refresh_after_write`` / ``delete_facts``.
Relationship instance edges use ``onto/<leaf>``. Entity IRIs via ``entity_uri``.
"""

from __future__ import annotations

import structlog

from infona_client.config import settings  # noqa: F401 — _host().settings
from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.job_store import JobStore
from infona_client.enrichment.sources.base import (  # noqa: F401 — _host().get_adapter
    SourceAdapter,
    get_adapter,
    register_adapter,
)
from infona_client.graph.client import NeptuneClient
from infona_client.graph.kg_writer import (  # noqa: F401 — monkeypatch surface
    delete_facts,
    insert_facts,
    refresh_after_write,
)
from infona_client.graph.ontology_queries import (
    PRIMITIVE_TYPES,  # noqa: F401
    entity_uri as _entity_uri,
)
from infona_client.graph.store import resolve_optional_graph_store

from infona_client.enrichment.executor_const import (  # noqa: F401
    ADAPTER_LOOKUP_TIMEOUT_S,
    ENRICH_ATTR_DATATYPE,
    ENRICH_ATTR_DESCRIPTION,
    NAME_FALLBACK_ATTRS,
    ONTO_PRED_PREFIX,
    PROGRESS_FLUSH_EVERY,
    RDF_PROPERTY,
    RDF_TYPE,
    RDFS_DOMAIN,
    RDFS_LABEL,
    REFRESH_AUTHORITY,
    WORKER_POOL_SIZE,
    _MAX_ERROR_MSG,
    _ORG_ATTR_LEAVES,
    _ORG_TYPE_PREFERENCE,
)
from infona_client.enrichment.executor_helpers import (  # noqa: F401
    _IRI_RE,
    _attr_uri,
    _canonical_provenance_enabled,
    _entity_iri_type,
    _host,
    _infer_datatype_from_values,
    _infer_relationship_target,
    _instance_pred_iris_for_leaf,
    _is_float,
    _is_int,
    _is_iso_datetime,
    _local_name,
    _now,
    _parse_vals,
    _prop_key_for_leaf,
    _resolve_pred_iris_from_catalog,
    _safe_iri,
    _slug_from_uri,
    _strategy_version_with_instructions,
    _type_uri,
    _validate_entity_uris,
    _values_match,
    _values_match_with_strategy,
)
from infona_client.enrichment.executor_select import (  # noqa: F401
    _extract_bind_attrs,
    _select_entities_via_store,
)
from infona_client.enrichment.executor_tally import _ProviderTally  # noqa: F401

logger = structlog.stdlib.get_logger("infona.enrichment")

# Patch surfaces tests setattr on this module even when the implementation
# lives in a sibling. Bound here so `_host().NAME` sees the patched object.
openrouter_chat = None
_spawn = None


def _mint_entity_uri(type_name: str, raw_id: str) -> str:
    """The only entity mint — ``entity_uri(type, raw_id)``. Do not hand-build IRIs."""
    return _entity_uri(type_name, raw_id)


async def _write_facts(client, graph_uri, triples, **kwargs):
    """Shared write path — always ``insert_facts``. Do not fork a second flush."""
    graph_store = kwargs.pop("store", resolve_optional_graph_store())
    return await insert_facts(
        client,
        graph_uri,
        triples,
        store=graph_store,
        **kwargs,
    )


async def _refresh_facts(client, **kwargs):
    """Shared post-write housekeeping — always ``refresh_after_write``."""
    return await refresh_after_write(client, **kwargs)


# Mixins after helpers so ``import executor as _mod`` inside them sees
# logger / insert_facts / get_adapter already bound.
from infona_client.enrichment.executor_apply import EnrichmentApplyMixin  # noqa: E402
from infona_client.enrichment.executor_declare import EnrichmentDeclareMixin  # noqa: E402
from infona_client.enrichment.executor_lookup import EnrichmentLookupMixin  # noqa: E402
from infona_client.enrichment.executor_provenance import (  # noqa: E402
    EnrichmentProvenanceMixin,
)
from infona_client.enrichment.executor_refresh import EnrichmentRefreshMixin  # noqa: E402
from infona_client.enrichment.executor_run import EnrichmentRunMixin  # noqa: E402
from infona_client.enrichment.executor_run_finish import (  # noqa: E402
    EnrichmentRunFinishMixin,
)
from infona_client.enrichment.executor_scope import EnrichmentScopeMixin  # noqa: E402
from infona_client.enrichment.executor_triples import EnrichmentTriplesMixin  # noqa: E402


class EnrichmentExecutor(
    EnrichmentScopeMixin,
    EnrichmentLookupMixin,
    EnrichmentRunMixin,
    EnrichmentRunFinishMixin,
    EnrichmentProvenanceMixin,
    EnrichmentRefreshMixin,
    EnrichmentDeclareMixin,
    EnrichmentTriplesMixin,
    EnrichmentApplyMixin,
):
    """Async executor for enrichment jobs (lite tier = wikidata, with cache)."""

    def __init__(
        self,
        neptune_client: NeptuneClient,
        job_store: JobStore,
        cache: EnrichmentCache,
        wikidata_adapter: SourceAdapter,
    ) -> None:
        self._neptune = neptune_client
        self._jobs = job_store
        self._cache = cache
        self._wikidata = wikidata_adapter
        # Register the wikidata adapter into the global adapter registry so
        # chain-based lookups can resolve it by name. Idempotent.
        try:
            register_adapter(wikidata_adapter)
        except Exception:  # noqa: BLE001
            pass
