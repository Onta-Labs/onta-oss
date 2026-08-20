"""LLM client + pandas ground-truth helpers for the eval harness.

Looks up ``_llm_call`` on the :mod:`infona_client.eval` facade at call
time so existing monkeypatches keep working.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import structlog

from infona_client.eval_models import (
    EVAL_MAX_TOKENS,
    EVAL_MODEL,
    EVAL_REASONING,
    OPENROUTER_URL,
)
from infona_client.eval_prompts import GROUND_TRUTH_PROMPT

logger = structlog.stdlib.get_logger("infona.eval")


def _host():
    """Call-time lookup of the public eval module (monkeypatch surface)."""
    from infona_client import eval as _mod

    return _mod


def _part_text(value) -> str:
    if isinstance(value, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in value
        )
    return str(value or "")


def _assistant_text(payload: dict) -> str:
    """Final answer text from an OpenRouter chat completion.

    Reasoning models put chain-of-thought in ``message.reasoning`` (or
    ``<think>`` inside content). Prefer ``content``; fall back to
    ``reasoning`` when the provider left content empty.
    """
    choices = payload.get("choices") if isinstance(payload, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = _part_text(message.get("content")).strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    if not text:
        text = _part_text(message.get("reasoning")).strip()
    if not text:
        finish = choice.get("finish_reason")
        detail = f" (finish_reason={finish})" if finish else ""
        raise ValueError(f"empty LLM response{detail}")
    return text


async def _llm_call(
    prompt: str,
    system: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens: int = EVAL_MAX_TOKENS,
    json_mode: bool = False,
) -> str:
    """Call an LLM via OpenRouter. Returns the response text."""
    model = model or EVAL_MODEL
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    reasoning = dict(EVAL_REASONING)
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "reasoning": reasoning,
    }
    if json_mode:
        # Think, but put only the JSON answer in content. Echoed
        # chain-of-thought starved two judge parses on V4 Pro.
        reasoning["exclude"] = True
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=600) as client:
        res = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        res.raise_for_status()
        return _assistant_text(res.json())


def _parse_json(text: str) -> dict | list:
    """Parse JSON from LLM response, including JSON buried in prose."""
    stripped = text.strip()
    if "</think>" in stripped:
        stripped = stripped.rsplit("</think>", 1)[-1].strip()
    if stripped.startswith("```"):
        lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(stripped):
        if ch in "{[":
            obj, _ = decoder.raw_decode(stripped, i)
            return obj
    return json.loads(stripped)


async def _compute_ground_truth(
    question: str,
    csv_path: Path,
    api_key: str = "",
) -> str | None:
    """Compute deterministic ground truth by running pandas on the CSV.

    Uses an LLM to translate the question into a pandas expression,
    then executes it against the full DataFrame. Returns the exact answer
    as a string, or None if computation fails.
    """
    import pandas as pd

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        logger.warning("ground_truth_csv_read_error", error=str(e))
        return None

    # Coerce numeric columns
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col], errors="ignore")
        except Exception:
            pass

    columns = ", ".join(df.columns.tolist())
    dtypes = "\n".join(f"  {col}: {df[col].dtype}" for col in df.columns)

    prompt = GROUND_TRUTH_PROMPT.format(columns=columns, dtypes=dtypes)
    # Show sample values and basic stats for numeric columns
    col_info = []
    for col in df.columns:
        sample_vals = df[col].dropna().head(5).tolist()
        if pd.api.types.is_numeric_dtype(df[col]):
            col_info.append(f"  {col}: numeric, sample={sample_vals}, min={df[col].min()}, max={df[col].max()}")
        else:
            col_info.append(f"  {col}: string, sample={sample_vals}")
    col_details = "\n".join(col_info)

    user_content = (
        f"Question: {question}\n\n"
        f"Column details:\n{col_details}\n\n"
        f"First 3 rows:\n{df.head(3).to_string()}"
    )

    try:
        expression = await _host()._llm_call(
            prompt=user_content,
            system=prompt,
            api_key=api_key,
            max_tokens=EVAL_MAX_TOKENS,
        )
        # Strip markdown fences if present
        expression = expression.strip()
        if expression.startswith("```"):
            lines = [l for l in expression.split("\n") if not l.strip().startswith("```")]
            expression = "\n".join(lines).strip()

        safe_builtins = {
            "len": len, "round": round, "sum": sum, "min": min, "max": max,
            "int": int, "float": float, "str": str, "abs": abs, "sorted": sorted,
            "list": list, "dict": dict, "tuple": tuple, "set": set, "bool": bool,
            "enumerate": enumerate, "zip": zip, "range": range, "map": map,
            "filter": filter, "isinstance": isinstance, "type": type,
            "True": True, "False": False, "None": None,
        }
        import numpy as np
        result = eval(expression, {"df": df, "pd": pd, "np": np, "__builtins__": safe_builtins})  # noqa: S307
        answer = str(result)
        logger.info("ground_truth_computed", question=question[:60], answer=answer[:60], expr=expression[:80])
        return answer
    except Exception as e:
        logger.warning("ground_truth_compute_error", question=question[:60], error=str(e))
        return None
