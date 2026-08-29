"""Post-install first run: credentials → acquire → first answer (INF-593).

Install writes ontology, type skills, an optional sample, and the lock.
It does not acquire live data (INF-564). First-run:

1. Walk required credentials from the package source definitions.
   ``byok`` sources fail closed when the workspace has no key.
2. Run ``acquire_condition_set`` through the existing API-registry
   executor and the shared write path. Paid / hosted bindings stay
   premium — this module does not fetch them.
3. Answer the package's first supported question on the tenant graph.
   ``question`` only overrides the echoed prompt. Sample is never
   current (INF-587).

Request credentials are passed into the executor as a request-scoped
environ + secret resolver. Process ``os.environ`` is not mutated
(INF-580).

Boundary: OSS protocol. ``infona_client.*`` / stdlib only. Tenant-confined.
Credentials are never stored on the package or echoed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from infona_client.api_registry.executor import RegistryApiSource
from infona_client.blueprint.first_run_acquire import (
    FIRST_RUN_MAX_ROWS,
    acquire_condition_set,
    catalog_slug_for,
    facts_from_registry_rows,
)
from infona_client.blueprint.first_run_answer import (
    answer_supported_question,
    default_supported_question,
)
from infona_client.blueprint.lock import make_blueprint_lock_store
from infona_client.blueprint.models import BlueprintManifest
from infona_client.blueprint.plan import (
    BlueprintCredentialsMissing,
    BlueprintNotInstalled,
    load_and_validate,
)

ACQUIRE_TASK_ID = "acquire_condition_set"
ANSWER_TASK_ID = "answer_supported_question"


@dataclass(frozen=True)
class RequiredCredential:
    source_id: str
    key_env: str
    title: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "key_env": self.key_env,
            "title": self.title,
        }


@dataclass
class FirstRunResult:
    status: str
    tenant_id: str
    blueprint_id: str
    kg: str
    task: str
    acquired_rows: int
    acquired_subjects: list[str]
    question: str
    answer: str
    citations: list[str]
    sample_is_current: bool
    sample_used: bool
    sample_captured_at: str | None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tenant_id": self.tenant_id,
            "blueprint_id": self.blueprint_id,
            "kg": self.kg,
            "task": self.task,
            "acquired_rows": self.acquired_rows,
            "acquired_subjects": list(self.acquired_subjects),
            "question": self.question,
            "answer": self.answer,
            "citations": list(self.citations),
            "sample_is_current": self.sample_is_current,
            "sample_used": self.sample_used,
            "sample_captured_at": self.sample_captured_at,
            "sources": list(self.sources),
        }


def required_credentials(manifest: BlueprintManifest) -> list[RequiredCredential]:
    """BYOK sources declared on the package. Credential *values* are not here."""
    return [
        RequiredCredential(source_id=src.id, key_env=src.key_env, title=src.title)
        for src in manifest.sources
        if src.credential == "byok"
    ]


def resolve_credential(
    req: RequiredCredential,
    provided: Mapping[str, str] | None,
    environ: Mapping[str, str],
) -> str | None:
    """Workspace-side lookup. Package never supplies the value."""
    supplied = provided or {}
    for key in (req.key_env, req.source_id):
        value = supplied.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    env_value = environ.get(req.key_env)
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip()
    return None


def missing_credentials(
    manifest: BlueprintManifest,
    *,
    provided: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[RequiredCredential]:
    env = environ if environ is not None else os.environ
    return [
        req
        for req in required_credentials(manifest)
        if resolve_credential(req, provided, env) is None
    ]


def _raise_if_missing(
    manifest: BlueprintManifest,
    *,
    provided: Mapping[str, str] | None,
    environ: Mapping[str, str] | None,
) -> None:
    missing = missing_credentials(manifest, provided=provided, environ=environ)
    if missing:
        raise BlueprintCredentialsMissing(
            "required source credentials are missing",
            details={
                "missing": [req.to_dict() for req in missing],
                "fail_closed": True,
            },
        )


def _request_environ(
    manifest: BlueprintManifest,
    *,
    credentials: Mapping[str, str] | None,
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    """Merge supplied keys into a request-scoped copy. Never writes os.environ."""
    env = {
        str(key): str(value)
        for key, value in (environ if environ is not None else os.environ).items()
        if isinstance(value, str)
    }
    for req in required_credentials(manifest):
        value = resolve_credential(req, credentials, env)
        if value:
            env[req.key_env] = value
    return env


async def run_first_run(
    tenant_id: str,
    blueprint_id: str,
    *,
    credentials: Mapping[str, str] | None = None,
    question: str | None = None,
    max_rows: int = FIRST_RUN_MAX_ROWS,
    executor: RegistryApiSource | None = None,
    neptune: Any = None,
    environ: Mapping[str, str] | None = None,
) -> FirstRunResult:
    """Install must already have happened. Credentials → acquire → one answer."""
    lock = await make_blueprint_lock_store().get(tenant_id, blueprint_id)
    if lock is None:
        raise BlueprintNotInstalled(
            f"blueprint {blueprint_id!r} is not installed in this workspace"
        )
    from infona_client.blueprint.catalog import make_blueprint_package_store, shipped_seed_path

    cataloged = await make_blueprint_package_store().get(tenant_id, blueprint_id)
    source: Any = cataloged.manifest if cataloged is not None else None
    if source is None:
        seed = shipped_seed_path(blueprint_id)
        if seed is not None:
            source = seed
    if source is None:
        raise BlueprintNotInstalled(
            f"blueprint {blueprint_id!r} has no package in this workspace"
        )
    manifest = load_and_validate(source)
    _raise_if_missing(manifest, provided=credentials, environ=environ)
    env = _request_environ(manifest, credentials=credentials, environ=environ)
    rows, subjects, sources = await acquire_condition_set(
        manifest,
        tenant_id=tenant_id,
        kg=lock.kg,
        max_rows=max_rows,
        executor=executor,
        neptune=neptune,
        credentials=credentials,
        environ=env,
    )
    asked, answer, citations, sample_used = await answer_supported_question(
        manifest,
        tenant_id=tenant_id,
        kg=lock.kg,
        question=question,
        sample_subjects=lock.sample_subjects,
        sample_captured_at=lock.sample_captured_at,
    )
    return FirstRunResult(
        status="answered",
        tenant_id=tenant_id,
        blueprint_id=lock.blueprint_id,
        kg=lock.kg,
        task=ACQUIRE_TASK_ID,
        acquired_rows=rows,
        acquired_subjects=subjects,
        question=asked,
        answer=answer,
        citations=citations,
        sample_is_current=False,
        sample_used=sample_used,
        sample_captured_at=lock.sample_captured_at,
        sources=sources,
    )


__all__ = [
    "ACQUIRE_TASK_ID",
    "ANSWER_TASK_ID",
    "FIRST_RUN_MAX_ROWS",
    "FirstRunResult",
    "RequiredCredential",
    "acquire_condition_set",
    "answer_supported_question",
    "catalog_slug_for",
    "default_supported_question",
    "facts_from_registry_rows",
    "missing_credentials",
    "required_credentials",
    "run_first_run",
]
