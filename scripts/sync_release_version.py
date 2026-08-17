#!/usr/bin/env python3
"""Keep @infona-ai/cli, @infona-ai/mcp, and infona-client on one version.

Usage (repo root):
  python scripts/sync_release_version.py                   # fail if in-repo versions drift
  python scripts/sync_release_version.py --check-published # fail if npm/PyPI latest drift
  python scripts/sync_release_version.py --bump patch      # max(current)+1, write
  python scripts/sync_release_version.py --set 0.1.19
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI_PKG = ROOT / "packages" / "cli" / "package.json"
MCP_PKG = ROOT / "packages" / "mcp" / "package.json"
PYPROJECT = ROOT / "pyproject.toml"

_PY_VERSION_RE = re.compile(
    r'^version\s*=\s*"(?P<ver>\d+\.\d+\.\d+)"\s*$', re.M
)


def _parse(ver: str) -> tuple[int, int, int]:
    parts = ver.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"not a patch-level semver: {ver!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _fmt(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def read_npm_version(path: Path) -> str:
    data = json.loads(path.read_text())
    ver = data.get("version")
    if not isinstance(ver, str):
        raise SystemExit(f"{path}: missing version")
    _parse(ver)
    return ver


def read_py_version() -> str:
    text = PYPROJECT.read_text()
    match = _PY_VERSION_RE.search(text)
    if not match:
        raise SystemExit(f"{PYPROJECT}: no version = \"x.y.z\" line")
    ver = match.group("ver")
    _parse(ver)
    return ver


def current_versions() -> dict[str, str]:
    return {
        "@infona-ai/cli": read_npm_version(CLI_PKG),
        "@infona-ai/mcp": read_npm_version(MCP_PKG),
        "infona-client": read_py_version(),
    }


def read_mcp_cli_pin() -> str:
    data = json.loads(MCP_PKG.read_text())
    pin = (data.get("dependencies") or {}).get("@infona-ai/cli")
    if not isinstance(pin, str) or not pin:
        raise SystemExit(f"{MCP_PKG}: missing @infona-ai/cli dependency")
    return pin


def next_patch(versions: dict[str, str]) -> str:
    highest = max((_parse(v) for v in versions.values()), default=(0, 0, 0))
    return _fmt((highest[0], highest[1], highest[2] + 1))


def write_npm_version(path: Path, version: str, *, pin_cli: bool = False) -> None:
    data = json.loads(path.read_text())
    data["version"] = version
    if pin_cli:
        deps = data.setdefault("dependencies", {})
        if "@infona-ai/cli" in deps:
            deps["@infona-ai/cli"] = version
    path.write_text(json.dumps(data, indent=2) + "\n")


def write_py_version(version: str) -> None:
    text = PYPROJECT.read_text()
    new, n = _PY_VERSION_RE.subn(f'version = "{version}"', text, count=1)
    if n != 1:
        raise SystemExit(f"{PYPROJECT}: expected one version line, found {n}")
    PYPROJECT.write_text(new)


def apply(version: str) -> None:
    _parse(version)
    write_npm_version(CLI_PKG, version)
    write_npm_version(MCP_PKG, version, pin_cli=True)
    write_py_version(version)


def check_repo() -> int:
    """Fail if the three in-repo versions differ or mcp does not pin cli exactly."""
    versions = current_versions()
    for name, ver in versions.items():
        print(f"{name}={ver}")
    pin = read_mcp_cli_pin()
    print(f"@infona-ai/mcp→@infona-ai/cli={pin}")
    unique = set(versions.values())
    ok = True
    if len(unique) != 1:
        print("DRIFT: package versions differ", file=sys.stderr)
        ok = False
        expected_pin = versions["@infona-ai/cli"]
    else:
        expected_pin = next(iter(unique))
    if pin != expected_pin:
        print(
            f"DRIFT: mcp pins @infona-ai/cli at {pin!r}, want exact {expected_pin!r}",
            file=sys.stderr,
        )
        ok = False
    return 0 if ok else 1


def _http_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "infona-oss-lockstep",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    if not isinstance(payload, dict):
        raise SystemExit(f"{url}: expected JSON object")
    return payload


def published_versions() -> dict[str, str]:
    cli = _http_json("https://registry.npmjs.org/@infona-ai/cli/latest")["version"]
    mcp_latest = _http_json("https://registry.npmjs.org/@infona-ai/mcp/latest")
    mcp = mcp_latest["version"]
    pypi = _http_json("https://pypi.org/pypi/infona-client/json")["info"]["version"]
    pin = (mcp_latest.get("dependencies") or {}).get("@infona-ai/cli")
    return {
        "@infona-ai/cli": str(cli),
        "@infona-ai/mcp": str(mcp),
        "infona-client": str(pypi),
        "@infona-ai/mcp→@infona-ai/cli": str(pin or ""),
    }


def check_published() -> int:
    """Fail if latest npm/PyPI versions differ or the published mcp pin drifted."""
    published = published_versions()
    for name, ver in published.items():
        print(f"{name}={ver}")
    versions = {
        k: published[k]
        for k in ("@infona-ai/cli", "@infona-ai/mcp", "infona-client")
    }
    unique = set(versions.values())
    pin = published["@infona-ai/mcp→@infona-ai/cli"]
    ok = True
    if len(unique) != 1:
        print("DRIFT: published npm/PyPI versions differ", file=sys.stderr)
        ok = False
        expected_pin = versions["@infona-ai/cli"]
    else:
        expected_pin = next(iter(unique))
    if pin != expected_pin:
        print(
            f"DRIFT: published mcp pins @infona-ai/cli at {pin!r}, "
            f"want exact {expected_pin!r}",
            file=sys.stderr,
        )
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bump", choices=("patch",), help="write max(current)+1")
    group.add_argument("--set", dest="set_version", metavar="X.Y.Z")
    group.add_argument(
        "--check-published",
        action="store_true",
        help="compare latest npm + PyPI versions (network)",
    )
    args = parser.parse_args(argv)

    if args.check_published:
        return check_published()
    versions = current_versions()
    if args.bump:
        version = next_patch(versions)
        apply(version)
        print(version)
        return 0
    if args.set_version is not None:
        apply(args.set_version)
        print(args.set_version)
        return 0
    return check_repo()


if __name__ == "__main__":
    raise SystemExit(main())
