"""Tests for the unified Ask-AI agent: registry, planner, clean-before-enrich.

Everything is stubbed so the suite is deterministic and fast (no network, no
real Neptune): the classifier LLM, the inference LLM, predicate sampling, and the
underlying enrichment executor / normalization apply. Each async test is wrapped
in ``asyncio.wait_for`` to fail loudly rather than hang.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cograph_client.agent import planner as planner_mod
from cograph_client.agent.capabilities.enrich_cap import EnrichCapability
from cograph_client.agent.capabilities.normalize_cap import NormalizeCapability
from cograph_client.agent.capabilities.query import QueryCapability
from cograph_client.agent.conversation_store import reset_conversation_store
from cograph_client.agent.planner import (
    StoredPlan,
    execute_plan,
    handle,
    make_plan_store,
    register_default_capabilities,
    reset_plan_store,
)
from cograph_client.agent.registry import (
    AgentContext,
    PlanStep,
    get_capabilities,
    get_capability,
    order_steps,
    register_capability,
    reset_capabilities,
)
from cograph_client.normalization.rules import NormalizationRule

TIMEOUT = 5.0


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeNeptune:
    """Returns no rows by default; tests that need sampled values patch the
    inference sampling helper directly instead of teaching this a SPARQL dialect."""

    def __init__(self):
        self.updates: list[str] = []

    async def query(self, q):
        return {"head": {"vars": []}, "results": {"bindings": []}}

    async def update(self, q):
        self.updates.append(q)
        return None


class FakeJobStore:
    def __init__(self):
        self.created = []
        self.updated = []

    async def create(self, job):
        self.created.append(job)

    async def get(self, job_id):
        for j in self.created:
            if j.id == job_id:
                return j
        return None

    async def update(self, job):
        self.updated.append(job)


class FakeExecutor:
    def __init__(self):
        self.ran = []

    async def run(self, job, tenant_id):
        self.ran.append((job, tenant_id))


def _ctx(neptune=None, **extras_kw):
    return AgentContext(
        tenant_id="t1",
        kg_name="kg1",
        neptune=neptune or FakeNeptune(),
        type_name="Mentor",
        openrouter_key="fake-key",
        anthropic_key="fake-anthropic",
        extras={
            "enrichment_executor": extras_kw.get("executor", FakeExecutor()),
            "enrichment_job_store": extras_kw.get("job_store", FakeJobStore()),
        },
    )


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Each test starts from the default OSS capability set + an empty plan store."""
    reset_capabilities()
    reset_plan_store()
    reset_conversation_store()
    register_default_capabilities()
    yield
    reset_capabilities()
    reset_plan_store()
    reset_conversation_store()


@pytest.fixture(autouse=True)
def _track_bg_tasks(monkeypatch):
    """Schedule capability-spawned background coroutines as TRACKED tasks.

    Capabilities use the strong-ref ``_spawn`` (create_task) for long work
    (normalize apply / enrichment run). In tests we still want that work to run
    (so we can assert the executor/store was actually driven), just tracked so
    nothing leaks. We replace ``_spawn`` with one that creates a real task and
    keeps a strong ref — the underlying apply/run is itself stubbed per-test.
    """
    import cograph_client.agent.capabilities.dedup_cap as dedup_cap
    import cograph_client.agent.capabilities.enrich_cap as enrich_cap
    import cograph_client.agent.capabilities.normalize_cap as norm_cap

    spawned: list = []

    def tracking_spawn(coro):
        task = asyncio.ensure_future(coro)
        spawned.append(task)
        task.add_done_callback(lambda t: None)

    monkeypatch.setattr(norm_cap, "_spawn", tracking_spawn)
    monkeypatch.setattr(enrich_cap, "_spawn", tracking_spawn)
    monkeypatch.setattr(dedup_cap, "_spawn", tracking_spawn)
    return spawned


def _stub_classifier(monkeypatch, intent: str, clarify: str = ""):
    async def fake_chat(*args, **kwargs):
        import json

        return json.dumps({"intent": intent, "clarify": clarify})

    monkeypatch.setattr(planner_mod, "openrouter_chat", fake_chat)


# The Mentor type's schema the agent grounds extraction in: ``company`` is
# ABSENT (so it must be proposed as a new attribute), ``title`` + ``skills`` are
# present attributes, and ``speaks`` is a relationship to a Language type.
_MENTOR_SCHEMA = {
    "attributes": ["title", "skills"],
    "relationships": [{"name": "speaks", "target_type": "Language"}],
}


def _stub_schema(monkeypatch, schema: dict | None = None):
    """Stub ``list_type_schema`` in BOTH capabilities to the Mentor schema."""
    schema = schema if schema is not None else _MENTOR_SCHEMA

    async def fake_schema(neptune, tenant_id, type_name):
        return schema

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )
    monkeypatch.setattr(
        "cograph_client.agent.capabilities.normalize_cap.list_type_schema", fake_schema
    )


def _stub_enrich_extract(monkeypatch, payload: dict):
    """Stub the enrich capability's extraction LLM to return ``payload`` JSON."""
    import json

    async def fake_chat(*args, **kwargs):
        return json.dumps(payload)

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.openrouter_chat", fake_chat
    )


def _stub_normalize_extract(monkeypatch, payload: dict):
    """Stub the normalize capability's directive LLM to return ``payload`` JSON."""
    import json

    async def fake_chat(*args, **kwargs):
        return json.dumps(payload)

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.normalize_cap.openrouter_chat", fake_chat
    )


# --------------------------------------------------------------------------- #
# 1. Registry roundtrip — adding a capability needs no route change
# --------------------------------------------------------------------------- #
def test_register_and_get_capability_roundtrip():
    names_before = {c.name for c in get_capabilities()}
    assert {"query", "normalize", "enrich"} <= names_before

    class DedupCapability:
        name = "dedup"

        def describe(self):
            return "merge duplicate entities"

        async def plan(self, ctx, instruction):
            return []

        async def execute(self, ctx, step):
            return {"kind": "ack"}

    register_capability(DedupCapability())
    assert get_capability("dedup") is not None
    assert "dedup" in {c.name for c in get_capabilities()}
    # No route/endpoint was added — the single endpoint dispatches by name.


def test_order_steps_respects_depends_on():
    a = PlanStep(capability="normalize", action="x")
    b = PlanStep(capability="enrich", action="y", depends_on=[a.id])
    ordered = order_steps([b, a])  # deliberately reversed input
    assert [s.id for s in ordered] == [a.id, b.id]


# --------------------------------------------------------------------------- #
# 2. Classifier routing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_question_routes_to_answer(monkeypatch):
    _stub_classifier(monkeypatch, "question")

    async def fake_answer(self, ctx, q):
        return {"answer": "42", "sparql": "SELECT ...", "rows": [], "narrative": ""}

    monkeypatch.setattr(QueryCapability, "answer", fake_answer)

    out = await asyncio.wait_for(
        handle(_ctx(), "how many mentors are there?"), TIMEOUT
    )
    assert out["kind"] == "answer"
    assert out["answer"] == "42"
    assert out["sparql"].startswith("SELECT")


@pytest.mark.asyncio
async def test_enrich_routes_to_plan(monkeypatch):
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {"attributes": ["company"], "scope": None, "tier": "core"},
    )
    out = await asyncio.wait_for(
        handle(_ctx(), "enrich company for managers"), TIMEOUT
    )
    assert out["kind"] == "plan"
    assert out["plan_id"]
    steps = out["steps"]
    assert len(steps) == 1
    assert steps[0]["capability"] == "enrich"
    assert steps[0]["action"] == "run_enrichment"
    assert steps[0]["params"]["attributes"] == ["company"]


# --------------------------------------------------------------------------- #
# 2b. Deterministic web-discovery guard
#     An explicit "… from the web" request must route to discovery even when the
#     classifier mis-files it as question/ambiguous (the payload reads like a
#     query: "list … with …"). The Explorer's "Add data from the web" entry point
#     depends on this — see WebDiscoveryStep.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_explicit_web_request_forces_discover_over_question(monkeypatch):
    # Classifier WRONGLY says "question" (the message contains "list"). The guard
    # must override and route to discovery, NOT the query answerer.
    _stub_classifier(monkeypatch, "question")

    async def fake_answer(self, ctx, q):
        return {"answer": "SHOULD_NOT_RUN", "sparql": "", "rows": [], "narrative": ""}

    monkeypatch.setattr(QueryCapability, "answer", fake_answer)

    out = await asyncio.wait_for(
        handle(
            _ctx(),
            "Add data from the web: list models TTS models with a VAPI humanness score",
        ),
        TIMEOUT,
    )
    # Did NOT fall through to the read-only query path.
    assert out.get("answer") != "SHOULD_NOT_RUN"
    # With no web-source provider registered in OSS, the discover capability
    # answers with a clear "not enabled" message — proof it handled the turn.
    body = f"{out.get('narrative', '')} {out.get('answer', '')}".lower()
    assert "enabled" in body


@pytest.mark.asyncio
async def test_explicit_web_request_overrides_ambiguous(monkeypatch):
    # The reported bug: classifier returns the generic "ambiguous" clarify. The
    # guard must still route an explicit web request to discovery.
    _stub_classifier(monkeypatch, "ambiguous", clarify="Could you clarify?")
    out = await asyncio.wait_for(
        handle(_ctx(), "add the S&P 500 companies from the web"), TIMEOUT
    )
    # Not the generic clarify dead-end.
    assert not (
        out["kind"] == "clarify"
        and "clarify what you'd like" in out.get("question", "").lower()
    )
    body = f"{out.get('narrative', '')} {out.get('answer', '')}".lower()
    assert "enabled" in body  # routed to discovery (degrades to not-enabled in OSS)


@pytest.mark.asyncio
async def test_plain_list_question_is_not_hijacked(monkeypatch):
    # A genuine read-only question that does NOT mention the web must still
    # answer — the guard must not over-trigger.
    _stub_classifier(monkeypatch, "question")

    async def fake_answer(self, ctx, q):
        return {"answer": "42", "sparql": "SELECT", "rows": [], "narrative": ""}

    monkeypatch.setattr(QueryCapability, "answer", fake_answer)
    out = await asyncio.wait_for(
        handle(_ctx(), "list the TTS models in this graph"), TIMEOUT
    )
    assert out["kind"] == "answer"
    assert out["answer"] == "42"


@pytest.mark.asyncio
async def test_web_question_is_not_hijacked(monkeypatch):
    # "how many … from the web?" is a read-only question (trailing '?', question
    # lead) — the guard must leave it alone.
    _stub_classifier(monkeypatch, "question")

    async def fake_answer(self, ctx, q):
        return {"answer": "7", "sparql": "SELECT", "rows": [], "narrative": ""}

    monkeypatch.setattr(QueryCapability, "answer", fake_answer)
    out = await asyncio.wait_for(
        handle(_ctx(), "how many companies did we add from the web?"), TIMEOUT
    )
    assert out["kind"] == "answer"
    assert out["answer"] == "7"


@pytest.mark.asyncio
async def test_discovery_beats_enrich_keyword_on_empty_graph(monkeypatch):
    """ONTA-244 — a clearly-new-data ask that leads with "discover" (and even
    contains the word "enrich") routes to DISCOVERY, not enrich. The classifier
    WRONGLY word-triggers "enrich" (the message says "then enrich each…"); the
    widened deterministic web-discovery guard must override it. With no web-source
    provider registered in OSS the discover rail degrades to a clear "not enabled"
    answer — proof the turn routed to discovery, not into an empty enrich loop.
    Asserts on the CAPABILITY that ran, not on any field token."""
    _stub_classifier(monkeypatch, "enrich")  # the mis-classification

    # If it wrongly routed to enrich, this would run and NOT say "enabled".
    async def fake_enrich_plan(self, ctx, instruction, parsed=None):
        return [
            PlanStep(
                capability="enrich",
                action="clarify",
                params={"question": "ENRICH_RAN", "options": []},
            )
        ]

    monkeypatch.setattr(EnrichCapability, "plan", fake_enrich_plan)

    out = await asyncio.wait_for(
        handle(
            _ctx(),
            "discover all Sprockets in Region 7 then enrich each with vendor",
        ),
        TIMEOUT,
    )
    # Routed to DISCOVERY (degrades to not-enabled in OSS), not the enrich clarify.
    assert out.get("question") != "ENRICH_RAN"
    body = f"{out.get('narrative', '')} {out.get('answer', '')} {out.get('question', '')}".lower()
    assert "enabled" in body


@pytest.mark.asyncio
async def test_zero_match_empty_type_offers_discovery(monkeypatch):
    """ONTA-244 — an enrich ask against a type the graph has ZERO of is a
    discover-vs-enrich mis-route. Enrichment's 0-match clarify must offer a
    "Discover … from the web" option (not ONLY "Enrich all"), since enriching a
    type with no entities would do nothing. Asserts on the OPTION mechanism, with
    an invented type."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Sprocket"])
    _stub_schema(monkeypatch, {"attributes": ["material"], "relationships": []})
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["vendor"],
            "scope": {"predicate": "material", "value": "titanium"},
            "tier": "core",
        },
    )

    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        return [], "literal"

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.sample_predicate_values",
        fake_sample,
    )

    class EmptyTypeExecutor:
        # 0 for BOTH the scoped filter AND the whole-type count → empty type.
        async def count_entities(self, tenant_id, kg_name, type_name, scope=None):
            return 0

    out = await asyncio.wait_for(
        handle(
            _ctx(executor=EmptyTypeExecutor()),
            "enrich the vendor for titanium Sprockets",
        ),
        TIMEOUT,
    )
    assert out["kind"] == "clarify"
    opts = out.get("options") or []
    # A discovery option is offered (not only "Enrich all Sprocket").
    assert any("discover" in o.lower() for o in opts), opts
    # And clicking it re-routes to discovery — it matches the deterministic guard.
    from cograph_client.agent.planner import _is_web_discovery_request

    discover_opt = next(o for o in opts if "discover" in o.lower())
    assert _is_web_discovery_request(discover_opt)


def _stub_kg_types(monkeypatch, names: list[str]):
    """Stub the enrich capability's KG type listing to ``names``."""

    async def fake_list_types(ctx):
        return list(names)

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap._list_types", fake_list_types
    )


@pytest.mark.asyncio
async def test_enrich_infers_type_from_message_over_selection(monkeypatch):
    """The type NAMED in the message wins over the UI selection: "enrich brokers
    with their websites" targets Broker even though ctx.type_name is Mentor (the
    selected-but-wrong type). Regression for the enrich-uses-selection bug."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Broker", "PropertyListing", "Mentor"])

    captured: dict = {}

    async def fake_schema(neptune, tenant_id, type_name):
        captured["schema_type"] = type_name
        return {"attributes": ["website"], "relationships": []}

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )
    _stub_enrich_extract(
        monkeypatch, {"attributes": ["website"], "scope": None, "tier": "core"}
    )

    out = await asyncio.wait_for(
        handle(_ctx(), "enrich brokers with their websites"), TIMEOUT
    )
    assert out["kind"] == "plan"
    step = out["steps"][0]
    assert step["params"]["type_name"] == "Broker"  # message won, not Mentor
    assert captured["schema_type"] == "Broker"  # grounded in Broker's schema
    assert step["params"]["attributes"] == ["website"]


@pytest.mark.asyncio
async def test_enrich_infers_type_with_no_selection(monkeypatch):
    """With NO type selected (ctx.type_name is None), a message that names the
    type still plans (resolves Broker) instead of bailing to clarify."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Broker", "PropertyListing"])

    async def fake_schema(neptune, tenant_id, type_name):
        return {"attributes": ["website"], "relationships": []}

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )
    _stub_enrich_extract(
        monkeypatch, {"attributes": ["website"], "scope": None, "tier": "core"}
    )

    ctx = AgentContext(
        tenant_id="t1",
        kg_name="kg1",
        neptune=FakeNeptune(),
        type_name=None,  # nothing selected in the UI
        openrouter_key="fake-key",
        extras={
            "enrichment_executor": FakeExecutor(),
            "enrichment_job_store": FakeJobStore(),
        },
    )
    out = await asyncio.wait_for(
        handle(ctx, "look up the websites for the top 5 brokers"), TIMEOUT
    )
    assert out["kind"] == "plan"
    assert out["steps"][0]["params"]["type_name"] == "Broker"


def _stub_subset_resolver(monkeypatch, uris: list[str]):
    """Stub the NL→SPARQL subset resolver the enrich cap calls."""

    async def fake_select(self, description, type_name, graph_uri, instance_graph=None, limit=None):
        return list(uris)

    monkeypatch.setattr(
        "cograph_client.nlp.pipeline.NLQueryPipeline.select_entity_uris", fake_select
    )


@pytest.mark.asyncio
async def test_enrich_resolves_ranked_subset_to_entity_uris(monkeypatch):
    """"enrich the top 5 brokers by listings with websites" resolves the ranked
    subset to concrete entity IRIs and enriches EXACTLY those (entity_uris) — not
    the whole Broker type. scope/limit are cleared and the preview count is 5."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Broker", "PropertyListing"])

    async def fake_schema(neptune, tenant_id, type_name):
        return {"attributes": ["website"], "relationships": []}

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["website"],
            "scope": None,
            "subset": {
                "description": "the 5 brokers with the most property listings",
                "limit": 5,
            },
            "tier": "core",
        },
    )
    uris = [f"https://onta.dev/e/broker/{i}" for i in range(5)]
    _stub_subset_resolver(monkeypatch, uris)

    out = await asyncio.wait_for(
        handle(
            _ctx(),
            "enrich the top 5 brokers by number of listings with their websites",
        ),
        TIMEOUT,
    )
    assert out["kind"] == "plan"
    p = out["steps"][0]["params"]
    assert p["type_name"] == "Broker"
    assert p["entity_uris"] == uris  # exactly the resolved 5
    assert p["scope"] is None  # explicit set supersedes value-scope
    assert p["limit"] is None  # no whole-type cap for an explicit set
    assert out["steps"][0]["preview"]["entity_count"] == 5


@pytest.mark.asyncio
async def test_enrich_unresolvable_subset_fails_closed(monkeypatch):
    """When the user names a subset we CANNOT resolve to entities, fail closed —
    clarify instead of silently enriching the whole type."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Broker"])

    async def fake_schema(neptune, tenant_id, type_name):
        return {"attributes": ["website"], "relationships": []}

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["website"],
            "scope": None,
            "subset": {"description": "the brokers Sarah mentioned", "limit": None},
            "tier": "core",
        },
    )
    _stub_subset_resolver(monkeypatch, [])  # resolution finds nothing

    out = await asyncio.wait_for(
        handle(_ctx(), "enrich the brokers Sarah mentioned with their websites"),
        TIMEOUT,
    )
    assert out["kind"] == "clarify"  # not a whole-type plan
    # Brief + targeted: names the type and offers guiding options to converge.
    assert "Broker" in out["question"]
    assert out.get("options")


@pytest.mark.asyncio
async def test_enrich_zero_match_scope_clarifies(monkeypatch):
    """A value-scope that matches 0 entities asks a brief question instead of
    proposing an empty paid job (confirm-the-scope on 0 results)."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Mentor"])
    _stub_schema(monkeypatch)  # Mentor: 'speaks' relationship present
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["company"],
            "scope": {"predicate": "speaks", "value": "Klingon"},
            "tier": "core",
        },
    )

    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        return [], "literal"

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.sample_predicate_values",
        fake_sample,
    )

    class ZeroExecutor:
        async def count_entities(self, tenant_id, kg_name, type_name, scope=None):
            return 0

    out = await asyncio.wait_for(
        handle(
            _ctx(executor=ZeroExecutor()),
            "enrich the company for mentors who speak Klingon",
        ),
        TIMEOUT,
    )
    assert out["kind"] == "clarify"
    assert "Mentor" in out["question"]  # targeted to the type
    assert out.get("options")  # offers "enrich all" / "different value"


# --------------------------------------------------------------------------- #
# 2c. Refresh-existing routing + multi-value subset scoping
#     "refresh <attrs> for <named/scoped existing subset>" must route to the
#     enrichment VERIFY path scoped to the matching existing records — NOT a fresh
#     discovery — and a subset named by a LIST of values must MATCH existing
#     records case/normalization-insensitively. Regression for the persona-eval
#     refresh gap (agent ran a new discovery build; the crammed-literal scope
#     matched 0 → premature-clarify → discovery). All-invented types/attrs/values.
# --------------------------------------------------------------------------- #
class _ScopeValueExecutor:
    """A FakeExecutor whose ``select_scope_value_uris`` matches a value SET
    case/normalization-insensitively against a canned {value -> uri} table — the
    deterministic multi-value resolver the enrich cap drives. Records the values
    it was asked for so a test can assert the crammed list was split."""

    def __init__(self, value_to_uri: dict[str, str]):
        self.ran = []
        # Normalize the table keys so matching is case/whitespace-insensitive.
        self._table = {k.strip().lower(): v for k, v in value_to_uri.items()}
        self.select_calls: list[tuple] = []

    async def run(self, job, tenant_id):
        self.ran.append((job, tenant_id))

    async def count_entities(self, tenant_id, kg_name, type_name, scope=None,
                             entity_uris=None):
        # Whole-type is non-empty (so a 0-scope match is "filter too narrow", not
        # "empty type") — but the multi-value path resolves before this is hit.
        return 5

    async def select_scope_value_uris(self, tenant_id, kg_name, type_name,
                                      predicate, values, limit=None):
        self.select_calls.append((predicate, list(values)))
        uris: list[str] = []
        for v in values:
            u = self._table.get(str(v).strip().lower())
            if u and u not in uris:
                uris.append(u)
        return uris


def _stub_refresh_classifier_wrong(monkeypatch):
    """Stub the LLM classifier to return the WRONG intent (discover) — proving the
    deterministic refresh-existing guard recovers routing without the classifier."""
    _stub_classifier(monkeypatch, "discover")


@pytest.mark.asyncio
async def test_refresh_multi_value_subset_routes_to_enrich_verify(monkeypatch):
    """A "refresh <attrs> for <comma+and list of values>" request routes to the
    enrichment VERIFY path scoped to the matching existing records — even when the
    LLM classifier mis-classifies it as DISCOVER. The crammed value list is split,
    matched case-insensitively to existing entity_uris, and the plan is a scoped
    refresh (refresh=True), NOT a discovery build. Invented type/attr/values."""
    # Classifier is WRONG (discover) — the deterministic route must recover.
    _stub_refresh_classifier_wrong(monkeypatch)
    _stub_kg_types(monkeypatch, ["Widget"])
    _stub_schema(
        monkeypatch, {"attributes": ["price", "made_by"], "relationships": []}
    )
    # The extractor crams the vendor LIST into a single scope.value (the real bug).
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["price"],
            "scope": {"predicate": "made_by", "value": "Acme, Globex and Initech"},
            "tier": "core",
        },
    )
    # Existing records for two of the three named vendors (mixed casing/spacing).
    execu = _ScopeValueExecutor(
        {
            "acme": "https://onta.dev/e/widget/1",
            "globex": "https://onta.dev/e/widget/2",
            # "Initech" intentionally absent — the set still matches the 2 present.
        }
    )
    out = await asyncio.wait_for(
        handle(
            _ctx(executor=execu),
            "refresh the price for the Acme, Globex and Initech widgets",
        ),
        TIMEOUT,
    )
    # Routed to a scoped ENRICH plan, not a discovery build / not a clarify.
    assert out["kind"] == "plan", out
    step = out["steps"][0]
    assert step["capability"] == "enrich"
    assert step["action"] == "run_enrichment"
    p = step["params"]
    # Scoped to EXACTLY the matched existing records (entity_uris), scope cleared.
    assert p["entity_uris"] == [
        "https://onta.dev/e/widget/1",
        "https://onta.dev/e/widget/2",
    ]
    assert p["scope"] is None
    # It is a REFRESH (verify) run — ran_enrich-equivalent true.
    assert p["refresh"] is True
    assert step["preview"]["refresh"] is True
    # The crammed literal was SPLIT into the three named values before matching.
    assert execu.select_calls, "multi-value resolver was not called"
    _, asked_values = execu.select_calls[0]
    assert [v.lower() for v in asked_values] == ["acme", "globex", "initech"]


@pytest.mark.asyncio
async def test_multi_value_scope_matches_case_insensitively(monkeypatch):
    """The subset is matched case/normalization-insensitively: the user types the
    values in one casing, the existing records store them in another, and the scope
    still resolves. Drives the enrich cap directly through EnrichCapability.plan."""
    _stub_kg_types(monkeypatch, ["Vendor"])

    async def fake_schema(neptune, tenant_id, type_name):
        # ``region`` must be a real schema predicate or the extractor's scope is
        # (correctly) dropped as unknown before it can be split.
        return {"attributes": ["rating", "region"], "relationships": []}

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["rating"],
            # Different casing + a slash delimiter than the stored records.
            "scope": {"predicate": "region", "value": "NORTH / south"},
            "tier": "core",
        },
    )
    execu = _ScopeValueExecutor(
        {"north": "https://onta.dev/e/vendor/n", "south": "https://onta.dev/e/vendor/s"}
    )
    cap = EnrichCapability()
    ctx = _ctx(executor=execu)
    steps = await asyncio.wait_for(
        cap.plan(ctx, "update the rating for vendors in NORTH / south region"),
        TIMEOUT,
    )
    assert len(steps) == 1
    p = steps[0].params
    assert p["entity_uris"] == [
        "https://onta.dev/e/vendor/n",
        "https://onta.dev/e/vendor/s",
    ]
    assert p["scope"] is None
    # "update" is a refresh-existing verb → verify mode.
    assert p.get("refresh") is True


@pytest.mark.asyncio
async def test_refresh_existing_guard_recovers_wrong_classification(monkeypatch):
    """The planner-level deterministic guard: a refresh-existing message forces the
    ENRICH intent even when the classifier returns discover — asserted on the
    capability the turn reached, not on any field token. If routing had followed
    the (wrong) discover classification, the enrich plan below would never run."""
    _stub_refresh_classifier_wrong(monkeypatch)  # classifier says "discover"

    captured: dict = {}

    async def fake_enrich_plan(self, ctx, instruction, parsed=None):
        captured["reached_enrich"] = True
        return [
            PlanStep(
                capability="enrich",
                action="run_enrichment",
                params={"type_name": "Gadget", "attributes": ["spec"],
                        "tier": "core", "scope": None, "limit": None,
                        "entity_uris": None},
                preview={},
            )
        ]

    monkeypatch.setattr(EnrichCapability, "plan", fake_enrich_plan)

    out = await asyncio.wait_for(
        handle(_ctx(), "re-verify the spec for the existing Gadgets"), TIMEOUT
    )
    assert captured.get("reached_enrich") is True  # forced onto the enrich rail
    assert out["kind"] == "plan"
    assert out["steps"][0]["capability"] == "enrich"


@pytest.mark.asyncio
async def test_refresh_guard_defers_to_web_discovery(monkeypatch):
    """The refresh guard must NOT hijack a genuine mint-new "… from the web"
    discovery even though it contains a refresh verb — the web-discovery guard
    still wins so we don't force enrich on records that don't exist yet."""
    _stub_classifier(monkeypatch, "ambiguous", clarify="?")
    # A refresh verb ("refresh our data") next to an unmistakable mint-new fetch
    # ("… pull new Gizmos from the web") — the web-discovery guard must still win.
    out = await asyncio.wait_for(
        handle(_ctx(), "refresh our data — pull new Gizmos from the web"),
        TIMEOUT,
    )
    # Routed to DISCOVERY (degrades to not-enabled in OSS), not the enrich rail.
    body = f"{out.get('narrative', '')} {out.get('answer', '')}".lower()
    assert "enabled" in body


@pytest.mark.asyncio
async def test_multi_value_scope_no_match_stays_on_enrich_rail(monkeypatch):
    """When a multi-value scope matches NO existing record, the clarify keeps the
    user on the ENRICH rail (offer "Enrich all"), and does NOT lead with a
    discovery option — the mis-route this fix closes. The clicked option must NOT
    be a web-discovery trigger."""
    from cograph_client.agent.planner import _is_web_discovery_request

    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Widget"])
    _stub_schema(
        monkeypatch, {"attributes": ["price", "made_by"], "relationships": []}
    )
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["price"],
            "scope": {"predicate": "made_by", "value": "Foo, Bar and Baz"},
            "tier": "core",
        },
    )
    execu = _ScopeValueExecutor({})  # nothing matches
    out = await asyncio.wait_for(
        handle(_ctx(executor=execu), "refresh the price for Foo, Bar and Baz widgets"),
        TIMEOUT,
    )
    assert out["kind"] == "clarify"
    opts = out.get("options") or []
    # Enrich-rail guidance only — NO discovery option that would re-route to a build.
    assert opts, opts
    assert not any(_is_web_discovery_request(o) for o in opts), opts


@pytest.mark.asyncio
async def test_execute_enrich_passes_entity_uris(monkeypatch):
    """An enrich step carrying entity_uris builds an EnrichJob scoped to exactly
    those IRIs (entity_uris wins over scope in the executor)."""
    cap = EnrichCapability()
    job_store = FakeJobStore()
    ctx = _ctx(job_store=job_store)
    uris = ["https://onta.dev/e/broker/1", "https://onta.dev/e/broker/2"]
    step = PlanStep(
        capability="enrich",
        action="run_enrichment",
        params={
            "type_name": "Broker",
            "attributes": ["website"],
            "tier": "core",
            "confidence_min": 0.4,
            "scope": None,
            "limit": None,
            "entity_uris": uris,
        },
    )
    out = await asyncio.wait_for(cap.execute(ctx, step), TIMEOUT)
    assert out["kind"] == "ack"
    assert len(job_store.created) == 1
    job = job_store.created[0]
    assert job.entity_uris == uris
    assert job.type_name == "Broker"
    assert job.limit is None


def test_pipeline_entity_uris_from_bindings():
    """The resolver extracts the ?uri column (deduped, order-preserving, capped),
    falling back to the first IRI-looking value when a row lacks ?uri."""
    from cograph_client.nlp.pipeline import NLQueryPipeline

    bindings = [
        {"uri": "https://onta.dev/e/b/1", "n": "299"},
        {"uri": "https://onta.dev/e/b/2", "n": "194"},
        {"uri": "https://onta.dev/e/b/1", "n": "299"},  # duplicate dropped
        {"name": "no-iri-here"},  # skipped — no IRI
        {"x": "https://onta.dev/e/b/3"},  # fallback to first IRI value
    ]
    assert NLQueryPipeline._entity_uris_from_bindings(bindings, limit=10) == [
        "https://onta.dev/e/b/1",
        "https://onta.dev/e/b/2",
        "https://onta.dev/e/b/3",
    ]
    assert NLQueryPipeline._entity_uris_from_bindings(bindings, limit=1) == [
        "https://onta.dev/e/b/1",
    ]


@pytest.mark.asyncio
async def test_ambiguous_routes_to_clarify(monkeypatch):
    _stub_classifier(monkeypatch, "ambiguous", clarify="Which field?")
    out = await asyncio.wait_for(handle(_ctx(), "do the thing"), TIMEOUT)
    assert out["kind"] == "clarify"
    assert out["question"] == "Which field?"


@pytest.mark.asyncio
async def test_unregistered_intent_clarifies(monkeypatch):
    # 'ontology' is recognized by the classifier but no capability is registered
    # yet (A2) → clarify. (dedup IS registered now; see the dedup tests below.)
    _stub_classifier(monkeypatch, "ontology")
    out = await asyncio.wait_for(handle(_ctx(), "rename the type"), TIMEOUT)
    assert out["kind"] == "clarify"


# --------------------------------------------------------------------------- #
# 3. clean-before-enrich composition
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_clean_before_enrich_composes_depends_on(monkeypatch):
    """'enrich company for mentors who speak Persian' where the speaks target
    sample is composite ('English__Persian') → the plan contains a normalize
    step that the enrich step depends_on, ordered normalize-first."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["company"],
            "scope": {"predicate": "speaks", "value": "Persian"},
            "tier": "core",
        },
    )

    # The scope predicate 'speaks' has a composite target sample.
    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        assert pred_leaf == "speaks"
        return (["English__Persian", "English"], "relationship")

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.sample_predicate_values",
        fake_sample,
    )

    # The normalize capability's plan() infers a list_explode rule for 'speaks'.
    async def fake_suggest(neptune, tenant_id, kg, type_name, leaves):
        assert leaves == ["speaks"]
        return [
            NormalizationRule(
                id="kg1__Mentor__speaks",
                kg_name="kg1",
                type_name="Mentor",
                predicate="speaks",
                target_kind="relationship",
                rule_type="list_explode",
                params={"delimiters": ["__"], "target": "entity"},
                confidence=0.95,
                rationale="composite language values",
                sample_values=["English__Persian"],
            )
        ]

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.normalize_cap.suggest_rules_for_predicates",
        fake_suggest,
    )

    out = await asyncio.wait_for(
        handle(_ctx(), "enrich company for mentors who speak Persian"), TIMEOUT
    )
    assert out["kind"] == "plan"
    steps = out["steps"]
    assert len(steps) == 2
    normalize_step, enrich_step = steps[0], steps[1]
    assert normalize_step["capability"] == "normalize"
    assert enrich_step["capability"] == "enrich"
    # The enrich step depends on the normalize step → ordered normalize-first.
    assert enrich_step["depends_on"] == [normalize_step["id"]]
    # Scope was parsed from the NL.
    assert enrich_step["params"]["scope"] == {
        "predicate": "speaks",
        "value": "Persian",
    }
    # Dry-run preview shows the split.
    assert normalize_step["preview"]["rule_type"] == "list_explode"


@pytest.mark.asyncio
async def test_no_prereq_when_scope_target_atomic(monkeypatch):
    """If the scope target sample is already atomic, no normalize prerequisite."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["company"],
            "scope": {"predicate": "speaks", "value": "Persian"},
            "tier": "core",
        },
    )

    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        return (["English", "Persian"], "relationship")  # atomic

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.sample_predicate_values",
        fake_sample,
    )
    out = await asyncio.wait_for(
        handle(_ctx(), "enrich company for mentors who speak Persian"), TIMEOUT
    )
    assert out["kind"] == "plan"
    assert len(out["steps"]) == 1
    assert out["steps"][0]["capability"] == "enrich"


# --------------------------------------------------------------------------- #
# 4. confirm/execute runs steps in dependency order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_plan_runs_in_dependency_order(monkeypatch):
    """A persisted [normalize→enrich] plan executes normalize before enrich."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["company"],
            "scope": {"predicate": "speaks", "value": "Persian"},
            "tier": "core",
        },
    )

    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        return (["English__Persian"], "relationship")

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.sample_predicate_values",
        fake_sample,
    )

    async def fake_suggest(neptune, tenant_id, kg, type_name, leaves):
        return [
            NormalizationRule(
                id="kg1__Mentor__speaks",
                kg_name="kg1",
                type_name="Mentor",
                predicate="speaks",
                target_kind="relationship",
                rule_type="list_explode",
                params={"delimiters": ["__"], "target": "entity"},
                confidence=0.9,
                sample_values=["English__Persian"],
            )
        ]

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.normalize_cap.suggest_rules_for_predicates",
        fake_suggest,
    )

    # Record execution order across both capabilities.
    order: list[str] = []
    orig_norm_execute = NormalizeCapability.execute
    orig_enrich_execute = EnrichCapability.execute

    async def norm_execute(self, ctx, step):
        order.append("normalize")
        return await orig_norm_execute(self, ctx, step)

    async def enrich_execute(self, ctx, step):
        order.append("enrich")
        return await orig_enrich_execute(self, ctx, step)

    monkeypatch.setattr(NormalizeCapability, "execute", norm_execute)
    monkeypatch.setattr(EnrichCapability, "execute", enrich_execute)

    # Stub the normalize store save so execute() doesn't touch a real store.
    async def fake_save(self, tenant_id, rule):
        return None

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.normalize_cap.NormalizationRuleStore.save",
        fake_save,
    )

    job_store = FakeJobStore()
    executor = FakeExecutor()
    ctx = _ctx(executor=executor, job_store=job_store)

    plan_out = await asyncio.wait_for(
        handle(ctx, "enrich company for mentors who speak Persian"), TIMEOUT
    )
    assert plan_out["kind"] == "plan"
    plan_id = plan_out["plan_id"]

    result = await asyncio.wait_for(execute_plan(ctx, plan_id), TIMEOUT)
    assert result["kind"] == "result"
    assert order == ["normalize", "enrich"]  # dependency order honored
    # Let the spawned background tasks (executor.run, normalize apply) run.
    await asyncio.sleep(0)
    # The enrich step actually created + ran a job through the real executor path.
    assert len(job_store.created) == 1
    assert len(executor.ran) == 1
    statuses = [s["status"] for s in result["steps"]]
    assert statuses == ["ok", "ok"]


@pytest.mark.asyncio
async def test_execute_plan_unknown_id_errors():
    out = await asyncio.wait_for(execute_plan(_ctx(), "nope"), TIMEOUT)
    assert out["kind"] == "error"


# --------------------------------------------------------------------------- #
# 4b. One-shot confirm guard: a duplicate confirm can never re-run the steps.
#
# DELIBERATE contract change (2026-07-03): execute_plan used to be "idempotent-
# ish — re-running a done plan re-issues the acks", i.e. it re-executed every
# step. But capability executes are NOT idempotent (web discovery spawns a
# fresh background job + a full paid provider fan-out and re-ingests every row
# per run), so a retried confirm — the Explorer auto-confirm double-firing, a
# client retry after a gateway timeout whose first request DID spawn the work —
# double-billed and double-wrote silently. The plan is now claimed atomically
# and executed exactly once; a duplicate confirm replays the persisted result
# (finished) or is refused (still in flight / no result persisted).
# --------------------------------------------------------------------------- #


class _RecordingCap:
    """A registrable capability that records executions and returns a job ack.

    ``gate`` (optional) makes execute() block until the event is set, so a test
    can observe a plan mid-execution deterministically.
    """

    name = "once_cap"

    def __init__(self, gate: asyncio.Event | None = None):
        self.calls: list[str] = []
        self.gate = gate

    def describe(self):
        return "records executions (test)"

    async def plan(self, ctx, instruction):
        return []

    async def execute(self, ctx, step):
        self.calls.append(step.id)
        if self.gate is not None:
            await self.gate.wait()
        return {"kind": "ack", "job_id": f"job-{len(self.calls)}", "job_status": "queued"}


async def _save_plan(plan_id: str = "p-once", **overrides) -> StoredPlan:
    """Persist a minimal single-step plan for the one-shot tests."""
    plan = StoredPlan(
        plan_id=plan_id,
        tenant_id="t1",
        kg_name="kg1",
        type_name="Mentor",
        message="test plan",
        steps=[PlanStep(capability="once_cap", action="go")],
        **overrides,
    )
    await make_plan_store().save(plan)
    return plan


@pytest.mark.asyncio
async def test_duplicate_confirm_replays_result_without_rerunning():
    """A re-confirm of a finished plan returns the SAME acks/job ids (marked
    ``replayed``) and does not execute anything a second time."""
    cap = _RecordingCap()
    register_capability(cap)
    await _save_plan("p-once")
    ctx = _ctx()

    first = await asyncio.wait_for(execute_plan(ctx, "p-once"), TIMEOUT)
    assert first["kind"] == "result"
    assert first["steps"][0]["job_id"] == "job-1"
    assert "replayed" not in first

    second = await asyncio.wait_for(execute_plan(ctx, "p-once"), TIMEOUT)
    assert second["kind"] == "result"
    assert second["replayed"] is True
    assert second["steps"] == first["steps"]  # identical acks + job ids
    assert len(cap.calls) == 1  # the steps ran exactly once

    stored = await make_plan_store().get("p-once", "t1")
    assert stored.status == "done"
    assert stored.result["steps"] == first["steps"]


@pytest.mark.asyncio
async def test_confirm_racing_inflight_execution_is_refused():
    """A confirm arriving while the first is still executing is refused with a
    typed error — and once the first finishes, a later confirm replays."""
    gate = asyncio.Event()
    cap = _RecordingCap(gate=gate)
    register_capability(cap)
    await _save_plan("p-flight")

    first = asyncio.ensure_future(execute_plan(_ctx(), "p-flight"))
    for _ in range(50):  # let the first confirm claim + enter execute()
        if cap.calls:
            break
        await asyncio.sleep(0)
    assert cap.calls, "first confirm never reached execute()"

    dup = await asyncio.wait_for(execute_plan(_ctx(), "p-flight"), TIMEOUT)
    assert dup["kind"] == "error"
    assert dup["code"] == "plan_already_executing"

    gate.set()
    result = await asyncio.wait_for(first, TIMEOUT)
    assert result["kind"] == "result"
    assert len(cap.calls) == 1  # the duplicate never ran the step

    replay = await asyncio.wait_for(execute_plan(_ctx(), "p-flight"), TIMEOUT)
    assert replay.get("replayed") is True


@pytest.mark.asyncio
async def test_stale_executing_claim_is_recoverable():
    """An ``executing`` claim orphaned by a mid-run crash (older than the
    staleness cutoff) is claimable again; a FRESH claim stays exclusive."""
    cap = _RecordingCap()
    register_capability(cap)
    now = datetime.now(timezone.utc)

    # Fresh claim (just taken) → still exclusive, refused.
    await _save_plan("p-stuck", status="executing", executed_at=now)
    fresh = await asyncio.wait_for(execute_plan(_ctx(), "p-stuck"), TIMEOUT)
    assert fresh["kind"] == "error" and fresh["code"] == "plan_already_executing"
    assert cap.calls == []

    # Same plan, claim now older than the cutoff → the executor is presumed
    # dead; the re-confirm claims and actually runs it.
    stuck = await make_plan_store().get("p-stuck", "t1")
    stuck.executed_at = now - timedelta(hours=1)
    await make_plan_store().save(stuck)
    recovered = await asyncio.wait_for(execute_plan(_ctx(), "p-stuck"), TIMEOUT)
    assert recovered["kind"] == "result"
    assert len(cap.calls) == 1


@pytest.mark.asyncio
async def test_finished_plan_without_persisted_result_is_refused():
    """A done plan with NO persisted result (finished by a build predating the
    guard, or a catastrophic failure) refuses rather than re-running."""
    cap = _RecordingCap()
    register_capability(cap)
    await _save_plan("p-legacy", status="done")

    out = await asyncio.wait_for(execute_plan(_ctx(), "p-legacy"), TIMEOUT)
    assert out["kind"] == "error"
    assert out["code"] == "plan_already_executed"
    assert cap.calls == []


@pytest.mark.asyncio
async def test_done_save_failure_still_returns_result(monkeypatch):
    """A store blip on the FINAL done-save must not discard the acks — the
    steps ran (paid work may be in flight), so the caller gets the result and
    the plan simply stays claimed (no replay) until the stale cutoff."""
    cap = _RecordingCap()
    register_capability(cap)
    await _save_plan("p-blip")
    store = make_plan_store()
    real_save = store.save

    async def flaky_save(plan):
        if plan.status == "done":
            raise RuntimeError("pool blip")
        return await real_save(plan)

    monkeypatch.setattr(store, "save", flaky_save)

    out = await asyncio.wait_for(execute_plan(_ctx(), "p-blip"), TIMEOUT)
    assert out["kind"] == "result"
    assert out["steps"][0]["job_id"] == "job-1"
    assert len(cap.calls) == 1

    # Still claimed: a duplicate confirm within the cutoff cannot re-run it.
    dup = await asyncio.wait_for(execute_plan(_ctx(), "p-blip"), TIMEOUT)
    assert dup["kind"] == "error"
    assert dup["code"] == "plan_already_executing"
    assert len(cap.calls) == 1


@pytest.mark.asyncio
async def test_catastrophic_failure_marks_plan_failed_and_stays_one_shot(
    monkeypatch,
):
    """A non-step crash persists status=failed, and a re-confirm is refused —
    steps that DID run may have spawned paid work, so a retry could re-bill."""
    cap = _RecordingCap()
    register_capability(cap)
    await _save_plan("p-boom")

    def exploding_order(steps):
        raise RuntimeError("orchestration blew up")

    monkeypatch.setattr(planner_mod, "order_steps", exploding_order)
    with pytest.raises(RuntimeError, match="orchestration blew up"):
        await asyncio.wait_for(execute_plan(_ctx(), "p-boom"), TIMEOUT)
    monkeypatch.undo()

    stored = await make_plan_store().get("p-boom", "t1")
    assert stored.status == "failed"

    again = await asyncio.wait_for(execute_plan(_ctx(), "p-boom"), TIMEOUT)
    assert again["kind"] == "error"
    assert again["code"] == "plan_already_executed"
    assert cap.calls == []


# --------------------------------------------------------------------------- #
# 5. Route-level: confirm:{plan_id} runs the persisted plan
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_route_confirm_executes_plan(monkeypatch):
    """End-to-end through the HTTP route: handle → plan, then confirm → result."""
    import os

    os.environ.setdefault("OMNIX_API_KEYS", '{"test-key": "test-tenant"}')
    os.environ.setdefault("OMNIX_NEPTUNE_ENDPOINT", "http://fake:8182")
    # monkeypatch (NOT os.environ[...]=) so the key is reverted after the test
    # and never leaks into other tests' NLQueryPipeline construction.
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from cograph_client.api.app import create_app
    from cograph_client.graph.client import NeptuneClient

    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["company"],
            "scope": {"predicate": "speaks", "value": "Persian"},
            "tier": "core",
        },
    )

    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        return (["English"], "relationship")  # atomic → single enrich step

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.sample_predicate_values",
        fake_sample,
    )

    # The route uses a real executor over a mocked (empty) Neptune, so a real
    # COUNT would read 0 and the planner would (correctly) clarify "nothing
    # matched". This test exercises the confirm→execute flow, so give it a
    # non-zero matched count to get an actual plan.
    async def fake_count(self, *args, **kwargs):
        return 3

    monkeypatch.setattr(
        "cograph_client.enrichment.executor.EnrichmentExecutor.count_entities",
        fake_count,
    )

    app = create_app()
    n = AsyncMock(spec=NeptuneClient)
    n.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    n.update.return_value = None
    app.state.neptune_client = n
    client = TestClient(app)
    headers = {"X-API-Key": "test-key"}

    r1 = client.post(
        "/graphs/test-tenant/agent",
        json={
            "message": "enrich company for mentors who speak Persian",
            "context": {"kg_name": "kg1", "type_name": "Mentor"},
        },
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["kind"] == "plan"
    plan_id = body["plan_id"]

    r2 = client.post(
        "/graphs/test-tenant/agent",
        json={
            "message": "",
            "context": {"kg_name": "kg1", "type_name": "Mentor"},
            "confirm": {"plan_id": plan_id},
        },
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    result = r2.json()
    assert result["kind"] == "result"
    assert all(s["status"] == "ok" for s in result["steps"])

    # A RETRIED confirm (the client re-sending after a gateway timeout, or the
    # Explorer auto-confirm firing twice) replays the same result instead of
    # re-running the plan — no second job, no second bill.
    r3 = client.post(
        "/graphs/test-tenant/agent",
        json={
            "message": "",
            "context": {"kg_name": "kg1", "type_name": "Mentor"},
            "confirm": {"plan_id": plan_id},
        },
        headers=headers,
    )
    assert r3.status_code == 200, r3.text
    retried = r3.json()
    assert retried["kind"] == "result"
    assert retried["replayed"] is True
    assert retried["steps"] == result["steps"]


# --------------------------------------------------------------------------- #
# 6. Schema-grounded plan() extraction (COG-119)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enrich_extracts_real_attr_and_web_tier(monkeypatch):
    """'enrich the current company for mentors who speak Persian':
    - attribute is the real noun 'company' (NOT the modifier 'current'),
    - tier is 'core' (company is an open-web fact Wikidata lacks),
    - scope is the validated 'speaks' relationship = Persian.
    company is ABSENT from the schema, so it is proposed as a new attribute."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["company"],
            "scope": {"predicate": "speaks", "value": "Persian"},
            "tier": "core",
            "confidence_min": 0.85,
        },
    )

    # speaks samples are atomic → no clean-before-enrich prerequisite.
    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        return (["English", "Persian"], "relationship")

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.sample_predicate_values",
        fake_sample,
    )

    out = await asyncio.wait_for(
        handle(_ctx(), "enrich the current company for mentors who speak Persian"),
        TIMEOUT,
    )
    assert out["kind"] == "plan"
    steps = out["steps"]
    assert len(steps) == 1
    enrich_step = steps[0]
    assert enrich_step["capability"] == "enrich"
    assert enrich_step["params"]["attributes"] == ["company"]
    assert enrich_step["params"]["tier"] == "core"
    assert enrich_step["params"]["scope"] == {
        "predicate": "speaks",
        "value": "Persian",
    }


@pytest.mark.asyncio
async def test_enrich_drops_stray_modifier_word_on_fallback(monkeypatch):
    """When the LLM extraction is unavailable, the deterministic fallback still
    yields a real attribute ('company') from 'the current company' — never the
    stray modifier 'current' — and defaults the tier to the paid web 'core'."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("no llm")

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.openrouter_chat", boom
    )

    out = await asyncio.wait_for(
        handle(_ctx(), "enrich the current company"), TIMEOUT
    )
    assert out["kind"] == "plan"
    step = out["steps"][0]
    assert step["params"]["attributes"] == ["company"]
    assert step["params"]["tier"] == "core"  # web-fact backstop


@pytest.mark.asyncio
async def test_normalize_strip_emoji_on_title_is_a_plan(monkeypatch):
    """'remove emojis from the title field' → a strip_emoji PLAN on 'title',
    NOT a clarify (the old behavior, because the live sample had no emoji)."""
    _stub_classifier(monkeypatch, "clean")
    _stub_schema(monkeypatch)
    _stub_normalize_extract(
        monkeypatch,
        {
            "rule_type": "strip_emoji",
            "predicate": "title",
            "params": {},
            "confidence": 0.9,
            "rationale": "user asked to remove emoji from title",
        },
    )

    # sample_predicate_values powers the dry-run preview for the built rule.
    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        assert pred_leaf == "title"
        return (["🚀 Founder", "CTO"], "attribute")

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.normalize_cap.sample_predicate_values",
        fake_sample,
    )

    out = await asyncio.wait_for(
        handle(_ctx(), "remove emojis from the title field"), TIMEOUT
    )
    assert out["kind"] == "plan", out
    step = out["steps"][0]
    assert step["capability"] == "normalize"
    rule = step["params"]["rule"]
    assert rule["rule_type"] == "strip_emoji"
    assert rule["predicate"] == "title"
    assert step["preview"]["rule_type"] == "strip_emoji"


@pytest.mark.asyncio
async def test_normalize_list_explode_maps_languages_to_speaks(monkeypatch):
    """'split the languages into separate ones' → list_explode on the 'speaks'
    relationship (the NL phrase 'languages' maps onto the real predicate)."""
    _stub_classifier(monkeypatch, "clean")
    _stub_schema(monkeypatch)
    _stub_normalize_extract(
        monkeypatch,
        {
            "rule_type": "list_explode",
            "predicate": "speaks",
            "params": {},
            "confidence": 0.92,
            "rationale": "languages packed together",
        },
    )

    async def fake_sample(neptune, tenant_id, kg, type_name, pred_leaf):
        assert pred_leaf == "speaks"
        return (["English__Persian", "French"], "relationship")

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.normalize_cap.sample_predicate_values",
        fake_sample,
    )

    out = await asyncio.wait_for(
        handle(_ctx(), "split the languages into separate ones"), TIMEOUT
    )
    assert out["kind"] == "plan", out
    rule = out["steps"][0]["params"]["rule"]
    assert rule["rule_type"] == "list_explode"
    assert rule["predicate"] == "speaks"
    assert rule["target_kind"] == "relationship"
    assert rule["params"]["target"] == "entity"


@pytest.mark.asyncio
async def test_normalize_vague_message_still_clarifies(monkeypatch):
    """A genuinely vague instruction ('fix this data') → clarify: no field can
    be identified, the LLM returns no rule_type, and no predicate is spotted."""
    _stub_classifier(monkeypatch, "clean")
    _stub_schema(monkeypatch)
    _stub_normalize_extract(
        monkeypatch,
        {"rule_type": None, "predicate": None, "params": {}, "confidence": 0.0},
    )

    out = await asyncio.wait_for(handle(_ctx(), "fix this data"), TIMEOUT)
    assert out["kind"] == "clarify"


# --------------------------------------------------------------------------- #
# 7. COG-123 cost estimate + COG-121 confidence — the agent's plan is honest
# --------------------------------------------------------------------------- #
from cograph_client.enrichment.models import EnrichmentTier  # noqa: E402
from cograph_client.enrichment.sources.base import (  # noqa: E402
    _adapters,
    register_adapter,
)
from cograph_client.enrichment.tiers import register_tier, reset_tiers  # noqa: E402


class _CountingExecutor:
    """A FakeExecutor that also answers count_entities (the matched-count path
    the plan reuses for its cost estimate, COG-123)."""

    def __init__(self, count: int = 0, raises: bool = False):
        self.ran = []
        self._count = count
        self._raises = raises
        self.count_calls = []

    async def run(self, job, tenant_id):
        self.ran.append((job, tenant_id))

    async def count_entities(self, tenant_id, kg_name, type_name, scope=None,
                             entity_uris=None):
        self.count_calls.append((type_name, scope))
        if self._raises:
            raise RuntimeError("neptune down")
        return self._count


class _MockPaidAdapter:
    """A generic PAID adapter declaring cost via the protocol's metadata — stands
    in for a proprietary web adapter (Exa/Parallel) WITHOUT importing one. The
    cost model must derive everything from these declared attributes, never the
    name (COG-123 boundary)."""

    name = "mock_paid_web"
    is_paid = True
    cost_per_call = 0.01

    async def lookup(self, entity_label, attribute, context):
        return []


class _MockFreeAdapter:
    name = "mock_free"
    is_paid = False
    cost_per_call = 0.0

    async def lookup(self, entity_label, attribute, context):
        return []


@pytest.fixture
def _adapters_and_tiers():
    """Register mock adapters + wire tiers, restoring global state afterwards so
    the registry/tier mutations never leak into other tests."""
    saved = dict(_adapters)
    register_adapter(_MockPaidAdapter())
    register_adapter(_MockFreeAdapter())
    # core => a paid/web chain; lite => an all-free chain.
    register_tier(EnrichmentTier.core, ["cache", "mock_paid_web"])
    register_tier(EnrichmentTier.lite, ["cache", "mock_free"])
    yield
    _adapters.clear()
    _adapters.update(saved)
    reset_tiers()


@pytest.mark.asyncio
async def test_paid_tier_cost_scales_with_matched_count(monkeypatch, _adapters_and_tiers):
    """COG-123: a paid chain yields a non-zero cost that scales with the matched
    count, the plan proposes a bounded limit, and the preview no longer says
    'no paid calls'."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {"attributes": ["company"], "scope": None, "tier": "core"},
    )
    executor = _CountingExecutor(count=50)
    ctx = _ctx(executor=executor)

    out = await asyncio.wait_for(handle(ctx, "enrich company"), TIMEOUT)
    assert out["kind"] == "plan"
    step = out["steps"][0]
    cost = step["cost"]
    # 50 matched × $0.01/entity = $0.50, paid_calls = 50 (under the 200 cap).
    assert cost["paid_calls"] == 50
    assert cost["estimated_usd"] == 0.5
    assert cost["per_entity_cost_usd"] == 0.01
    assert cost["paid_calls_estimated"] is False  # exact count, not an upper bound
    assert "no paid calls" not in cost["note"].lower()
    # A bounded limit was proposed + surfaced.
    assert step["params"]["limit"] == 200
    assert step["preview"]["limit"] == 200
    # The matched-count COUNT was reused (not a new query engine).
    assert executor.count_calls == [("Mentor", None)]


@pytest.mark.asyncio
async def test_paid_cost_capped_by_limit(monkeypatch, _adapters_and_tiers):
    """COG-123: cost is bounded by the proposed limit — matched 5000 but the
    plan caps paid calls at the default limit (200)."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {"attributes": ["company"], "scope": None, "tier": "core"},
    )
    ctx = _ctx(executor=_CountingExecutor(count=5000))
    out = await asyncio.wait_for(handle(ctx, "enrich company"), TIMEOUT)
    cost = out["steps"][0]["cost"]
    assert cost["paid_calls"] == 200  # min(5000, limit)
    assert cost["estimated_usd"] == 2.0  # 200 × $0.01


@pytest.mark.asyncio
async def test_free_tier_cost_is_zero(monkeypatch, _adapters_and_tiers):
    """COG-123: an all-free chain (Wikidata-style) costs nothing, even with a
    large matched count."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        # 'lite' resolves to the all-free chain; confidence stays at the strict
        # default for a free/structured source (not a web override).
        {"attributes": ["country"], "scope": None, "tier": "lite"},
    )
    ctx = _ctx(executor=_CountingExecutor(count=1000))
    out = await asyncio.wait_for(handle(ctx, "look up the country code"), TIMEOUT)
    cost = out["steps"][0]["cost"]
    assert cost["paid_calls"] == 0
    assert cost["estimated_usd"] == 0.0
    # A free tier keeps the strict default confidence (no web override).
    assert out["steps"][0]["params"]["confidence_min"] == 0.85


@pytest.mark.asyncio
async def test_cost_falls_back_to_limit_when_count_unavailable(
    monkeypatch, _adapters_and_tiers
):
    """COG-123: when the matched COUNT can't be computed (executor raises), the
    paid cost is reported as a clearly-labeled UPPER BOUND (the cap), never a
    silent 0 for a paid tier."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {"attributes": ["company"], "scope": None, "tier": "core"},
    )
    ctx = _ctx(executor=_CountingExecutor(raises=True))
    out = await asyncio.wait_for(handle(ctx, "enrich company"), TIMEOUT)
    cost = out["steps"][0]["cost"]
    assert cost["paid_calls"] == 200          # bounded by the proposed cap
    assert cost["paid_calls_estimated"] is True  # flagged as upper-bound
    assert "up to" in cost["note"].lower()
    assert cost["estimated_usd"] == 2.0


@pytest.mark.asyncio
async def test_paid_cost_scales_with_attribute_count(monkeypatch, _adapters_and_tiers):
    """COG-123 (review fix): the executor calls the adapter chain once per
    (entity, attribute) pair, so a multi-attribute enrich must scale paid_calls
    AND the dollar estimate by len(attributes). Quoting only by entities
    under-counts by n_attributes×. This test FAILS against the entity-only
    estimate."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    # TWO paid attributes on a paid (core) chain.
    _stub_enrich_extract(
        monkeypatch,
        {"attributes": ["company", "website"], "scope": None, "tier": "core"},
    )
    executor = _CountingExecutor(count=50)
    ctx = _ctx(executor=executor)

    out = await asyncio.wait_for(handle(ctx, "enrich company and website"), TIMEOUT)
    assert out["kind"] == "plan"
    cost = out["steps"][0]["cost"]
    # 50 entities × 2 attributes = 100 paid calls; 100 × $0.01 = $1.00.
    assert cost["paid_calls"] == 100
    assert cost["estimated_usd"] == 1.0
    assert cost["attributes"] == 2
    # The note states the entities × attributes = calls basis.
    note = cost["note"].lower()
    assert "2 attributes" in note
    assert "100 paid lookups" in note


def test_estimate_cost_keys_match_web_contract():
    """MAJOR review fix: the emitted cost dict MUST use the EXACT keys the web
    plan-step cost contract reads — ``estimated_usd`` and ``paid_calls`` (see
    web/app/components/explore/useAgentChat.ts ``AgentStepCost`` and
    AgentChat.tsx ``PlanStepRow``). Asserting on the literal keys here pins the
    contract so a future rename can't silently blank the cost badge again. Covers
    both the free and paid branches of _estimate_cost."""
    from cograph_client.agent.capabilities.enrich_cap import _estimate_cost

    # Free branch (no paid adapter).
    free = _estimate_cost(
        tier=EnrichmentTier.lite,
        per_entity_cost=0.0,
        paid_adapters=0,
        has_paid=False,
        matched=10,
        matched_exact=True,
        limit=200,
        n_attributes=1,
    )
    assert "estimated_usd" in free
    assert "paid_calls" in free
    assert "estimated_cost_usd" not in free  # the old (wrong) key is gone

    # Paid branch.
    paid = _estimate_cost(
        tier=EnrichmentTier.core,
        per_entity_cost=0.01,
        paid_adapters=1,
        has_paid=True,
        matched=10,
        matched_exact=True,
        limit=200,
        n_attributes=2,
    )
    assert "estimated_usd" in paid
    assert "paid_calls" in paid
    assert "estimated_cost_usd" not in paid
    # The web UI only reads these two; confirm both are populated/typed.
    assert isinstance(paid["paid_calls"], int)
    assert isinstance(paid["estimated_usd"], float)


@pytest.mark.asyncio
async def test_web_tier_lowers_confidence_min(monkeypatch, _adapters_and_tiers):
    """COG-121: a web-sourced (paid-chain) enrich lowers confidence_min from the
    strict 0.85 default to a functional floor, surfaced in the preview, so web
    verdicts aren't all silently filtered → 0 writes."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        # No explicit confidence → the default; the plan should override it.
        {"attributes": ["company"], "scope": None, "tier": "core"},
    )
    ctx = _ctx(executor=_CountingExecutor(count=10))
    out = await asyncio.wait_for(handle(ctx, "enrich company"), TIMEOUT)
    step = out["steps"][0]
    assert step["params"]["confidence_min"] == 0.4  # functional web floor
    assert step["preview"]["confidence_min"] == 0.4
    assert "confidence_min lowered" in step["preview"]["confidence_note"]


@pytest.mark.asyncio
async def test_user_confidence_not_overridden_on_web_tier(
    monkeypatch, _adapters_and_tiers
):
    """COG-121: an EXPLICIT user confidence is respected even on a web tier — we
    only override the unset (default 0.85) value."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {
            "attributes": ["company"],
            "scope": None,
            "tier": "core",
            "confidence_min": 0.9,  # user asked for stricter
        },
    )
    ctx = _ctx(executor=_CountingExecutor(count=10))
    out = await asyncio.wait_for(handle(ctx, "enrich company strictly"), TIMEOUT)
    step = out["steps"][0]
    assert step["params"]["confidence_min"] == 0.9  # respected, not lowered


@pytest.mark.asyncio
async def test_plan_limit_carried_into_enrich_job(monkeypatch, _adapters_and_tiers):
    """COG-123: the proposed limit is honored at execute time — the EnrichJob the
    capability builds carries the cap."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_schema(monkeypatch)
    _stub_enrich_extract(
        monkeypatch,
        {"attributes": ["company"], "scope": None, "tier": "core"},
    )
    job_store = FakeJobStore()
    executor = _CountingExecutor(count=10)
    ctx = _ctx(executor=executor, job_store=job_store)

    plan_out = await asyncio.wait_for(handle(ctx, "enrich company"), TIMEOUT)
    result = await asyncio.wait_for(
        execute_plan(ctx, plan_out["plan_id"]), TIMEOUT
    )
    assert result["kind"] == "result"
    await asyncio.sleep(0)
    assert len(job_store.created) == 1
    job = job_store.created[0]
    assert job.limit == 200
    assert abs(job.confidence_min - 0.4) < 1e-9  # web floor carried through


# --------------------------------------------------------------------------- #
# 8. Dedup capability (COG-122) — registered → plans + drives the ER engine
# --------------------------------------------------------------------------- #
from cograph_client.agent.capabilities.dedup_cap import DedupCapability  # noqa: E402
from cograph_client.enrichment.models import JobCategory, JobStatus  # noqa: E402


def test_dedup_capability_is_registered_by_default():
    """register_default_capabilities() appends DedupCapability → the single
    endpoint can dispatch 'dedup' with no route change."""
    names = {c.name for c in get_capabilities()}
    assert "dedup" in names
    cap = get_capability("dedup")
    assert isinstance(cap, DedupCapability)


class _TypedNeptune(FakeNeptune):
    """Neptune fake whose query() returns the given rdf:type URIs so the dedup
    plan can enumerate ER-enabled types for its preview."""

    def __init__(self, type_uris: list[str]):
        super().__init__()
        self._type_uris = type_uris

    async def query(self, q):
        return {
            "head": {"vars": ["t"]},
            "results": {"bindings": [{"t": {"value": u}} for u in self._type_uris]},
        }


@pytest.mark.asyncio
async def test_dedup_routes_to_plan_with_er_types(monkeypatch):
    """'merge the duplicates' → a dedup PLAN (not clarify), grounded in the KG's
    real ER-enabled types. 'Person' resolves to an ERConfig (kept); 'Skill' does
    not (filtered out)."""
    _stub_classifier(monkeypatch, "dedup")
    prefix = "https://cograph.tech/types/"
    neptune = _TypedNeptune([f"{prefix}Person", f"{prefix}Skill"])

    out = await asyncio.wait_for(
        handle(_ctx(neptune=neptune), "merge the duplicate people"), TIMEOUT
    )
    assert out["kind"] == "plan"
    assert len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["capability"] == "dedup"
    assert step["action"] == "run_dedup"
    assert step["params"]["kg_name"] == "kg1"
    # Only ER-enabled types are previewed; 'Skill' has no ERConfig → dropped.
    assert step["params"]["er_types"] == ["Person"]
    # Dedup is compute, not paid web calls.
    assert step["cost"]["paid_calls"] == 0
    assert step["cost"]["estimated_usd"] == 0.0


@pytest.mark.asyncio
async def test_dedup_plan_degrades_when_type_enum_fails(monkeypatch):
    """If type enumeration raises (Neptune down), the plan still proposes a dedup
    step with an empty er_types list rather than failing."""
    _stub_classifier(monkeypatch, "dedup")

    class _BoomNeptune(FakeNeptune):
        async def query(self, q):
            raise RuntimeError("neptune down")

    out = await asyncio.wait_for(
        handle(_ctx(neptune=_BoomNeptune()), "find and merge duplicates"), TIMEOUT
    )
    assert out["kind"] == "plan"
    step = out["steps"][0]
    assert step["capability"] == "dedup"
    assert step["params"]["er_types"] == []
    assert "all ER-enabled types" in step["preview"]["summary"]


@pytest.mark.asyncio
async def test_dedup_execute_drives_rebuild_engine(monkeypatch):
    """execute() creates a dedupe-category job and drives the EXISTING ER engine
    (rebuild_kg) as a tracked background worker, then records the merge volume."""
    captured: dict = {}

    async def fake_rebuild_kg(client, instance_graph):
        captured["instance_graph"] = instance_graph
        return {
            "types": [{"type": "Person", "fragments_absorbed": 7}],
            "fragments_absorbed_total": 7,
        }

    # Patch the engine entry point + the recompute hook the worker imports
    # lazily from the route module.
    monkeypatch.setattr(
        "cograph_client.resolver.er.rebuild.rebuild_kg", fake_rebuild_kg
    )

    recompute_calls: list = []
    monkeypatch.setattr(
        "cograph_client.api.routes.explore.schedule_recompute",
        lambda client, tenant_id, kg_name: recompute_calls.append((tenant_id, kg_name)),
    )

    job_store = FakeJobStore()
    ctx = _ctx(job_store=job_store)
    cap = get_capability("dedup")

    plan = await asyncio.wait_for(cap.plan(ctx, "merge duplicates"), TIMEOUT)
    ack = await asyncio.wait_for(cap.execute(ctx, plan[0]), TIMEOUT)

    assert ack["kind"] == "ack"
    assert ack["capability"] == "dedup"
    assert ack["job_id"]
    # A dedupe-category job was created in the queued state.
    assert len(job_store.created) == 1
    job = job_store.created[0]
    assert job.category == JobCategory.dedupe
    assert job.type_name == ""  # KG-wide, not type-scoped

    # Let the spawned background rebuild worker run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The engine ran against the KG's instance graph (the same primitive the
    # er-rebuild route uses), the job landed in 'applied' with the merge volume
    # recorded, and a type-stats recompute was scheduled.
    assert captured["instance_graph"] == "https://cograph.tech/graphs/t1/kg/kg1"
    assert job.status == JobStatus.applied
    assert job.progress.processed == 7
    assert "merged 7 duplicate fragment" in (job.error or "")
    assert recompute_calls == [("t1", "kg1")]


@pytest.mark.asyncio
async def test_dedup_execute_records_failure(monkeypatch):
    """If the rebuild engine raises, the worker records a failed job (detached —
    the error is captured on the job, never propagated)."""

    async def boom_rebuild(client, instance_graph):
        raise RuntimeError("merge blew up")

    monkeypatch.setattr(
        "cograph_client.resolver.er.rebuild.rebuild_kg", boom_rebuild
    )
    monkeypatch.setattr(
        "cograph_client.api.routes.explore.schedule_recompute",
        lambda *a, **k: None,
    )

    job_store = FakeJobStore()
    ctx = _ctx(job_store=job_store)
    cap = get_capability("dedup")
    plan = await asyncio.wait_for(cap.plan(ctx, "dedupe"), TIMEOUT)
    await asyncio.wait_for(cap.execute(ctx, plan[0]), TIMEOUT)

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    job = job_store.created[0]
    assert job.status == JobStatus.failed
    assert "dedup failed" in (job.error or "")


@pytest.mark.asyncio
async def test_dedup_execute_requires_job_store():
    """execute() raises a clear error when the job store isn't in the context."""
    ctx = AgentContext(
        tenant_id="t1", kg_name="kg1", neptune=FakeNeptune(), type_name="Person",
        extras={},
    )
    cap = get_capability("dedup")
    step = PlanStep(capability="dedup", action="run_dedup", params={"kg_name": "kg1"})
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(cap.execute(ctx, step), TIMEOUT)


@pytest.mark.asyncio
async def test_dedup_execute_via_plan_store(monkeypatch):
    """End-to-end through the planner: handle → dedup plan, confirm → result with
    an 'ok' step that drove the engine (the only mutating path)."""
    _stub_classifier(monkeypatch, "dedup")

    async def fake_rebuild_kg(client, instance_graph):
        return {"types": [], "fragments_absorbed_total": 0}

    monkeypatch.setattr(
        "cograph_client.resolver.er.rebuild.rebuild_kg", fake_rebuild_kg
    )
    monkeypatch.setattr(
        "cograph_client.api.routes.explore.schedule_recompute",
        lambda *a, **k: None,
    )

    job_store = FakeJobStore()
    ctx = _ctx(job_store=job_store)
    plan_out = await asyncio.wait_for(handle(ctx, "merge duplicates"), TIMEOUT)
    assert plan_out["kind"] == "plan"
    result = await asyncio.wait_for(execute_plan(ctx, plan_out["plan_id"]), TIMEOUT)
    assert result["kind"] == "result"
    assert [s["status"] for s in result["steps"]] == ["ok"]
    assert result["steps"][0]["capability"] == "dedup"
    await asyncio.sleep(0)
    assert len(job_store.created) == 1
    assert job_store.created[0].category == JobCategory.dedupe


# --- discovery-intent guard: no false-positive on "not enrichment" adjective --- #
# Regression for the persona-eval RCA review nit: the deterministic discovery
# guard must NOT force-route a read-only ask into discovery just because it uses
# "enrichment" as an ADJECTIVE ("not enrichment candidates"). Invented tokens
# (Widget/Sprocket/Gadget) so nothing overfits.


@pytest.mark.parametrize(
    "message",
    [
        "Show me all records that are not enrichment candidates",
        "List the Widgets that are not enrichment candidates",
        "give me the Sprockets that are not enrichment targets",
        "show me all Gadgets that are not enrichment matches",
    ],
)
def test_discovery_guard_not_forced_by_enrichment_adjective(message):
    """A read-only ask that says "not enrichment <noun>" is NOT a discovery job —
    'enrichment' is an adjective here, and 'show me'/'list'/'give me' are read-only
    display verbs. The guard must leave these to normal classification."""
    from cograph_client.agent.planner import _is_web_discovery_request

    assert _is_web_discovery_request(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "discover all Gadgets in Zone 3, this is not enrichment",
        "This is a new discovery task, not enrichment - find Gadgets in Zone 3",
        "find all Sprockets in Zone 9, this is a new discovery",
        "scrape Widgets from example.test, not enrichment.",
        "add all Gadgets from the web",
        # MID-SENTENCE positive self-labels must still force-route (they were
        # over-narrowed by the first clause-boundary anchor; the review nit fix
        # restores them). "new discovery" / "discovery task" are always positive.
        "new discovery run of Widgets",
        "kick off a discovery task now",
        "discovery task for Sprockets",
        "kick off a discovery task for Sprockets now",
    ],
)
def test_discovery_guard_still_fires_on_genuine_self_label(message):
    """A genuine "this is a new discovery / not enrichment" self-label (the "not
    enrichment" one at a clause boundary; "new discovery"/"discovery task" anywhere,
    including mid-sentence), a leading discover/scrape imperative, or a '... from the
    web' fetch still force-routes to discovery — the nit fix must not break the real
    path, nor under-trigger on a mid-sentence positive self-label."""
    from cograph_client.agent.planner import _is_web_discovery_request

    assert _is_web_discovery_request(message) is True


# --------------------------------------------------------------------------- #
# Session-context bleed (invented tokens — assert the MECHANISM, not a domain)
# --------------------------------------------------------------------------- #
# Three defects amplified one real persona-eval failure where a COMPLETED prior
# request's text was replayed into a new, unrelated request:
#   1. planner._effective_instruction concatenated EVERY prior user turn, so a
#      finished ask bled into the next one.
#   2. enrich_cap._resolve_target_type let the longest type named anywhere in the
#      (bled) instruction win, overriding the type named in the LIVE turn.
#   3. enrich_cap._validate_enrich_request garbled a crammed multi-attribute list
#      into one token and let hallucinated attributes past validation.
# These tests pin each fix with invented tokens.


def _turn(role, text, kind=None):
    from cograph_client.agent.conversation_store import Turn

    return Turn(role=role, text=text, kind=kind)


def test_effective_instruction_drops_completed_prior_request():
    """A committed plan RESETS the accumulation window: a completed prior request
    ("enrich Widget entities") does NOT bleed into a new one ("discover Sprocket
    entities"). Only the current message survives."""
    history = [
        _turn("user", "enrich Widget entities with their price"),
        _turn("assistant", "Proposed a plan (enrich).", kind="plan"),
    ]
    eff = planner_mod._effective_instruction(history, "discover Sprocket entities")
    assert eff == "discover Sprocket entities"
    assert "Widget" not in eff  # the finished prior intent is not replayed


def test_effective_instruction_resets_after_answered_question():
    """An answered question is a committed boundary too — it does not bleed into
    the next, unrelated request."""
    history = [
        _turn("user", "how many Widget entities are there?"),
        _turn("assistant", "There are 5.", kind="answer"),
    ]
    eff = planner_mod._effective_instruction(history, "enrich Sprocket weights")
    assert eff == "enrich Sprocket weights"
    assert "Widget" not in eff


def test_effective_instruction_keeps_open_clarify_chain():
    """A clarify does NOT close the window: the field named before the clarify
    still accumulates so a terse answer converges (the COG-130 behavior we must
    preserve)."""
    history = [
        _turn("user", "clean the Alpha field"),
        _turn("assistant", "Clean the values or split them?", kind="clarify"),
    ]
    eff = planner_mod._effective_instruction(history, "split them")
    assert "clean the Alpha field" in eff  # survives the clarify
    assert "split them" in eff
    # The current message is last so it dominates.
    assert eff.splitlines()[-1] == "split them"


def test_effective_instruction_window_starts_after_last_committed_turn():
    """Only turns SINCE the last committed plan/answer accumulate. An earlier,
    finished request is excluded; the open clarify chain after it is kept."""
    history = [
        _turn("user", "enrich Widget prices"),  # finished ask
        _turn("assistant", "Proposed a plan (enrich).", kind="plan"),  # boundary
        _turn("user", "clean the Alpha field"),  # new open ask
        _turn("assistant", "Clean or split?", kind="clarify"),  # window stays open
    ]
    eff = planner_mod._effective_instruction(history, "split it")
    assert "Widget" not in eff  # the pre-boundary request is dropped
    assert "clean the Alpha field" in eff and "split it" in eff


@pytest.mark.asyncio
async def test_completed_prior_request_does_not_bleed_through_planner(monkeypatch):
    """End-to-end through handle(): a COMPLETED prior enrich (a plan was proposed)
    must not replay into a later enrich on a different type. The new turn targets
    the new type and the accumulated instruction the enrich cap sees drops the
    prior request's text."""
    _stub_classifier(monkeypatch, "enrich")
    _stub_kg_types(monkeypatch, ["Widget", "Sprocket"])

    async def fake_schema(neptune, tenant_id, type_name):
        return {"attributes": ["price", "weight"], "relationships": []}

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )

    captured: dict = {}

    async def fake_extract(ctx, instruction, type_name, schema):
        captured["instruction"] = instruction
        captured["type_name"] = type_name
        return {
            "attributes": ["weight"],
            "scope": None,
            "subset": None,
            "tier": "core",
            "confidence_min": 0.85,
        }

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap._extract_enrich_request",
        fake_extract,
    )

    ctx = _ctx()
    session = {"id": "bleed-sess"}
    t1 = await asyncio.wait_for(
        handle(ctx, "enrich Widget entities with their price", session), TIMEOUT
    )
    assert t1["kind"] == "plan"  # a committed plan → resets the window
    t2 = await asyncio.wait_for(
        handle(ctx, "enrich Sprocket entities with their weight", session), TIMEOUT
    )
    assert t2["kind"] == "plan"
    assert captured["type_name"] == "Sprocket"  # the live turn's type won
    # The completed Widget request did NOT bleed into the new instruction.
    assert "Widget" not in captured["instruction"]
    assert captured["instruction"] == "enrich Sprocket entities with their weight"


# --------------------------------------------------------------------------- #
# Explicit / live-turn type wins over a stale mention (2a)
# --------------------------------------------------------------------------- #
def test_live_message_type_beats_stale_history_mention():
    """The type named in the CURRENT message wins over one lingering in the
    accumulated instruction — an explicit live target must not be overridden by a
    stale mention (even a longer one)."""
    from cograph_client.agent.capabilities.enrich_cap import _resolve_target_type

    resolved = _resolve_target_type(
        instruction="enrich BetaGadget entities\nnow enrich Alpha entities",
        known_types=["Alpha", "BetaGadget"],
        selected=None,
        current_message="now enrich Alpha entities",
    )
    # Without the fix, "longest type named anywhere" would pick BetaGadget.
    assert resolved == "Alpha"


def test_open_ask_type_beats_wrong_selection():
    """When the current message names no type, the type named in the open ask
    (accumulated instruction) is used — and it beats a wrong UI selection, so a
    clarify-chain reply resolves the right type."""
    from cograph_client.agent.capabilities.enrich_cap import _resolve_target_type

    resolved = _resolve_target_type(
        instruction="enrich Alpha entities\nall of them",
        known_types=["Alpha", "Beta"],
        selected="Beta",  # a wrong UI selection must not win
        current_message="all of them",
    )
    assert resolved == "Alpha"


def test_resolve_target_type_backward_compatible_without_current_message():
    """Omitting current_message (a direct/legacy call) collapses to the prior
    instruction-first behavior — existing callers are unaffected."""
    from cograph_client.agent.capabilities.enrich_cap import _resolve_target_type

    assert (
        _resolve_target_type("enrich Alpha entities", ["Alpha", "Beta"], "Beta")
        == "Alpha"
    )


# --------------------------------------------------------------------------- #
# Multi-attribute parsing + schema intersection (2b)
# --------------------------------------------------------------------------- #
def test_validate_enrich_intersects_multi_attrs_with_schema():
    """[foo, bar, baz] on a schema with {foo, bar, qux} → {foo, bar}: baz is
    dropped as a non-member (real fields present → strict intersection)."""
    from cograph_client.agent.capabilities.enrich_cap import _validate_enrich_request

    out = _validate_enrich_request(
        {"attributes": ["foo", "bar", "baz"]},
        attr_names=["foo", "bar", "qux"],
        rel_names=[],
        type_name="Widget",
    )
    assert out["attributes"] == ["foo", "bar"]


def test_validate_enrich_splits_crammed_list_not_one_garbled_token():
    """A crammed list + a stray "attributes:" label in ONE string is parsed into
    the individual real fields, NOT fused into a single garbled token."""
    from cograph_client.agent.capabilities.enrich_cap import _validate_enrich_request

    out = _validate_enrich_request(
        {"attributes": ["attributes: foo, bar, qux"]},
        attr_names=["foo", "bar", "qux"],
        rel_names=[],
        type_name="Widget",
    )
    assert set(out["attributes"]) == {"foo", "bar", "qux"}
    assert all("attributes_" not in a for a in out["attributes"])  # not garbled


def test_validate_enrich_drops_hallucinated_and_type_name_attrs():
    """A real field mixed with a hallucinated attr and the TYPE NAME itself keeps
    only the real field — the hallucinations are dropped."""
    from cograph_client.agent.capabilities.enrich_cap import _validate_enrich_request

    out = _validate_enrich_request(
        {"attributes": ["foo", "data_from", "Widget"]},
        attr_names=["foo", "bar"],
        rel_names=[],
        type_name="Widget",
    )
    assert out["attributes"] == ["foo"]


def test_validate_enrich_keeps_new_attribute_when_none_in_schema():
    """When NO extracted attr matches the schema, the user is naming a brand-new
    attribute to add — keep the clean noun, but never the type name itself."""
    from cograph_client.agent.capabilities.enrich_cap import _validate_enrich_request

    out = _validate_enrich_request(
        {"attributes": ["company", "Widget"]},
        attr_names=["foo", "bar"],
        rel_names=[],
        type_name="Widget",
    )
    assert out["attributes"] == ["company"]  # new attr kept; type name dropped


def test_validate_enrich_empty_schema_keeps_named_attrs():
    """An empty/uningested schema can't validate members, so clean named attrs are
    kept (minus the type name) — a brand-new type is still enrichable."""
    from cograph_client.agent.capabilities.enrich_cap import _validate_enrich_request

    out = _validate_enrich_request(
        {"attributes": ["foo", "bar"]},
        attr_names=[],
        rel_names=[],
        type_name="Widget",
    )
    assert out["attributes"] == ["foo", "bar"]
