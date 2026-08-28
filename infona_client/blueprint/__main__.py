"""``python -m infona_client.blueprint validate PATH`` — offline INF-563 check."""

from __future__ import annotations

import sys
from pathlib import Path

from infona_client.blueprint.package import validate_package


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stderr.write(
            "usage: python -m infona_client.blueprint validate <dir-or-file>\n"
        )
        return 0 if args and args[0] in {"-h", "--help"} else 2
    if args[0] != "validate" or len(args) != 2:
        sys.stderr.write(
            "usage: python -m infona_client.blueprint validate <dir-or-file>\n"
        )
        return 2
    errors = validate_package(Path(args[1]))
    if errors:
        for err in errors:
            sys.stderr.write(f"{err}\n")
        return 1
    sys.stdout.write("valid\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
