"""Validate a Blueprint package from the command line.

    python -m infona_client.blueprint validate path/to/package

Install is INF-565 and is not implemented here.
"""

from __future__ import annotations

import argparse
import sys

from infona_client.blueprint.load import validate_blueprint_package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infona_client.blueprint",
        description="Validate a Blueprint package (ADR 0014 / INF-563).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    validate = sub.add_parser("validate", help="validate a directory or manifest")
    validate.add_argument("path", help="package directory or blueprint.yaml")
    args = parser.parse_args(argv)

    if args.cmd != "validate":
        parser.error(f"unknown command {args.cmd}")
        return 2

    errors = validate_blueprint_package(args.path)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
