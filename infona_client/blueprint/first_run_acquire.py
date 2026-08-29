"""First-run acquisition: registry executor + shared write path (INF-593).

``acquire_condition_set`` is the Clinical Trials seed task id. This module
implements that task against the existing API-registry executor and
``insert_facts`` / ``refresh_after_write``. It does not invent a second
pipeline.

The row→Fact projector is the Clinical Trials seed special case (CT.gov
flattened fields → ClinicalTrial + Organization + MedicalCondition).
Paid / hosted catalog bindings raise and stay premium.
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlparse

from infona_client.api_registry.catalog import make_api_source_catalog
from infona_client.api_registry.executor import RegistryApiSource, SecretResolver
from infona_client.blueprint.models import (
    REAL_NCT_RE,
    AcquisitionInstruction,
    BlueprintManifest,
    SourceDefinition,
)
from infona_client.blueprint.plan import (
    BlueprintAcquisitionFailed,
    BlueprintCredentialsMissing,
    BlueprintPaidBinding,
)
from infona_client.graph.facts import Fact
from infona_client.graph.kg_writer import insert_facts, refresh_after_write
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import kg_graph_uri

FIRST_RUN_MAX_ROWS = 25
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
# Titles / ids / status are one value. Do not comma-split them.
_SCALAR_TRIAL_LEAVES = frozenset(
    {
        "nct_id",
        "brief_title",
        "official_title",
        "overall_status",
        "study_type",
        "enrollment",
    }
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
    """Split list-like registry cells (conditions, phases). Not titles."""
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


def _scalar_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        return str(raw[0]).strip() if raw else ""
    return str(raw).strip()


def _trial_literals(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dest in _REGISTRY_TO_TRIAL.items():
        if src not in row or dest in out:
            continue
        if dest in _SCALAR_TRIAL_LEAVES:
            value = _scalar_text(row[src])
            if value:
                out[dest] = value
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


def request_secret_resolver(
    credentials: Mapping[str, str] | None,
    environ: Mapping[str, str],
) -> SecretResolver:
    """Resolve a key from the request body, then the request-scoped environ."""

    async def resolve(name: str) -> str | None:
        if credentials:
            raw = credentials.get(name)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        raw = environ.get(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    return resolve


async def acquire_condition_set(
    manifest: BlueprintManifest,
    *,
    tenant_id: str,
    kg: str,
    max_rows: int = FIRST_RUN_MAX_ROWS,
    executor: RegistryApiSource | None = None,
    neptune: Any = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """First pull declared by the package. Uses the registry executor + insert_facts."""
    runner = executor or RegistryApiSource()
    catalog = make_api_source_catalog()
    env = dict(environ if environ is not None else os.environ)
    resolver = request_secret_resolver(credentials, env)
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
            secret_resolver=resolver,
            environ=env,
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


__all__ = [
    "FIRST_RUN_MAX_ROWS",
    "acquire_condition_set",
    "catalog_slug_for",
    "facts_from_registry_rows",
    "request_secret_resolver",
]
