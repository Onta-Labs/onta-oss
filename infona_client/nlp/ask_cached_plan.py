"""Cached-plan replay for the zero-key first run (ONTA-544).

Used **only** when no LLM key / local model is configured (or when
``INFONA_ASK_CACHED_PLAN=1``). Production ``/ask`` stays always-LLM Cypher
whenever a model is configured. This path executes a stored Cypher plan
against the prebuilt trials snapshot — it is not live inference.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from infona_client.graph.scope import GraphScope
from infona_client.graph.store import GraphQueryError, GraphRecord
from infona_client.models.query import NLResult
from infona_client.nlp.cypher_schema import records_to_bindings
from infona_client.nlp.cypher_scope import confine_generated_cypher

REPLAY_LABEL = "cached-plan replay — not live inference"
CACHED_PLAN_ENV = "INFONA_ASK_CACHED_PLAN"
HERO_QUESTION = "Which Phase 3 NSCLC trials is AstraZeneca running?"
HERO_KG = "trials"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAN_FILE = _REPO_ROOT / "examples" / "prebuilt" / "ask_plan_flaura2.json"
_PLACEHOLDER_KEYS = frozenset(
    {
        "sk-or-...",
        "sk-ant-...",
        "csk-...",
        "changeme",
        "your-key-here",
        "replace-me",
        "changemeplease",
    }
)
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_question(question: str) -> str:
    q = _PUNCT_RE.sub(" ", (question or "").strip().lower())
    return _WS_RE.sub(" ", q).strip()


def _looks_like_real_key(raw: str | None) -> bool:
    key = (raw or "").strip()
    if not key:
        return False
    if key.lower() in _PLACEHOLDER_KEYS or key.endswith("..."):
        return False
    return True


def llm_configured(pipeline: Any | None = None) -> bool:
    """True when a real query model can be called (always-LLM /ask)."""
    if _looks_like_real_key(os.environ.get("OPENROUTER_API_KEY")):
        return True
    if _looks_like_real_key(os.environ.get("INFONA_OPENROUTER_API_KEY")):
        return True
    if _looks_like_real_key(os.environ.get("CEREBRAS_API_KEY")):
        return True
    if _looks_like_real_key(os.environ.get("INFONA_CEREBRAS_API_KEY")):
        return True
    if _looks_like_real_key(os.environ.get("INFONA_ANTHROPIC_API_KEY")):
        return True
    if _looks_like_real_key(os.environ.get("ANTHROPIC_API_KEY")):
        return True
    if (os.environ.get("INFONA_LLM_BASE_URL") or "").strip():
        return True
    if (os.environ.get("INFONA_QUERY_BASE_URL") or "").strip():
        return True
    if pipeline is None:
        return False
    if _looks_like_real_key(getattr(pipeline, "_openrouter_key", None)):
        return True
    if _looks_like_real_key(getattr(pipeline, "_cerebras_key", None)):
        return True
    try:
        anth = getattr(pipeline, "anthropic", None)
        if anth is not None and _looks_like_real_key(getattr(anth, "api_key", None)):
            return True
    except Exception:
        pass
    return False


def cached_plan_enabled(pipeline: Any | None = None) -> bool:
    """Replay only when forced, or when no model is configured."""
    flag = (os.environ.get(CACHED_PLAN_ENV) or "").strip().lower()
    if flag in {"1", "true", "yes", "on", "force"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return not llm_configured(pipeline)


def plan_path() -> Path:
    override = (os.environ.get("INFONA_CACHED_PLAN_PATH") or "").strip()
    if override:
        return Path(override)
    return _PLAN_FILE


def default_plan() -> dict[str, Any]:
    """Baked-in hero plan so the API image works without ``examples/``."""
    return {
        "id": "hero-az-phase3-nsclc-active",
        "question": HERO_QUESTION,
        "kg": HERO_KG,
        "replay_label": REPLAY_LABEL,
        "explanation": (
            "Replayed a stored Cypher plan over the prebuilt trials graph "
            "(AstraZeneca × Phase 3 × NSCLC × Active). "
            "This is not live LLM inference."
        ),
        "cypher": (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})"
            "-[:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg}) "
            "WHERE c.name IN $type_names OR c.id IN $type_names "
            "MATCH (a_s:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e) "
            "MATCH (a_s)-[:OBJECT]->(sponsor:Entity {tenant_id: $tenant_id, kg: $kg}) "
            "MATCH (a_s)-[:PREDICATE]->(p_s:Property {tenant_id: $tenant_id, kg: $kg}) "
            "WHERE p_s.name = $sponsor_attr AND ("
            "toLower(coalesce(sponsor.display_name, '')) = toLower($sponsor_name) "
            "OR toLower(coalesce(sponsor.name, '')) = toLower($sponsor_name)) "
            "MATCH (a_i:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e) "
            "MATCH (a_i)-[:OBJECT]->(ind:Entity {tenant_id: $tenant_id, kg: $kg}) "
            "MATCH (a_i)-[:PREDICATE]->(p_i:Property {tenant_id: $tenant_id, kg: $kg}) "
            "WHERE p_i.name = $indication_attr AND ("
            "toLower(coalesce(ind.display_name, '')) = toLower($indication_name) "
            "OR toLower(coalesce(ind.name, '')) = toLower($indication_name)) "
            "MATCH (a_p:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
            "-[:PREDICATE]->(p_p:Property {tenant_id: $tenant_id, kg: $kg}) "
            "WHERE p_p.name = $phase_key AND toString(a_p.literal_value) = $phase_value "
            "MATCH (a_st:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
            "-[:PREDICATE]->(p_st:Property {tenant_id: $tenant_id, kg: $kg}) "
            "WHERE p_st.name = $status_key AND toString(a_st.literal_value) = $status_value "
            "RETURN DISTINCT e.id AS id, coalesce(e.title, e.name) AS title, "
            "e.primary_type AS primary_type ORDER BY e.id LIMIT $limit"
        ),
        "params": {
            "type_names": ["Trial"],
            "sponsor_attr": "sponsor",
            "sponsor_name": "AstraZeneca",
            "indication_attr": "indication",
            "indication_name": "NSCLC",
            "phase_key": "phase",
            "phase_value": "Phase 3",
            "status_key": "status",
            "status_value": "Active",
            "limit": 25,
        },
        "steps": [
            {
                "template": "related_entity_name_filter",
                "params": {
                    "type_names": ["Trial"],
                    "rel_attr": "sponsor",
                    "target_name": "AstraZeneca",
                    "limit": 50,
                },
            },
            {
                "template": "related_entity_name_filter",
                "params": {
                    "type_names": ["Trial"],
                    "rel_attr": "indication",
                    "target_name": "NSCLC",
                    "limit": 50,
                },
            },
            {
                "template": "literal_values",
                "params": {
                    "type_names": ["Trial"],
                    "prop_key": "phase",
                    "prop_value": "Phase 3",
                    "limit": 50,
                },
            },
            {
                "template": "literal_values",
                "params": {
                    "type_names": ["Trial"],
                    "prop_key": "status",
                    "prop_value": "Active",
                    "limit": 50,
                },
            },
        ],
        "intersect_on": "id",
    }


def load_plan(path: Path | None = None) -> dict[str, Any]:
    dest = path or plan_path()
    if dest.is_file():
        data = json.loads(dest.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("cypher"):
            return data
    return default_plan()


def match_cached_plan(
    question: str,
    *,
    kg_name: str = "",
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the stored plan only for the exact (normalized) hero question."""
    payload = dict(plan) if plan is not None else load_plan()
    wanted_kg = str(payload.get("kg") or HERO_KG)
    if kg_name and wanted_kg and kg_name != wanted_kg:
        return None
    if normalize_question(question) != normalize_question(
        str(payload.get("question") or HERO_QUESTION)
    ):
        return None
    return payload


def _record_id(rec: GraphRecord | Mapping[str, Any], key: str) -> str:
    getter = rec.get if hasattr(rec, "get") else lambda k, d=None: rec[k]  # type: ignore[index]
    val = getter(key)
    if val is None:
        val = getter("id")
    return str(val or "")


def _intersect(
    batches: list[list[GraphRecord]],
    *,
    key: str,
) -> list[GraphRecord]:
    if not batches:
        return []
    first = batches[0]
    if len(batches) == 1:
        return list(first)
    allowed: set[str] | None = None
    for batch in batches[1:]:
        ids = {_record_id(r, key) for r in batch}
        ids.discard("")
        allowed = ids if allowed is None else (allowed & ids)
    if allowed is None:
        return list(first)
    return [r for r in first if _record_id(r, key) in allowed]


def _title_records(records: list[GraphRecord]) -> list[GraphRecord]:
    out: list[GraphRecord] = []
    for rec in records:
        data = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
        title = data.get("title") or data.get("name") or data.get("id")
        out.append(GraphRecord(data={"title": title}))
    return out


async def execute_cached_plan(
    store: Any,
    plan: Mapping[str, Any],
    *,
    tenant_id: str,
    kg_name: str,
) -> tuple[list[GraphRecord], str]:
    """Run the stored Cypher, falling back to allowlisted template steps."""
    session = store.session(GraphScope.for_instance(tenant_id, kg_name))
    params = dict(plan.get("params") or {})
    cypher = str(plan.get("cypher") or "").strip()
    if cypher:
        confined, forced = confine_generated_cypher(
            cypher, tenant_id=tenant_id, kg=kg_name, params=params
        )
        try:
            records = await session.execute_read(confined, forced)
            return list(records), "cached_plan"
        except GraphQueryError:
            pass
        except Exception:
            pass

    steps = list(plan.get("steps") or [])
    if not steps:
        return [], "cached_plan_empty"
    batches: list[list[GraphRecord]] = []
    for step in steps:
        template = str(step.get("template") or "")
        if not template:
            continue
        step_params = dict(step.get("params") or {})
        rows = await session.execute_template(template, step_params)
        batches.append(list(rows))
    key = str(plan.get("intersect_on") or "id")
    return _intersect(batches, key=key), "cached_plan_steps"


def _banner(plan: Mapping[str, Any]) -> str:
    return str(plan.get("replay_label") or REPLAY_LABEL)


def format_replay_answer(body: str, plan: Mapping[str, Any]) -> str:
    text = (body or "").strip() or "No results found."
    return f"{text}\n\n({_banner(plan)})"


async def try_cached_plan_ask(pipeline: Any, st: Any) -> NLResult | None:
    """Hook for ``_ask_cypher``: replay or ``None`` to continue always-LLM."""
    if not cached_plan_enabled(pipeline):
        return None
    store = getattr(st, "store", None)
    if store is None:
        return None
    question = str(getattr(st, "question", "") or "")
    kg_name = str(getattr(st, "kg_name", "") or "")
    plan = match_cached_plan(question, kg_name=kg_name)
    if plan is None:
        return None

    t0 = float(getattr(st, "t0", None) or time.time())
    timing = dict(getattr(st, "timing", None) or {})
    tenant_id = str(getattr(st, "tenant_id", "") or "")
    t_exec = time.time()
    records, exec_path = await execute_cached_plan(
        store, plan, tenant_id=tenant_id, kg_name=kg_name
    )
    timing["cypher_exec_path"] = exec_path
    timing["cypher_exec_ms"] = round((time.time() - t_exec) * 1000, 1)
    timing["cached_plan_replay"] = 1.0
    timing["cached_plan_id"] = str(plan.get("id") or "")
    timing["query_language"] = "cypher"
    timing["model"] = "cached-plan"
    titles = _title_records(records)
    _vars, bindings = records_to_bindings(titles)
    timing["rows"] = float(len(bindings))
    formatter = getattr(pipeline, "_format_answer", None)
    if callable(formatter):
        body = await formatter(bindings, str(plan.get("explanation") or ""))
    elif bindings:
        body = "\n".join(str(next(iter(row.values()))) for row in bindings if row)
    else:
        body = "No results found."
    timing["total_ms"] = round((time.time() - t0) * 1000, 1)
    return NLResult(
        answer=format_replay_answer(str(body), plan),
        sparql=str(plan.get("cypher") or ""),
        explanation=str(plan.get("explanation") or _banner(plan)),
        timing=timing,
        token_usage=[],
    )
