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
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vrdu_binder.constants import MODEL_08B, TOGETHER_BASE_URL, TOGETHER_USER_AGENT
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
    p_est = sub.add_parser("estimate", help="Estimate LoRA job price after upload")
    p_est.add_argument("--file", required=True, help="Together file id")
    p_est.add_argument("--epochs", type=int, default=3)
    p_hw = sub.add_parser("hardware", help="List hardware for a model (no key leak)")
    p_hw.add_argument("--model", required=True)
    p_ep = sub.add_parser("create-endpoint", help="Start a dedicated endpoint")
    p_ep.add_argument("--model", required=True)
    p_ep.add_argument("--hardware", required=True)
    p_ep.add_argument("--display-name", required=True)
    p_ep.add_argument("--inactive-timeout", type=int, default=20)
    p_ew = sub.add_parser("wait-endpoint", help="Poll until STARTED")
    p_ew.add_argument("--id", required=True)
    p_ew.add_argument("--timeout-sec", type=int, default=45 * 60)
    p_eg = sub.add_parser("get-endpoint", help="Print endpoint fields (no key)")
    p_eg.add_argument("--id", required=True)
    p_ed = sub.add_parser("delete-endpoint", help="Delete a dedicated endpoint")
    p_ed.add_argument("--id", required=True)
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
        if args.cmd == "estimate":
            print(json.dumps(_estimate(key, args.file, args.epochs), indent=2))
            return 0
        if args.cmd == "hardware":
            print(json.dumps(_hardware(key, args.model), indent=2))
            return 0
        if args.cmd == "create-endpoint":
            print(json.dumps(_create_endpoint(
                key,
                model=args.model,
                hardware=args.hardware,
                display_name=args.display_name,
                inactive_timeout=args.inactive_timeout,
            ), indent=2))
            return 0
        if args.cmd == "wait-endpoint":
            print(json.dumps(_wait_endpoint(key, args.id, args.timeout_sec), indent=2))
            return 0
        if args.cmd == "get-endpoint":
            print(json.dumps(_public_endpoint(_get_endpoint(key, args.id)), indent=2))
            return 0
        if args.cmd == "delete-endpoint":
            print(json.dumps(_delete_endpoint(key, args.id), indent=2))
            return 0
        print(json.dumps(_public_job(_get(key, args.job)), indent=2))
        return 0
    except ProtocolError as exc:
        print(exc, file=sys.stderr)
        return 2


def _headers(key: str, *, json_body: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}", "User-Agent": TOGETHER_USER_AGENT}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _upload(key: str, path: Path) -> str:
    if not path.is_file():
        raise ProtocolError(f"missing jsonl {path}")
    _assert_messages_only(path)
    boundary = "----vrduBinderTogether"
    fields = {
        "purpose": "fine-tune",
        "file_name": path.name,
        "file_type": "jsonl",
        "checksum": _sha256(path),
    }
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    payload = "".join(chunks).encode("utf-8") + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/files",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": TOGETHER_USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=180) as resp:
            raise ProtocolError(
                f"Together file init expected 302, got {resp.status}"
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            body = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
            existing = body.get("file_id")
            if isinstance(existing, str) and existing:
                _wait_file(key, existing)
                return existing
        if exc.code not in {301, 302, 303, 307, 308}:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise ProtocolError(f"Together HTTP {exc.code}: {body}") from exc
        redirect = exc.headers.get("Location")
        file_id = exc.headers.get("X-Together-File-Id")
        if not redirect or not file_id:
            raise ProtocolError(
                f"Together file init missing Location/File-Id (code={exc.code})"
            )
    put = urllib.request.Request(
        redirect,
        data=path.read_bytes(),
        headers={"Content-Type": "application/octet-stream", "User-Agent": TOGETHER_USER_AGENT},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(put, timeout=180) as resp:
            if resp.status not in {200, 201, 204}:
                raise ProtocolError(f"Together S3 PUT status {resp.status}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise ProtocolError(f"Together S3 PUT HTTP {exc.code}: {body}") from exc
    prep = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/files/{file_id}/preprocess",
        data=b"{}",
        headers=_headers(key),
        method="POST",
    )
    _read_json(prep)
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
        "batch_size": 8,
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
        "token_count",
        "total_price",
        "estimated_token_count",
        "price",
    )
    return {k: job[k] for k in keep if k in job}


def _estimate(key: str, file_id: str, epochs: int) -> dict[str, object]:
    payload = {
        "training_file": file_id,
        "model": MODEL_08B,
        "n_epochs": epochs,
        "n_checkpoints": 1,
        "learning_rate": 1e-5,
        "warmup_ratio": 0,
        "train_on_inputs": "auto",
        "lora": True,
        "batch_size": 8,
        "training_method": {"method": "sft"},
        "training_type": {"type": "Lora", "lora_r": 8},
    }
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/fine-tunes/estimate-price",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(key),
        method="POST",
    )
    return _read_json(req)


def _hardware(key: str, model: str) -> dict[str, object]:
    q = urllib.parse.quote(model, safe="")
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/hardware?model={q}",
        headers=_headers(key, json_body=False),
        method="GET",
    )
    return _read_json(req)


def _public_endpoint(ep: dict[str, object]) -> dict[str, object]:
    keep = (
        "id",
        "name",
        "display_name",
        "model",
        "hardware",
        "type",
        "state",
        "owner",
        "inactive_timeout",
        "created_at",
    )
    return {k: ep[k] for k in keep if k in ep}


def _create_endpoint(
    key: str,
    *,
    model: str,
    hardware: str,
    display_name: str,
    inactive_timeout: int,
) -> dict[str, object]:
    del key, hardware, inactive_timeout
    raise ProtocolError(
        "Together v1 POST /endpoints create is disabled. Host with v2: "
        f"tg beta endpoints deploy {model} --endpoint {display_name} "
        "then pass --model <project-slug>/<endpoint> to experiment-run. "
        "Scale to 0 when done. See EXPERIMENT.md."
    )


def _get_endpoint(key: str, endpoint_id: str) -> dict[str, object]:
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/endpoints/{endpoint_id}",
        headers=_headers(key, json_body=False),
        method="GET",
    )
    return _read_json(req)


def _wait_endpoint(key: str, endpoint_id: str, timeout_sec: int) -> dict[str, object]:
    deadline = time.time() + timeout_sec
    while True:
        ep = _get_endpoint(key, endpoint_id)
        state = str(ep.get("state") or "")
        print(f"endpoint={endpoint_id} state={state}", file=sys.stderr)
        if state in {"STARTED", "ERROR", "FAILED", "STOPPED"}:
            if state != "STARTED":
                raise ProtocolError(f"Together endpoint {endpoint_id} ended {state}")
            return _public_endpoint(ep)
        if time.time() > deadline:
            raise ProtocolError(f"Together endpoint {endpoint_id} still {state}")
        time.sleep(15)


def _delete_endpoint(key: str, endpoint_id: str) -> dict[str, object]:
    req = urllib.request.Request(
        f"{TOGETHER_BASE_URL}/endpoints/{endpoint_id}",
        headers=_headers(key, json_body=False),
        method="DELETE",
    )
    try:
        return _public_endpoint(_read_json(req))
    except ProtocolError as exc:
        if "HTTP 404" in str(exc):
            return {"id": endpoint_id, "state": "deleted"}
        raise


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
