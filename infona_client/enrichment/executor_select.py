"""GraphStore entity select + binding-attribute extraction for enrichment."""

from __future__ import annotations

from typing import Optional

from infona_client.enrichment.executor_const import NAME_FALLBACK_ATTRS
from infona_client.enrichment.executor_helpers import (
    _attr_uri,
    _host,
    _prop_key_for_leaf,
    _slug_from_uri,
    _values_match,
)
from infona_client.enrichment.models import EnrichScope
from infona_client.graph.provenance import (
    attr_provenance_companion_uri,
    legacy_attr_companion_uri,
)


def _extract_bind_attrs(
    props: dict,
    bind_leaves,
    *,
    uri: str = "",
    label: str = "",
) -> dict[str, str]:
    """Pull ``attribute:<leaf>`` binding values out of a GraphStore property map.

    Target-attr ``vals`` never include identifier leaves (nct_id, …) because
    those are not the attributes being filled. Registry adapters still need
    them to construct the API request. When a leaf has a well-known id format
    guard (NCT) and the property is missing, the entity URI slug / label is
    tried — ingest often keys the node by that id.
    """
    from infona_client.api_registry.ids import (
        has_id_format_guard,
        normalize_attribute_binding,
    )

    out: dict[str, str] = {}
    slug = _slug_from_uri(uri) if uri else ""
    for leaf in bind_leaves or ():
        leaf_s = str(leaf or "").strip()
        if not leaf_s:
            continue
        raw = ""
        key = _prop_key_for_leaf(leaf_s)
        if key:
            val = props.get(key) if isinstance(props, dict) else None
            if val is not None and val != "":
                raw = str(val)
        if not raw and has_id_format_guard(leaf_s):
            for cand in (slug, label, str((props or {}).get("id") or "")):
                if cand and normalize_attribute_binding(leaf_s, cand):
                    raw = cand
                    break
        if not raw:
            continue
        out[leaf_s] = normalize_attribute_binding(leaf_s, raw) or raw
    return out


async def _select_entities_via_store(
    tenant_id: str,
    kg_name: str,
    type_name: str,
    attributes: list[str],
    *,
    limit: Optional[int] = None,
    scope: Optional[EnrichScope] = None,
    entity_uris: Optional[list[str]] = None,
) -> Optional[list[dict]]:
    """List enrich targets from GraphStore (ONTA-534). ``None`` = store unavailable.

    Same shape as the former SPARQL SELECT path: ``{uri, label, vals}`` with
    ``vals`` keyed by attribute IRI so :meth:`EnrichmentExecutor.run` is
    store-agnostic.
    """
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import GraphConfigError, get_optional_graph_store

    try:
        store = get_optional_graph_store()
        session = store.session(GraphScope.for_instance(tenant_id, kg_name))
    except GraphConfigError:
        return None
    except Exception:  # noqa: BLE001
        _host().logger.error("enrich_store_session_failed", exc_info=True)
        return None

    try:
        rows = await session.execute_template(
            "entity_list_by_type", {"primary_type": type_name}
        )
    except Exception:  # noqa: BLE001
        _host().logger.error("enrich_store_list_failed", type_name=type_name, exc_info=True)
        return None

    summaries = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows]
    if entity_uris:
        allowed = set(entity_uris)
        summaries = [s for s in summaries if s.get("id") in allowed]

    cap = int(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit else None
    entities: list[dict] = []
    for summary in summaries:
        eid = str(summary.get("id") or "").strip()
        if not eid:
            continue
        props: dict = {}
        try:
            detail_rows = await session.execute_template("entity_detail", {"id": eid})
        except Exception:  # noqa: BLE001 — still emit the entity with no vals
            detail_rows = []
        if detail_rows:
            detail = (
                detail_rows[0].to_dict()
                if hasattr(detail_rows[0], "to_dict")
                else dict(detail_rows[0])
            )
            raw_props = detail.get("props") or {}
            if isinstance(raw_props, dict):
                props = raw_props

        if scope is not None and scope.predicate and scope.value is not None:
            want = (scope.predicate or "").strip().lower()
            have = ""
            pred_key = _prop_key_for_leaf(scope.predicate)
            if pred_key:
                have = props.get(pred_key)
            if have is None or have == "":
                for k, v in props.items():
                    if str(k).lower() == want:
                        have = v
                        break
            if have is None or have == "":
                # Display name is stored on Entity.name (rdfs:label).
                if want in {"name", "label", "title"}:
                    have = props.get("name") or summary.get("name")
            if not _values_match(str(have or ""), str(scope.value)):
                continue

        label = summary.get("name") or props.get("name") or ""
        slug = _slug_from_uri(eid)
        if not label or label == slug:
            for fb in NAME_FALLBACK_ATTRS:
                alt = props.get(fb)
                if alt:
                    label = str(alt)
                    break
        if not label:
            label = slug

        vals: dict[str, str] = {}
        for attr in attributes:
            key = _prop_key_for_leaf(attr)
            if not key:
                continue
            val = props.get(key)
            if val is None or val == "":
                continue
            if isinstance(val, list):
                val = val[0] if val else ""
            if val == "":
                continue
            vals[_attr_uri(type_name, attr)] = str(val)
            for suffix in ("source_url", "verified_at"):
                raw_c = props.get(f"{attr}_{suffix}")
                if raw_c is None or raw_c == "":
                    continue
                cite = str(raw_c[0] if isinstance(raw_c, list) else raw_c)
                vals[attr_provenance_companion_uri(type_name, attr, suffix)] = cite
                vals[legacy_attr_companion_uri(type_name, attr, suffix)] = cite

        # Stash the full property map so binding-source leaves (e.g. nct_id
        # for ClinicalTrials.gov) can be read without a residual SPARQL hop.
        # Production GraphStore is Neo4j-only; the retired SPARQL bind path
        # fail-opened to {}.
        entities.append(
            {"uri": eid, "label": str(label), "vals": vals, "props": props}
        )
        if cap is not None and len(entities) >= cap:
            break
    return entities
