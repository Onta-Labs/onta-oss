"""Shared fakes/helpers for the ENRICHMENT-RAIL cluster tests (ONTA-245/247/246).

Kept in one importable module so the three per-ticket test files
(``test_enrichment_provenance*.py``, ``test_freshness_gating.py``,
``test_conflict_staging_durable.py``) share ONE definition of the fake Neptune
responses + job factory instead of copy-pasting them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from infona_client.enrichment.models import (
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


def _parse_vals_field(vals_field: str) -> dict[str, str]:
    """Parse the legacy ``predicate::value||…`` test fixture into triples."""
    out: dict[str, str] = {}
    if not vals_field:
        return out
    for chunk in vals_field.split("||"):
        if "::" not in chunk:
            continue
        p, _, v = chunk.partition("::")
        if p and p not in out:
            out[p] = v
    return out


async def seed_enrich_entities(
    type_name: str,
    rows: list[dict],
    *,
    tenant_id: str = "test-tenant",
    kg_name: str = "kg",
) -> None:
    """Write enrich-target entities into the process MemoryGraphStore.

    ``rows`` is the old SPARQL-JSON fixture shape:
    ``{uri, label?, nameAttr?, vals?}`` where ``vals`` is
    ``pred::value||pred::value``.
    """
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.queries import kg_graph_uri

    rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    rdfs_label = "http://www.w3.org/2000/01/rdf-schema#label"
    type_iri = f"{IRI_BASE}/types/{type_name}"
    triples: list[tuple[str, str, str]] = []
    for r in rows:
        uri = r["uri"]
        triples.append((uri, rdf_type, type_iri))
        if r.get("label"):
            triples.append((uri, rdfs_label, str(r["label"])))
        if r.get("nameAttr"):
            triples.append(
                (uri, f"{IRI_BASE}/types/{type_name}/attrs/name", str(r["nameAttr"]))
            )
        for pred, val in _parse_vals_field(r.get("vals") or "").items():
            triples.append((uri, pred, val))
    if triples:
        await insert_facts(None, kg_graph_uri(tenant_id, kg_name), triples)


async def seed_declared_types(
    names: list[str], *, tenant_id: str = "test-tenant"
) -> None:
    """Declare catalog types so ``list_declared_types`` can resolve them."""
    from infona_client.graph.ontology_catalog import upsert_type

    for name in names:
        await upsert_type(name=name, tenant_id=tenant_id, layer="tenant")


async def seed_strategy_triples(
    triples: list[tuple[str, str, str]], *, tenant_id: str = "test-tenant"
) -> None:
    """Write strategy triples into the tenant ontology GraphStore."""
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.queries import tenant_graph_uri

    if triples:
        await insert_facts(None, tenant_graph_uri(tenant_id), triples)


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
