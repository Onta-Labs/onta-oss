"""TENANT skill create from a markdown file or a skill-package zip.

Covers ``parse_skill_upload`` and the existing ``POST /graphs/{tenant}/skills``
route's ``archive_b64`` path. One canonical create route — no second endpoint.
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from infona_client.skills import (
    parse_skill_upload,
    reset_global_type_skill_store,
    reset_skill_layers,
    reset_type_skill_store,
)


@pytest.fixture(autouse=True)
def _clean_skill_state():
    reset_skill_layers()
    reset_type_skill_store()
    reset_global_type_skill_store()
    yield
    reset_skill_layers()
    reset_type_skill_store()
    reset_global_type_skill_store()


_BASE = "/graphs/test-tenant/skills"


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# parse_skill_upload — markdown
# --------------------------------------------------------------------------- #
def test_md_file_body_slug_and_title_from_heading():
    data = b"# Clinic billing\n\nNever merge clinics by street address.\n"
    got = parse_skill_upload(
        filename="Clinic Billing.md", data=data, type_name="Clinic"
    )
    assert got["body"] == data.decode("utf-8")
    assert got["slug"] == "clinic-billing"
    assert got["title"] == "Clinic billing"
    assert got["type_name"] == "Clinic"
    assert got["summary"] == ""
    assert got["metadata"]["source_filename"] == "Clinic Billing.md"


def test_frontmatter_slug_title_summary():
    text = (
        "---\n"
        "slug: custom-slug\n"
        "title: Custom Title\n"
        'summary: "a gist"\n'
        "---\n"
        "# Ignored heading\n\n"
        "Body text.\n"
    )
    got = parse_skill_upload(
        filename="ignored.md", data=text.encode("utf-8"), type_name="Clinic"
    )
    assert got["slug"] == "custom-slug"
    assert got["title"] == "Custom Title"
    assert got["summary"] == "a gist"
    assert got["body"].lstrip().startswith("# Ignored heading")
    assert "Body text." in got["body"]


def test_txt_and_markdown_extensions_are_accepted():
    for name in ("notes.txt", "notes.markdown"):
        got = parse_skill_upload(
            filename=name, data=b"# Notes\n\nProse.\n", type_name="Person"
        )
        assert got["slug"] == "notes"
        assert "Prose." in got["body"]


# --------------------------------------------------------------------------- #
# parse_skill_upload — zip
# --------------------------------------------------------------------------- #
def test_zip_with_skill_md_at_root():
    data = _zip_bytes({"SKILL.md": "# Root skill\n\nGuidance."})
    got = parse_skill_upload(
        filename="root-pack.zip", data=data, type_name="Clinic"
    )
    assert "Guidance." in got["body"]
    assert got["title"] == "Root skill"
    assert got["slug"] == "root-pack"
    assert got["type_name"] == "Clinic"


def test_zip_with_nested_folder_skill_md():
    data = _zip_bytes({"my-skill/SKILL.md": "# Nested\n\nNested body."})
    got = parse_skill_upload(filename="pack.zip", data=data, type_name="Clinic")
    assert "Nested body." in got["body"]
    assert got["title"] == "Nested"
    assert got["slug"] == "pack"


def test_zip_falls_back_to_first_md_when_no_skill_md():
    data = _zip_bytes(
        {
            "readme.txt": "not a skill",
            "docs/notes.md": "# First md\n\nUsed as fallback.",
        }
    )
    got = parse_skill_upload(filename="pack.zip", data=data, type_name="Clinic")
    assert "Used as fallback." in got["body"]
    assert got["title"] == "First md"


def test_zip_slip_rejected():
    data = _zip_bytes({"../evil.md": "# Evil\n\nhacked"})
    with pytest.raises(ValueError, match="unsafe path"):
        parse_skill_upload(filename="bad.zip", data=data, type_name="Clinic")


def test_zip_slip_absolute_path_rejected():
    data = _zip_bytes({"/tmp/evil.md": "# Evil\n\nhacked"})
    with pytest.raises(ValueError, match="unsafe path"):
        parse_skill_upload(filename="bad.zip", data=data, type_name="Clinic")


def test_no_markdown_in_zip_rejected():
    data = _zip_bytes({"readme.txt": "not a skill", "data.json": "{}"})
    with pytest.raises(ValueError, match="no markdown"):
        parse_skill_upload(filename="empty.zip", data=data, type_name="Clinic")


def test_empty_upload_rejected():
    with pytest.raises(ValueError, match="empty"):
        parse_skill_upload(filename="blank.md", data=b"", type_name="Clinic")


# --------------------------------------------------------------------------- #
# POST /graphs/{tenant}/skills
# --------------------------------------------------------------------------- #
def test_post_create_with_archive_b64_md(client, auth_headers):
    raw = b"# Uploaded\n\nFrom an archive."
    resp = client.post(
        _BASE,
        json={
            "type_name": "Clinic",
            "filename": "uploaded.md",
            "archive_b64": base64.b64encode(raw).decode("ascii"),
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "uploaded"
    assert body["title"] == "Uploaded"
    assert "From an archive." in body["body"]
    assert body["type_name"] == "Clinic"
    assert body["layer"] == "tenant"


def test_post_create_with_archive_b64_zip(client, auth_headers):
    raw = _zip_bytes({"SKILL.md": "# Zip skill\n\nZip body."})
    resp = client.post(
        _BASE,
        json={
            "type_name": "Clinic",
            "filename": "zip-skill.zip",
            "archive_b64": base64.b64encode(raw).decode("ascii"),
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "zip-skill"
    assert body["title"] == "Zip skill"
    assert "Zip body." in body["body"]


def test_explicit_json_overrides_archive_fields(client, auth_headers):
    raw = b"# Uploaded\n\nFrom an archive."
    resp = client.post(
        _BASE,
        json={
            "type_name": "Clinic",
            "filename": "uploaded.md",
            "archive_b64": base64.b64encode(raw).decode("ascii"),
            "slug": "override-slug",
            "title": "Override title",
            "summary": "Override summary",
            "body": "Override body.",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "override-slug"
    assert body["title"] == "Override title"
    assert body["summary"] == "Override summary"
    assert body["body"] == "Override body."


def test_existing_json_body_create_still_works(client, auth_headers):
    resp = client.post(
        _BASE,
        json={
            "slug": "naming",
            "type_name": "Person",
            "body": "A Person here is always a clinician.",
            "title": "Naming",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["slug"] == "naming"
    assert resp.json()["body"] == "A Person here is always a clinician."
    assert resp.json()["title"] == "Naming"


def test_neither_body_nor_archive_is_422(client, auth_headers):
    resp = client.post(
        _BASE,
        json={"type_name": "Clinic", "slug": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "body" in str(detail).lower() or "archive" in str(detail).lower()


def test_bad_archive_is_422(client, auth_headers):
    resp = client.post(
        _BASE,
        json={
            "type_name": "Clinic",
            "filename": "bad.zip",
            "archive_b64": base64.b64encode(_zip_bytes({"only.txt": "nope"})).decode(
                "ascii"
            ),
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
