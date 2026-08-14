"""Parse a TENANT skill from an uploaded markdown file or skill-package zip.

Used by ``POST /graphs/{tenant}/skills`` so a client can create a skill from a
``.md`` / ``.markdown`` / ``.txt`` file or a zip that contains ``SKILL.md``
without a second create route. Skills stay prose — this module never executes
anything in the archive.

Boundary: OSS. Stdlib only (``zipfile``, ``io``, ``re``) plus ``.models``
for the slug shape. No ``from infona.*``.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from .models import SLUG_RE

_MD_EXTS = {".md", ".markdown", ".txt"}
_ZIP_EXTS = {".zip"}
_ATX_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_ZIP_LOCAL = b"PK\x03\x04"
_ZIP_EMPTY = b"PK\x05\x06"


def parse_skill_upload(*, filename: str, data: bytes, type_name: str) -> dict[str, Any]:
    """Turn an uploaded file into fields for ``CreateSkillRequest``.

    Returns ``slug``, ``type_name``, ``body``, ``title``, ``summary``,
    ``metadata``. Raises ``ValueError`` with a human message on bad input.
    Body size is *not* capped here — ``validate_skill`` is the single cap.
    """
    if not data:
        raise ValueError("uploaded file is empty")
    name = (filename or "").strip() or "skill.md"
    ext = _ext(name)

    if ext in _ZIP_EXTS or _looks_like_zip(data):
        text, member_name = _read_zip_markdown(data)
        # Prefer the zip's own stem (``clinic-notes.zip``) over ``SKILL``.
        stem_source = name if ext in _ZIP_EXTS else member_name
        return _fields_from_markdown(
            text, filename=stem_source, type_name=type_name, source_filename=name
        )

    if ext in _MD_EXTS or ext == "":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"skill file is not valid UTF-8: {exc}") from exc
        return _fields_from_markdown(
            text, filename=name, type_name=type_name, source_filename=name
        )

    raise ValueError(
        f"unsupported skill file type {ext!r}; use .md, .markdown, .txt, or .zip"
    )


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def _fields_from_markdown(
    text: str, *, filename: str, type_name: str, source_filename: str
) -> dict[str, Any]:
    text = text.lstrip("\ufeff")
    meta, body = _parse_frontmatter(text)
    if not (body or "").strip():
        raise ValueError("skill body is empty — a skill IS its markdown body")

    stem = _stem(filename)
    slug = (meta.get("slug") or "").strip() or _sanitize_slug(stem)
    if not SLUG_RE.match(slug):
        raise ValueError(
            "could not derive a valid slug from the filename "
            f"{filename!r} (need lowercase alphanumeric with - or _)"
        )

    heading = _first_atx_heading(body)
    title = (meta.get("title") or "").strip() or heading or stem
    summary = (meta.get("summary") or "").strip()

    return {
        "slug": slug,
        "type_name": type_name,
        "body": body,
        "title": title,
        "summary": summary,
        "metadata": {"source_filename": source_filename},
    }


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Tiny ``key: value`` reader. Unterminated ``---`` is treated as all-body
    so a malformed header never eats the content."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            meta: dict[str, str] = {}
            for raw in lines[1:idx]:
                if ":" not in raw:
                    continue
                k, _, v = raw.partition(":")
                meta[k.strip().casefold()] = v.strip().strip('"').strip("'")
            return meta, "\n".join(lines[idx + 1 :]).lstrip("\n")
    return {}, text


def _first_atx_heading(body: str) -> str:
    match = _ATX_H1.search(body)
    return match.group(1).strip() if match else ""


def _sanitize_slug(raw: str) -> str:
    s = (raw or "").strip().casefold()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    return s[:64]


# --------------------------------------------------------------------------- #
# Zip
# --------------------------------------------------------------------------- #
def _read_zip_markdown(data: bytes) -> tuple[str, str]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("file is not a valid zip archive") from exc

    with zf:
        names = [info.filename for info in zf.infolist() if not info.is_dir()]
        if not names:
            raise ValueError("zip is empty")
        for name in names:
            if _is_unsafe_zip_path(name):
                raise ValueError(
                    f"zip contains an unsafe path {name!r} "
                    "(absolute paths and '..' are not allowed)"
                )
        member = _choose_zip_member(names)
        raw = zf.read(member)
    if not raw:
        raise ValueError(f"zip member {member!r} is empty")
    try:
        return raw.decode("utf-8"), member
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"zip member {member!r} is not valid UTF-8: {exc}"
        ) from exc


def _choose_zip_member(names: list[str]) -> str:
    """``SKILL.md`` at zip root, else first ``<dir>/SKILL.md``, else first
    ``*.md`` / ``*.markdown`` in archive order. Case-insensitive."""
    normed = [(orig, orig.replace("\\", "/")) for orig in names]

    for orig, n in normed:
        if n.lower() == "skill.md":
            return orig
    for orig, n in normed:
        parts = n.split("/")
        if len(parts) == 2 and parts[1].lower() == "skill.md":
            return orig
    for orig, n in normed:
        leaf = n.rsplit("/", 1)[-1].lower()
        if leaf.endswith(".md") or leaf.endswith(".markdown"):
            return orig
    raise ValueError("zip contains no markdown skill file (expected SKILL.md)")


def _is_unsafe_zip_path(name: str) -> bool:
    if not name:
        return True
    norm = name.replace("\\", "/")
    if norm.startswith("/") or norm.startswith("//"):
        return True
    if re.match(r"^[A-Za-z]:", norm):
        return True
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    return any(p == ".." for p in parts)


# --------------------------------------------------------------------------- #
# Filename crumbs
# --------------------------------------------------------------------------- #
def _leaf(filename: str) -> str:
    return (filename or "").replace("\\", "/").rsplit("/", 1)[-1]


def _ext(filename: str) -> str:
    leaf = _leaf(filename)
    if "." not in leaf:
        return ""
    return "." + leaf.rsplit(".", 1)[-1].lower()


def _stem(filename: str) -> str:
    leaf = _leaf(filename)
    if "." in leaf:
        return leaf.rsplit(".", 1)[0]
    return leaf


def _looks_like_zip(data: bytes) -> bool:
    return data.startswith(_ZIP_LOCAL) or data.startswith(_ZIP_EMPTY)
