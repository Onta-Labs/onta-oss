"""``import dlt`` may appear in exactly one wrapper module (ONTA-553)."""

from __future__ import annotations

import re
from pathlib import Path

import infona_client

ROOT = Path(infona_client.__file__).resolve().parent
ALLOWED = {
    ROOT / "ingestion" / "dlt_source.py",
}

_IMPORT = re.compile(r"^\s*(import dlt\b|from dlt\b)", re.M)


def test_import_dlt_only_in_wrapper():
    hits: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if path.name.endswith(".pyc"):
            continue
        text = path.read_text(encoding="utf-8")
        if _IMPORT.search(text):
            hits.append(path)
    unexpected = [p for p in hits if p not in ALLOWED]
    missing = [p for p in ALLOWED if p not in hits]
    assert not unexpected, (
        "import dlt / from dlt is only allowed in "
        "infona_client/ingestion/dlt_source.py; found:\n  "
        + "\n  ".join(str(p.relative_to(ROOT)) for p in unexpected)
    )
    assert not missing, (
        "allowlisted wrapper no longer imports dlt — update the allowlist "
        "if the wrapper moved:\n  "
        + "\n  ".join(str(p.relative_to(ROOT)) for p in missing)
    )


def test_no_dlt_destination_anywhere_in_client():
    dest = re.compile(r"dlt\.destinations|dlt\.pipeline\(")
    offenders = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if dest.search(text):
            offenders.append(path)
    assert not offenders, "dlt destination / pipeline configured:\n  " + "\n  ".join(
        str(p) for p in offenders
    )
