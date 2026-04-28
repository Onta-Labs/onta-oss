"""Failure diagnosis for the multi-layer eval-fix loop.

Triages each failed eval question into one of three layers:
  - INGESTION: data is wrong/missing in the graph (needs reingest)
  - ONTOLOGY: graph structure is wrong (needs SPARQL UPDATE)
  - QUERY: SPARQL generation is wrong (needs prompt fix)

Three-stage pipeline per question:
  Stage A — Graph Probe: deterministic SPARQL queries against Neptune (~200ms)
  Stage B — Pattern Matching: rules-based classification (instant)
  Stage C — LLM Diagnosis: for ambiguous cases only (~500ms)
"""

import json
import os
import re
from dataclasses import dataclass, field

import httpx


@dataclass
class FailureDiagnosis:
    """Diagnosis result for a single failed eval question."""
    question: str
    layer: str  # "ingestion" | "ontology" | "query" | "ambiguous"
    sub_category: str  # "pipe_splitting" | "normalization" | "missing_predicate" | "wrong_filter" | ...
    confidence: float  # 0.0-1.0
    evidence: str  # human-readable explanation of diagnosis
    signature: str  # deterministic hash for pattern grouping
    affected_entity: str = ""  # e.g., "ClinicalTrial.phase"
    fix_type: str = ""  # "reingest" | "sparql_update" | "prompt_edit"


async def diagnose_failure(
    question_result: dict,
    api_url: str,
    api_key: str,
    tenant: str,
    kg_name: str,
    openrouter_key: str = "",
) -> FailureDiagnosis:
    """Diagnose why a question failed. Runs Stage A + B, falls back to C.

    Args:
        question_result: QuestionResult dict from eval report
        api_url: Omnix API base URL
        api_key: API authentication key
        tenant: Tenant ID
        kg_name: Knowledge graph name
        openrouter_key: For Stage C LLM fallback

    Returns:
        FailureDiagnosis with layer classification and evidence
    """
    question = question_result.get("question", "")
    expected = str(question_result.get("expected", ""))
    answer = str(question_result.get("answer", ""))
    sparql = question_result.get("sparql", "")
    failure_cat = question_result.get("failure_category", "")
    explanation = question_result.get("explanation", "")

    # Stage A: Graph Probes
    diagnosis = await _stage_a_graph_probe(
        question, expected, answer, sparql,
        api_url, api_key, tenant, kg_name,
    )
    if diagnosis and diagnosis.confidence >= 0.7:
        return diagnosis

    # Stage B: Pattern Matching
    diagnosis_b = _stage_b_pattern_match(
        question, expected, answer, sparql, failure_cat, explanation,
    )
    if diagnosis_b and diagnosis_b.confidence >= 0.6:
        return diagnosis_b

    # Merge signals from A and B
    if diagnosis and diagnosis_b:
        # Prefer the higher confidence one
        best = diagnosis if diagnosis.confidence >= diagnosis_b.confidence else diagnosis_b
        if best.confidence >= 0.5:
            return best

    # Stage C: LLM Diagnosis (for ambiguous)
    if openrouter_key:
        diagnosis_c = await _stage_c_llm_diagnosis(
            question, expected, answer, sparql, failure_cat, explanation,
            openrouter_key,
        )
        if diagnosis_c:
            return diagnosis_c

    # Default: classify as query issue (prompt fix is cheapest to try)
    return FailureDiagnosis(
        question=question,
        layer="query",
        sub_category="unknown",
        confidence=0.3,
        evidence="Could not determine root cause, defaulting to query layer",
        signature=f"unknown_{_hash_question(question)}",
        fix_type="prompt_edit",
    )


async def diagnose_all_failures(
    report: dict,
    api_url: str,
    api_key: str,
    tenant: str,
    kg_name: str,
    openrouter_key: str = "",
) -> list[FailureDiagnosis]:
    """Diagnose all failed questions in an eval report."""
    import asyncio

    results = report.get("queries", {}).get("results", [])
    failures = [r for r in results if r.get("verdict") != "correct"]

    # Skip HTTP errors (infrastructure, not fixable)
    actionable = []
    for f in failures:
        ans = str(f.get("answer", ""))
        if "HTTP 429" in ans or "HTTP 500" in ans or "ReadTimeout" in ans:
            continue
        actionable.append(f)

    tasks = [
        diagnose_failure(f, api_url, api_key, tenant, kg_name, openrouter_key)
        for f in actionable
    ]
    return list(await asyncio.gather(*tasks))


# ---------------------------------------------------------------------------
# Stage A: Graph Probes
# ---------------------------------------------------------------------------

async def _stage_a_graph_probe(
    question: str,
    expected: str,
    answer: str,
    sparql: str,
    api_url: str,
    api_key: str,
    tenant: str,
    kg_name: str,
) -> FailureDiagnosis | None:
    """Query Neptune to check if the data supports the expected answer."""
    graph_uri = f"https://cograph.tech/graphs/{tenant}/kg/{kg_name}"
    base = f"{api_url}/graphs/{tenant}"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    try:
        # Probe 1: Check if pipe characters exist in entity names
        pipe_query = (
            f"SELECT (COUNT(?v) AS ?cnt) FROM <{graph_uri}> "
            f"WHERE {{ ?s ?p ?v . FILTER(CONTAINS(STR(?v), \"|\")) }} LIMIT 1"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                f"{base}/query", headers=headers,
                json={"query": pipe_query},
            )
            if res.status_code == 200:
                bindings = res.json().get("bindings", [])
                pipe_count = int(bindings[0].get("cnt", 0)) if bindings else 0
                if pipe_count > 0:
                    # Check if the expected answer relates to a count mismatch
                    try:
                        expected_num = float(re.sub(r"[^\d.\-]", "", expected))
                        answer_nums = re.findall(r"[\d]+\.?\d*", answer)
                        if answer_nums:
                            answer_num = float(answer_nums[0])
                            if expected_num > answer_num * 1.1:
                                return FailureDiagnosis(
                                    question=question,
                                    layer="ingestion",
                                    sub_category="pipe_splitting",
                                    confidence=0.75,
                                    evidence=f"Graph has pipe-delimited values. Expected {expected_num}, got {answer_num} (count gap suggests unsplit multi-values)",
                                    signature="pipe_split_attribute",
                                    fix_type="reingest",
                                )
                    except (ValueError, IndexError):
                        pass

        # Probe 2: If SPARQL has a specific predicate, check it exists in ontology
        if sparql:
            predicate_uris = re.findall(r"<(https://omnix\.dev/(?:onto|types)/[^>]+)>", sparql)
            ontology_graph = f"https://cograph.tech/graphs/{tenant}"
            for pred_uri in predicate_uris:
                if "/onto/" in pred_uri or "/attrs/" in pred_uri:
                    check_query = (
                        f"ASK FROM <{ontology_graph}> WHERE {{ "
                        f"<{pred_uri}> ?p ?o }}"
                    )
                    async with httpx.AsyncClient(timeout=10) as client:
                        res = await client.post(
                            f"{base}/query", headers=headers,
                            json={"query": check_query},
                        )
                        if res.status_code == 200:
                            # Neptune ASK returns boolean
                            result_text = res.text
                            if '"boolean":false' in result_text or '"boolean": false' in result_text:
                                return FailureDiagnosis(
                                    question=question,
                                    layer="ontology",
                                    sub_category="missing_predicate",
                                    confidence=0.85,
                                    evidence=f"Predicate <{pred_uri}> not found in ontology graph",
                                    signature=f"missing_pred_{pred_uri.split('/')[-1]}",
                                    affected_entity=pred_uri,
                                    fix_type="sparql_update",
                                )

    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Stage B: Pattern Matching
# ---------------------------------------------------------------------------

def _stage_b_pattern_match(
    question: str,
    expected: str,
    answer: str,
    sparql: str,
    failure_cat: str,
    explanation: str,
) -> FailureDiagnosis | None:
    """Rules-based classification from failure signals."""

    # Rule 1: Answer contains pipe characters → unsplit values in graph
    if "|" in answer and "HTTP" not in answer:
        return FailureDiagnosis(
            question=question,
            layer="ingestion",
            sub_category="pipe_splitting",
            confidence=0.9,
            evidence=f"Answer contains pipe characters: '{answer[:80]}'",
            signature="pipe_in_answer",
            fix_type="reingest",
        )

    # Rule 2: Large count gap (expected >> actual) suggests missing data
    try:
        expected_num = float(re.sub(r"[^\d.\-]", "", expected))
        answer_nums = re.findall(r"[\d]+\.?\d*", answer)
        if answer_nums and expected_num > 0:
            answer_num = float(answer_nums[0])
            ratio = answer_num / expected_num if expected_num != 0 else 0
            if 0 < ratio < 0.5:
                # Got less than half expected — likely ingestion issue
                return FailureDiagnosis(
                    question=question,
                    layer="ingestion",
                    sub_category="missing_data",
                    confidence=0.6,
                    evidence=f"Got {answer_num}, expected {expected_num} (ratio: {ratio:.2f}). Large gap suggests missing data in graph.",
                    signature=f"count_gap_{int(ratio*10)}",
                    fix_type="reingest",
                )
    except (ValueError, IndexError):
        pass

    # Rule 3: Case mismatch in explanation
    if "case" in explanation.lower() and ("mismatch" in explanation.lower() or "sensitivity" in explanation.lower()):
        return FailureDiagnosis(
            question=question,
            layer="ingestion",
            sub_category="normalization",
            confidence=0.7,
            evidence=f"Judge flagged case sensitivity: {explanation[:100]}",
            signature="case_normalization",
            fix_type="reingest",
        )

    # Rule 4: Failure category maps directly to query layer
    query_categories = {"bad_predicate_uri", "wrong_filter", "wrong_aggregation", "missing_join"}
    if failure_cat in query_categories:
        return FailureDiagnosis(
            question=question,
            layer="query",
            sub_category=failure_cat,
            confidence=0.7,
            evidence=f"Failure category '{failure_cat}' is a SPARQL generation issue",
            signature=f"query_{failure_cat}",
            fix_type="prompt_edit",
        )

    # Rule 5: Empty result when data should exist
    if failure_cat == "empty_result" or answer == "No results found.":
        return FailureDiagnosis(
            question=question,
            layer="query",
            sub_category="empty_result",
            confidence=0.6,
            evidence="Query returned no results. Likely wrong predicate or missing join.",
            signature="query_empty_result",
            fix_type="prompt_edit",
        )

    # Rule 6: URI instead of value
    if failure_cat == "uri_instead_of_value":
        return FailureDiagnosis(
            question=question,
            layer="query",
            sub_category="uri_instead_of_value",
            confidence=0.8,
            evidence="Query returned entity URIs instead of human-readable values",
            signature="query_uri_value",
            fix_type="prompt_edit",
        )

    # Rule 7: SPARQL is empty or malformed
    if not sparql or len(sparql.strip()) < 20:
        return FailureDiagnosis(
            question=question,
            layer="query",
            sub_category="generation_failure",
            confidence=0.9,
            evidence="No SPARQL generated or malformed query",
            signature="query_no_sparql",
            fix_type="prompt_edit",
        )

    return None


# ---------------------------------------------------------------------------
# Stage C: LLM Diagnosis (expensive fallback)
# ---------------------------------------------------------------------------

DIAGNOSIS_PROMPT = """You are diagnosing why a knowledge graph query returned the wrong answer.

Failed question: {question}
Expected answer: {expected}
System's answer: {answer}
Generated SPARQL: {sparql}
Failure explanation: {explanation}

Classify the root cause into one of three layers:

1. INGESTION — the data in the graph is wrong or missing
   Examples: values not split ("A|B" stored as one entity), wrong capitalization, missing rows
2. ONTOLOGY — the graph structure is wrong
   Examples: attribute should be a relationship, missing subClassOf, wrong predicate domain
3. QUERY — the SPARQL query is wrong
   Examples: wrong predicate URI, missing FILTER, wrong aggregation, missing JOIN

Respond with JSON:
{{
  "layer": "ingestion" | "ontology" | "query",
  "sub_category": "brief category name",
  "confidence": 0.0-1.0,
  "evidence": "one sentence explaining why",
  "fix_type": "reingest" | "sparql_update" | "prompt_edit"
}}"""


async def _stage_c_llm_diagnosis(
    question: str,
    expected: str,
    answer: str,
    sparql: str,
    failure_cat: str,
    explanation: str,
    openrouter_key: str,
) -> FailureDiagnosis | None:
    """LLM-based diagnosis for ambiguous failures."""
    prompt = DIAGNOSIS_PROMPT.format(
        question=question,
        expected=expected[:200],
        answer=answer[:200],
        sparql=sparql[:500],
        explanation=explanation[:200],
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek/deepseek-v3.2",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 256,
                    "temperature": 0,
                },
            )
            res.raise_for_status()
            text = res.json()["choices"][0]["message"]["content"]
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
                stripped = "\n".join(lines)
            result = json.loads(stripped)

            return FailureDiagnosis(
                question=question,
                layer=result.get("layer", "query"),
                sub_category=result.get("sub_category", "unknown"),
                confidence=result.get("confidence", 0.5),
                evidence=result.get("evidence", ""),
                signature=f"llm_{result.get('layer', 'query')}_{result.get('sub_category', 'unknown')}",
                fix_type=result.get("fix_type", "prompt_edit"),
            )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _hash_question(question: str) -> str:
    """Short hash for question dedup."""
    import hashlib
    return hashlib.md5(question.encode()).hexdigest()[:8]


def group_by_signature(diagnoses: list[FailureDiagnosis]) -> dict[str, list[FailureDiagnosis]]:
    """Group diagnoses by signature for pattern detection."""
    groups: dict[str, list[FailureDiagnosis]] = {}
    for d in diagnoses:
        groups.setdefault(d.signature, []).append(d)
    return groups


def summarize_diagnoses(diagnoses: list[FailureDiagnosis]) -> dict:
    """Summary stats for a set of diagnoses."""
    by_layer = {"ingestion": 0, "ontology": 0, "query": 0, "ambiguous": 0}
    for d in diagnoses:
        by_layer[d.layer] = by_layer.get(d.layer, 0) + 1

    patterns = group_by_signature(diagnoses)
    top_patterns = sorted(patterns.items(), key=lambda x: -len(x[1]))[:5]

    return {
        "total_failures": len(diagnoses),
        "by_layer": by_layer,
        "top_patterns": [
            {"signature": sig, "count": len(ds), "layer": ds[0].layer, "sub_category": ds[0].sub_category}
            for sig, ds in top_patterns
        ],
    }
