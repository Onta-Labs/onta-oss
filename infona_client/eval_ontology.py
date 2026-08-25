"""Ontology-quality evaluator.

Looks up ``_llm_call`` / ``_parse_json`` on the :mod:`infona_client.eval`
facade at call time so existing monkeypatches keep working.
"""
from __future__ import annotations

import json

import httpx
import structlog

from infona_client.eval_models import (
    SOURCE_SAMPLE_CHARS,
    OntologyDimension,
    OntologyScore,
)
from infona_client.eval_prompts import ONTOLOGY_JUDGE_PROMPT

logger = structlog.stdlib.get_logger("infona.eval")


def _host():
    """Call-time lookup of the public eval module (monkeypatch surface)."""
    from infona_client import eval as _mod

    return _mod


class OntologyEvaluator:
    """Evaluates the quality of an ontology created by ingestion.

    Usage::

        evaluator = OntologyEvaluator(api_url, api_key, tenant)
        score = await evaluator.evaluate(kg_name, source_sample)

    The evaluator fetches the current ontology schema from the API, sends it
    to an LLM judge along with a sample of the source data, and returns a
    structured score across 6 dimensions.
    """

    def __init__(self, api_url: str, api_key: str, tenant: str, openrouter_key: str = ""):
        self._api_url = api_url
        self._api_key = api_key
        self._tenant = tenant
        self._openrouter_key = openrouter_key

    async def evaluate(
        self,
        kg_name: str | None = None,
        source_sample: str = "",
    ) -> OntologyScore:
        """Fetch ontology and evaluate its quality.

        Args:
            kg_name: Knowledge graph name (None for default graph).
            source_sample: A sample of the source data that was ingested,
                so the judge can assess whether the ontology captures it well.

        Returns:
            OntologyScore with per-dimension scores and weak points.
        """
        # Fetch ontology schema
        ontology_text = await self._fetch_ontology(kg_name)
        if not ontology_text:
            return OntologyScore(
                dimensions=[],
                weak_points=["No ontology found — nothing to evaluate"],
            )

        # Build judge prompt
        user_prompt = (
            f"Ontology schema:\n{ontology_text}\n\n"
            f"Source data sample (first {SOURCE_SAMPLE_CHARS} chars):\n"
            f"{source_sample[:SOURCE_SAMPLE_CHARS]}"
        )

        # Call LLM judge
        response = await _host()._llm_call(
            prompt=user_prompt,
            system=ONTOLOGY_JUDGE_PROMPT,
            api_key=self._openrouter_key,
            json_mode=True,
        )

        # Parse response
        try:
            data = _host()._parse_json(response)
            dimensions = [
                OntologyDimension(
                    name=d["name"],
                    score=int(d["score"]),
                    explanation=d.get("explanation", ""),
                    issues=d.get("issues", []),
                )
                for d in data.get("dimensions", [])
            ]
            weak_points = data.get("weak_points", [])
            return OntologyScore(
                dimensions=dimensions,
                weak_points=weak_points,
                raw_judge_response=response,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("ontology_judge_parse_error", error=str(e))
            return OntologyScore(
                dimensions=[],
                weak_points=[f"Judge response parse error: {e}"],
                raw_judge_response=response,
            )

    async def _fetch_ontology(self, kg_name: str | None) -> str:
        """Fetch ontology types from the API, formatted as text."""
        base = f"{self._api_url}/graphs/{self._tenant}"
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(f"{base}/ontology/types", headers=headers)
            if res.status_code != 200:
                logger.warning("ontology_fetch_failed", status=res.status_code)
                return ""
            types = res.json()

        if not types:
            return ""

        lines = []
        for t in types:
            parent = f" (subClassOf {t['parent_type']})" if t.get("parent_type") else ""
            lines.append(f"Type: {t['name']}{parent}")
            for attr in t.get("attributes", []):
                datatype = attr.get("datatype", "string")
                lines.append(f"  .{attr['name']} ({datatype})")
            for sub in t.get("subtypes", []):
                lines.append(f"  subtype: {sub}")
        return "\n".join(lines)

