"""Tests for the A1 Source Bundle artifact (ONTA-346).

Two layers:

1. Unit tests over the frozen :class:`SourceBundle` / :func:`build_source_bundle`
   — the secret_refs-ONLY invariant, tier validation, and deterministic fact-id
   lineage.
2. A discovery-run test that drives ``web_ingest_cap.execute()`` fully offline
   (a canned :class:`_FakeProvider`, a deterministic monkeypatched
   ``SchemaResolver.ingest``, no LLM / network) and asserts the run materializes
   a :class:`SourceBundle` at the Find→Extract boundary carrying the right
   workspace/run identity, per-row fact-id lineage, per-row ``source_url``
   citations, the source TIER, and ZERO resolved credentials (secret_refs only).

The load-bearing control: a run backed by a registry Tier -1 (source-of-truth)
provider shows ``tier == "authoritative"`` and carries the provider's LOGICAL
``secret_ref`` — with its decoy plaintext credential nowhere in the bundle —
while a plain web run shows ``tier == "web"`` and carries no secret_refs.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from unittest.mock import MagicMock

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability
from cograph_client.agent.registry import AgentContext
from cograph_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id
from cograph_client.pipeline.source_bundle import (
    KNOWN_TIERS,
    SOURCE_BUNDLE_STAGE,
    TIER_AUTHORITATIVE,
    TIER_WEB,
    SourceBundle,
    SourceRow,
    _row_local_key,
    build_source_bundle,
    is_secret_ref,
)
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.web_sources import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)

# Query subject + confirmed spec, mirroring the test_web_ingest_cap harness so the
# plan()/execute() path runs the SAME way (no LLM: the spec is injected via
# plan()'s ``parsed`` hook, the ontology/extraction preview is stubbed).
CONFIRMED_SPEC = {
    "entity_type": "OpenRouterModel",
    "key_attribute": "name",
    "query": "OpenRouter models",
    "confirmed_attributes": ["context_length"],
    "suggested_attributes": ["provider", "context_length"],
}
FULL_ROWS = [
    {"name": "anthropic/claude-opus-4-8", "context_length": "200000"},
    {"name": "openai/gpt-5", "context_length": "400000"},
    {"name": "google/gemini-2.5-flash", "context_length": "1000000"},
    {"name": "meta/llama-4", "context_length": "128000"},
]

# The plaintext credential a registry source resolves at FETCH time. The bundle
# must NEVER carry it — only the logical ``secret_ref`` name. A distinctive
# sentinel so a leak is unmistakable.
PLAINTEXT_CREDENTIAL = "sk-live-PLAINTEXT-SHOULD-NEVER-LEAK-9f8e7d"


class _FakeProvider:
    """Canned web-source provider (projects rows to hint_columns, emits per-row
    provenance) — the offline discovery harness. ``is_source_of_truth`` /
    ``secret_ref`` let a single instance masquerade as a registry Tier -1 source
    so the tier + secret_ref threading is exercised end-to-end."""

    def __init__(
        self,
        *,
        name: str = "web_fake",
        is_paid: bool = False,
        cost_per_call: float = 0.0,
        rows=None,
        provenance: bool = True,
        is_source_of_truth: bool = False,
        secret_ref: str = "",
    ) -> None:
        self.name = name
        self.is_paid = is_paid
        self.cost_per_call = cost_per_call
        self._rows = FULL_ROWS if rows is None else rows
        self._provenance = provenance
        # Registry-source masquerade flags (read defensively by the capability).
        self.is_source_of_truth = is_source_of_truth
        self.secret_ref = secret_ref
        # A DECOY resolved credential the bundle builder must never read. If the
        # builder ever reached past the logical ref into a resolved secret, this
        # string would surface in the artifact — the "no plaintext" guardrail.
        self.resolved_api_key = PLAINTEXT_CREDENTIAL if secret_ref else ""
        self.calls: list[tuple] = []

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        self.calls.append((query, sample, max_rows, tuple(hint_columns or ())))
        rows = self._rows[: (5 if sample else max_rows)]
        if hint_columns:
            rows = [{c: r.get(c, "unknown") for c in hint_columns} for r in rows]
        prov: dict[str, str] = {}
        if self._provenance:
            prov = {
                r.get("name", str(i)): f"https://src.example/page-{i}"
                for i, r in enumerate(rows)
            }
        return DiscoverResult(
            rows=rows,
            provenance=prov,
            sources=["https://openrouter.ai/models"],
            estimated_total=len(self._rows),
            is_partial=sample,
        )


def _single_type_entities():
    return [
        ExtractedEntity(
            type_name="OpenRouterModel",
            id=r["name"],
            attributes=[
                ExtractedAttribute(name="context_length", value=r["context_length"])
            ],
        )
        for r in FULL_ROWS[:5]
    ]


def _patch_preview(monkeypatch):
    async def fake_fetch_ontology(self, graph_uri):
        return {}, {}

    async def fake_extract(self, content, content_type, existing=None):
        return ExtractionResult(entities=_single_type_entities(), relationships=[])

    monkeypatch.setattr(SchemaResolver, "_fetch_ontology", fake_fetch_ontology)
    monkeypatch.setattr(SchemaResolver, "_extract", fake_extract)


def _ctx(sink) -> AgentContext:
    """Agent context with a source-bundle observer sink wired on extras."""
    return AgentContext(
        tenant_id="demo-tenant",
        kg_name="models",
        neptune=MagicMock(),
        anthropic_key="sk-ant-test",
        openrouter_key="",
        extras={"prior_clarify_count": 0, "source_bundle_sink": sink},
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_web_sources()
    yield
    reset_web_sources()


async def _run_discovery(monkeypatch, provider) -> list[SourceBundle]:
    """Drive plan()+execute() for one provider and return the emitted bundles."""
    register_web_source(provider)
    _patch_preview(monkeypatch)

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    bundles: list[SourceBundle] = []
    ctx = _ctx(bundles)
    cap = WebIngestCapability()
    step = (await cap.plan(ctx, "find a list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    assert step.action == "discover_ingest"
    await cap.execute(ctx, step)
    await spawned["task"]
    return bundles


# --------------------------------------------------------------------------- #
# discovery-run: the artifact is materialized at the Find→Extract boundary
# --------------------------------------------------------------------------- #
async def test_discovery_run_materializes_authoritative_source_bundle(monkeypatch):
    """A discovery run backed by a registry Tier -1 (source-of-truth) provider
    produces a SourceBundle with the right workspace/run identity, per-row fact-id
    lineage, per-row source_url citations, tier=authoritative, and the provider's
    LOGICAL secret_ref — with ZERO resolved credentials."""
    provider = _FakeProvider(
        name="acme_api",
        is_paid=True,
        cost_per_call=0.05,
        is_source_of_truth=True,
        secret_ref="acme_secret",
        provenance=True,
    )
    bundles = await _run_discovery(monkeypatch, provider)

    # Exactly one (provider × sub-query) batch → one bundle.
    assert len(bundles) == 1
    bundle = bundles[0]
    assert isinstance(bundle, SourceBundle)

    # Workspace/run identity comes from the envelope (ADR 0011).
    assert bundle.envelope.workspace_id == "demo-tenant"
    assert bundle.workspace_id == "demo-tenant"
    assert bundle.run_id  # a non-empty run id, threaded from the run

    # Root envelope fact_id derived from run_id + the bundle key (A1 is a root
    # artifact — no parent lineage).
    expected_root = derive_fact_id(
        run_id=bundle.run_id,
        stage=SOURCE_BUNDLE_STAGE,
        parent_fact_ids=(),
        local_key="acme_api:OpenRouter models",
    )
    assert bundle.envelope.fact_id == expected_root
    assert bundle.envelope.parent_fact_ids == ()

    # One row per discovered record, each with its OWN fact_id — a child of the
    # bundle root, deterministically derived (per-row lineage).
    assert len(bundle.rows) == len(FULL_ROWS)
    assert len(set(bundle.fact_ids)) == len(FULL_ROWS)  # all distinct
    for i, row in enumerate(bundle.rows):
        assert row.fact_id != bundle.envelope.fact_id  # child, not the root
        expected_child = derive_fact_id(
            run_id=bundle.run_id,
            stage=SOURCE_BUNDLE_STAGE,
            parent_fact_ids=(bundle.envelope.fact_id,),
            local_key=_row_local_key(row.data, i, "name", "source_url", "acme_api:OpenRouter models"),
        )
        assert row.fact_id == expected_child
        # Per-record source_url citation (bound pre-dedupe, keyed by name).
        assert row.source_url == f"https://src.example/page-{i}"
        assert "name" in row.data and "context_length" in row.data

    # Source TIER: a registry source-of-truth run is authoritative (Tier -1).
    assert bundle.tiers == frozenset({TIER_AUTHORITATIVE})
    assert all(r.tier == TIER_AUTHORITATIVE for r in bundle.rows)

    # secret_refs ONLY: the LOGICAL reference is present; the resolved plaintext
    # credential is NOWHERE in the artifact.
    assert bundle.secret_refs == ("acme_secret",)
    assert is_secret_ref("acme_secret")
    serialized = json.dumps(bundle.to_dict())
    assert PLAINTEXT_CREDENTIAL not in serialized
    assert all(PLAINTEXT_CREDENTIAL not in ref for ref in bundle.secret_refs)


async def test_discovery_run_web_source_bundle_is_web_tier_no_secret(monkeypatch):
    """CONTROL: the SAME run shape backed by a plain WEB provider yields
    tier=web and carries NO secret_refs — the load-bearing contrast to the
    authoritative case above."""
    provider = _FakeProvider(name="web_fake", provenance=True)  # no secret, not SoT
    bundles = await _run_discovery(monkeypatch, provider)

    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.envelope.workspace_id == "demo-tenant"
    assert len(bundle.rows) == len(FULL_ROWS)
    assert bundle.tiers == frozenset({TIER_WEB})
    assert all(r.tier == TIER_WEB for r in bundle.rows)
    assert all(r.provider == "web_fake" for r in bundle.rows)
    # A web/free source references no secret.
    assert bundle.secret_refs == ()


async def test_discovery_run_write_behavior_unchanged(monkeypatch):
    """The bundle is a PRE-write artifact: assembling it does not change what the
    write receives. The rows committed through resolver.ingest are byte-identical
    to the bundle's row data (same records, same source_url citations)."""
    provider = _FakeProvider(name="web_fake", provenance=True)
    register_web_source(provider)
    _patch_preview(monkeypatch)

    committed: list[dict] = []

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        committed.extend(rows)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    bundles: list[SourceBundle] = []
    ctx = _ctx(bundles)
    cap = WebIngestCapability()
    step = (await cap.plan(ctx, "find a list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    await cap.execute(ctx, step)
    await spawned["task"]

    assert len(bundles) == 1
    bundle = bundles[0]
    # Every committed record matches a bundle row (keyed by name), same source_url.
    committed_by_name = {r["name"]: r for r in committed}
    bundle_by_name = {r.data["name"]: r for r in bundle.rows}
    assert set(committed_by_name) == set(bundle_by_name)
    for name, crow in committed_by_name.items():
        brow = bundle_by_name[name]
        assert crow.get("source_url") == brow.source_url
        assert crow.get("context_length") == brow.data.get("context_length")


async def test_no_sink_is_a_no_op(monkeypatch):
    """Absent a sink on the context, the run still completes cleanly (the artifact
    is materialized but simply not observed)."""
    provider = _FakeProvider(name="web_fake")
    register_web_source(provider)
    _patch_preview(monkeypatch)

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    ctx = AgentContext(
        tenant_id="demo-tenant", kg_name="models", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="",
        extras={"prior_clarify_count": 0},  # no source_bundle_sink
    )
    cap = WebIngestCapability()
    step = (await cap.plan(ctx, "find a list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    ack = await cap.execute(ctx, step)
    await spawned["task"]
    assert ack["kind"] == "ack"


# --------------------------------------------------------------------------- #
# unit: builder lineage + the secret_refs-only / tier invariants
# --------------------------------------------------------------------------- #
def test_builder_lineage_and_shape():
    rows = [
        {"name": "A", "source_url": "https://x.test/a"},
        {"name": "B", "source_url": "https://x.test/b"},
    ]
    bundle = build_source_bundle(
        rows,
        workspace_id="ws-1",
        run_id="run-1",
        provider="acme_api",
        tier=TIER_AUTHORITATIVE,
        secret_refs=["acme_secret"],
        key_attribute="name",
        bundle_key="acme_api:q",
    )
    assert isinstance(bundle.envelope, ArtifactEnvelope)
    assert bundle.workspace_id == "ws-1"
    assert bundle.run_id == "run-1"
    assert bundle.envelope.parent_fact_ids == ()
    assert len(bundle.rows) == 2
    assert bundle.rows[0].source_url == "https://x.test/a"
    assert all(r.tier == TIER_AUTHORITATIVE for r in bundle.rows)
    # Children of the root, distinct.
    assert len(set(bundle.fact_ids)) == 2
    assert all(fid != bundle.envelope.fact_id for fid in bundle.fact_ids)
    # Row data is a snapshot COPY — mutating the input row does not touch the bundle.
    rows[0]["name"] = "MUTATED"
    assert bundle.rows[0].data["name"] == "A"


def test_builder_is_deterministic():
    """Replaying the same run mints the same fact_ids (provenance-derived ids)."""
    rows = [{"name": "A"}, {"name": "B"}]
    kw = dict(
        workspace_id="ws", run_id="run", provider="p",
        tier=TIER_WEB, key_attribute="name", bundle_key="p:q",
    )
    b1 = build_source_bundle(rows, **kw)
    b2 = build_source_bundle(rows, **kw)
    assert b1.envelope.fact_id == b2.envelope.fact_id
    assert b1.fact_ids == b2.fact_ids


def test_secret_refs_only_rejects_resolved_credential():
    """The constructor rejects anything that isn't a well-formed logical secret
    reference — a resolved/decrypted credential can never be smuggled through."""
    env = ArtifactEnvelope(
        workspace_id="ws", run_id="run",
        fact_id=derive_fact_id(run_id="run", stage="A1", local_key="p"),
    )
    for bad in [
        PLAINTEXT_CREDENTIAL,            # sk-… prefix + '-' → not a ref
        "AKIAIOSFODNN7EXAMPLE",          # uppercase key material
        "Zm9vYmFy=",                     # base64-ish (has '=')
        "a b c",                         # whitespace
        "secret/with/slash",             # path chars
        "x" * 65,                        # too long
    ]:
        assert not is_secret_ref(bad)
        with pytest.raises(ValueError):
            SourceBundle(envelope=env, rows=(), secret_refs=(bad,))
    # A genuine logical reference is accepted.
    ok = SourceBundle(envelope=env, rows=(), secret_refs=("acme_secret", "acme_secret"))
    assert ok.secret_refs == ("acme_secret",)  # order-preserving dedupe


def test_tier_validation():
    env = ArtifactEnvelope(
        workspace_id="ws", run_id="run",
        fact_id=derive_fact_id(run_id="run", stage="A1", local_key="p"),
    )
    assert TIER_AUTHORITATIVE in KNOWN_TIERS and TIER_WEB in KNOWN_TIERS
    with pytest.raises(ValueError):
        SourceRow(fact_id="f1", data={}, source_url=None, tier="bogus", provider="p")
    # A valid row through the bundle is fine.
    row = SourceRow(fact_id="f1", data={"name": "A"}, source_url=None, tier=TIER_WEB, provider="p")
    SourceBundle(envelope=env, rows=(row,))


def test_authoritative_vs_web_contrast():
    """Load-bearing control at the builder level: an authoritative bundle carries
    a secret_ref (no plaintext) while a web bundle carries none."""
    auth = build_source_bundle(
        [{"name": "A"}], workspace_id="ws", run_id="r",
        provider="acme_api", tier=TIER_AUTHORITATIVE, secret_refs=["acme_secret"],
        key_attribute="name",
    )
    web = build_source_bundle(
        [{"name": "B"}], workspace_id="ws", run_id="r",
        provider="web_fake", tier=TIER_WEB,
        key_attribute="name",
    )
    assert auth.tiers == frozenset({TIER_AUTHORITATIVE})
    assert web.tiers == frozenset({TIER_WEB})
    assert auth.secret_refs == ("acme_secret",)
    assert web.secret_refs == ()
    assert PLAINTEXT_CREDENTIAL not in json.dumps(auth.to_dict())
