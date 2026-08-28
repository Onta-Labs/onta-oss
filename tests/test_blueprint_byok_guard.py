"""BYOK guard for Blueprint source bindings (ADR 0014 §2.4 / boundary §26).

A keyed source reachable from an OSS Blueprint is bring-your-own-key:
``credential: byok`` plus an environment-variable NAME in ``key_env``.
The package never ships a credential value, a URL with userinfo, or a
shared/platform key. Unkeyed public registries (ClinicalTrials.gov, NPPES)
are ``credential: none`` and must not set ``key_env``.
"""

from __future__ import annotations

import re
from pathlib import Path

from infona_client.blueprint import load_blueprint_package
from infona_client.blueprint.models import ENV_NAME_RE
from infona_client.blueprint.seeds import CLINICAL_TRIALS, SEEDS_DIR

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

_URL_USERINFO = re.compile(r"://[^/\s@]+:[^/\s@]+@")
_URL_CRED_PARAM = re.compile(
    r"[?&](?:api[_-]?key|apikey|token|access[_-]?token|secret|password|passwd|key)="
    r"[^&\s]+",
    re.IGNORECASE,
)
_SECRET_PREFIX = re.compile(
    r"\b(?:"
    r"sk-(?:or-|ant-|proj-)?(?:v\d+-)?[A-Za-z0-9]{16,}"
    r"|fc-[A-Za-z0-9]{16,}"
    r"|csk-[A-Za-z0-9]{16,}"
    r"|gh[posru]_[A-Za-z0-9]{16,}"
    r")"
)


def _seed_packages() -> list[Path]:
    return sorted(
        path.parent
        for path in SEEDS_DIR.glob("*/blueprint.yaml")
    )


def test_seed_sources_are_byok_or_none():
    violations: list[str] = []
    for package in _seed_packages():
        manifest = load_blueprint_package(package)
        for source in manifest.sources:
            prefix = f"{package.name}.sources.{source.id}"
            if source.credential == "none":
                if source.key_env:
                    violations.append(f"{prefix}: key_env set on an unkeyed source")
                continue
            if source.credential != "byok":
                violations.append(f"{prefix}: credential {source.credential!r} is not none/byok")
                continue
            if not ENV_NAME_RE.match(source.key_env):
                violations.append(
                    f"{prefix}: byok source must set key_env to an env-var NAME, "
                    f"got {source.key_env!r}"
                )
            env_text = _ENV_EXAMPLE.read_text(encoding="utf-8")
            if source.key_env and source.key_env not in env_text:
                violations.append(
                    f"{prefix}: key_env {source.key_env} is not documented in .env.example"
                )
            if _SECRET_PREFIX.search(source.key_env):
                violations.append(f"{prefix}: key_env looks like a secret value")
    assert not violations, "\n".join(violations)


def test_seed_urls_do_not_embed_credentials():
    violations: list[str] = []
    for package in _seed_packages():
        manifest = load_blueprint_package(package)
        for source in manifest.sources:
            url = source.url
            if _URL_USERINFO.search(url) or _URL_CRED_PARAM.search(url):
                violations.append(f"{package.name}.{source.id}: url embeds credentials")
            if _SECRET_PREFIX.search(url):
                violations.append(f"{package.name}.{source.id}: url looks like a secret")
    assert not violations, "\n".join(violations)


def test_clinical_trials_sources_are_public_and_unkeyed():
    manifest = load_blueprint_package(CLINICAL_TRIALS)
    ids = {s.id for s in manifest.sources}
    assert ids == {"ctgov", "nppes"}
    for source in manifest.sources:
        assert source.credential == "none"
        assert source.key_env == ""
