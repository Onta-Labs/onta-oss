"""Blueprint package CLI.

    python -m infona_client.blueprint validate path/to/package
    python -m infona_client.blueprint install path/to/package --tenant T --kg K
    python -m infona_client.blueprint inspect namespace/name --tenant T
    python -m infona_client.blueprint uninstall namespace/name --tenant T
    python -m infona_client.blueprint fork namespace/name --tenant T [--as ns/name]
    python -m infona_client.blueprint extend namespace/name --tenant T --overlay file
    python -m infona_client.blueprint update path/to/new --tenant T --id ns/name
    python -m infona_client.blueprint first-run namespace/name --tenant T

The HTTP / npm / MCP surfaces call the same engine. Export is
``infona_client.blueprint.export`` (INF-565).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from infona_client.blueprint.first_run import run_first_run
from infona_client.blueprint.install import (
    BlueprintError,
    inspect_blueprint,
    install_blueprint,
    uninstall_blueprint,
)
from infona_client.blueprint.fork import fork_blueprint
from infona_client.blueprint.layer import extend_blueprint, update_blueprint
from infona_client.blueprint.load import validate_blueprint_package


def _run(coro):
    return asyncio.run(coro)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infona_client.blueprint",
        description="Validate, install, inspect, uninstall, fork, extend, update, or first-run a Blueprint.",
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

    fork = sub.add_parser("fork", help="copy the package into a new identity with lineage")
    fork.add_argument("id", help="parent blueprint id (namespace/name)")
    fork.add_argument("--tenant", required=True)
    fork.add_argument(
        "--as",
        dest="as_id",
        default=None,
        help="new package id (default: {tenant}/{parent-name})",
    )

    extend = sub.add_parser("extend", help="add a private overlay on the installed pin")
    extend.add_argument("id", help="installed blueprint id (namespace/name)")
    extend.add_argument("--tenant", required=True)
    extend.add_argument("--overlay", required=True, help="overlay YAML or JSON file")

    update = sub.add_parser("update", help="apply a new public base without clobbering overlay")
    update.add_argument("path", help="new package directory or blueprint.yaml")
    update.add_argument("--tenant", required=True)
    update.add_argument("--id", dest="blueprint_id", required=True, help="installed pin")

    first = sub.add_parser(
        "first-run",
        help="credentials → acquire_condition_set → first supported answer",
    )
    first.add_argument("id", help="installed blueprint id (namespace/name)")
    first.add_argument("--tenant", required=True)
    first.add_argument(
        "--question",
        default=None,
        help="echo this prompt; first-run always answers the package's first supported question",
    )
    first.add_argument(
        "--credential",
        action="append",
        default=[],
        help="KEY_ENV=value (repeatable). Missing byok keys fail closed.",
    )
    first.add_argument("--max-rows", type=int, default=25)

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
        if args.cmd == "fork":
            result = _run(
                fork_blueprint(args.tenant, args.id, as_id=args.as_id)
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        if args.cmd == "extend":
            raw = Path(args.overlay).read_text(encoding="utf-8")
            stripped = raw.lstrip()
            if stripped.startswith("{") or stripped.startswith("["):
                overlay = json.loads(raw)
            else:
                import yaml

                overlay = yaml.safe_load(raw)
            if not isinstance(overlay, dict):
                print("overlay must be a YAML/JSON object", file=sys.stderr)
                return 2
            body = _run(extend_blueprint(args.tenant, args.id, overlay))
            print(json.dumps(body, indent=2))
            return 0
        if args.cmd == "update":
            body = _run(
                update_blueprint(
                    args.path,
                    tenant_id=args.tenant,
                    blueprint_id=args.blueprint_id,
                )
            )
            print(json.dumps(body, indent=2))
            return 0
        if args.cmd == "first-run":
            creds: dict[str, str] = {}
            for item in args.credential:
                if "=" not in item:
                    print("credential must be KEY_ENV=value", file=sys.stderr)
                    return 2
                key, value = item.split("=", 1)
                creds[key] = value
            body = _run(
                run_first_run(
                    args.tenant,
                    args.id,
                    credentials=creds or None,
                    question=args.question,
                    max_rows=args.max_rows,
                )
            )
            print(json.dumps(body.to_dict(), indent=2))
            return 0
    except BlueprintError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
