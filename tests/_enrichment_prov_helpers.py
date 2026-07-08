"""Shared fakes/helpers for the ENRICHMENT-RAIL cluster tests (ONTA-245/247/246).

Kept in one importable module so the three per-ticket test files
(``test_enrichment_provenance*.py``, ``test_freshness_gating.py``,
``test_conflict_staging_durable.py``) share ONE definition of the fake Neptune
responses + job factory instead of copy-pasting them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
)

XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"

# Two unrelated (type, attr, entity, value, source_url) domains for the no-overfit
# matrix — a hardware catalog and a gadget catalog, both INVENTED schema names.
DOMAINS = [
    ("Widget", "sku", "Alpha Widget", "WX-1000", "https://parts.example/alpha"),
    ("Gadget", "weight_kg", "Beta Gadget", "3.2", "https://specs.example/beta"),
]


class FakeWikidata:
    name = "wikidata"

    def __init__(self, mapping):
        self._mapping = mapping

    async def lookup(self, entity_label, attribute, context):
        return list(self._mapping.get((entity_label, attribute), []))


def entities_query_response(rows):
    bindings = []
    for r in rows:
        b = {"e": {"type": "uri", "value": r["uri"]}}
        if r.get("label") is not None:
            b["label"] = {"type": "literal", "value": r["label"]}
        if r.get("vals") is not None:
            b["vals"] = {"type": "literal", "value": r["vals"]}
        bindings.append(b)
    return {
        "head": {"vars": ["e", "label", "nameAttr", "vals"]},
        "results": {"bindings": bindings},
    }


def range_response(range_uri=None):
    bindings = [{"range": {"type": "uri", "value": range_uri}}] if range_uri else []
    return {"head": {"vars": ["range"]}, "results": {"bindings": bindings}}


def query_router(entities, *, existing_range=None):
    """AsyncMock ``query`` side_effect: serves the entity-selection SELECT and the
    per-attribute range SELECT from one fake Neptune."""

    async def _route(sparql, *a, **k):
        if "SELECT ?range" in sparql:
            return range_response(existing_range)
        return entities

    return _route


def make_job(*, type_name, attributes, policy, kg="kg", **kw):
    return EnrichJob(
        id=f"job-{type_name}",
        tenant_id="test-tenant",
        kg_name=kg,
        type_name=type_name,
        attributes=attributes,
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=policy,
        **kw,
    )


def all_updates(neptune) -> str:
    return " ".join(
        (c.args[0] if c.args else c.kwargs.get("sparql", ""))
        for c in neptune.update.await_args_list
    )
