#!/usr/bin/env python3
"""Upload train-only Together JSONL and start LoRA jobs. Never prints the key.

Usage (from repo root):

    PYTHONPATH=benchmarks/vrdu-binder/src \\
      python benchmarks/vrdu-binder/scripts/together_lora.py create \\
      --jsonl /tmp/lora/sd0-vanilla.together.jsonl \\
      --suffix vrdu-v11-vanilla-sd0

    python benchmarks/vrdu-binder/scripts/together_lora.py wait --job ft-...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vrdu_binder.constants import MODEL_08B, TOGETHER_BASE_URL
from vrdu_binder.llm import resolve_api_key
from vrdu_binder.protocol import ProtocolError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("upload", help="Upload a messages-only JSONL")
    p_up.add_argument("--jsonl", type=Path, required=True)
    p_create = sub.add_parser("create", help="Upload + start LoRA on 0.8B")
    p_create.add_argument("--jsonl", type=Path, required=True)
    p_create.add_argument("--suffix", required=True)
    p_create.add_argument("--epochs", type=int, default=3)
    p_wait = sub.add_parser("wait", help="Poll a fine-tune job")
    p_wait.add_argument("--job", required=True)
    p_wait.add_argument("--timeout-sec", type=int, default=6 * 60 * 60)
    p_get = sub.add_parser("get", help="Print job status fields (no key)")
    p_get.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        key = resolve_api_key()
        if args.cmd == "upload":
            print(json.dumps({"file_id": _upload(key, args.jsonl)}))
            return 0
        if args.cmd == "create":
            file_id = _upload(key, args.jsonl)
            job = _create(key, file_id, suffix=args.suffix, epochs=args.epochs)
            print(json.dumps(job, indent=2))
            return 0
        if args.cmd == "wait":
            print(json.dumps(_wait(key, args.job, args.timeout_sec), indent=2))
            return 0
        print(json.dumps(_public_job(_get(key, args.job)), indent=2))
        return 0
    except ProtocolError as exc:
        print(exc, file=sys.stderr)
        return 2


def _headers(key: str, *, json_body: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _upload(key: str, path: Path) -> str:
    if not path.is_file():
        raise ProtocolError(f"missing jsonl {path}")
    _assert_messages_only(path)
    boundary = "----vrduBinderTogether"
    raw = path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="purpose"\r\n\r\nfine-tune\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file_name"\r\n\r\n{path.name}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: application/jsonl\r\n\r\n"
    ).encode("utf-8") + raw + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/files",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    meta = _read_json(req)
    file_id = meta.get("id")
    if not isinstance(file_id, str) or not file_id:
        raise ProtocolError(f"upload returned no file id (keys={sorted(meta)})")
    _wait_file(key, file_id)
    return file_id


def _wait_file(key: str, file_id: str) -> None:
    deadline = time.time() + 30 * 60
    while True:
        req = urllib.request.Request(
            f"{TOGETHER_BASE_URL}/files/{file_id}",
            headers=_headers(key, json_body=False),
            method="GET",
        )
        meta = _read_json(req)
        status = str(meta.get("processing_status") or meta.get("status") or "")
        if status in {"COMPLETED", "processed", ""}:
            report = meta.get("validation_report") or {}
            if isinstance(report, dict) and report.get("valid") is False:
                raise ProtocolError(f"Together rejected file {file_id}: {report}")
            if status == "COMPLETED" or report.get("valid") is True:
                return
            if not status:
                return
        if status in {"INVALID_FORMAT", "FAILED"}:
            raise ProtocolError(f"Together file {file_id} status={status}")
        if time.time() > deadline:
            raise ProtocolError(f"Together file {file_id} still {status}")
        time.sleep(5)


def _create(key: str, file_id: str, *, suffix: str, epochs: int) -> dict[str, object]:
    payload = {
        "training_file": file_id,
        "model": MODEL_08B,
        "n_epochs": epochs,
        "n_checkpoints": 1,
        "learning_rate": 1e-5,
        "warmup_ratio": 0,
        "train_on_inputs": "auto",
        "lora": True,
        "suffix": suffix,
    }
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/fine-tunes",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(key),
        method="POST",
    )
    job = _read_json(req)
    return _public_job(job)


def _get(key: str, job_id: str) -> dict[str, object]:
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/fine-tunes/{job_id}",
        headers=_headers(key, json_body=False),
        method="GET",
    )
    return _read_json(req)


def _wait(key: str, job_id: str, timeout_sec: int) -> dict[str, object]:
    deadline = time.time() + timeout_sec
    while True:
        job = _get(key, job_id)
        status = str(job.get("status") or "")
        print(f"job={job_id} status={status}", file=sys.stderr)
        if status in {"completed", "error", "cancelled", "failed"}:
            if status != "completed":
                raise ProtocolError(f"Together job {job_id} ended {status}")
            return _public_job(job)
        if time.time() > deadline:
            raise ProtocolError(f"Together job {job_id} still {status}")
        time.sleep(60)


def _public_job(job: dict[str, object]) -> dict[str, object]:
    keep = (
        "id",
        "status",
        "model",
        "output_name",
        "suffix",
        "model_output_name",
        "model_output_path",
        "api_model_object_id",
        "model_object_id",
        "training_file",
        "created_at",
    )
    return {k: job[k] for k in keep if k in job}


def _assert_messages_only(path: Path) -> None:
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if set(row.keys()) != {"messages"}:
            raise ProtocolError("Together JSONL rows must be {messages: ...} only")
        n += 1
    if n < 1:
        raise ProtocolError("Together JSONL is empty")


def _read_json(req: urllib.request.Request) -> dict[str, object]:
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise ProtocolError(f"Together HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"Together HTTP failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("Together response is not a JSON object")
    return raw


if __name__ == "__main__":
    raise SystemExit(main())
