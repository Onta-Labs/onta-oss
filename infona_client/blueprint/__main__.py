"""Blueprint package CLI.

    python -m infona_client.blueprint validate path/to/package
    python -m infona_client.blueprint install path/to/package --tenant T --kg K
    python -m infona_client.blueprint inspect namespace/name --tenant T
    python -m infona_client.blueprint uninstall namespace/name --tenant T

The HTTP / npm / MCP surfaces call the same engine. Export is
``infona_client.blueprint.export`` (INF-565).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from infona_client.blueprint.install import (
    BlueprintError,
    inspect_blueprint,
    install_blueprint,
    uninstall_blueprint,
)
from infona_client.blueprint.load import validate_blueprint_package


def _run(coro):
    return asyncio.run(coro)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infona_client.blueprint",
        description="Validate, install, inspect, or uninstall a Blueprint package.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate = sub.add_parser("validate", help="validate a directory or manifest")
    validate.add_argument("path", help="package directory or blueprint.yaml")

    install = sub.add_parser("install", help="install into a workspace (idempotent)")
    install.add_argument("path", help="package directory")
    install.add_argument("--tenant", required=True, help="workspace / tenant id")
    install.add_argument("--kg", required=True, help="knowledge graph name")
    install.add_argument(
        "--no-sample",
        action="store_true",
        help="skip the bounded sample (empty graph only)",
    )

    inspect = sub.add_parser("inspect", help="show the installed pin")
    inspect.add_argument("id", help="blueprint id (namespace/name)")
    inspect.add_argument("--tenant", required=True)

    uninstall = sub.add_parser("uninstall", help="remove what install wrote")
    uninstall.add_argument("id", help="blueprint id (namespace/name)")
    uninstall.add_argument("--tenant", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "validate":
        errors = validate_blueprint_package(args.path)
        if errors:
            for err in errors:
                print(err, file=sys.stderr)
            return 1
        print("valid")
        return 0

    try:
        if args.cmd == "install":
            result = _run(
                install_blueprint(
                    args.path,
                    tenant_id=args.tenant,
                    kg=args.kg,
                    include_sample=not args.no_sample,
                )
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        if args.cmd == "inspect":
            card = _run(inspect_blueprint(args.tenant, args.id))
            print(json.dumps(card, indent=2))
            return 0
        if args.cmd == "uninstall":
            body = _run(uninstall_blueprint(args.tenant, args.id))
            print(json.dumps(body, indent=2))
            return 0
    except BlueprintError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
