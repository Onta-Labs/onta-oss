"""Bind then extract. Misbind → empty slots."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from sgd_binder.instances import Instance
from sgd_binder.protocol import ProtocolError
from sgd_binder.score import Score, score_predictions


def run_instances(
    instances: list[Instance],
    *,
    binder: Any,
    extractor: Any,
    out_path: Path,
    concurrency: int = 1,
    extra_meta: dict[str, Any] | None = None,
) -> Score:
    pred_type: dict[str, str] = {}
    pred_slots: dict[str, dict[str, str]] = {}

    def _one(inst: Instance) -> tuple[str, str, dict[str, str]]:
        try:
            bound = binder.bind(inst.prompt)
        except ProtocolError:
            return inst.instance_id, "", {}
        if bound != inst.type_id:
            return inst.instance_id, bound, {}
        try:
            slots = extractor.extract(inst.prompt, bound)
        except ProtocolError:
            slots = {}
        return inst.instance_id, bound, slots

    if concurrency <= 1:
        rows = [_one(inst) for inst in instances]
    else:
        rows = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(_one, inst) for inst in instances]
            for fut in as_completed(futs):
                rows.append(fut.result())
    for iid, bound, slots in rows:
        pred_type[iid] = bound
        pred_slots[iid] = slots
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"spec": "sgd-v1", "bind": "predicted", "n": len(instances)}
    if extra_meta:
        meta.update(extra_meta)
    payload = {
        "meta": meta,
        "pred_type": pred_type,
        "pred_slots": pred_slots,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    return score_predictions(instances, pred_type, pred_slots)
