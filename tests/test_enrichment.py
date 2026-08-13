"""Tests for the auto-enrichment feature (lite tier)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.executor import (
    EnrichmentExecutor,
    _ProviderTally,
    _entity_iri_type,
    _infer_datatype_from_values,
    _instance_pred_iris_for_leaf,
    _is_float,
    _is_int,
    _parse_vals,
    _resolve_pred_iris_from_catalog,
    _values_match,
)
from tests._enrichment_prov_helpers import (
    seed_declared_types,
    seed_enrich_entities,
    seed_strategy_triples,
)
from infona_client.enrichment.job_store import InMemoryJobStore
from infona_client.enrichment.models import (
    ConflictPolicy,
    ConflictReview,
    EnrichJob,
    EnrichmentTier,
    EnrichScope,
    JobStatus,
    Verdict,
)
from infona_client.enrichment.sources.wikidata import (
    WikidataAdapter,
    _clean_label_candidates,
)


# The subclass-aware entity-type constraint the enrichment SELECT/COUNT now emits
# (Fix B): a reflexive `a/rdfs:subClassOf*` closure instead of a bare `a`, so
# enriching a supertype reaches its leaf-typed instances too. `*` is zero-or-more,
# so a directly-typed entity still matches (reflexive) — same predicate IRI that
# graph.ontology_queries.insert_subtype writes.
_MENTOR_TYPED = (
    "?e <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>/"
    "<http://www.w3.org/2000/01/rdf-schema#subClassOf>* "
    "<https://graph.infona.ai/types/Mentor> ."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    type_name: str = "Product",
    attributes: list[str] | None = None,
    policy: ConflictPolicy = ConflictPolicy.stage,
    confidence_min: float = 0.85,
    scope: EnrichScope | None = None,
    entity_uris: list[str] | None = None,
) -> EnrichJob:
    return EnrichJob(
        id="job-1",
        tenant_id="test-tenant",
        kg_name="kg",
        type_name=type_name,
        attributes=attributes or ["manufacturer"],
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=policy,
        confidence_min=confidence_min,
        scope=scope,
        entity_uris=entity_uris,
    )


async def _prep_neptune(rows, type_name="Product", extra_types=None):
    """Seed GraphStore entities and return a SPARQL-retired Neptune mock."""
    await seed_enrich_entities(type_name, rows)
    if extra_types:
        for tname, trows in extra_types:
            await seed_enrich_entities(tname, trows)
    neptune = AsyncMock()
    neptune.query.side_effect = AssertionError("enrich must not SPARQL")
    neptune.update.return_value = None
    return neptune


async def _seed_existing_range(type_name: str, attr_name: str, existing_range: str) -> None:
    """Declare a catalog range so enrichment cannot SPARQL-read it."""
    from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type

    await upsert_type(name=type_name, tenant_id="test-tenant")
    if existing_range.endswith("#integer"):
        dt = "integer"
    elif existing_range.lower().endswith("#datetime"):
        dt = "datetime"
    elif "/types/" in existing_range:
        dt = existing_range.rsplit("/", 1)[-1]
    else:
        dt = "string"
    await upsert_attribute(
        type_name=type_name,
        attr_name=attr_name,
        datatype=dt,
        tenant_id="test-tenant",
    )



def _entities_query_response(rows: list[dict]) -> dict:
    bindings = []
    for r in rows:
        b: dict = {"e": {"type": "uri", "value": r["uri"]}}
        if r.get("label") is not None:
            b["label"] = {"type": "literal", "value": r["label"]}
        if r.get("nameAttr") is not None:
            b["nameAttr"] = {"type": "literal", "value": r["nameAttr"]}
        if r.get("vals") is not None:
            b["vals"] = {"type": "literal", "value": r["vals"]}
        bindings.append(b)
    return {"head": {"vars": ["e", "label", "nameAttr", "vals"]}, "results": {"bindings": bindings}}


def _count_response(n: int) -> dict:
    return {
        "head": {"vars": ["n"]},
        "results": {"bindings": [{"n": {"type": "literal", "value": str(n)}}]},
    }


def _range_response(range_uri: str | None = None) -> dict:
    """SPARQL result for get_attribute_range_query: zero rows when ``range_uri`` is
    None (attribute has no existing range → enrichment uses its inferred range), or
    one ``?range`` binding when an existing range should be preserved."""
    bindings = (
        [{"range": {"type": "uri", "value": range_uri}}] if range_uri else []
    )
    return {"head": {"vars": ["range"]}, "results": {"bindings": bindings}}


# ---------------------------------------------------------------------------
# Store readers (ported by ONTA-527)
#
# Enrichment writes instance data through ``GraphStore`` (``kg_writer`` →
# ``pg_ops``) and declares schema through the ontology CATALOG, neither of which
# emits SPARQL. "What was written" is therefore read back off the process store
# the hermetic conftest fixture installs, instead of off the update strings a
# recording Neptune mock collected. The mock is still passed in — and still
# asserted un-called for the data path — so a regression that reintroduces a
# SPARQL write is visible rather than silently tolerated.
# ---------------------------------------------------------------------------


def _graph_store():
    from infona_client.graph.store import get_graph_store

    return get_graph_store()


async def _declared_attrs(type_name: str, tenant_id: str = "test-tenant") -> dict:
    """``{attribute name: OntoAttrRecord}`` the tenant ontology catalog holds.

    The successor of "grep the emitted SPARQL for rdf:Property + rdfs:domain":
    a declaration is now an ``:OntoAttr`` row carrying ``kind`` (literal /
    relationship), ``datatype`` and ``range_type``.
    """
    from infona_client.graph import ontology_catalog as oc

    return {
        a.name: a
        for a in await oc.list_attributes(type_name=type_name, tenant_id=tenant_id)
    }


def _props(entity_uri: str) -> dict:
    """Literal properties written on one entity ({} when it was never written)."""
    for row in _graph_store().snapshot_entities():
        if row["id"] == entity_uri:
            return row["props"]
    return {}


def _rels_for(attr: str) -> list[dict]:
    """Relationship edges written for one attribute leaf (any subject)."""
    return [r for r in _graph_store().snapshot_rels() if r["attr"] == attr]


def _citation(entity_uri: str, attr: str) -> dict | None:
    """The ``:AttrCitation`` companion enrichment wrote for one (entity, attr)."""
    for row in _graph_store().snapshot_citations():
        if row["entity_id"] == entity_uri and row["attr"] == attr:
            return row
    return None


def _stored_values(attr: str) -> set:
    """Every value stored for ``attr`` across all entities (props + rel targets)."""
    values = {
        props[attr]
        for props in (row["props"] for row in _graph_store().snapshot_entities())
        if attr in props
    }
    return values | {r["end_id"] for r in _rels_for(attr)}


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------


def test_job_store_crud():
    async def run():
        store = InMemoryJobStore()
        job = _make_job()
        await store.create(job)

        got = await store.get("job-1")
        assert got is not None
        assert got.id == "job-1"

        # Update
        got.status = JobStatus.running
        await store.update(got)
        again = await store.get("job-1")
        assert again.status == JobStatus.running

        summaries = await store.list_for_tenant("test-tenant")
        assert len(summaries) == 1
        assert summaries[0].id == "job-1"

        await store.delete("job-1")
        assert await store.get("job-1") is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_get_put():
    async def run():
        cache = EnrichmentCache()
        # Miss
        assert await cache.get("Bosch", "manufacturer", "wikidata") is None

        v = Verdict(value="Bosch GmbH", confidence=0.95, source="wikidata")
        await cache.put("Bosch", "manufacturer", "wikidata", [v])

        # Case-insensitive on entity_label
        hit = await cache.get("bosch", "manufacturer", "wikidata")
        assert hit is not None and len(hit) == 1
        assert hit[0].value == "Bosch GmbH"

        # Different attribute → still miss
        assert await cache.get("Bosch", "country", "wikidata") is None

    asyncio.run(run())


def test_cache_key_normalizes_label_and_versions(monkeypatch):
    """ADR-0005 §2 cache keying:

    (a) "City", "city", and "  City  " produce the SAME key (normalized label).
    (b) Changing strategy_version produces a DIFFERENT key (clean miss).
    """
    from infona_client.enrichment import cache as cache_mod

    # (a) Normalized-label equivalence at the key level.
    k1 = cache_mod._key("Place", "City", "name", "v1", "wikidata")
    k2 = cache_mod._key("Place", "city", "name", "v1", "wikidata")
    k3 = cache_mod._key("Place", "  City  ", "name", "v1", "wikidata")
    assert k1 == k2 == k3
    assert cache_mod._normalize_label("  City  ") == "city"
    # Internal whitespace runs collapse to a single space.
    assert cache_mod._normalize_label("New   York") == "new york"

    async def run():
        cache = EnrichmentCache()
        v = Verdict(value="Springfield", confidence=0.95, source="wikidata")

        # Put under one strategy_version, then read back with label variants.
        await cache.put(
            "City", "name", "wikidata", [v],
            entity_type="Place", strategy_version="v1",
        )
        for variant in ("City", "city", "  City  "):
            hit = await cache.get(
                variant, "name", "wikidata",
                entity_type="Place", strategy_version="v1",
            )
            assert hit is not None and hit[0].value == "Springfield"

        # (b) A different strategy_version is a cache miss (auto-invalidation).
        miss = await cache.get(
            "City", "name", "wikidata",
            entity_type="Place", strategy_version="v2",
        )
        assert miss is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Wikidata adapter
# ---------------------------------------------------------------------------


def _mk_response(payload: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def test_wikidata_adapter_unknown_attribute_returns_empty():
    async def run():
        adapter = WikidataAdapter()
        result = await adapter.lookup("Bosch", "not_a_known_attr", {})
        assert result == []

    asyncio.run(run())


def test_wikidata_client_has_granular_per_phase_timeout():
    """COG-112: the lazily-built httpx client must use an explicit per-phase
    ``httpx.Timeout`` (connect/read/write/pool all bounded), not a bare float.
    A bare total timeout does not bound a dribbling connection — that is what
    let the production lookup hang forever."""

    async def run():
        adapter = WikidataAdapter()
        client = await adapter._get_client()
        try:
            t = client.timeout
            assert isinstance(t, httpx.Timeout)
            # Every phase is bounded (no None == "no timeout").
            assert t.connect is not None and t.connect > 0
            assert t.read is not None and t.read > 0
            assert t.write is not None and t.write > 0
            assert t.pool is not None and t.pool > 0
        finally:
            await adapter.aclose()

    asyncio.run(run())


def test_wikidata_adapter_resolves_entity_id_claim():
    async def run():
        adapter = WikidataAdapter()
        # Inject a fake httpx client.
        client = AsyncMock()
        # Sequence: search → entities (claims) → entities (label for target)
        client.get.side_effect = [
            _mk_response({"search": [{"id": "Q176"}]}),
            _mk_response(
                {
                    "entities": {
                        "Q176": {
                            "claims": {
                                "P17": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {
                                                "type": "wikibase-entityid",
                                                "value": {"id": "Q183"},
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            ),
            _mk_response(
                {
                    "entities": {
                        "Q183": {"labels": {"en": {"value": "Germany"}}}
                    }
                }
            ),
        ]
        adapter._client = client
        verdicts = await adapter.lookup("Bosch", "country", {})
        assert len(verdicts) == 1
        assert verdicts[0].value == "Germany"
        assert verdicts[0].source == "wikidata"
        assert verdicts[0].source_url == "https://www.wikidata.org/wiki/Q176"
        assert verdicts[0].confidence == 0.95

    asyncio.run(run())


def test_wikidata_adapter_handles_429_gracefully():
    async def run():
        adapter = WikidataAdapter()
        client = AsyncMock()
        client.get.side_effect = [_mk_response({}, status=429)]
        adapter._client = client
        verdicts = await adapter.lookup("Bosch", "country", {})
        assert verdicts == []

    asyncio.run(run())


def test_wikidata_adapter_no_search_results():
    async def run():
        adapter = WikidataAdapter()
        client = AsyncMock()
        # All 4 fallback candidates return no hits — capped at 4 search calls.
        client.get.side_effect = [_mk_response({"search": []})] * 4
        adapter._client = client
        verdicts = await adapter.lookup("ZZZNOPE", "country", {})
        assert verdicts == []

    asyncio.run(run())


def test_wikidata_label_strips_trailing_sku():
    """First search (full label) misses; SKU-stripped candidate hits.

    Confidence is reduced by 0.05 because we used the first fallback step.
    """
    async def run():
        adapter = WikidataAdapter()
        client = AsyncMock()
        # 1) original "Apple MacBook Pro M3" → empty
        # 2) "Apple MacBook Pro" → hit Q312 (Apple Inc.)
        # 3) entity claims for manufacturer (P176) → string value
        client.get.side_effect = [
            _mk_response({"search": []}),
            _mk_response({"search": [{"id": "Q312"}]}),
            _mk_response(
                {
                    "entities": {
                        "Q312": {
                            "claims": {
                                "P176": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {
                                                "type": "string",
                                                "value": "Apple Inc.",
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            ),
        ]
        adapter._client = client
        verdicts = await adapter.lookup(
            "Apple MacBook Pro M3", "manufacturer", {}
        )
        assert len(verdicts) == 1
        assert verdicts[0].value == "Apple Inc."
        # Direct hit would be 0.95; one fallback step → 0.90.
        assert verdicts[0].confidence == pytest.approx(0.90)

    asyncio.run(run())


def test_wikidata_label_falls_back_to_first_two_tokens():
    """Original + SKU-strip both miss; first-2-tokens candidate hits.

    Confidence reduced by 0.10 (two fallback steps).
    """
    async def run():
        adapter = WikidataAdapter()
        client = AsyncMock()
        # Candidates for "Bosch fuel injector 0261545109":
        #   ["...", "Bosch fuel injector", "Bosch fuel", "Bosch"]
        # 1) original → empty
        # 2) "Bosch fuel injector" → empty
        # 3) "Bosch fuel" → hit Q234021
        # 4) entity claims for country (P17) → entity-id
        # 5) label for Q183 → "Germany"
        client.get.side_effect = [
            _mk_response({"search": []}),
            _mk_response({"search": []}),
            _mk_response({"search": [{"id": "Q234021"}]}),
            _mk_response(
                {
                    "entities": {
                        "Q234021": {
                            "claims": {
                                "P17": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {
                                                "type": "wikibase-entityid",
                                                "value": {"id": "Q183"},
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            ),
            _mk_response(
                {
                    "entities": {
                        "Q183": {"labels": {"en": {"value": "Germany"}}}
                    }
                }
            ),
        ]
        adapter._client = client
        verdicts = await adapter.lookup(
            "Bosch fuel injector 0261545109", "country", {}
        )
        assert len(verdicts) == 1
        assert verdicts[0].value == "Germany"
        # Two fallback steps → 0.95 - 0.10 = 0.85.
        assert verdicts[0].confidence == pytest.approx(0.85)

    asyncio.run(run())


def test_wikidata_label_cleaning_unit():
    """Pure tokenizer/cleaner behavior."""
    assert _clean_label_candidates("Apple MacBook Pro M3") == [
        "Apple MacBook Pro M3",
        "Apple MacBook Pro",
        "Apple MacBook",
        "Apple",
    ]
    assert _clean_label_candidates("Bosch fuel injector 0261545109") == [
        "Bosch fuel injector 0261545109",
        "Bosch fuel injector",
        "Bosch fuel",
        "Bosch",
    ]
    # Sony case: trailing-only stripping leaves "headphones" in place;
    # SKU "WH-1000XM5" sits in the middle and is not stripped. Length is 3
    # so Candidate B (first 2 tokens) fires from the original list.
    assert _clean_label_candidates("Sony WH-1000XM5 headphones") == [
        "Sony WH-1000XM5 headphones",
        "Sony WH-1000XM5",
        "Sony",
    ]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_vals():
    assert _parse_vals("") == {}
    out = _parse_vals("p1::v1||p2::v2||p1::dup")
    assert out == {"p1": "v1", "p2": "v2"}


def test_values_match():
    assert _values_match("Bosch", "Bosch GmbH")
    assert _values_match("Germany", "germany")
    assert not _values_match("Bosch", "Siemens")
    assert not _values_match("", "Bosch")



# ---------------------------------------------------------------------------
# COG-112 scoped enrichment: SPARQL generation
# ---------------------------------------------------------------------------


















# ---------------------------------------------------------------------------
# COG-112 review: SPARQL-injection hardening (validators + escaping)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_predicate",
    [
        "has>level",          # IRI-closing bracket
        "has level",          # whitespace
        'has"level',          # quote
        "has{level}",         # braces
        "",                   # empty
        "   ",                # whitespace-only
        "1level",             # must start with letter/underscore
        "ns:level",           # colon (would let it look like a prefixed name)
    ],
)
def test_enrich_scope_rejects_injecting_or_empty_predicate(bad_predicate):
    """An injecting / empty scope.predicate is rejected by the model validator
    (422 at the API boundary) and never reaches the SPARQL builder."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EnrichScope(predicate=bad_predicate, value="Manager")


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_enrich_scope_rejects_empty_value(bad_value):
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EnrichScope(predicate="haslevel", value=bad_value)


def test_enrich_request_rejects_injecting_entity_uri():
    """A non-IRI / injecting entity_uris entry is rejected by the request model
    before it can be spliced into a VALUES block."""
    import pydantic

    from infona_client.enrichment.models import EnrichRequest

    bad = [
        "https://graph.infona.ai/entities/Mentor/m1",  # valid
        "https://evil> } DROP",                      # injects out of <…>
    ]
    with pytest.raises(pydantic.ValidationError):
        EnrichRequest(
            type_name="Mentor",
            attributes=["bio"],
            kg_name="kg",
            entity_uris=bad,
        )
    # A clean list is accepted.
    ok = EnrichRequest(
        type_name="Mentor",
        attributes=["bio"],
        kg_name="kg",
        entity_uris=["https://graph.infona.ai/entities/Mentor/m1"],
    )
    assert ok.entity_uris == ["https://graph.infona.ai/entities/Mentor/m1"]



# ---------------------------------------------------------------------------
# Executor end-to-end
# ---------------------------------------------------------------------------


class FakeWikidata:
    name = "wikidata"

    def __init__(self, mapping: dict[tuple[str, str], list[Verdict]]):
        self._mapping = mapping
        self.calls: list[tuple[str, str]] = []

    async def lookup(self, entity_label, attribute, context):
        self.calls.append((entity_label, attribute))
        return list(self._mapping.get((entity_label, attribute), []))


def test_executor_prefers_name_attribute_over_slug_label():
    async def run():
        # For keyed data (CSV type_id ingest) rdfs:label is the opaque entity-id
        # slug ("Roma_tomatoes") while attrs/name carries the real name. The
        # executor must hand adapters the HUMAN name — a slug degrades every
        # search-based lookup and breaks the whitespace relaxation ladder
        # (prod regression: 15/18, the 3 relaxation-dependent items unfindable).
        rows = [
            {
                "uri": "https://graph.infona.ai/entities/Product/Roma_tomatoes",
                "label": "Roma_tomatoes",          # rdfs:label = entity-id slug
                "nameAttr": "Roma tomatoes",       # attrs/name = human name
                "vals": "",
            },
            # The gate: a REAL human rdfs:label must NOT be displaced by a
            # name-ish fallback (title/headline are job titles/headlines on
            # many types, not names).
            {
                "uri": "https://graph.infona.ai/entities/Product/p1",
                "label": "Jane Smith",             # real human label
                "vals": "https://graph.infona.ai/types/Product/attrs/title::VP of Sales",
            },
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        wikidata = FakeWikidata({
            # Keyed ONLY on the expected labels: the wrong pick yields no fill.
            ("Roma tomatoes", "manufacturer"): [
                Verdict(value="X", confidence=0.95, source="wikidata")
            ],
            ("Jane Smith", "manufacturer"): [
                Verdict(value="Y", confidence=0.95, source="wikidata")
            ],
        })
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), wikidata)
        job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.stage)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.progress.filled == 2

    asyncio.run(run())


def test_executor_end_to_end_filled_verified_conflict():
    async def run():
        # Three entities: one missing manufacturer (filled), one with matching
        # value (verified), one with different value (conflict).
        mfr_pred = "https://graph.infona.ai/types/Product/attrs/manufacturer"
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
            {
                "uri": "https://graph.infona.ai/entities/Product/p2",
                "label": "Drill 18V",
                "vals": f"{mfr_pred}::Bosch",
            },
            {
                "uri": "https://graph.infona.ai/entities/Product/p3",
                "label": "Saw",
                "vals": f"{mfr_pred}::Acme Tools",
            },
        ]

        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
                ("Drill 18V", "manufacturer"): [
                    Verdict(value="Bosch", confidence=0.95, source="wikidata")
                ],
                ("Saw", "manufacturer"): [
                    Verdict(value="Bosch", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.stage)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final is not None
        assert final.status == JobStatus.review
        assert final.progress.total == 3
        assert final.progress.processed == 3
        assert final.progress.filled == 1
        assert final.progress.verified == 1
        assert final.progress.conflicts == 1
        # Fills, verifications, AND conflicts are retained in results so the
        # cited verdict (value + source_url + provenance) is retrievable, not
        # just conflicts. Skips/no-matches carry no verdict and are dropped.
        assert len(final.results) == 3
        assert {r.action for r in final.results} == {"filled", "verified", "conflict"}
        conflict = next(r for r in final.results if r.action == "conflict")
        assert conflict.existing_value == "Acme Tools"
        # NEW (ONTA-159): a conflict-free fill is APPLIED even under stage — only
        # the conflict is held for review. So a write DID happen and it carried
        # the p1 fill value; the conflicting p3 value was NOT written.
        # (Ported by ONTA-527: read off the store, not the emitted SPARQL.)
        assert _props("https://graph.infona.ai/entities/Product/p1")["manufacturer"] == (
            "Robert Bosch GmbH"
        )
        # The conflicting entity (p3) is untouched — its incumbent value is not
        # overwritten and the proposal is not written either.
        assert _props("https://graph.infona.ai/entities/Product/p3")["manufacturer"] == (
            "Acme Tools"
        )
        # The verified entity (p2) keeps its incumbent value.
        assert _props("https://graph.infona.ai/entities/Product/p2")["manufacturer"] == (
            "Bosch"
        )
        # None of this went out as SPARQL.
        writes = " ".join(
            str(c.args[0]) if c.args else "" for c in neptune.update.call_args_list
        )
        assert "Robert Bosch GmbH" not in writes

    asyncio.run(run())


def test_executor_overwrite_writes_triples():
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.status == JobStatus.applied
        # The value landed. (ONTA-527: an `update.await_count >= 1` check would
        # pass on the post-write triple-count invalidation alone, proving nothing
        # about the data write, which no longer emits SPARQL.)
        assert _stored_values("manufacturer") == {"Robert Bosch GmbH"}

    asyncio.run(run())


def test_executor_cache_hit_increment():
    async def run():
        mfr_pred = "https://graph.infona.ai/types/Product/attrs/manufacturer"
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(policy=ConflictPolicy.stage)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        # Second entity (same label) should hit cache.
        assert final.progress.cache_hits >= 1

    asyncio.run(run())


def test_executor_no_match_when_no_verdict():
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Unknown", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job()
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.progress.filled == 0
        assert final.progress.conflicts == 0
        assert final.progress.processed == 1
        # A stage-policy run that found NOTHING to stage must complete as
        # ``applied`` (a finished run that changed nothing), NOT ``review`` —
        # there is nothing to review, so "In review" with zero results would
        # strand the job and confuse the user.
        assert final.status == JobStatus.applied
        assert final.results == []

    asyncio.run(run())


def test_executor_apply_routes_through_shared_writer(monkeypatch):
    """The enrichment apply path MUST go through the shared writer's post-write
    housekeeping (graph/kg_writer.refresh_after_write) — the convergence
    guarantee with CSV/JSON ingestion. Regression guard: if someone reintroduces
    a bespoke write tail that skips re-embed / cache-invalidate, this fails."""
    import infona_client.enrichment.executor as ex

    captured: dict = {}

    async def fake_refresh(neptune, *, tenant_id, kg_name, affected_types, recompute_stats=True):
        captured["called"] = True
        captured["tenant_id"] = tenant_id
        captured["kg_name"] = kg_name
        captured["affected_types"] = set(affected_types)

    monkeypatch.setattr(ex, "refresh_after_write", fake_refresh)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)
        store = InMemoryJobStore()
        wikidata = FakeWikidata(
            {("Bosch", "manufacturer"): [Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), wikidata)
        # overwrite policy + empty existing → action=filled → a write happens.
        job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        assert captured.get("called") is True
        assert captured["kg_name"] == "kg"
        assert captured["affected_types"] == {"Product"}

    asyncio.run(run())


def test_executor_stage_with_no_results_completes_applied():
    """stage policy boundaries (ONTA-159): all no_match → ``applied`` (nothing to
    do); a conflict-free FILL → ``applied`` (auto-applied, nothing to reconcile);
    only a real value-vs-value CONFLICT → ``review``."""

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Unknown", "vals": ""},
        ]

        # No verdicts → no_match for every row → nothing staged.
        neptune = await _prep_neptune(rows)
        store = InMemoryJobStore()
        empty_job = _make_job(policy=ConflictPolicy.stage)
        await store.create(empty_job)
        await EnrichmentExecutor(
            neptune, store, EnrichmentCache(), FakeWikidata({})
        ).run(empty_job, "test-tenant")
        done_empty = await store.get(empty_job.id)
        assert done_empty.status == JobStatus.applied
        assert done_empty.results == []

        # A confident verdict into an EMPTY field → conflict-free fill → APPLIED.
        neptune2 = await _prep_neptune(rows)
        neptune2.update.return_value = None
        store2 = InMemoryJobStore()
        fill_job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.stage)
        await store2.create(fill_job)
        wikidata = FakeWikidata(
            {("Unknown", "manufacturer"): [Verdict(value="Acme", confidence=0.95, source="wikidata")]}
        )
        await EnrichmentExecutor(
            neptune2, store2, EnrichmentCache(), wikidata
        ).run(fill_job, "test-tenant")
        done_fill = await store2.get(fill_job.id)
        assert done_fill.status == JobStatus.applied  # ONTA-159: fills auto-apply
        assert done_fill.progress.filled == 1
        neptune2.update.assert_called()

        # A verdict that DIFFERS from an existing value → real conflict → REVIEW.
        mfr_pred = "https://graph.infona.ai/types/Product/attrs/manufacturer"
        rows3 = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Widget",
             "vals": f"{mfr_pred}::Acme Tools"},
        ]
        neptune3 = AsyncMock()
        neptune3 = await _prep_neptune(rows3)
        neptune3.update.return_value = None
        store3 = InMemoryJobStore()
        conflict_job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.stage)
        await store3.create(conflict_job)
        wikidata3 = FakeWikidata(
            {("Widget", "manufacturer"): [Verdict(value="Globex", confidence=0.95, source="wikidata")]}
        )
        await EnrichmentExecutor(
            neptune3, store3, EnrichmentCache(), wikidata3
        ).run(conflict_job, "test-tenant")
        done_conflict = await store3.get(conflict_job.id)
        assert done_conflict.status == JobStatus.review
        assert done_conflict.progress.conflicts == 1
        assert done_conflict.results[0].action == "conflict"

    asyncio.run(run())


def test_executor_no_match_is_counted_and_excluded_from_results():
    """A lookup that finds nothing is a FIRST-CLASS, COUNTED outcome — not a black
    hole. When the adapter chain returns NO verdict for EVERY (entity, attribute)
    pair, the finished job must report ``progress.no_match == n*a`` (every pair
    missed) with ``filled``/``verified``/``conflicts`` all 0, and the no_match rows
    (which carry no verdict) must NOT appear in ``job.results``."""

    async def run():
        # 2 entities × 2 attributes = 4 pairs, all missing.
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "ZZZNOPE", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "QQQNADA", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        # Empty mapping → FakeWikidata returns [] for every (label, attribute).
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(
            attributes=["manufacturer", "country"],
            policy=ConflictPolicy.stage,
        )
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final is not None
        # Every (entity, attribute) pair is a miss.
        assert final.progress.total == 4
        assert final.progress.processed == 4
        assert final.progress.no_match == 4  # 2 entities × 2 attributes
        # Nothing was filled / verified / conflicted.
        assert final.progress.filled == 0
        assert final.progress.verified == 0
        assert final.progress.conflicts == 0
        # no_match rows carry no verdict → they are dropped from results entirely.
        assert final.results == []
        assert all(r.action != "no_match" for r in final.results)
        # Nothing new written: seeded entities stay, no manufacturer fill.
        assert all(
            "manufacturer" not in (row.get("props") or {})
            for row in _graph_store().snapshot_entities()
        )
        neptune.update.assert_not_called()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# COG-112: a hung adapter lookup must NOT strand the whole job (the production
# hang). A single ``await adapter.lookup(...)`` that never returns and never
# raises (a stalled network call) used to leave the job in ``running`` forever:
# logs stop right after the scoped SELECT, no outbound HTTP, no
# enrichment_job_failed, no completion. The executor now bounds every adapter
# call with ``asyncio.wait_for``, so a stall surfaces as a logged
# ``enrichment_adapter_timeout`` (verdicts=[] → the chain moves on) and the job
# completes. Each test wraps ``executor.run`` in its own ``asyncio.wait_for`` so
# that if the bound regresses the test FAILS (TimeoutError) instead of hanging
# CI forever.
# ---------------------------------------------------------------------------


class _HangingAdapter:
    """A SourceAdapter whose ``lookup`` never returns and never raises —
    mimics a stalled httpx network call (no connect/read timeout fires because
    the connection lingers). Named ``wikidata`` so the default ``lite`` chain
    (["wikidata"]) resolves it after the executor registers it."""

    name = "wikidata"

    def __init__(self) -> None:
        self.calls = 0

    async def lookup(self, entity_label, attribute, context):
        self.calls += 1
        await asyncio.Event().wait()  # block forever, never raise
        return []


def test_executor_hung_adapter_does_not_strand_job(monkeypatch):
    """Regression for COG-112: a forever-hanging adapter must time out per
    lookup and let the job finish, not leave it stuck in ``running``."""

    async def run():
        # Tiny per-adapter timeout so the test is fast. The executor reads this
        # env var at module import, so patch the module-level constant directly.
        import infona_client.enrichment.executor as ex_mod

        monkeypatch.setattr(ex_mod, "ADAPTER_LOOKUP_TIMEOUT_S", 0.2)

        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Acme", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Globex", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        hang = _HangingAdapter()
        executor = EnrichmentExecutor(neptune, store, cache, hang)

        job = _make_job(policy=ConflictPolicy.skip)
        await store.create(job)

        # If the per-adapter timeout regresses, run() hangs → wait_for raises
        # TimeoutError → the test FAILS (loud) instead of hanging CI.
        await asyncio.wait_for(executor.run(job, "test-tenant"), timeout=10)

        final = await store.get(job.id)
        assert final is not None
        # The job MUST reach a terminal state, not be stuck in `running`.
        assert final.status == JobStatus.applied
        # The adapter was actually invoked (and timed out) for each entity.
        assert hang.calls == 2
        # Nothing usable came back, so nothing new was written.
        assert all(
            "manufacturer" not in (row.get("props") or {})
            for row in _graph_store().snapshot_entities()
        )
        neptune.update.assert_not_called()

    asyncio.run(run())


def test_executor_completes_with_fast_adapter_under_wait_for():
    """Control: with a fast adapter the same job completes well within the
    wait_for budget and writes triples — proving the timeout backstop does not
    interfere with the normal path."""

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(policy=ConflictPolicy.overwrite)
        await store.create(job)
        await asyncio.wait_for(executor.run(job, "test-tenant"), timeout=10)

        final = await store.get(job.id)
        assert final.status == JobStatus.applied
        assert final.progress.filled == 1
        assert _stored_values("manufacturer") == {"Robert Bosch GmbH"}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Optional enrichment knobs: instructions + sources override
# ---------------------------------------------------------------------------


class _NamedAdapter:
    """A SourceAdapter that yields a fixed verdict and records the context dict
    it was called with, so tests can assert instructions threading + which
    adapter a chain override actually invoked. Configurable ``name``."""

    def __init__(self, name: str, value: str = "FromAdapter") -> None:
        self.name = name
        self._value = value
        self.calls: list[tuple[str, str, dict]] = []

    async def lookup(self, entity_label, attribute, context):
        self.calls.append((entity_label, attribute, dict(context)))
        return [Verdict(value=self._value, confidence=0.95, source=self.name)]


async def _single_product_neptune():
    rows = [
        {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
    ]
    return await _prep_neptune(rows)


def test_executor_sources_override_uses_named_chain():
    """When job.sources is set (and no per-attribute strategy sources), the
    executor walks THAT chain instead of the tier default — invoking the named,
    registered adapter."""

    async def run():
        from infona_client.enrichment.sources.base import register_adapter

        neptune = await _single_product_neptune()
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})  # tier default would call this; it must NOT
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        custom = _NamedAdapter("customsrc", value="Robert Bosch GmbH")
        register_adapter(custom)

        job = _make_job(policy=ConflictPolicy.stage)
        job.sources = ["customsrc"]
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.status == JobStatus.applied  # ONTA-159: conflict-free fill auto-applies
        assert final.progress.filled == 1
        # The override adapter was used; the tier-default wikidata was not.
        assert custom.calls and custom.calls[0][1] == "manufacturer"
        assert wikidata.calls == []

    asyncio.run(run())


def test_executor_sources_empty_falls_back_to_tier_chain():
    """An empty sources list is falsy → the executor keeps today's tier default
    (wikidata), so omitting/clearing the override changes nothing."""

    async def run():
        neptune = await _single_product_neptune()
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(policy=ConflictPolicy.stage)
        job.sources = []  # explicitly empty → fall back
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.progress.filled == 1
        # Fell back to the tier default chain (wikidata).
        assert wikidata.calls == [("Bosch", "manufacturer")]

    asyncio.run(run())


def test_executor_sources_unknown_provider_falls_back_to_tier_chain():
    """A sources override naming ONLY unregistered (e.g. premium-only) providers
    falls back to the tier default chain rather than enriching nothing — matching
    the UI's "falls back to Auto if unavailable" promise. The job completes and
    the default (wikidata) adapter is still consulted."""

    async def run():
        neptune = await _single_product_neptune()
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(policy=ConflictPolicy.stage)
        job.sources = ["exa"]  # not registered in OSS → no available override
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        # The all-unavailable override falls back to the tier chain, so wikidata
        # is consulted and the attribute is filled (not a silent empty job).
        assert final.status == JobStatus.applied  # ONTA-159: conflict-free fill auto-applies
        assert final.progress.filled == 1
        assert final.progress.processed == 1
        assert wikidata.calls != []

    asyncio.run(run())


def test_executor_instructions_flow_into_lookup_context():
    """job.instructions is threaded into the adapter lookup context dict; the
    job's entity type always rides along as ``entity_type`` (ONTA-191). When
    instructions are absent the context carries only ``entity_type``."""

    async def run():
        from infona_client.enrichment.sources.base import register_adapter

        # With instructions.
        neptune = await _single_product_neptune()
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        executor = EnrichmentExecutor(neptune, store, cache, FakeWikidata({}))
        adapter = _NamedAdapter("instr_src", value="Robert Bosch GmbH")
        register_adapter(adapter)

        job = _make_job(policy=ConflictPolicy.stage)
        job.sources = ["instr_src"]
        job.instructions = "Prefer the official legal entity name."
        await store.create(job)
        await executor.run(job, "test-tenant")

        assert adapter.calls
        # entity_type ("Product", the job's type_name) always rides in the ctx
        # (ONTA-191) alongside the optional instructions; tenant_id (the job's
        # tenant) always rides too (ONTA-2xx — a tenant_custom registry adapter
        # needs it to build its per-tenant secret resolver).
        assert adapter.calls[0][2] == {
            "instructions": "Prefer the official legal entity name.",
            "entity_type": "Product",
            "tenant_id": "test-tenant",
        }

        # Without instructions → context carries entity_type + tenant_id (no
        # instructions key). The call shape is unchanged aside from the always-on
        # entity_type + tenant_id contract.
        neptune2 = await _single_product_neptune()
        store2 = InMemoryJobStore()
        executor2 = EnrichmentExecutor(
            neptune2, store2, EnrichmentCache(), FakeWikidata({})
        )
        adapter2 = _NamedAdapter("instr_src2", value="Robert Bosch GmbH")
        register_adapter(adapter2)
        job2 = _make_job(policy=ConflictPolicy.stage)
        job2.sources = ["instr_src2"]
        await store2.create(job2)
        await executor2.run(job2, "test-tenant")
        assert adapter2.calls and adapter2.calls[0][2] == {
            "entity_type": "Product",
            "tenant_id": "test-tenant",
        }

    asyncio.run(run())


def test_executor_instructions_vary_cache_key():
    """Two jobs with DIFFERENT instructions must not share a cached verdict —
    instructions are folded into the cache strategy_version, so the second job
    re-queries the adapter rather than reusing the first job's cached result."""

    async def run():
        from infona_client.enrichment.sources.base import register_adapter

        store = InMemoryJobStore()
        cache = EnrichmentCache()  # shared across both jobs
        adapter = _NamedAdapter("cachesrc", value="Robert Bosch GmbH")
        register_adapter(adapter)

        async def run_job(instructions):
            neptune = await _single_product_neptune()
            executor = EnrichmentExecutor(neptune, store, cache, FakeWikidata({}))
            job = _make_job(policy=ConflictPolicy.stage)
            job.id = f"job-{instructions or 'none'}"
            job.sources = ["cachesrc"]
            job.instructions = instructions
            await store.create(job)
            await executor.run(job, "test-tenant")

        await run_job("instruction A")
        await run_job("instruction B")
        # Different instructions → different cache key → adapter invoked twice.
        assert len(adapter.calls) == 2

        # Same instructions as the first run → cache HIT, no third invocation.
        await run_job("instruction A")
        assert len(adapter.calls) == 2

    asyncio.run(run())


def test_strategy_version_with_instructions_helper():
    """The cache-version helper is byte-for-byte identity without instructions,
    and stable + distinct per instruction string with them."""
    from infona_client.enrichment.executor import (
        _strategy_version_with_instructions,
    )

    assert _strategy_version_with_instructions("v1", None) == "v1"
    assert _strategy_version_with_instructions("v1", "") == "v1"
    a = _strategy_version_with_instructions("v1", "do X")
    b = _strategy_version_with_instructions("v1", "do Y")
    assert a != b and a.startswith("v1+instr:")
    # Stable for the same input.
    assert a == _strategy_version_with_instructions("v1", "do X")


# ---------------------------------------------------------------------------
# COG-112 scoped enrichment: GraphStore entity select
# ---------------------------------------------------------------------------


HASLEVEL = "https://graph.infona.ai/types/Mentor/attrs/haslevel"
TITLE = "https://graph.infona.ai/types/Mentor/attrs/title"
NAME = "https://graph.infona.ai/types/Mentor/attrs/name"


def test_executor_scope_relationship_selects_only_scoped_entities():
    """A scope on haslevel=Manager only enriches matching Mentors."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada",
             "vals": f"{HASLEVEL}::Manager"},
            {"uri": "https://graph.infona.ai/entities/Mentor/m2", "label": "Grace",
             "vals": f"{HASLEVEL}::Manager"},
            {"uri": "https://graph.infona.ai/entities/Mentor/m3", "label": "Linus",
             "vals": f"{HASLEVEL}::IC"},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Ada", "bio"): [Verdict(value="Ada bio", confidence=0.95, source="wikidata")],
                ("Grace", "bio"): [Verdict(value="Grace bio", confidence=0.95, source="wikidata")],
                ("Linus", "bio"): [Verdict(value="Linus bio", confidence=0.95, source="wikidata")],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(
            type_name="Mentor",
            attributes=["bio"],
            scope=EnrichScope(predicate="haslevel", value="Manager"),
        )
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final is not None
        assert final.progress.total == 2
        assert final.progress.processed == 2
        assert final.progress.filled == 2
        assert sorted(lbl for lbl, _ in wikidata.calls) == ["Ada", "Grace"]

    asyncio.run(run())


def test_executor_scope_predicate_casing_matches_via_lcase():
    """A mixed-case request predicate (`hasLevel`) matches stored `haslevel`."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada",
             "vals": f"{HASLEVEL}::Manager"},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {("Ada", "bio"): [Verdict(value="Ada bio", confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(
            type_name="Mentor",
            attributes=["bio"],
            scope=EnrichScope(predicate="hasLevel", value="Manager"),
        )
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.progress.total == 1
        assert final.progress.processed == 1

    asyncio.run(run())


def test_executor_scope_relationship_not_in_ontology_attrs_still_matches():
    """A relationship leaf not declared as an attribute still scopes via props."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada",
             "vals": f"{HASLEVEL}::Manager"},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {("Ada", "bio"): [Verdict(value="Ada bio", confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(
            type_name="Mentor",
            attributes=["bio"],
            scope=EnrichScope(predicate="haslevel", value="Manager"),
        )
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final is not None
        assert final.progress.total == 1
        assert final.progress.processed == 1

    asyncio.run(run())


def test_resolve_scope_predicate_iris_unions_direct_build_for_relationships():
    """Catalog miss + direct build still yields …/onto/<pred> and the attr IRI."""
    async def run():
        from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type

        await upsert_type(name="Mentor", tenant_id="test-tenant")
        await upsert_attribute(
            type_name="Mentor", attr_name="title", datatype="string",
            tenant_id="test-tenant",
        )
        neptune = AsyncMock()
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(), FakeWikidata({})
        )
        iris = await executor._resolve_scope_predicate_iris(
            "test-tenant", "Mentor", EnrichScope(predicate="haslevel", value="Manager")
        )
        assert "https://graph.infona.ai/onto/haslevel" in iris
        assert "https://graph.infona.ai/types/Mentor/attrs/haslevel" in iris

        attr_iris = await executor._resolve_scope_predicate_iris(
            "test-tenant", "Mentor", EnrichScope(predicate="TITLE", value="Senior")
        )
        assert attr_iris == [
            "https://graph.infona.ai/types/Mentor/attrs/title",
            "https://graph.infona.ai/onto/title",
        ]

    asyncio.run(run())


def test_executor_scope_literal_attribute_matches_title():
    """A scope on a literal title only selects matching entities."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada",
             "vals": f"{TITLE}::Director"},
            {"uri": "https://graph.infona.ai/entities/Mentor/m2", "label": "Grace",
             "vals": f"{TITLE}::IC"},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {("Ada", "bio"): [Verdict(value="Ada bio", confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(
            type_name="Mentor",
            attributes=["bio"],
            scope=EnrichScope(predicate="title", value="Director"),
        )
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.progress.total == 1
        assert final.progress.processed == 1
        assert wikidata.calls == [("Ada", "bio")]

    asyncio.run(run())


def test_executor_scope_literal_name_value_matches_display_name():
    """A name/label scope matches the entity display name."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada Lovelace"},
            {"uri": "https://graph.infona.ai/entities/Mentor/m2", "label": "Grace Hopper"},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {("Ada Lovelace", "bio"): [Verdict(value="Ada bio", confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(
            type_name="Mentor",
            attributes=["bio"],
            scope=EnrichScope(predicate="name", value="Ada Lovelace"),
        )
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.progress.total == 1
        assert wikidata.calls == [("Ada Lovelace", "bio")]

    asyncio.run(run())


def test_executor_entity_uris_subset_only_those_enriched():
    """entity_uris restricts the run to exactly those URIs."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Mentor/m2", "label": "Grace", "vals": ""},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {("Ada", "bio"): [Verdict(value="Ada bio", confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(
            type_name="Mentor",
            attributes=["bio"],
            entity_uris=["https://graph.infona.ai/entities/Mentor/m1"],
        )
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.progress.total == 1
        assert final.progress.processed == 1
        assert wikidata.calls == [("Ada", "bio")]

    asyncio.run(run())


def test_executor_no_scope_runs_whole_type():
    """No scope/entity_uris → every entity of the type is selected."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Mentor/m2", "label": "Grace", "vals": ""},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(type_name="Mentor", attributes=["bio"])
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.progress.total == 2

    asyncio.run(run())


def test_count_entities_honors_scope_and_entity_uris():
    """count_entities uses the same GraphStore subset as the run select."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada",
             "vals": f"{HASLEVEL}::Manager"},
            {"uri": "https://graph.infona.ai/entities/Mentor/m2", "label": "Grace",
             "vals": f"{HASLEVEL}::Manager"},
            {"uri": "https://graph.infona.ai/entities/Mentor/m3", "label": "Linus",
             "vals": f"{HASLEVEL}::IC"},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(), FakeWikidata({})
        )
        n = await executor.count_entities(
            "test-tenant", "kg", "Mentor",
            scope=EnrichScope(predicate="haslevel", value="Manager"),
        )
        assert n == 2
        n = await executor.count_entities(
            "test-tenant", "kg", "Mentor",
            scope=EnrichScope(predicate="haslevel", value="Manager"),
            entity_uris=["https://graph.infona.ai/entities/Mentor/m1"],
        )
        assert n == 1
        n = await executor.count_entities("test-tenant", "kg", "Mentor")
        assert n == 3

    asyncio.run(run())


def test_count_entities_relationship_not_in_ontology_attrs_still_counts():
    """A relationship leaf still counts via entity props (no SPARQL)."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Mentor/m1", "label": "Ada",
             "vals": f"{HASLEVEL}::Manager"},
        ]
        neptune = await _prep_neptune(rows, type_name="Mentor")
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(), FakeWikidata({})
        )
        n = await executor.count_entities(
            "test-tenant", "kg", "Mentor",
            scope=EnrichScope(predicate="haslevel", value="Manager"),
        )
        assert n == 1

    asyncio.run(run())


def test_count_entities_no_store_returns_zero_without_sparql():
    """A store outage logs and returns 0 — it must not SPARQL."""
    from infona_client.graph.store import reset_graph_store_for_tests

    async def run():
        reset_graph_store_for_tests()
        neptune = AsyncMock()
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(), FakeWikidata({})
        )
        n = await executor.count_entities(
            "test-tenant", "kg", "Mentor",
            scope=EnrichScope(predicate="haslevel", value="Manager"),
        )
        assert n == 0

    asyncio.run(run())


def test_apply_decisions_writes_accepted_only(monkeypatch):
    # apply_decisions now schedules a real stats recompute after a write; stub it
    # so this test stays focused on the write itself (and doesn't leave a
    # fire-and-forget recompute task draining against the AsyncMock).
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        neptune = AsyncMock()
        # Declaration reads the attribute's existing range first; none exists yet.
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(policy=ConflictPolicy.stage)
        await store.create(job)

        decisions = [
            ConflictReview(
                entity_uri="https://graph.infona.ai/entities/Product/p1",
                attribute="manufacturer",
                existing_value="Acme",
                proposed=Verdict(value="Bosch", confidence=0.95, source="wikidata"),
                decision="accept",
            ),
            ConflictReview(
                entity_uri="https://graph.infona.ai/entities/Product/p2",
                attribute="manufacturer",
                existing_value="X",
                proposed=Verdict(value="Y", confidence=0.95, source="wikidata"),
                decision="reject",
            ),
        ]

        applied = await executor.apply_decisions(job.id, decisions)
        assert applied == 1
        # Only the ACCEPTED decision is written.
        assert _stored_values("manufacturer") == {"Bosch"}

    asyncio.run(run())


def test_executor_apply_schedules_stats_recompute(monkeypatch):
    """An auto-apply that writes triples must bust the Explorer summary cache by
    scheduling a stats recompute for the job's (tenant, kg). (ONTA-279: an
    overwrite refresh routes each value through the P6 supersession op, which
    refreshes per fact, so the recompute may be scheduled more than once — the
    invariant is that it IS scheduled for this (tenant, kg).)"""
    import infona_client.api.routes.explore as explore_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        explore_mod,
        "schedule_recompute",
        lambda client, tenant_id, kg_name: calls.append((tenant_id, kg_name)),
    )

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.status == JobStatus.applied

    asyncio.run(run())
    assert ("test-tenant", "kg") in calls
    assert all(c == ("test-tenant", "kg") for c in calls)


def test_executor_no_apply_does_not_recompute(monkeypatch):
    """A stage job whose only result is a CONFLICT writes nothing (the conflict
    is held for review) → no recompute should be scheduled. (ONTA-159: a stage
    FILL now DOES write + recompute, so the no-write case must be a conflict.)"""
    import infona_client.api.routes.explore as explore_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        explore_mod,
        "schedule_recompute",
        lambda client, tenant_id, kg_name: calls.append((tenant_id, kg_name)),
    )

    async def run():
        mfr_pred = "https://graph.infona.ai/types/Product/attrs/manufacturer"
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch",
             "vals": f"{mfr_pred}::Acme Tools"},  # existing value → verdict will conflict
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        # stage + a sole conflict → held for review, nothing written.
        job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.stage)
        await store.create(job)
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.status == JobStatus.review
        assert final.progress.conflicts == 1

    asyncio.run(run())
    assert calls == []


def test_apply_decisions_schedules_stats_recompute(monkeypatch):
    """A review-apply that accepts >=1 fact schedules a recompute for (tenant, kg)."""
    import infona_client.api.routes.explore as explore_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        explore_mod,
        "schedule_recompute",
        lambda client, tenant_id, kg_name: calls.append((tenant_id, kg_name)),
    )

    async def run():
        neptune = AsyncMock()
        # Declaration reads the attribute's existing range first; none exists yet.
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(policy=ConflictPolicy.stage)
        await store.create(job)

        decisions = [
            ConflictReview(
                entity_uri="https://graph.infona.ai/entities/Product/p1",
                attribute="manufacturer",
                existing_value="Acme",
                proposed=Verdict(value="Bosch", confidence=0.95, source="wikidata"),
                decision="accept",
            ),
        ]
        applied = await executor.apply_decisions(job.id, decisions)
        assert applied == 1

    asyncio.run(run())
    # job's (tenant_id, kg_name) come from _make_job: "test-tenant" / "kg".
    assert calls == [("test-tenant", "kg")]


def test_apply_decisions_no_accept_does_not_recompute(monkeypatch):
    """All-reject review applies nothing → no recompute scheduled."""
    import infona_client.api.routes.explore as explore_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        explore_mod,
        "schedule_recompute",
        lambda client, tenant_id, kg_name: calls.append((tenant_id, kg_name)),
    )

    async def run():
        neptune = AsyncMock()
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(policy=ConflictPolicy.stage)
        await store.create(job)

        decisions = [
            ConflictReview(
                entity_uri="https://graph.infona.ai/entities/Product/p2",
                attribute="manufacturer",
                existing_value="X",
                proposed=Verdict(value="Y", confidence=0.95, source="wikidata"),
                decision="reject",
            ),
        ]
        applied = await executor.apply_decisions(job.id, decisions)
        assert applied == 0

    asyncio.run(run())
    assert calls == []


# ---------------------------------------------------------------------------
# Enrichment extends the ontology (declare-then-write) — COG-112
# ---------------------------------------------------------------------------


def test_executor_apply_declares_attribute_in_ontology(monkeypatch):
    """An auto-apply that writes a value for an attribute must ALSO declare that
    attribute in the TENANT ontology — so the enriched attribute is first-class
    schema (COG-112). Its provenance companions are deliberately NOT declared
    (ONTA-262: attr_meta metadata, never sibling attributes).

    Ported by ONTA-527: a declaration is an ``:OntoAttr`` row in the tenant
    catalog now, not an ``rdf:Property`` + ``rdfs:domain`` INSERT, so the same
    claims are checked against the catalog.
    """
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "company"): [
                    Verdict(
                        value="Robert Bosch GmbH",
                        confidence=0.95,
                        source="wikidata",
                        source_url="https://www.wikidata.org/wiki/Q234021",
                    )
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["company"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.status == JobStatus.applied

        # The primary 'company' attribute is declared on <Product>, as a literal
        # with a string range, in the TENANT catalog layer.
        declared = await _declared_attrs("Product")
        assert "company" in declared, "company attribute not declared"
        company = declared["company"]
        assert company.domain == "Product"
        assert company.layer == "tenant"
        assert company.tenant_id == "test-tenant"
        assert (company.kind, company.datatype) == ("literal", "string")

        # Companion provenance metadata is NEVER declared (ONTA-262): companions
        # live on the attr_meta namespace as instance metadata, and declaring
        # them is what rendered `<attr>_source_url` / `<attr>_provenance` as
        # sibling columns on every schema surface.
        assert set(declared) == {"company"}

    asyncio.run(run())


def test_executor_stage_mode_does_not_declare(monkeypatch):
    """A stage job whose only result is a CONFLICT writes nothing yet → it must
    NOT declare attributes in the ontology. (ONTA-159: a stage FILL now writes +
    declares, so the no-declare case must be a held conflict.)"""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        company_pred = "https://graph.infona.ai/types/Product/attrs/company"
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch",
             "vals": f"{company_pred}::Old Holdings"},  # existing → verdict conflicts
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "company"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["company"], policy=ConflictPolicy.stage)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final.status == JobStatus.review
        # The conflict is held, not written → the incumbent stays, nothing declared.
        assert await _declared_attrs("Product") == {}
        assert _props("https://graph.infona.ai/entities/Product/p1")["company"] == (
            "Old Holdings"
        )
        neptune.update.assert_not_called()

    asyncio.run(run())


def test_executor_no_match_does_not_declare(monkeypatch):
    """An attribute that found no value contributes no triples → it must NOT be
    declared (no ontology pollution with empty slots)."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Unknown", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})  # no verdicts → no_match
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["company"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        assert await _declared_attrs("Product") == {}

    asyncio.run(run())


def test_apply_decisions_declares_accepted_attribute(monkeypatch):
    """Accepting a review decision also extends the ontology (declares the
    accepted attribute + its provenance companions in the tenant graph)."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        neptune = AsyncMock()
        neptune.update.return_value = None
        # Declaration reads the attribute's existing range first; none exists yet.
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(attributes=["company"], policy=ConflictPolicy.stage)
        await store.create(job)

        decisions = [
            ConflictReview(
                entity_uri="https://graph.infona.ai/entities/Product/p1",
                attribute="company",
                existing_value="Acme",
                proposed=Verdict(
                    value="Robert Bosch GmbH",
                    confidence=0.95,
                    source="wikidata",
                    source_url="https://www.wikidata.org/wiki/Q234021",
                ),
                decision="accept",
            ),
        ]
        applied = await executor.apply_decisions(job.id, decisions)
        assert applied == 1

        declared = await _declared_attrs("Product")
        assert "company" in declared
        assert declared["company"].layer == "tenant"
        assert declared["company"].tenant_id == "test-tenant"

    asyncio.run(run())


def _query_router(entities: dict, *, existing_range: str | None = None):
    """An AsyncMock ``query`` side_effect that serves the entity-selection SELECT
    and the per-attribute range SELECT (``SELECT ?range``) from one fake Neptune.
    ``existing_range`` is the range the ontology already declares for the enriched
    attribute (None = undeclared)."""

    async def _route(sparql: str, *a, **k):
        if "SELECT ?range" in sparql:
            return _range_response(existing_range)
        return entities

    return _route


def test_executor_apply_infers_integer_range_for_numeric_values(monkeypatch):
    """A brand-new enriched attribute whose applied values are all numeric must be
    declared with an xsd:integer range — NOT blindly stamped xsd:string (the
    hardcoded-datatype bug)."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Makita", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "humanness_score"): [
                    Verdict(value="92", confidence=0.95, source="wikidata")
                ],
                ("Makita", "humanness_score"): [
                    Verdict(value="87", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["humanness_score"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        declared = await _declared_attrs("Product")
        assert "humanness_score" in declared, "humanness_score attribute not declared"
        # The numeric attribute is typed as integer, NOT stamped string.
        assert declared["humanness_score"].datatype == "integer"

    asyncio.run(run())


def test_executor_apply_does_not_downgrade_existing_richer_range(monkeypatch):
    """If an attribute already carries a richer range (an ingest-inferred
    xsd:integer, or a relationship types/<Target> URI), applying an enrichment job
    on it must PRESERVE that range — never silently downgrade it to xsd:string."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def _assert_preserves(existing_range: str, value: str, expected: tuple[str, str]):
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        await _seed_existing_range("Product", "rating", existing_range)
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {("Bosch", "rating"): [Verdict(value=value, confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["rating"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        declared = await _declared_attrs("Product")
        assert "rating" in declared, "rating attribute not declared"
        rating = declared["rating"]
        # The pre-existing richer range is re-asserted verbatim, never downgraded
        # to a string literal range.
        kind, target = expected
        assert rating.kind == kind
        if kind == "literal":
            assert rating.datatype == target
        else:
            assert rating.range_type == target
            assert rating.datatype is None

    async def run():
        # (a) ingest-inferred integer range survives a string-valued enrichment.
        await _assert_preserves(
            "http://www.w3.org/2001/XMLSchema#integer",
            value="five stars",
            expected=("literal", "integer"),
        )
        # (b) a relationship range (types/<Target>) survives too — the edge stays.
        await _assert_preserves(
            "https://graph.infona.ai/types/Manufacturer",
            value="Robert Bosch GmbH",
            expected=("relationship", "Manufacturer"),
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# E2: date + entity-reference (relationship) datatype inference
# ---------------------------------------------------------------------------


def test_infer_datatype_dates_to_datetime():
    """ISO dates / datetimes (plain, Z-suffixed, +00:00) all infer ``datetime``."""
    assert _infer_datatype_from_values(["2026-06-28"]) == "datetime"
    assert (
        _infer_datatype_from_values(["2026-06-28", "2026-06-28T00:00:00Z"])
        == "datetime"
    )
    assert (
        _infer_datatype_from_values(["2026-06-28T21:24:50+00:00"]) == "datetime"
    )


def test_infer_datatype_integer_not_misread_as_date():
    """An all-integer column must stay ``integer`` — never a date false-positive,
    even though a bare year-like int contains no separator."""
    assert _infer_datatype_from_values(["2026", "1999", "42"]) == "integer"


def test_infer_datatype_entity_iris_same_type_is_relationship():
    """All values are entity IRIs sharing one ``<TypeName>`` → that bare type name
    (which maps to a ``types/<TypeName>`` relationship range)."""
    vals = [
        "https://graph.infona.ai/entities/Manufacturer/bosch",
        "https://graph.infona.ai/entities/Manufacturer/makita",
    ]
    assert _infer_datatype_from_values(vals) == "Manufacturer"


def test_infer_datatype_mixed_iri_types_falls_back_to_string():
    """Entity IRIs of DIFFERENT types have no single relationship range → string
    (don't guess)."""
    vals = [
        "https://graph.infona.ai/entities/Manufacturer/bosch",
        "https://graph.infona.ai/entities/Country/germany",
    ]
    assert _infer_datatype_from_values(vals) == "string"


def test_entity_iri_type_parses_and_rejects():
    """``_entity_iri_type`` extracts the type from a canonical entity IRI and
    returns None for non-matching values (literal, foreign URI, missing id)."""
    assert (
        _entity_iri_type("https://graph.infona.ai/entities/Manufacturer/bosch")
        == "Manufacturer"
    )
    assert _entity_iri_type("Robert Bosch GmbH") is None
    assert _entity_iri_type("https://graph.infona.ai/types/Manufacturer") is None
    # Missing <id> segment is not a complete entity IRI.
    assert _entity_iri_type("https://graph.infona.ai/entities/Manufacturer") is None
    assert _entity_iri_type("https://graph.infona.ai/entities/Manufacturer/") is None


def test_executor_apply_infers_datetime_range_for_date_values(monkeypatch):
    """A brand-new enriched attribute whose applied values are all ISO dates must
    be declared with an xsd:dateTime range — NOT stamped xsd:string."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Makita", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "founded"): [
                    Verdict(value="2026-06-28", confidence=0.95, source="wikidata")
                ],
                ("Makita", "founded"): [
                    Verdict(
                        value="2026-06-28T00:00:00Z", confidence=0.95, source="wikidata"
                    )
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["founded"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        declared = await _declared_attrs("Product")
        assert "founded" in declared, "founded attribute not declared"
        assert declared["founded"].datatype == "datetime"

    asyncio.run(run())


def test_executor_apply_entity_iri_values_declare_relationship_and_write_iri(monkeypatch):
    """When all applied values are entity IRIs of one type ``Manufacturer``, the
    attribute is declared with a ``Manufacturer`` relationship range AND the
    instance fact is written as a real EDGE to that entity, never a literal."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Drill", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Saw", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Drill", "manufacturer"): [
                    Verdict(
                        value="https://graph.infona.ai/entities/Manufacturer/bosch",
                        confidence=0.95,
                        source="wikidata",
                    )
                ],
                ("Saw", "manufacturer"): [
                    Verdict(
                        value="https://graph.infona.ai/entities/Manufacturer/makita",
                        confidence=0.95,
                        source="wikidata",
                    )
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        # (a) declared as a relationship ranged on Manufacturer, not a string literal.
        declared = await _declared_attrs("Product")
        assert "manufacturer" in declared, "manufacturer attribute not declared"
        assert declared["manufacturer"].kind == "relationship"
        assert declared["manufacturer"].range_type == "Manufacturer"
        assert declared["manufacturer"].datatype is None

        # (b) the instance fact is an EDGE to that entity, not a literal property.
        edges = {(r["start_id"], r["end_id"]) for r in _rels_for("manufacturer")}
        assert edges == {
            (
                "https://graph.infona.ai/entities/Product/p1",
                "https://graph.infona.ai/entities/Manufacturer/bosch",
            ),
            (
                "https://graph.infona.ai/entities/Product/p2",
                "https://graph.infona.ai/entities/Manufacturer/makita",
            ),
        }
        assert "manufacturer" not in _props(
            "https://graph.infona.ai/entities/Product/p1"
        )

    asyncio.run(run())


def test_executor_apply_does_not_downgrade_datetime_or_relationship_range(monkeypatch):
    """No-downgrade holds for the E2 ranges too: an existing xsd:dateTime or a
    relationship types/<Target> range survives an enrichment whose own values
    would infer something weaker."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def _assert_preserves(existing_range: str, value: str, expected: tuple[str, str]):
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        await _seed_existing_range("Product", "founded", existing_range)
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {("Bosch", "founded"): [Verdict(value=value, confidence=0.95, source="wikidata")]}
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["founded"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        declared = await _declared_attrs("Product")
        assert "founded" in declared, "founded attribute not declared"
        founded = declared["founded"]
        kind, target = expected
        assert founded.kind == kind
        if kind == "literal":
            assert founded.datatype == target
        else:
            assert founded.range_type == target
            assert founded.datatype is None

    async def run():
        # An existing xsd:dateTime survives a free-text enrichment value.
        await _assert_preserves(
            "http://www.w3.org/2001/XMLSchema#dateTime",
            value="sometime in 2026",
            expected=("literal", "datetime"),
        )
        # An existing relationship range survives a string-valued enrichment.
        await _assert_preserves(
            "https://graph.infona.ai/types/Manufacturer",
            value="Robert Bosch GmbH",
            expected=("relationship", "Manufacturer"),
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    from infona_client.enrichment.cache import reset_enrichment_cache
    from infona_client.enrichment.job_store import reset_job_store
    from infona_client.enrichment.tiers import reset_tiers

    reset_job_store()
    reset_enrichment_cache()
    # Clean chain/tier state (also clears any chain-prefix provider a prior
    # app-fixture test registered via deps → api_registry enrichment, ONTA-194).
    reset_tiers()
    yield
    reset_job_store()
    reset_enrichment_cache()
    reset_tiers()


def test_post_jobs_returns_job_id(client, auth_headers, mock_neptune):
    # The executor's background run loop may issue queries once spawned; we don't
    # care about its outcome here (create itself no longer counts entities).
    mock_neptune.query.return_value = _count_response(0)

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Product",
            "attributes": ["manufacturer"],
            "kg_name": "kg",
            "tier": "lite",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "queued"
    # Non-blocking create (COG-112): matched count is resolved by the background
    # executor (job.progress.total), not at create time, so it is None here.
    assert data["matched_entities"] is None


def test_post_jobs_holds_strong_ref_to_background_task(
    client, auth_headers, mock_neptune, monkeypatch
):
    """COG-112 regression guard: the create path must keep a *strong* reference to
    the spawned executor task. A bare ``asyncio.create_task(...)`` is only
    weak-referenced by the loop and gets GC'd at the first await after the request
    returns — stranding the job right after it selects entities. We capture the
    coroutine handed to the executor and assert create routes it through the
    module-level ``_spawn`` helper (which registers it in ``_bg_tasks``), never as
    a bare task."""
    import infona_client.api.routes.enrich as enrich_mod

    captured: list = []
    real_spawn = enrich_mod._spawn

    def _tracking_spawn(coro):
        captured.append(coro)
        real_spawn(coro)
        # Right after scheduling, the task must be held by the module set so it
        # cannot be garbage-collected mid-run.
        assert len(enrich_mod._bg_tasks) >= 1

    monkeypatch.setattr(enrich_mod, "_spawn", _tracking_spawn)
    mock_neptune.query.return_value = _count_response(0)

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Product",
            "attributes": ["manufacturer"],
            "kg_name": "kg",
        },
    )
    assert response.status_code == 202
    # create scheduled exactly one background task via the strong-ref helper.
    assert len(captured) == 1


def test_post_jobs_with_scope_threads_scope_without_blocking(
    client, auth_headers, mock_neptune
):
    """A scoped create-job (COG-112): create is NON-BLOCKING — it does NOT call
    count_entities in the request path — so it can never time out on a slow
    scoped COUNT. The stored job persists the scope so the background executor
    resolves it and surfaces the matched count via progress.total."""

    mock_neptune.query.side_effect = AssertionError("create must not SPARQL")

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Mentor",
            "attributes": ["bio"],
            "kg_name": "kg",
            "tier": "lite",
            "scope": {"predicate": "haslevel", "value": "Manager"},
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "queued"
    # Matched count is resolved by the background executor, not at create time.
    assert data["matched_entities"] is None

    # The stored job retains the scope (full-job view) so the executor uses it.
    job = client.get(
        f"/graphs/test-tenant/enrich/jobs/{data['job_id']}", headers=auth_headers
    ).json()
    assert job["scope"] == {"predicate": "haslevel", "value": "Manager"}
    assert job["entity_uris"] is None


def test_post_jobs_does_not_block_on_count_entities(
    client, auth_headers, mock_neptune, monkeypatch
):
    """The create path must NOT await count_entities (COG-112 non-blocking
    guarantee): even if count_entities hangs/raises, create still returns a job
    id promptly. We monkeypatch the executor's count_entities to blow up if
    called and assert create succeeds without invoking it."""
    from infona_client.enrichment import executor as executor_mod

    async def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("count_entities must not be called in create path")

    monkeypatch.setattr(
        executor_mod.EnrichmentExecutor, "count_entities", _boom
    )
    mock_neptune.query.return_value = _count_response(0)

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Mentor",
            "attributes": ["bio"],
            "kg_name": "kg",
            "scope": {"predicate": "haslevel", "value": "Manager"},
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "queued"


def test_post_jobs_with_entity_uris_subset(client, auth_headers, mock_neptune):
    """entity_uris on create-job persists the explicit subset; create is
    non-blocking so it does not count the subset up front (matched_entities is
    resolved later by the executor)."""
    mock_neptune.query.return_value = _count_response(1)
    uris = ["https://graph.infona.ai/entities/Mentor/m1"]
    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Mentor",
            "attributes": ["bio"],
            "kg_name": "kg",
            "entity_uris": uris,
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["matched_entities"] is None
    job = client.get(
        f"/graphs/test-tenant/enrich/jobs/{data['job_id']}", headers=auth_headers
    ).json()
    assert job["entity_uris"] == uris


def test_get_jobs_lists_jobs(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = _count_response(0)
    r = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Product",
            "attributes": ["manufacturer"],
            "kg_name": "kg",
        },
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    listing = client.get(
        "/graphs/test-tenant/enrich/jobs", headers=auth_headers
    )
    assert listing.status_code == 200
    rows = listing.json()
    ids = [j["id"] for j in rows]
    assert job_id in ids


def test_get_job_404(client, auth_headers, mock_neptune):
    response = client.get(
        "/graphs/test-tenant/enrich/jobs/does-not-exist", headers=auth_headers
    )
    assert response.status_code == 404


def test_conflicts_and_apply_flow(client, auth_headers, mock_neptune):
    """Seed a job directly, set a conflict result, then call /conflicts and /apply."""
    from infona_client.enrichment.job_store import get_job_store
    from infona_client.enrichment.models import RowResult

    job = _make_job(policy=ConflictPolicy.stage)
    job.tenant_id = "test-tenant"
    job.status = JobStatus.review
    verdict = Verdict(value="Bosch", confidence=0.95, source="wikidata")
    job.results = [
        RowResult(
            entity_uri="https://graph.infona.ai/entities/Product/p1",
            attribute="manufacturer",
            existing_value="Acme",
            verdict=verdict,
            action="conflict",
        )
    ]

    async def _seed():
        store = get_job_store()
        await store.create(job)

    asyncio.run(_seed())

    r = client.get(
        f"/graphs/test-tenant/enrich/jobs/{job.id}/conflicts", headers=auth_headers
    )
    assert r.status_code == 200
    conflicts = r.json()
    assert len(conflicts) == 1
    assert conflicts[0]["entity_uri"].endswith("/p1")

    apply_resp = client.post(
        f"/graphs/test-tenant/enrich/jobs/{job.id}/apply",
        headers=auth_headers,
        json={
            "decisions": [
                {
                    "entity_uri": "https://graph.infona.ai/entities/Product/p1",
                    "attribute": "manufacturer",
                    "existing_value": "Acme",
                    "proposed": verdict.model_dump(),
                    "decision": "accept",
                }
            ]
        },
    )
    assert apply_resp.status_code == 200
    assert apply_resp.json()["applied"] == 1
    # The accepted value reached the graph (ONTA-527: read the store, not an
    # update count that the post-write housekeeping would satisfy on its own).
    assert _stored_values("manufacturer") == {"Bosch"}


def test_cancel_job(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = _count_response(0)
    r = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Product",
            "attributes": ["manufacturer"],
            "kg_name": "kg",
        },
    )
    job_id = r.json()["job_id"]
    cancel = client.delete(
        f"/graphs/test-tenant/enrich/jobs/{job_id}", headers=auth_headers
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"


def _seed_job(client, auth_headers, *, status: JobStatus, job_id: str) -> None:
    """Insert a job straight into the app's (in-memory) job store so a route test
    can exercise GET behavior without driving a full run. Writes the backing dict
    directly — a TestClient runs the app in its own loop, so awaiting the store's
    async ``create`` from the test thread would fight that loop."""
    # Force the app's store to exist (the route's Depends lazily creates it).
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/__warmup__", headers=auth_headers
    )
    assert r.status_code == 404  # store now instantiated
    store = client.app.state.enrichment_job_store
    job = EnrichJob(
        id=job_id,
        tenant_id="test-tenant",
        kg_name="kg",
        type_name="Product",
        attributes=["manufacturer"],
        tier=EnrichmentTier.lite,
        status=status,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
    )
    store._jobs[job_id] = job


def test_get_job_wait_returns_immediately_when_already_terminal(client, auth_headers):
    """The ``wait`` long-poll (ONTA-238) returns AT ONCE for an already-terminal job
    — it must never block on a job that is already done."""
    _seed_job(client, auth_headers, status=JobStatus.applied, job_id="done-job")
    import time

    t0 = time.monotonic()
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/done-job?wait=10", headers=auth_headers
    )
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json()["status"] == "applied"
    # Returned effectively instantly (no blocking) despite a 10s wait budget.
    assert elapsed < 2.0


def test_get_job_wait_blocks_then_returns_running_job(client, auth_headers):
    """For a still-running job the long-poll blocks up to the (short) wait budget,
    then returns the current job — the caller re-polls to keep waiting. Proves the
    handler actually waited rather than returning instantly."""
    _seed_job(client, auth_headers, status=JobStatus.running, job_id="running-job")
    import time

    t0 = time.monotonic()
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/running-job?wait=1", headers=auth_headers
    )
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    # It blocked for roughly the wait budget (the poll interval is 0.5s, so it
    # loops at least once) but returned near the 1s budget, not the 25s ceiling.
    assert 0.4 <= elapsed < 5.0


def test_get_job_wait_zero_does_not_block(client, auth_headers):
    """wait=0 (the default) keeps the immediate, non-blocking behavior unchanged."""
    _seed_job(client, auth_headers, status=JobStatus.running, job_id="poll-job")
    import time

    t0 = time.monotonic()
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/poll-job", headers=auth_headers
    )
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Tier registry
# ---------------------------------------------------------------------------


def test_adapter_cost_metadata_protocol():
    """COG-123: the OSS cost model reads is_paid/cost_per_call generically.

    - An adapter that declares neither (legacy / OSS Wikidata) is free.
    - is_paid OR a positive cost_per_call marks an adapter paid.
    - The infona-shipped WikidataAdapter declares free explicitly.
    - A malformed cost_per_call coerces to 0.0 (never raises).
    """
    from infona_client.enrichment.sources.base import adapter_cost
    from infona_client.enrichment.sources.wikidata import WikidataAdapter

    class _Bare:  # declares nothing → free
        name = "bare"

    class _PaidFlagOnly:  # is_paid True, no cost → paid, $0
        name = "p1"
        is_paid = True
        cost_per_call = 0.0

    class _CostOnly:  # positive cost ⇒ paid even without is_paid
        name = "p2"
        cost_per_call = 0.02

    class _Malformed:
        name = "bad"
        is_paid = True
        cost_per_call = "not-a-number"

    assert adapter_cost(_Bare()) == (False, 0.0)
    assert adapter_cost(_PaidFlagOnly()) == (True, 0.0)
    assert adapter_cost(_CostOnly()) == (True, 0.02)
    # Malformed cost coerces to 0.0 but is_paid flag is honored.
    assert adapter_cost(_Malformed()) == (True, 0.0)
    # The OSS Wikidata adapter is free.
    assert adapter_cost(WikidataAdapter()) == (False, 0.0)


def test_register_tier_and_get_chain():
    from infona_client.enrichment.tiers import (
        get_chain,
        register_tier,
        reset_tiers,
    )

    reset_tiers()
    try:
        assert get_chain(EnrichmentTier.lite) == ["wikidata"]
        register_tier(EnrichmentTier.base, ["wikidata", "web"])
        assert get_chain(EnrichmentTier.base) == ["wikidata", "web"]
        # Idempotent: last write wins.
        register_tier(EnrichmentTier.base, ["wikidata"])
        assert get_chain(EnrichmentTier.base) == ["wikidata"]
        # Returned list is a copy: mutating it does not affect the registry.
        chain = get_chain(EnrichmentTier.lite)
        chain.append("mutated")
        assert get_chain(EnrichmentTier.lite) == ["wikidata"]
    finally:
        reset_tiers()


def test_executor_skips_unregistered_adapter(caplog):
    """Chain with a missing adapter name should log a warning and not fail."""
    import logging

    from infona_client.enrichment.tiers import (
        get_chain,
        register_tier,
        reset_tiers,
    )

    async def run():
        rows = [
            {
                "uri": "https://graph.infona.ai/entities/Product/p1",
                "label": "Bosch",
                "vals": "",
            },
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(
                        value="Robert Bosch GmbH",
                        confidence=0.95,
                        source="wikidata",
                    )
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        register_tier(EnrichmentTier.lite, ["wikidata", "nonexistent"])
        assert get_chain(EnrichmentTier.lite) == ["wikidata", "nonexistent"]

        job = _make_job(policy=ConflictPolicy.stage)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        # Job did not fail.
        assert final is not None
        assert final.status != JobStatus.failed
        # Wikidata produced a verdict, so the job filled the empty slot.
        assert final.progress.filled == 1

    reset_tiers()
    caplog.set_level(logging.WARNING)
    try:
        asyncio.run(run())
    finally:
        reset_tiers()


# ---------------------------------------------------------------------------
# Strategy loader
# ---------------------------------------------------------------------------


def test_load_strategy_returns_empty_when_no_triples():
    from infona_client.enrichment.strategy import load_strategy

    async def run():
        neptune = AsyncMock()
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        s = await load_strategy(neptune, "test-tenant", "LineItem")
        assert s.type_name == "LineItem"
        assert s.match_key is None
        assert s.lookup_priority is None
        assert s.attributes == {}

    asyncio.run(run())


def test_load_strategy_parses_attribute_triples():
    from infona_client.enrichment.strategy import load_strategy
    from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type

    type_uri = "https://graph.infona.ai/types/LineItem"
    mpn_uri = "https://graph.infona.ai/types/LineItem/attrs/mpn"
    brand_uri = "https://graph.infona.ai/types/LineItem/attrs/brand"
    onto = "https://graph.infona.ai/onto"

    async def run():
        await upsert_type(name="LineItem", tenant_id="test-tenant")
        await upsert_attribute(
            type_name="LineItem", attr_name="mpn", datatype="string",
            tenant_id="test-tenant",
        )
        await upsert_attribute(
            type_name="LineItem", attr_name="brand", datatype="string",
            tenant_id="test-tenant",
        )
        await seed_strategy_triples(
            [
                (type_uri, f"{onto}/matchKey", "description"),
                (type_uri, f"{onto}/lookupPriority", "1"),
                (mpn_uri, f"{onto}/enrichmentSource", "wikidata"),
                (mpn_uri, f"{onto}/enrichmentSource", "web"),
                (mpn_uri, f"{onto}/confidenceMin", "0.9"),
                (mpn_uri, f"{onto}/idPattern", "^[A-Z0-9-]{6,20}$"),
                (mpn_uri, f"{onto}/conflictPolicy", "stage"),
                (brand_uri, f"{onto}/canonicalizer", "title-case"),
                (brand_uri, f"{onto}/alias", "KN→K&N"),
                (brand_uri, f"{onto}/alias", "Mfg→Manufacturing"),
                (brand_uri, f"{onto}/alias", "bogus-no-arrow"),
            ]
        )
        neptune = AsyncMock()
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        s = await load_strategy(neptune, "test-tenant", "LineItem")
        assert s.match_key == "description"
        assert s.lookup_priority == 1
        assert "mpn" in s.attributes
        mpn = s.attributes["mpn"]
        assert mpn.sources == ["wikidata", "web"]
        assert mpn.confidence_min == 0.9
        assert mpn.id_pattern == "^[A-Z0-9-]{6,20}$"
        assert mpn.conflict_policy == "stage"
        brand = s.attributes["brand"]
        assert brand.canonicalizer == "title-case"
        assert brand.aliases == {"KN": "K&N", "Mfg": "Manufacturing"}

    asyncio.run(run())


def test_aliases_resolve_conflicts_to_verified():
    """Existing brand=KN, alias KN->K&N, verdict K&N -> verified, not conflict."""
    from infona_client.enrichment.tiers import reset_tiers

    type_uri = "https://graph.infona.ai/types/Product"
    brand_uri = "https://graph.infona.ai/types/Product/attrs/brand"
    onto = "https://graph.infona.ai/onto"
    brand_pred = brand_uri  # the predicate stored on the entity row

    async def run():
        from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type

        rows = [
            {
                "uri": "https://graph.infona.ai/entities/Product/p1",
                "label": "Filter",
                "vals": f"{brand_pred}::KN",
            },
        ]
        await upsert_type(name="Product", tenant_id="test-tenant")
        await upsert_attribute(
            type_name="Product", attr_name="brand", datatype="string",
            tenant_id="test-tenant",
        )
        await seed_strategy_triples([(brand_uri, f"{onto}/alias", "KN→K&N")])
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Filter", "brand"): [
                    Verdict(value="K&N", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        reset_tiers()
        job = _make_job(
            type_name="Product",
            attributes=["brand"],
            policy=ConflictPolicy.stage,
        )
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final is not None
        # ONTA-159: the alias resolved the would-be conflict to VERIFIED, so there
        # is no conflict left to review → the run completes APPLIED (nothing to do).
        assert final.status == JobStatus.applied
        assert final.progress.verified == 1, (
            f"expected verified, got progress={final.progress}"
        )
        assert final.progress.conflicts == 0

    reset_tiers()
    try:
        asyncio.run(run())
    finally:
        reset_tiers()


def test_canonicalize_title_case_handles_ampersand():
    from infona_client.enrichment.canonicalize import apply_canonicalizer

    assert apply_canonicalizer("title-case", "k&n filters") == "K&N Filters"
    assert apply_canonicalizer("title-case", "AT&T") == "AT&T"
    assert apply_canonicalizer("title-case", "  bosch  gmbh  ").strip() == "Bosch Gmbh"
    # Unknown canonicalizer returns value unchanged.
    assert apply_canonicalizer("nope", "anything") == "anything"
    assert apply_canonicalizer(None, "x") == "x"
    assert apply_canonicalizer("trim", "  hi  ") == "hi"


def test_enrichment_plugin_loaded_at_startup(monkeypatch):
    """Plugin's register() runs during create_app()."""
    from infona_client.api import app as app_module
    from infona_client.config import settings

    monkeypatch.setattr(
        settings, "enrichment_plugin", "tests.fake_enrichment_plugin:register"
    )
    try:
        app_module.create_app()
        from tests import fake_enrichment_plugin

        assert fake_enrichment_plugin.LOADED is True
    finally:
        from tests import fake_enrichment_plugin

        fake_enrichment_plugin.LOADED = False


def test_enrichment_plugin_invalid_format_logged(monkeypatch):
    """Malformed plugin spec is logged but does not raise."""
    from infona_client.api import app as app_module
    from infona_client.config import settings

    monkeypatch.setattr(settings, "enrichment_plugin", "no_colon_here")
    # Must not raise.
    app_module.create_app()


# ---------------------------------------------------------------------------
# Verdict provenance contract (ADR-0005 §5)
# ---------------------------------------------------------------------------


def test_verdict_backcompat_and_provenance():
    # (1) Legacy construction still works; new fields default to None.
    legacy = Verdict(value="Bosch GmbH", confidence=0.95, source="wikidata")
    assert legacy.value == "Bosch GmbH"
    assert legacy.confidence == 0.95
    assert legacy.source == "wikidata"
    assert legacy.raw_confidence is None
    assert legacy.retrieved_at is None
    assert legacy.source_published_at is None
    assert legacy.grounding_score is None
    assert legacy.extraction_method is None
    assert legacy.calibration_method is None

    # (2) A fully-populated verdict round-trips through model_dump/model_validate.
    retrieved = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    published = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    full = Verdict(
        value="Robert Bosch GmbH",
        confidence=0.91,
        source="exa",
        source_url="https://example.com/bosch",
        reasoning="matched on company registry",
        raw_confidence=0.42,
        retrieved_at=retrieved,
        source_published_at=published,
        grounding_score=0.88,
        extraction_method="llm-extract",
        calibration_method="isotonic",
    )
    dumped = full.model_dump()
    restored = Verdict.model_validate(dumped)
    assert restored == full
    assert restored.raw_confidence == 0.42
    assert restored.retrieved_at == retrieved
    assert restored.source_published_at == published
    assert restored.grounding_score == 0.88
    assert restored.extraction_method == "llm-extract"
    assert restored.calibration_method == "isotonic"


# ---------------------------------------------------------------------------
# COG-124: smart "auto" tier resolution + web confidence floor on create
# ---------------------------------------------------------------------------


class _FakePaidAdapter:
    """A registered PAID adapter so the ``core`` chain reads as paid (COG-123:
    paid is detected from declared metadata, never the adapter name)."""

    name = "fakepaid"
    is_paid = True
    cost_per_call = 0.01

    async def lookup(self, *args, **kwargs):  # pragma: no cover - never called
        return []


@pytest.fixture
def _paid_core_chain():
    """Register a paid adapter and point the ``core`` tier chain at it, so
    ``chain_has_paid(core)`` is True in this OSS test. Restores defaults after."""
    from infona_client.enrichment.sources import base as base_mod
    from infona_client.enrichment.tiers import register_tier, reset_tiers

    adapter = _FakePaidAdapter()
    base_mod.register_adapter(adapter)
    register_tier(EnrichmentTier.core, ["fakepaid"])
    try:
        yield
    finally:
        reset_tiers()
        base_mod._adapters.pop("fakepaid", None)


def test_resolve_auto_tier_no_key_falls_back_to_heuristic():
    """With no openrouter_key the resolver must NOT raise, must pick a concrete
    tier (never needs_clarification), and must set a routing_note."""
    from infona_client.enrichment.tier_router import resolve_auto_tier

    async def run():
        # Open-web fact → core, leaning paid.
        web = await resolve_auto_tier(["company", "website"], "Person", None)
        assert web.resolved_tier == "core"
        assert web.needs_clarification is False
        assert web.routing_note  # non-empty explanation

        # Structured identifier → lite (free Wikidata).
        structured = await resolve_auto_tier(["iso_code"], "Country", None)
        assert structured.resolved_tier == "lite"
        assert structured.needs_clarification is False
        assert structured.routing_note

        # Unknown attribute with no clear structured signal → lean paid (core).
        unknown = await resolve_auto_tier(["vibe"], "Person", None)
        assert unknown.resolved_tier == "core"
        assert unknown.needs_clarification is False

        # Empty key string is treated as "no key" too — still concrete, no raise.
        empty = await resolve_auto_tier(["company"], "Person", "")
        assert empty.resolved_tier in ("lite", "core")
        assert empty.needs_clarification is False

    asyncio.run(run())


def test_create_job_auto_needs_clarification_creates_no_job(
    client, auth_headers, mock_neptune, monkeypatch
):
    """When the auto resolver returns needs_clarification, create returns status
    'needs_clarification' with candidates and creates NO job (the store stays
    empty / the executor is never spawned)."""
    import infona_client.api.routes.enrich as enrich_mod
    from infona_client.enrichment.tier_router import TierDecision

    async def _ambiguous(attributes, type_name, openrouter_key, timeout_s=8.0):
        return TierDecision(
            resolved_tier=None,
            needs_clarification=True,
            candidates=["lite", "core"],
            routing_note="ambiguous — pick a tier",
        )

    monkeypatch.setattr(enrich_mod, "resolve_auto_tier", _ambiguous)

    # Guard: the executor must never be spawned on the clarification branch.
    spawned: list = []
    monkeypatch.setattr(enrich_mod, "_spawn", lambda coro: spawned.append(coro))

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Person",
            "attributes": ["company"],
            "kg_name": "kg",
            "tier": "auto",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["needs_clarification"] is True
    assert data["candidates"] == ["lite", "core"]
    assert data["job_id"] is None
    assert data["resolved_tier"] is None
    assert data["routing_note"] == "ambiguous — pick a tier"
    # NO job created, NO background work spawned.
    assert spawned == []
    listing = client.get(
        "/graphs/test-tenant/jobs?category=enrichment", headers=auth_headers
    ).json()
    assert listing == []


def test_create_job_auto_resolves_core_lowers_confidence_to_web_floor(
    client, auth_headers, mock_neptune, monkeypatch, _paid_core_chain
):
    """auto → core (paid) with the DEFAULT confidence must create a job at core
    AND lower confidence_min to the web floor (0.4) so web verdicts actually
    land instead of all being filtered out."""
    import infona_client.api.routes.enrich as enrich_mod
    from infona_client.enrichment.tier_router import TierDecision

    async def _to_core(attributes, type_name, openrouter_key, timeout_s=8.0):
        return TierDecision(
            resolved_tier="core",
            needs_clarification=False,
            routing_note="auto-routed to core",
        )

    monkeypatch.setattr(enrich_mod, "resolve_auto_tier", _to_core)
    mock_neptune.query.return_value = _count_response(0)

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Person",
            "attributes": ["company"],
            "kg_name": "kg",
            "tier": "auto",
            # confidence_min omitted → default 0.85 sentinel → floor applies.
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "queued"
    assert data["resolved_tier"] == "core"
    assert data["needs_clarification"] is False
    assert data["candidates"] is None
    assert data["routing_note"] == "auto-routed to core"

    job = client.get(
        f"/graphs/test-tenant/enrich/jobs/{data['job_id']}", headers=auth_headers
    ).json()
    assert job["tier"] == "core"
    # The web confidence floor was applied (0.85 default → 0.4).
    assert job["confidence_min"] == 0.4


def test_create_job_explicit_core_lowers_confidence_to_web_floor(
    client, auth_headers, mock_neptune, _paid_core_chain
):
    """An EXPLICIT (non-auto) core tier with the default confidence must ALSO get
    the web floor — previously only the agent path did this, so a direct core
    enrich filtered out every web verdict → 0 fills."""
    mock_neptune.query.return_value = _count_response(0)

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Person",
            "attributes": ["company"],
            "kg_name": "kg",
            "tier": "core",
        },
    )
    assert response.status_code == 202
    data = response.json()
    # Uniform contract for explicit tiers: resolved_tier echoes the request tier.
    assert data["resolved_tier"] == "core"
    assert data["routing_note"] is None
    assert data["needs_clarification"] is False
    assert data["candidates"] is None

    job = client.get(
        f"/graphs/test-tenant/enrich/jobs/{data['job_id']}", headers=auth_headers
    ).json()
    assert job["tier"] == "core"
    assert job["confidence_min"] == 0.4


def test_create_job_explicit_core_respects_user_confidence(
    client, auth_headers, mock_neptune, _paid_core_chain
):
    """A user-supplied (non-default) confidence_min must be respected — the floor
    only overrides the UNSET 0.85 sentinel, never an explicit value."""
    mock_neptune.query.return_value = _count_response(0)

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Person",
            "attributes": ["company"],
            "kg_name": "kg",
            "tier": "core",
            "confidence_min": 0.7,
        },
    )
    assert response.status_code == 202
    job = client.get(
        f"/graphs/test-tenant/enrich/jobs/{response.json()['job_id']}",
        headers=auth_headers,
    ).json()
    # User value preserved, NOT lowered to the floor.
    assert job["confidence_min"] == 0.7


def test_create_job_lite_does_not_lower_confidence(
    client, auth_headers, mock_neptune
):
    """A free tier (lite — no paid adapter) must NOT trigger the web floor; the
    default 0.85 confidence is preserved."""
    mock_neptune.query.return_value = _count_response(0)

    response = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Country",
            "attributes": ["iso_code"],
            "kg_name": "kg",
            "tier": "lite",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["resolved_tier"] == "lite"
    job = client.get(
        f"/graphs/test-tenant/enrich/jobs/{data['job_id']}", headers=auth_headers
    ).json()
    assert job["tier"] == "lite"
    assert job["confidence_min"] == 0.85


# ---------------------------------------------------------------------------
# P1: enriched INSTANCE values are stored as TYPED literals matching the
# declared range (not bare xsd:string). Bug: declared range said
# xsd:integer/dateTime but the stored literal was xsd:string, so typed NL
# filters silently returned empty. Fix routes each value through ingestion's
# validate_triple so the stored literal matches the declared datatype.
# ---------------------------------------------------------------------------

#: Shared xfail reason for the three typed-literal cases below.
_TYPED_LITERAL_LEAK = (
    "PRODUCT BUG (pre-dates ONTA-527, surfaced by it): on the property-graph "
    "path a typed literal's datatype annotation is stored INSIDE the value. "
    "resolver/validator.py::_typed_value emits the writer convention "
    "`<lexical>^^<xsd-uri>`, which graph/queries.py::_format_object used to split "
    "into a real typed SPARQL literal on the way out. graph/facts.py::"
    "classify_triple does no such split — it passes the object string straight "
    "through — so the Entity property cache AND Assertion.literal_value both hold "
    "'92^^http://www.w3.org/2001/XMLSchema#integer', while "
    "graph/rdf_model.py::assert_fact hardcodes literal_datatype=None. The "
    "attribute is DECLARED integer/dateTime and the stored value is a string with "
    "a URI glued on, so numeric/date comparison matches nothing — the exact "
    "declared-range-vs-stored-literal skew the typed write was added to remove. "
    "Not enrichment-specific: every writer that goes through classify_triple "
    "(ingest included) leaks the same way."
)


def test_executor_apply_writes_typed_integer_literal(monkeypatch):
    """A numeric enriched value is stored as a TYPED literal
    ``"92"^^<…#integer>`` matching the declared integer range — NOT a bare
    ``"92"`` xsd:string literal the typed NL filters would miss (the P1 bug)."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Makita", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "humanness_score"): [
                    Verdict(value="92", confidence=0.95, source="wikidata")
                ],
                ("Makita", "humanness_score"): [
                    Verdict(value="87", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["humanness_score"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        # Declared integer...
        declared = await _declared_attrs("Product")
        assert declared["humanness_score"].datatype == "integer"
        # ...and the STORED values are the numbers themselves. Where the datatype
        # is recorded is a design choice (Assertion.literal_datatype); what must
        # never happen is the datatype URI ending up inside the value.
        assert {str(v) for v in _stored_values("humanness_score")} == {"92", "87"}

    asyncio.run(run())


def test_executor_apply_writes_comma_number_as_string_not_dropped(monkeypatch):
    """A comma-grouped number ("1,234") is declared ``string`` and survives as a
    plain string literal — NOT declared integer and then dropped by the validator
    (the comma data-loss regression the re-review flagged). Inference and the
    write-side validator agree: commas are not numeric."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Makita", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "unit_sales"): [
                    Verdict(value="1,234", confidence=0.95, source="wikidata")
                ],
                ("Makita", "unit_sales"): [
                    Verdict(value="12,345", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["unit_sales"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        # The comma values are stored verbatim as plain strings — present, not
        # dropped by a validator that had been told to expect a number.
        assert _stored_values("unit_sales") == {"1,234", "12,345"}
        # Declared as string, so NOT typed integer.
        assert (await _declared_attrs("Product"))["unit_sales"].datatype == "string"

    asyncio.run(run())


def test_executor_apply_writes_typed_datetime_literal(monkeypatch):
    """A date enriched value is stored as a date matching the declared dateTime
    range — the ISO instant itself, not the instant with its datatype URI glued
    onto it."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "founded"): [
                    Verdict(value="2026-06-28", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["founded"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        assert (await _declared_attrs("Product"))["founded"].datatype == "datetime"
        (stored,) = _stored_values("founded")
        # Normalized to a full ISO-8601 instant by validate_triple's _typed_value,
        # and parseable as one — no datatype URI inside the value.
        assert "^^" not in str(stored), stored
        assert datetime.fromisoformat(str(stored)).year == 2026

    asyncio.run(run())


def test_executor_apply_writes_entity_iri_object(monkeypatch):
    """An entity-IRI enriched value is written as a relationship EDGE to that
    entity, never as a literal, and is declared with a relationship range."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Drill", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Drill", "manufacturer"): [
                    Verdict(
                        value="https://graph.infona.ai/entities/Manufacturer/bosch",
                        confidence=0.95,
                        source="wikidata",
                    )
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        # An edge to the target entity, not a literal on the subject.
        edges = _rels_for("manufacturer")
        assert [(r["start_id"], r["end_id"]) for r in edges] == [
            (
                "https://graph.infona.ai/entities/Product/p1",
                "https://graph.infona.ai/entities/Manufacturer/bosch",
            )
        ]
        assert "manufacturer" not in _props(
            "https://graph.infona.ai/entities/Product/p1"
        )

        # Declared as a relationship range, not a string literal.
        declared = await _declared_attrs("Product")
        assert declared["manufacturer"].kind == "relationship"
        assert declared["manufacturer"].range_type == "Manufacturer"

    asyncio.run(run())


def test_executor_apply_skips_value_not_conforming_to_existing_range(monkeypatch):
    """An attribute already declared with an integer range: a non-conforming
    value ("five stars") is REJECTED (nothing written for it) while a conforming
    numeric value IS written. The P1 guarantee: we never PIN a mismatched value
    under a declared richer range.

    Only the REJECTION is asserted here; whether the conforming value keeps its
    datatype out of the value string is
    ``test_executor_apply_writes_typed_integer_literal``'s xfail, so this case
    compares the value's LEXICAL form and stays green either way.
    """
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bad", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Good", "vals": ""},
        ]
        await _seed_existing_range(
            "Product", "rating", "http://www.w3.org/2001/XMLSchema#integer"
        )
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                # Non-conforming → must be rejected (no triple).
                ("Bad", "rating"): [
                    Verdict(value="five stars", confidence=0.95, source="wikidata")
                ],
                # Conforming → must be written typed.
                ("Good", "rating"): [
                    Verdict(value="5", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["rating"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        # The conforming value IS written (lexically "5" — see the docstring).
        assert {str(v).split("^^")[0] for v in _stored_values("rating")} == {"5"}
        assert "rating" in _props("https://graph.infona.ai/entities/Product/p2")
        # The rejected row produces NO rating write at all — not the raw value,
        # not a coerced one, and no citation claiming a source for it either.
        assert "rating" not in _props("https://graph.infona.ai/entities/Product/p1")
        assert _citation("https://graph.infona.ai/entities/Product/p1", "rating") is None

    asyncio.run(run())


def test_executor_apply_provenance_stays_plain_string(monkeypatch):
    """The provenance companions (source_url / provenance) stay PLAIN strings even
    when the primary value is typed — they are user-facing citations, never typed
    as anything richer — and remain metadata OF the attribute rather than
    attributes of their own (ONTA-262).

    Ported by ONTA-527: the companions fold onto an ``:AttrCitation`` record on
    the store path (``graph/pg_ops.py``) instead of riding attr_meta triples, so
    "plain string" is checked on the stored fields.
    """
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata(
            {
                ("Bosch", "humanness_score"): [
                    Verdict(
                        value="92",
                        confidence=0.95,
                        source="wikidata",
                        source_url="https://www.wikidata.org/wiki/Q234021",
                    )
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["humanness_score"], policy=ConflictPolicy.overwrite)
        await store.create(job)
        await executor.run(job, "test-tenant")

        entity = "https://graph.infona.ai/entities/Product/p1"
        citation = _citation(entity, "humanness_score")
        assert citation is not None
        # The citation fields are plain text — no XSD type annotation on either.
        assert citation["source_url"] == "https://www.wikidata.org/wiki/Q234021"
        assert citation["provenance"] == "wikidata"
        assert "^^" not in citation["provenance"]
        assert "^^" not in citation["source_url"]
        # The per-fact freshness stamp is a real instant (see test_freshness_gating
        # for the ordering property the xsd:dateTime annotation used to carry).
        assert datetime.fromisoformat(citation["verified_at"]).tzinfo is not None

        # The primary value is still there (lexically "92" — the datatype-in-value
        # leak is pinned by test_executor_apply_writes_typed_integer_literal).
        assert {
            str(v).split("^^")[0] for v in _stored_values("humanness_score")
        } == {"92"}

        # None of the three companions became an attribute of the entity or of
        # the ontology (ONTA-262).
        props = _props(entity)
        for suffix in ("source_url", "provenance", "verified_at"):
            assert suffix not in props
            assert f"humanness_score_{suffix}" not in props
        assert set(await _declared_attrs("Product")) == {"humanness_score"}

    asyncio.run(run())


def test_provenance_triples_stamps_per_fact_verified_at():
    """Every enriched fact carries a per-fact ``<attr>_verified_at`` freshness
    stamp (ONTA-241): a TYPED ``xsd:dateTime`` literal on the QUERYABLE
    ``attr_meta/`` metadata namespace (NOT the hidden ``onto/`` system-marker
    namespace, and — ONTA-262 — not the ``attrs/`` attribute namespace either),
    emitted unconditionally alongside the ``source_url`` companion — so the query layer can
    filter "verified in the last N days" per attribute.

    The STORED object must be a typed dateTime, not a bare string: the column is
    declared ``xsd:dateTime`` and the NL planner emits typed comparisons
    (``FILTER(?x >= "…"^^xsd:dateTime)``); an untyped string would be
    type-incompatible in SPARQL and the row would be SILENTLY DROPPED."""
    from datetime import datetime as _dt
    from infona_client.graph.queries import _escape_value

    XSD_DT = "http://www.w3.org/2001/XMLSchema#dateTime"

    verdict = Verdict(
        value="92",
        confidence=0.95,
        source="wikidata",
        source_url="https://www.wikidata.org/wiki/Q234021",
    )
    triples = EnrichmentExecutor._provenance_triples(
        "https://graph.infona.ai/entities/Product/p1", "Product", "humanness_score", verdict
    )
    verified = [
        (s, p, o) for (s, p, o) in triples if p.endswith("/verified_at")
    ]
    # Exactly one freshness stamp, and it is always emitted (no source_url needed).
    assert len(verified) == 1
    _s, pred, val = verified[0]
    # Queryable literal on the attr_meta metadata namespace — never the hidden
    # onto/ marker namespace, never the attrs/ attribute namespace (ONTA-262).
    assert pred == "https://graph.infona.ai/attr_meta/Product/humanness_score/verified_at"
    assert "/onto/" not in pred and "/attrs/" not in pred
    # The STORED object carries the xsd:dateTime type annotation (the `^^`
    # convention), so Neptune compares it as a dateTime — not an untyped string.
    assert val.endswith(f"^^{XSD_DT}"), f"verified_at must be a typed dateTime: {val!r}"
    # And it renders to a proper typed SPARQL literal, "…"^^<…#dateTime>.
    rendered = _escape_value(val)
    assert rendered.endswith(f'^^<{XSD_DT}>'), rendered
    assert rendered.startswith('"'), rendered
    # The lexical part is still a real, tz-aware ISO-8601 datetime (parseable).
    lexical = val.rsplit("^^", 1)[0]
    assert _dt.fromisoformat(lexical).tzinfo is not None
    # Inference sees through the type annotation and still declares xsd:dateTime
    # (so the DECLARED range matches the STORED literal — no string/dateTime skew).
    assert _infer_datatype_from_values([val]) == "datetime"
    # Emitted even when the verdict has no source_url (freshness is unconditional),
    # and still typed.
    bare = Verdict(value="7", confidence=0.9, source="wikidata")
    bare_triples = EnrichmentExecutor._provenance_triples(
        "https://graph.infona.ai/entities/Product/p2", "Product", "rating", bare
    )
    bare_verified = [o for (_s, p, o) in bare_triples if p.endswith("/rating/verified_at")]
    assert bare_verified and bare_verified[0].endswith(f"^^{XSD_DT}")


def test_apply_decisions_writes_typed_integer_literal(monkeypatch):
    """The review-apply path (apply_decisions) also types the accepted value: a
    numeric accepted value is stored as the number, matching the declared range —
    same P1 fix as the auto-apply run() path."""
    import infona_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        neptune = AsyncMock()
        neptune.update.return_value = None
        # No existing range → inference types it integer from the value.
        neptune.query.side_effect = AssertionError("enrich must not SPARQL")
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)
        job = _make_job(attributes=["humanness_score"], policy=ConflictPolicy.stage)
        await store.create(job)

        decisions = [
            ConflictReview(
                entity_uri="https://graph.infona.ai/entities/Product/p1",
                attribute="humanness_score",
                existing_value="",
                proposed=Verdict(value="92", confidence=0.95, source="wikidata"),
                decision="accept",
            ),
        ]
        applied = await executor.apply_decisions(job.id, decisions)
        assert applied == 1

        assert (await _declared_attrs("Product"))["humanness_score"].datatype == "integer"
        assert {str(v) for v in _stored_values("humanness_score")} == {"92"}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# P1: numeric inference is tightened so a string column isn't mis-declared
# numeric. _is_int / _is_float reject underscores and non-finite tokens.
# ---------------------------------------------------------------------------


def test_is_int_rejects_underscores_and_commas_keeps_plain_ints():
    """``_is_int`` rejects underscore AND comma groupings, while plain ints still
    parse True. Commas are rejected so the inference layer agrees with the
    write-side validator (which does NOT strip commas): otherwise a column like
    ``"1,234"`` would be declared integer here and then DROPPED at write time."""
    assert _is_int("92") is True
    assert _is_int("-3") is True
    assert _is_int("1,000") is False  # comma rejected (matches the validator)
    assert _is_int("1_000") is False
    assert _is_int("five") is False


def test_is_float_rejects_non_finite_commas_and_underscores():
    """``_is_float`` rejects the non-finite special tokens float() accepts
    (inf/-inf/infinity/nan), comma groupings, and underscore groupings, while real
    decimals and real scientific notation still parse True. Commas are rejected to
    agree with the validator (which would drop a comma value declared float)."""
    assert _is_float("8.5") is True
    assert _is_float("1e10") is True  # real scientific notation
    assert _is_float("-2.0") is True
    assert _is_float("inf") is False
    assert _is_float("-inf") is False
    assert _is_float("infinity") is False
    assert _is_float("nan") is False
    assert _is_float("1_000.5") is False
    assert _is_float("1,000.5") is False  # comma rejected (matches the validator)


def test_infer_datatype_does_not_mis_declare_special_tokens():
    """A column of ``inf``/``nan``/underscore/comma strings is NOT mis-declared
    float/int — it falls through to ``string`` (the tightened helpers feed
    inference), so the values survive at write time as plain string literals
    instead of being declared numeric and then dropped by the validator."""
    assert _infer_datatype_from_values(["inf", "nan"]) == "string"
    assert _infer_datatype_from_values(["1_000"]) == "string"
    assert _infer_datatype_from_values(["1,234", "12,345"]) == "string"


# ---------------------------------------------------------------------------
# Provider logs + error summary (run-detail observability)
# ---------------------------------------------------------------------------


def test_provider_tally_rollup_status_and_errors():
    """The tally rolls each provider's per-attempt outcomes into a coarse status
    (ok / no_match / error / skipped) and aggregates failures into an ordered
    error summary with representative messages + counts."""
    tally = _ProviderTally()
    # wikidata: one live match, one cache no_match → produced a usable result → ok
    tally.record_attempt("wikidata", cache_hit=False, outcome="match")
    tally.record_attempt("wikidata", cache_hit=True, outcome="no_match")
    # exa: every attempt failed (a timeout + two errors) → error
    tally.record_attempt("exa", cache_hit=False, outcome="timeout", error_msg="timed out after 30s")
    tally.record_attempt("exa", cache_hit=False, outcome="error", error_msg="HTTP 503 a")
    tally.record_attempt("exa", cache_hit=False, outcome="error", error_msg="HTTP 503 b")
    # gemini: ran but found nothing → no_match
    tally.record_attempt("gemini", cache_hit=False, outcome="no_match")
    # perplexity: named in a chain but not registered here → skipped
    tally.record_missing("perplexity")

    logs = {p.provider: p for p in tally.to_logs()}
    assert logs["wikidata"].status == "ok"
    assert logs["wikidata"].matches == 1
    assert logs["wikidata"].attempts == 1  # cache hit not counted as an attempt
    assert logs["wikidata"].cache_hits == 1
    assert logs["exa"].status == "error"
    assert logs["exa"].errors == 2
    assert logs["exa"].timeouts == 1
    assert logs["exa"].last_error  # carries a representative message
    assert logs["gemini"].status == "no_match"
    assert logs["perplexity"].status == "skipped"

    errs = tally.to_error_summary()
    # exa errors are aggregated with count, ordered most-frequent first.
    by_kind = {(e.provider, e.kind): e for e in errs}
    assert by_kind[("exa", "error")].count == 2
    assert by_kind[("exa", "timeout")].count == 1
    assert by_kind[("perplexity", "missing")].count == 1
    assert errs[0].count >= errs[-1].count  # sorted by count desc


def test_executor_records_provider_logs_end_to_end():
    """A completed enrichment run carries a per-provider log: which provider was
    used, how many lookups matched vs found nothing — surfaced in run detail."""
    async def run():
        rows = [
            {"uri": "https://graph.infona.ai/entities/Product/p1", "label": "Bosch", "vals": ""},
            {"uri": "https://graph.infona.ai/entities/Product/p2", "label": "Unknown Co", "vals": ""},
        ]
        neptune = await _prep_neptune(rows)

        store = InMemoryJobStore()
        cache = EnrichmentCache()
        # "Bosch" resolves; "Unknown Co" returns nothing (no_match).
        wikidata = FakeWikidata(
            {
                ("Bosch", "manufacturer"): [
                    Verdict(value="Robert Bosch GmbH", confidence=0.95, source="wikidata")
                ],
            }
        )
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["manufacturer"], policy=ConflictPolicy.stage)
        await store.create(job)
        await executor.run(job, "test-tenant")

        final = await store.get(job.id)
        assert final is not None
        assert len(final.provider_logs) == 1
        plog = final.provider_logs[0]
        assert plog.provider == "wikidata"
        assert plog.status == "ok"
        assert plog.attempts == 2
        assert plog.matches == 1
        assert plog.no_match == 1
        # A clean run records no errors.
        assert final.error_summary == []

    asyncio.run(run())


def test_lookup_chain_records_missing_provider():
    """A chain that names an unregistered provider records it as a 'skipped'
    provider log + a 'missing' error-summary entry, without failing the run."""
    async def run():
        neptune = AsyncMock()
        store = InMemoryJobStore()
        cache = EnrichmentCache()
        wikidata = FakeWikidata({})
        executor = EnrichmentExecutor(neptune, store, cache, wikidata)

        job = _make_job(attributes=["manufacturer"])
        tally = _ProviderTally()
        verdicts = await executor._lookup_chain(
            "Bosch",
            "manufacturer",
            ["ghost-provider"],  # not registered
            job,
            set(),
            0.85,
            tally=tally,
        )
        assert verdicts == []
        logs = tally.to_logs()
        assert len(logs) == 1
        assert logs[0].provider == "ghost-provider"
        assert logs[0].status == "skipped"
        errs = tally.to_error_summary()
        assert errs and errs[0].kind == "missing"

    asyncio.run(run())
