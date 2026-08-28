"""Post-install first run: credentials → acquire → first answer (INF-593).

Install writes ontology, type skills, an optional sample, and the lock.
It does not acquire live data (INF-564). First-run:

1. Walk required credentials from the package source definitions.
   ``byok`` sources fail closed when the workspace has no key.
2. Run ``acquire_condition_set`` through the existing API-registry
   executor and the shared write path. Paid / hosted bindings stay
   premium — this module does not fetch them.
3. Answer one supported question on the tenant graph. Sample is never
   current (INF-587).

Boundary: OSS protocol. ``infona_client.*`` / stdlib only. Tenant-confined
(INF-580). Credentials are never stored on the package or echoed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlparse

from infona_client.api_registry.catalog import make_api_source_catalog
from infona_client.api_registry.executor import RegistryApiSource
from infona_client.blueprint.lock import make_blueprint_lock_store
from infona_client.blueprint.models import (
    REAL_NCT_RE,
    AcquisitionInstruction,
    BlueprintManifest,
    SourceDefinition,
)
from infona_client.blueprint.plan import (
    BlueprintAcquisitionFailed,
    BlueprintCredentialsMissing,
    BlueprintNotInstalled,
    BlueprintPaidBinding,
    load_and_validate,
)
from infona_client.graph.assertion_model import property_uri
from infona_client.graph.facts import Fact
from infona_client.graph.kg_writer import insert_facts, refresh_after_write
from infona_client.graph.ontology_queries import entity_uri, type_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.rdfs_helpers import (
    session_entities_of_type,
    session_literal_values,
    session_object_values,
)
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import get_graph_store

ACQUIRE_TASK_ID = "acquire_condition_set"
ANSWER_TASK_ID = "answer_supported_question"
FIRST_RUN_MAX_ROWS = 25
_RECRUITING = frozenset({"RECRUITING", "ENROLLING_BY_INVITATION"})
_SOURCE_SLUGS = {
    "ctgov": "clinicaltrials_gov",
    "nppes": "nppes",
}
_REGISTRY_TO_TRIAL = {
    "nct_id": "nct_id",
    "title": "brief_title",
    "official_title": "official_title",
    "brief_title": "brief_title",
    "status": "overall_status",
    "overall_status": "overall_status",
    "phase": "phase",
    "study_type": "study_type",
    "enrollment": "enrollment",
}


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
            "sample_is_current": False,
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


def catalog_slug_for(source: SourceDefinition) -> str | None:
    if source.id in _SOURCE_SLUGS:
        return _SOURCE_SLUGS[source.id]
    host = (urlparse(source.url).hostname or "").lower()
    if not host:
        return None
    catalog = make_api_source_catalog()
    for spec in catalog.all():
        spec_host = (urlparse(spec.base_url).hostname or "").lower()
        if spec_host and (host == spec_host or host.endswith("." + spec_host)):
            return spec.slug
    return None


def _acquire_instructions(manifest: BlueprintManifest) -> list[AcquisitionInstruction]:
    """Sources ``acquire_condition_set`` mints from. Enrichment is not first pull."""
    minting: list[AcquisitionInstruction] = []
    for instruction in manifest.acquisition:
        text = instruction.first_pull.lower()
        if text.startswith("enrichment"):
            continue
        minting.append(instruction)
    if not minting:
        return list(manifest.acquisition)
    return minting


def _bindings_from_seed(seed_query: str) -> dict[str, str]:
    text = " ".join(seed_query.split())
    pairs = parse_qsl(text, keep_blank_values=False)
    raw = {key: value for key, value in pairs}
    bindings: dict[str, str] = {}
    if raw.get("query.cond"):
        bindings["condition"] = raw["query.cond"]
    if raw.get("query.term"):
        bindings["term"] = raw["query.term"]
    elif raw.get("query.intr"):
        bindings["term"] = raw["query.intr"]
    if raw.get("query.spons"):
        bindings["sponsor"] = raw["query.spons"]
    if raw.get("query.id"):
        bindings["id"] = raw["query.id"]
    if raw.get("filter.overallStatus"):
        bindings["status"] = raw["filter.overallStatus"]
    if not bindings and text:
        bindings["condition"] = text
    return bindings


def _as_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].replace("'", "").replace('"', "")
        return [part.strip() for part in inner.split(",") if part.strip()]
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def _trial_literals(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dest in _REGISTRY_TO_TRIAL.items():
        if src not in row or dest in out:
            continue
        values = _as_values(row[src])
        if not values:
            continue
        out[dest] = values[0] if dest != "phase" else ", ".join(values)
    return out


def facts_from_registry_rows(
    rows: list[Mapping[str, Any]],
    *,
    source_mark: str,
) -> tuple[list[Fact], list[str]]:
    """Project flattened registry rows onto ClinicalTrial + related nodes."""
    facts: list[Fact] = []
    subjects: list[str] = []
    seen: set[str] = set()
    for row in rows:
        literals = _trial_literals(row)
        nct = str(literals.get("nct_id") or "").strip()
        if not nct or not REAL_NCT_RE.match(nct):
            continue
        if nct in seen:
            continue
        seen.add(nct)
        trial = entity_uri("ClinicalTrial", nct)
        subjects.append(trial)
        facts.append(
            Fact(subject_id=trial, kind="type", key="ClinicalTrial", source=source_mark)
        )
        for key, value in literals.items():
            facts.append(
                Fact(
                    subject_id=trial,
                    kind="literal",
                    key=key,
                    value=value,
                    source=source_mark,
                )
            )
        sponsor = str(row.get("lead_sponsor") or "").strip()
        if sponsor:
            org = entity_uri("Organization", sponsor)
            facts.append(
                Fact(subject_id=org, kind="type", key="Organization", source=source_mark)
            )
            facts.append(
                Fact(
                    subject_id=org,
                    kind="literal",
                    key="org_name",
                    value=sponsor,
                    source=source_mark,
                )
            )
            org_class = str(row.get("lead_sponsor_class") or "").strip()
            if org_class:
                facts.append(
                    Fact(
                        subject_id=org,
                        kind="literal",
                        key="org_class",
                        value=org_class,
                        source=source_mark,
                    )
                )
            facts.append(
                Fact(
                    subject_id=trial,
                    kind="rel",
                    key="lead_sponsor",
                    value=org,
                    source=source_mark,
                )
            )
        for condition in _as_values(row.get("conditions")):
            cond = entity_uri("MedicalCondition", condition)
            facts.append(
                Fact(
                    subject_id=cond,
                    kind="type",
                    key="MedicalCondition",
                    source=source_mark,
                )
            )
            facts.append(
                Fact(
                    subject_id=cond,
                    kind="literal",
                    key="condition_name",
                    value=condition,
                    source=source_mark,
                )
            )
            facts.append(
                Fact(
                    subject_id=trial,
                    kind="rel",
                    key="studies_condition",
                    value=cond,
                    source=source_mark,
                )
            )
    return facts, subjects


async def acquire_condition_set(
    manifest: BlueprintManifest,
    *,
    tenant_id: str,
    kg: str,
    max_rows: int = FIRST_RUN_MAX_ROWS,
    executor: RegistryApiSource | None = None,
    neptune: Any = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """First pull declared by the package. Uses the registry executor + insert_facts."""
    runner = executor or RegistryApiSource()
    catalog = make_api_source_catalog()
    env = environ if environ is not None else os.environ
    all_facts: list[Fact] = []
    subjects: list[str] = []
    used: list[str] = []
    for instruction in _acquire_instructions(manifest):
        source = next((s for s in manifest.sources if s.id == instruction.source), None)
        if source is None:
            continue
        slug = catalog_slug_for(source)
        if slug is None:
            raise BlueprintAcquisitionFailed(
                f"no OSS acquisition binding for source {source.id!r}",
                details={"source_id": source.id, "fail_closed": True},
            )
        spec = catalog.get(slug)
        if spec is None:
            raise BlueprintAcquisitionFailed(
                f"no OSS acquisition binding for source {source.id!r}",
                details={"source_id": source.id, "slug": slug, "fail_closed": True},
            )
        if spec.is_paid:
            raise BlueprintPaidBinding(
                f"paid source {source.id!r} is a premium binding",
                details={"source_id": source.id, "slug": slug, "fail_closed": True},
            )
        cap = min(max(1, max_rows), instruction.page_cap)
        result = await runner.execute(
            spec,
            _bindings_from_seed(instruction.seed_query),
            max_rows=cap,
        )
        if result.dormant:
            raise BlueprintCredentialsMissing(
                f"source {source.id!r} is dormant without a credential",
                details={"source_id": source.id, "fail_closed": True},
            )
        if result.error:
            raise BlueprintAcquisitionFailed(
                f"acquisition failed for source {source.id!r}",
                details={
                    "source_id": source.id,
                    "error": result.error,
                    "fail_closed": True,
                },
            )
        mark = f"api:{slug}"
        facts, minted = facts_from_registry_rows(result.rows, source_mark=mark)
        all_facts.extend(facts)
        subjects.extend(minted)
        used.append(source.id)
    if all_facts:
        await insert_facts(
            neptune,
            kg_graph_uri(tenant_id, kg),
            facts=all_facts,
        )
        await refresh_after_write(
            neptune,
            tenant_id=tenant_id,
            kg_name=kg,
            affected_types=list({c.name for c in manifest.concepts}),
        )
    return len(subjects), subjects, used


def _lit(values: list[Any]) -> str:
    return str(values[0]).strip() if values else ""


async def _trial_card(
    session: Any, subject: str
) -> dict[str, Any]:
    nct = _lit(await session_literal_values(session, subject, property_uri("nct_id")))
    phase = _lit(await session_literal_values(session, subject, property_uri("phase")))
    status = _lit(
        await session_literal_values(session, subject, property_uri("overall_status"))
    )
    title = _lit(
        await session_literal_values(session, subject, property_uri("brief_title"))
    ) or _lit(
        await session_literal_values(session, subject, property_uri("official_title"))
    )
    cond_uris = await session_object_values(
        session, subject, property_uri("studies_condition")
    )
    conditions: list[str] = []
    for uri in cond_uris:
        name = _lit(
            await session_literal_values(session, uri, property_uri("condition_name"))
        )
        if name:
            conditions.append(name)
    return {
        "subject": subject,
        "nct_id": nct,
        "phase": phase,
        "overall_status": status,
        "title": title,
        "conditions": conditions,
    }


def _is_sample_nct(nct: str, sample_subjects: set[str], subject: str) -> bool:
    if subject in sample_subjects:
        return True
    return nct.upper().startswith("SAMPLE-") or not REAL_NCT_RE.match(nct)


def _matches_phase3_obesity_recruiting(card: Mapping[str, Any]) -> bool:
    phase = str(card.get("phase") or "").upper()
    status = str(card.get("overall_status") or "").upper()
    hay = " ".join(
        [
            str(card.get("title") or ""),
            " ".join(card.get("conditions") or []),
        ]
    ).lower()
    return (
        "PHASE3" in phase.replace(" ", "")
        and status in _RECRUITING
        and "obesity" in hay
    )


def default_supported_question(manifest: BlueprintManifest) -> str:
    if manifest.examples:
        return manifest.examples[0].question
    return "Which Phase 3 trials for obesity are currently recruiting?"


async def answer_supported_question(
    manifest: BlueprintManifest,
    *,
    tenant_id: str,
    kg: str,
    question: str | None,
    sample_subjects: list[str],
    sample_captured_at: str | None,
) -> tuple[str, str, list[str], bool]:
    """Answer on the tenant graph. Sample rows are labelled and never current."""
    asked = (question or "").strip() or default_supported_question(manifest)
    session = get_graph_store().session(GraphScope.for_instance(tenant_id, kg))
    subjects = await session_entities_of_type(
        session, type_uri("ClinicalTrial"), include_subclasses=False
    )
    cards = [await _trial_card(session, uri) for uri in subjects]
    sample_set = set(sample_subjects)
    matches = [c for c in cards if _matches_phase3_obesity_recruiting(c) and c["nct_id"]]
    live = [
        c
        for c in matches
        if not _is_sample_nct(c["nct_id"], sample_set, c["subject"])
    ]
    sample_hits = [
        c for c in matches if _is_sample_nct(c["nct_id"], sample_set, c["subject"])
    ]
    if live:
        ncts = [c["nct_id"] for c in live]
        answer = (
            f"{asked} — {', '.join(ncts)} "
            f"({len(ncts)} recruiting Phase 3 obesity trial(s) on this workspace graph)."
        )
        return asked, answer, ncts, False
    if sample_hits:
        ncts = [c["nct_id"] for c in sample_hits]
        captured = sample_captured_at or "unknown"
        answer = (
            f"{asked} — sample, captured {captured}, not current. "
            f"Expected sample ids: {', '.join(ncts)}."
        )
        return asked, answer, ncts, True
    live_any = [
        c
        for c in cards
        if c["nct_id"] and not _is_sample_nct(c["nct_id"], sample_set, c["subject"])
    ]
    if live_any:
        ncts = [c["nct_id"] for c in live_any[:8]]
        answer = (
            f"{asked} — no recruiting Phase 3 obesity trials matched; "
            f"acquired NCT ids on this graph include {', '.join(ncts)}."
        )
        return asked, answer, ncts, False
    answer = (
        f"{asked} — no acquired trials on this workspace graph yet. "
        "Sample rows, if present, are not current."
    )
    return asked, answer, [], False


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
    env = dict(environ if environ is not None else os.environ)
    applied: list[str] = []
    for req in required_credentials(manifest):
        value = resolve_credential(req, credentials, env)
        if value:
            env[req.key_env] = value
            if req.key_env not in os.environ:
                os.environ[req.key_env] = value
                applied.append(req.key_env)
    try:
        rows, subjects, sources = await acquire_condition_set(
            manifest,
            tenant_id=tenant_id,
            kg=lock.kg,
            max_rows=max_rows,
            executor=executor,
            neptune=neptune,
            environ=env,
        )
    finally:
        for key in applied:
            os.environ.pop(key, None)
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
