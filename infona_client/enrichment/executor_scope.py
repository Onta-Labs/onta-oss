"""Scope / count / multi-value URI resolution for EnrichmentExecutor."""

from __future__ import annotations

from typing import Optional

from infona_client.enrichment.executor_helpers import (
    _host,
    _instance_pred_iris_for_leaf,
    _prop_key_for_leaf,
    _resolve_pred_iris_from_catalog,
    _safe_iri,
    _values_match,
)
from infona_client.enrichment.executor_select import _select_entities_via_store
from infona_client.enrichment.models import EnrichScope


class EnrichmentScopeMixin:
    """Predicate resolution, entity counts, and multi-value scope IRIs."""

    async def _resolve_scope_predicate_iris(
        self, tenant_id: str, type_name: str, scope: EnrichScope
    ) -> list[str]:
        """Resolve ``scope.predicate`` (a local-name) to the concrete instance
        predicate IRI(s) to match.

        The candidate IRIs are the **union** of two sources:

          1. **Ontology catalog** — match ``scope.predicate`` case-insensitively
             against the type's declared attribute names. This gives
             case-normalisation: a request ``hasLevel`` resolves to the stored
             ``haslevel`` leaf.
          2. **Direct build from the (validated) input predicate** —
             :func:`_instance_pred_iris_for_leaf` → ``…/types/<Type>/attrs/<pred>``
             and ``…/onto/<pred>``. Relationships like ``haslevel`` are stored
             under ``…/onto/<pred>`` and may not be declared as an attribute,
             so the catalog arm alone returns ``[]`` for them. The direct
             build always yields ``…/onto/<pred>`` (and the attr IRI).

        A catalog/store error logs loudly and is skipped; the direct build
        still returns so a relationship scope resolves. No SPARQL.
        """
        ontology_iris: list[str] = []
        try:
            from infona_client.graph.ontology_catalog import list_attributes
            from infona_client.graph.store import GraphConfigError

            attrs = await list_attributes(
                tenant_id=tenant_id, type_name=type_name, layer="tenant"
            )
            names = [(getattr(a, "name", None) or "").strip() for a in attrs]
            ontology_iris = _resolve_pred_iris_from_catalog(
                type_name, scope.predicate, names
            )
        except GraphConfigError:
            _host().logger.error(
                "scope_predicate_resolve_no_store",
                tenant_id=tenant_id,
                type_name=type_name,
                predicate=scope.predicate,
            )
        except Exception:  # noqa: BLE001
            _host().logger.exception(
                "scope_predicate_resolve_failed",
                tenant_id=tenant_id,
                type_name=type_name,
                predicate=scope.predicate,
            )
        direct_iris = _instance_pred_iris_for_leaf(
            type_name, scope.predicate.strip().lower()
        )
        iris: list[str] = []
        seen: set[str] = set()
        for iri in [*ontology_iris, *direct_iris]:
            if iri not in seen and _safe_iri(iri):
                seen.add(iri)
                iris.append(iri)
        return iris

    async def count_entities(
        self,
        tenant_id: str,
        kg_name: str,
        type_name: str,
        scope: Optional[EnrichScope] = None,
        entity_uris: Optional[list[str]] = None,
    ) -> int:
        """Count entities the job will enrich.

        Whole-type by default; honors the same subset semantics as the
        GraphStore entity select (COG-112). ``entity_uris`` wins over ``scope``.

        NOTE (COG-112 non-blocking): create-job no longer calls this in the
        request path — the executor's background select resolves the matched
        subset and sets ``progress.total``. Store-only (ONTA-527): a store
        outage logs loudly and returns 0; there is no SPARQL fallback.
        """
        store_entities = await _select_entities_via_store(
            tenant_id,
            kg_name,
            type_name,
            attributes=[],
            limit=None,
            scope=scope,
            entity_uris=entity_uris,
        )
        if store_entities is None:
            _host().logger.error(
                "enrich_count_no_store",
                tenant_id=tenant_id,
                kg_name=kg_name,
                type_name=type_name,
            )
            return 0
        return len(store_entities)

    async def select_scope_value_uris(
        self,
        tenant_id: str,
        kg_name: str,
        type_name: str,
        predicate: str,
        values: list[str],
        limit: Optional[int] = None,
    ) -> list[str]:
        """Resolve a MULTI-VALUE scope to the concrete entity IRIs it names.

        "refresh pricing for OpenAI, Google, Deepgram and ElevenLabs" (or "the
        physicians named A. Selvan, R. Mokabberi, …") is a scope over a SET of
        values, not one literal. Matching the crammed literal
        (``provided_by = "OpenAI, Google, Deepgram, ElevenLabs"``) matches zero
        rows — the reported persona-eval gap. This returns the IRIs of
        ``type_name`` entities whose ``predicate`` value (or a related node's
        label, or its own ``rdfs:label``) is a case-insensitive member of
        ``values``, so the agent can enrich EXACTLY those via ``entity_uris`` (the
        already-converged scoped-enrich path) instead of premature-clarifying to
        0 and falling into a fresh discovery.

        Reuses the SAME predicate resolution (:meth:`_resolve_scope_predicate_iris`)
        and bound-predicate matching arms as the single-value scope, so casing /
        attribute-vs-relationship / label-only names are all handled identically.
        Returns a deduped, order-preserving, ``limit``-capped list; ``[]`` on any
        failure (unresolved predicate, empty value set, store error) — the caller
        fails closed rather than enriching the whole type by accident.
        """
        clean = [v.strip() for v in (values or []) if v and v.strip()]
        if not clean:
            return []
        try:
            EnrichScope(predicate=predicate, value=clean[0])
        except Exception:  # noqa: BLE001 — a bad predicate resolves to nothing
            return []
        store_entities = await _select_entities_via_store(
            tenant_id,
            kg_name,
            type_name,
            attributes=[],
            limit=None,
            scope=None,
            entity_uris=None,
        )
        if store_entities is None:
            _host().logger.error(
                "enrich_scope_value_select_no_store",
                tenant_id=tenant_id,
                kg_name=kg_name,
                type_name=type_name,
            )
            return []
        want = (predicate or "").strip().lower()
        pred_key = _prop_key_for_leaf(predicate)
        uris: list[str] = []
        seen: set[str] = set()
        for e in store_entities:
            props = e.get("props") or {}
            candidates: list[str] = []
            if pred_key:
                raw = props.get(pred_key)
                if raw is not None and raw != "":
                    candidates.append(str(raw))
            if not candidates:
                for k, v in props.items():
                    if str(k).lower() == want and v is not None and v != "":
                        candidates.append(str(v))
                        break
            if want in {"name", "label", "title"}:
                disp = e.get("label") or props.get("name")
                if disp:
                    candidates.append(str(disp))
            if any(_values_match(c, v) for c in candidates for v in clean):
                u = e.get("uri") or ""
                if u and u not in seen:
                    seen.add(u)
                    uris.append(u)
            if limit is not None and len(uris) >= int(limit):
                break
        return uris
