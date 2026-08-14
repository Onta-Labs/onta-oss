"""Structured /ask process log (debug + persona RCA).

Writes one JSON object per generation attempt (and a final summary) when
``INFONA_ASK_PROCESS_LOG`` is set to a directory or file path.

Never logs API keys. Truncates large ontology / cypher blobs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_MAX_TEXT = 6000


def _trunc(s: Any, n: int = _MAX_TEXT) -> str:
    t = "" if s is None else str(s)
    if len(t) <= n:
        return t
    return t[: n - 3] + "..."


def ask_log_enabled() -> bool:
    return bool((os.environ.get("INFONA_ASK_PROCESS_LOG") or "").strip())


def _log_path() -> Path | None:
    raw = (os.environ.get("INFONA_ASK_PROCESS_LOG") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.suffix.lower() == ".jsonl":
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    p.mkdir(parents=True, exist_ok=True)
    return p / "ask_process.jsonl"


def log_ask_event(event: str, **fields: Any) -> None:
    """Append one structured event to the process log (best-effort)."""
    path = _log_path()
    if path is None:
        return
    rec: dict[str, Any] = {
        "ts": time.time(),
        "event": event,
    }
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            rec[k] = _trunc(v) if isinstance(v, str) else v
        elif isinstance(v, (list, tuple)):
            rec[k] = [
                _trunc(x) if isinstance(x, str) else x for x in list(v)[:40]
            ]
        elif isinstance(v, dict):
            # Shallow sanitize
            rec[k] = {
                str(kk): (
                    _trunc(vv)
                    if isinstance(vv, str)
                    else (vv if isinstance(vv, (int, float, bool, type(None))) else _trunc(vv))
                )
                for kk, vv in list(v.items())[:40]
            }
        else:
            rec[k] = _trunc(v)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def apply_money_leaf_params(
    params: dict[str, Any] | None,
    *,
    money_leaf: str | None,
    money_cue: str | None = None,
) -> dict[str, Any]:
    """Rewrite *existing* bare synonym params to a unique resolved leaf.

    Only keys already present in ``params`` are touched. Does not invent
    ``$prop`` / ``$cost_prop`` the plan never used. Does not pick a leaf
    when resolve was ambiguous — caller must pass a unique ``money_leaf``.
    """
    out = dict(params or {})
    leaf = (money_leaf or "").strip()
    if not leaf:
        return out
    bare = frozenset(
        {
            "",
            "cost",
            "price",
            "tuition",
            "amount",
            "fee",
            "charge",
        }
    )
    keys = (
        "prop_key",
        "cost_prop",
        "cost_prop_key",
        "price_prop",
        "measure_prop",
    )
    rewritten = False
    for k in keys:
        if k not in out:
            continue
        cur = out.get(k)
        if cur is None or str(cur).strip().lower() in bare:
            out[k] = leaf
            rewritten = True
    if rewritten:
        if money_cue:
            out.setdefault("_money_cue", money_cue)
        out["_money_leaf_bound"] = leaf
    return out


__all__ = [
    "apply_money_leaf_params",
    "ask_log_enabled",
    "log_ask_event",
]
