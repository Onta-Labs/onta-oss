"""Shared constants for the enrichment executor (re-exported by executor.py)."""

from __future__ import annotations

import os

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.graph.iri import IRI_BASE

RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
RDFS_DOMAIN = "http://www.w3.org/2000/01/rdf-schema#domain"
# Relationship instance triples use the `…/onto/<predName>` namespace; literal
# attribute instance triples use `…/types/<Type>/attrs/<name>`. A scope
# predicate's ontology declaration doesn't tell us which the data uses, so a
# resolved local-name maps to BOTH candidate instance IRIs.
ONTO_PRED_PREFIX = f"{IRI_BASE}/onto/"
NAME_FALLBACK_ATTRS = ["name", "title", "headline"]
WORKER_POOL_SIZE = 8
PROGRESS_FLUSH_EVERY = 10

# rdfs:comment stamped on an enrichment-declared attribute so the ontology
# /schema view + Explorer can distinguish a schema slot that arrived via
# enrichment from one declared by ingest or the ontology endpoint.
ENRICH_ATTR_DESCRIPTION = "Added by enrichment job"
# Default declared range when a brand-new enriched attribute carries no values
# we can type. The actual range is INFERRED per-attribute from the applied
# values and, for an attribute already declared with a richer range, the
# existing range is PRESERVED rather than downgraded.
ENRICH_ATTR_DATATYPE = "string"

# Default source-authority level for a machine refresh/scrape (ONTA-279). A
# generic scrape carries no explicit authority, so it defaults HERE to a
# strong-but-NOT-top machine level. NEVER user_assertion: that top level is
# minted only by the human-correction write path.
REFRESH_AUTHORITY = AuthorityLevel.source_of_truth

# Hard ceiling on a single adapter lookup (COG-112). Overridable via
# INFONA_ADAPTER_LOOKUP_TIMEOUT_S. Tests monkeypatch the copy on executor.
ADAPTER_LOOKUP_TIMEOUT_S = float(os.environ.get("INFONA_ADAPTER_LOOKUP_TIMEOUT_S", "30"))

# Cap stored per-provider error/summary messages so a chatty adapter exception
# can't bloat the job payload.
_MAX_ERROR_MSG = 300

# Org-valued enrich leaves. Prefer a type that already exists in the tenant
# catalog; otherwise mint Company.
_ORG_ATTR_LEAVES = frozenset(
    {
        "lead_sponsor",
        "sponsor",
        "sponsor_name",
        "lead_sponsor_name",
    }
)
_ORG_TYPE_PREFERENCE = ("Company", "Organization", "Sponsor")
