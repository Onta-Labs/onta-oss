#!/usr/bin/env python3
"""Train-only LoRA recipe check. Does not download Qwen weights.

Usage (from repo root):

    PYTHONPATH=benchmarks/vrdu-binder/src \\
      python benchmarks/vrdu-binder/scripts/train_lora.py check \\
      --jsonl /tmp/lora/sd0-vanilla.jsonl

`train` is a documented local-GPU step. It refuses unless transformers+peft
are installed and `--i-have-gpu` is set. Preferred hosted path is
`together_lora.py` (Together LoRA on Qwen/Qwen3.5-0.8B). Valid is not a
flag. There is no early-stopping split.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python scripts/train_lora.py` from this folder.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vrdu_binder.constants import MODEL_08B
from vrdu_binder.protocol import ProtocolError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="Validate train-only JSONL + manifest")
    p_check.add_argument("--jsonl", type=Path, required=True)
    p_train = sub.add_parser("train", help="GPU LoRA (refuses without --i-have-gpu)")
    p_train.add_argument("--jsonl", type=Path, required=True)
    p_train.add_argument("--out", type=Path, required=True)
    p_train.add_argument("--i-have-gpu", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "check":
            return _check(args.jsonl)
        return _train(args)
    except ProtocolError as exc:
        print(exc, file=sys.stderr)
        return 2


def _check(jsonl: Path) -> int:
    manifest = jsonl.with_suffix(".manifest.json")
    if not jsonl.is_file():
        raise ProtocolError(f"missing jsonl {jsonl}")
    if not manifest.is_file():
        raise ProtocolError(f"missing manifest {manifest}")
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    if meta.get("valid_used") or meta.get("test_used"):
        raise ProtocolError("manifest says valid or test was used")
    if meta.get("early_stopping") != "none" or meta.get("model_selection") != "none":
        raise ProtocolError("LoRA recipe must not use valid for selection or stopping")
    n = 0
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        n += 1
        name = row.get("filename")
        if not name:
            raise ProtocolError("lora row missing filename")
        blob = json.dumps(row.get("messages"))
        if name in blob:
            raise ProtocolError(f"messages leaked filename {name!r}")
        for banned in ("registration-form/", "ad-buy-form/", "FARA", "DeepForm"):
            if banned in blob:
                raise ProtocolError(f"messages leaked {banned!r}")
    print(f"ok rows={n} recipe={meta.get('recipe')} seed={meta.get('seed')} base={MODEL_08B}")
    print("train on GPU with: transformers + peft LoRA on", MODEL_08B)
    print("do not pass a valid split. do not early-stop on valid.")
    return 0


def _train(args: argparse.Namespace) -> int:
    _check(args.jsonl)
    if not args.i_have_gpu:
        raise ProtocolError(
            "refusing to start LoRA train without --i-have-gpu. "
            "This PR does not download Qwen/Qwen3.5-0.8B."
        )
    try:
        import peft  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as exc:
        raise ProtocolError(
            "transformers and peft are required for train. "
            f"Install them on the GPU box. ({exc})"
        ) from exc
    raise ProtocolError(
        "train loop is intentionally not implemented here. "
        "Load the JSONL chat rows, LoRA-tune "
        f"{MODEL_08B}, write adapters to {args.out}. "
        "No valid split. No early stopping on held-out VRDU."
    )


if __name__ == "__main__":
    raise SystemExit(main())
