"""Hermetic guards for the OSS one-command local loop.

Parses ``scripts/oss_up.sh``, ``Dockerfile``, and ``docker-compose.yml`` only.
Never starts Docker, never hits the network.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OSS_UP = REPO / "scripts" / "oss_up.sh"
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(REPO)}"
    return path.read_text(encoding="utf-8")


def test_oss_up_script_exists_and_is_executable_intent():
    text = _read(OSS_UP)
    assert text.startswith("#!/usr/bin/env bash"), "oss_up.sh must be a bash script"
    mode = OSS_UP.stat().st_mode
    assert mode & stat.S_IXUSR, "oss_up.sh should be executable (git filemode +x)"


def test_oss_up_script_mentions_compose_setup_and_health():
    text = _read(OSS_UP)
    assert "docker compose up -d --build" in text
    assert "oss_setup.sh" in text
    assert "/health" in text
    assert "neo4j" in text
    assert "healthy" in text
    assert ".env.example" in text
    assert "OPENROUTER_API_KEY" in text
    assert "npx @infona-ai/cli" in text


def test_oss_up_script_does_not_open_a_browser():
    text = _read(OSS_UP)
    assert not re.search(r"\b(xdg-open|open)\s+https?://", text)
    assert "Never opens a browser" in text


def test_oss_up_script_prints_next_loop_commands():
    text = _read(OSS_UP)
    assert "ingest examples/trials.csv --kg trials" in text
    assert "AstraZeneca" in text
    assert "export --kg trials" in text


def test_dockerfile_exists_and_does_not_copy_env():
    text = _read(DOCKERFILE)
    assert not re.search(r"^\s*COPY\s+\.env\b", text, re.M | re.I)
    assert not re.search(r"^\s*COPY\s+\.\s+\.", text, re.M)
    assert "pip install" in text
    assert "uvicorn" in text
    ignore = _read(REPO / ".dockerignore")
    assert re.search(r"(?m)^\.env$", ignore)


def test_compose_has_neo4j_and_api_depends_on_healthy():
    text = _read(COMPOSE)
    assert re.search(r"(?m)^  neo4j:", text)
    assert re.search(r"(?m)^  api:", text)
    api_block = text.split("\n  api:", 1)[1].split("\n  fuseki:", 1)[0]
    assert "depends_on:" in api_block
    assert "neo4j:" in api_block
    assert "condition: service_healthy" in api_block
    assert "bolt://neo4j:7687" in api_block
    assert "INFONA_GRAPH_BACKEND" in api_block
    assert "infona-dev-password" in api_block
    assert "/health" in api_block
    assert "8000" in api_block
    assert ".env" in api_block
    fuseki = text.split("\n  fuseki:", 1)[1]
    assert "legacy-sparql" in fuseki
