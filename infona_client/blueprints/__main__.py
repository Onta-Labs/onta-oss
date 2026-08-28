"""``python -m infona_client.blueprints path.json`` — validate a manifest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from infona_client.blueprints.validate import (
    BlueprintValidationError,
    dump_manifest,
    validate_manifest,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(
            "usage: python -m infona_client.blueprints <manifest.json|yaml>",
            file=sys.stderr,
        )
        return 2
    path = Path(args[0])
    try:
        manifest = validate_manifest(path)
    except BlueprintValidationError as exc:
        print("INVALID", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(json.dumps(dump_manifest(manifest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
