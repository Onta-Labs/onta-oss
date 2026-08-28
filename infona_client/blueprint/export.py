"""Export a live workspace KG to a schema-valid Blueprint (INF-565).

Reads the ontology slice through the existing catalog helper re-exported
from ``graph/ontology_queries.py`` (``schema_types_for_kg``). Does not
hand-roll a second reader. Source definitions come from the API-registry
catalog; credentials, scheduled job rows, instance records, citations,
and freshness *status* are classified and dropped. Anything we cannot
classify raises :class:`ExportRedactionError`.

Boundary: OSS. ``infona_client.*`` / stdlib only — no ``from infona.*``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from infona_client.api_registry.catalog import get_api_source_catalog
from infona_client.api_registry.spec import ApiSourceSpec, AuthMode
from infona_client.blueprint.models import (
    SCHEMA_STATUS,
    SCHEMA_VERSION,
    SLUG_RE,
    AcquisitionInstruction,
    BlueprintManifest,
    BlueprintSkill,
    Concept,
    ConceptAttribute,
    ConflictRule,
    EntityResolutionConfig,
    EvalCheck,
    ExampleQuestion,
    Freshness,
    FreshnessPolicy,
    Lineage,
    NamedFunction,
    Relationship,
    Rules,
    SourceDefinition,
    SourceMapping,
    Task,
    TombstoneRule,
    parse_blueprint,
)
from infona_client.blueprint.package import package_files
from infona_client.blueprint.redact import (
    ExportRedactionError,
    assert_exportable,
    redact_definition_url,
)
from infona_client.blueprint.validate import validate_blueprint
from infona_client.enrichment.strategy import TypeStrategy, load_strategy
from infona_client.functions.store import StoredFunction, make_function_store
from infona_client.graph.ontology_catalog_models import OntoAttrRecord, SchemaTypeSummary
from infona_client.graph.ontology_queries import schema_types_for_kg
from infona_client.graph.predicates import ATTR_META_SUFFIXES
from infona_client.graph.queries import is_valid_kg_name
from infona_client.graph.store import GraphStore, get_graph_store
from infona_client.resolver.er.types import config_for_with_hierarchy
from infona_client.scheduling.models import USER_SCHEDULABLE_ACTIONS, Schedule
from infona_client.scheduling.store import make_schedule_store
from infona_client.skills.models import TypeSkill
from infona_client.skills.store import make_type_skill_store

_ID_HINTS = (
    "nct_id",
    "org_name",
    "email",
    "isbn",
    "doi",
    "gtin",
    "orcid",
    "npi",
    "id",
    "name",
    "title",
)
_MAPPING_ALIASES = {
    "title": ("official_title", "brief_title", "title"),
    "status": ("overall_status", "status"),
    "brief_title": ("official_title", "brief_title"),
    "lead_sponsor_class": ("org_class",),
}
_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


@dataclass
class ExportOptions:
    """Package identity. None of these may be instance data."""

    namespace: str = "infona"
    version: str = "0.1.0"
    license: str = "Apache-2.0"
    attribution: str | None = None
    name: str | None = None
    package_id: str | None = None
    acquisition_revision: int = 1


@dataclass
class BlueprintExport:
    """Validated manifest plus the on-disk file map."""

    manifest: BlueprintManifest
    files: dict[str, str] = field(default_factory=dict)

    def as_response(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.model_dump(mode="json", exclude_none=True),
            "files": dict(self.files),
        }


async def export_blueprint(
    *,
    tenant_id: str,
    kg: str,
    store: GraphStore | None = None,
    options: ExportOptions | None = None,
    skill_store: Any | None = None,
    function_store: Any | None = None,
    schedule_store: Any | None = None,
    extra_sources: Sequence[ApiSourceSpec] | None = None,
) -> BlueprintExport:
    """Build a schema-valid Blueprint from a live workspace KG.

    ``include_sample`` is intentionally absent: live records are never the
    sample (INF-564 / INF-587). A sample is authored, not dumped.
    """
    if not is_valid_kg_name(kg):
        raise ExportRedactionError(f"kg name {kg!r} is not a valid identifier")
    options = options or ExportOptions()
    store = store or get_graph_store()

    summaries = await schema_types_for_kg(
        store, tenant_id=tenant_id, kg=kg, include_attrs=True
    )
    selected = _select_types(summaries)
    if not selected:
        raise ExportRedactionError(
            f"no ontology slice for {tenant_id}/{kg}: catalog is empty"
        )

    concepts, relationships, attr_index, parent_of = _concepts_from_summaries(selected)
    strategies = await _load_strategies(tenant_id, [c.name for c in concepts])
    catalog = get_api_source_catalog(tenant_id)
    specs = list(catalog.entries.values())
    if extra_sources:
        specs.extend(extra_sources)

    type_names = {c.name for c in concepts}
    attr_names = {a.name for c in concepts for a in c.attributes}
    matched = [s for s in specs if s.enabled and _source_covers(s, type_names, attr_names)]
    if not matched:
        raise ExportRedactionError(
            f"cannot classify any source definition for {kg}; "
            "a Blueprint requires at least one"
        )

    sources, example_asks = _export_sources(matched, attr_index, {r.name for r in relationships})
    schedules = await _list_schedules(schedule_store, tenant_id, kg)
    cadence_by_action = _cadence_policies(schedules)
    rules = _export_rules(sources, strategies)
    acquisition = _export_acquisition(sources, matched, rules, cadence_by_action)
    tasks = _export_tasks(schedules, concepts, sources)
    validation = _default_validation(concepts)
    freshness = _export_freshness(concepts, sources, strategies, cadence_by_action, parent_of)
    skills = await _export_skills(skill_store, tenant_id, type_names)
    functions = await _export_functions(function_store, tenant_id, type_names)
    examples = _export_examples(example_asks, concepts, relationships)
    evals = _export_evals(concepts, relationships)

    today = date.today().isoformat()
    package_id = options.package_id or f"{options.namespace}/{_kebab(kg)}"
    name = options.name or _title_from_kg(kg)
    attribution = options.attribution or (
        f"Exported from workspace {tenant_id}/{kg}. Sources retain their publishers' licenses."
    )
    raw = {
        "schema_version": SCHEMA_VERSION,
        "schema_status": SCHEMA_STATUS,
        "id": package_id,
        "name": name,
        "namespace": options.namespace,
        "version": options.version,
        "acquisition_revision": options.acquisition_revision,
        "license": options.license,
        "attribution": attribution,
        "published_at": today,
        "last_reviewed_at": today,
        "lineage": {"chain": []},
        "concepts": [c.model_dump(mode="json", exclude_none=True) for c in concepts],
        "relationships": [r.model_dump(mode="json", exclude_none=True) for r in relationships],
        "tasks": [t.model_dump(mode="json", exclude_none=True) for t in tasks],
        "rules": rules.model_dump(mode="json", exclude_none=True),
        "sources": [s.model_dump(mode="json", exclude_none=True) for s in sources],
        "acquisition": [a.model_dump(mode="json", exclude_none=True) for a in acquisition],
        "validation": validation,
        "freshness": freshness.model_dump(mode="json", exclude_none=True),
        "skills": [s.model_dump(mode="json", exclude_none=True) for s in skills],
        "functions": [f.model_dump(mode="json", exclude_none=True) for f in functions],
        "mcp_tools": ["ask"],
        "examples": [e.model_dump(mode="json") for e in examples],
        "evals": [e.model_dump(mode="json") for e in evals],
    }
    assert_exportable(raw)
    errors = validate_blueprint(raw)
    if errors:
        raise ExportRedactionError(
            "exported document is not a valid v1 Blueprint: " + "; ".join(errors)
        )
    manifest = parse_blueprint(raw)
    return BlueprintExport(manifest=manifest, files=package_files(manifest))


def _select_types(summaries: Sequence[SchemaTypeSummary]) -> list[SchemaTypeSummary]:
    by_name = {s.name: s for s in summaries}
    populated = {s.name for s in summaries if s.entity_count > 0}
    if populated:
        names = set(populated)
        changed = True
        while changed:
            changed = False
            for name in list(names):
                summary = by_name.get(name)
                if summary is None:
                    continue
                if summary.parent_type and summary.parent_type not in names:
                    if summary.parent_type in by_name:
                        names.add(summary.parent_type)
                        changed = True
                for attr in summary.attributes:
                    target = attr.range_type
                    if attr.kind == "relationship" and target and target not in names:
                        if target in by_name:
                            names.add(target)
                            changed = True
        return [by_name[n] for n in names if n in by_name]
    return [s for s in summaries if s.attributes]


def _concepts_from_summaries(
    summaries: Sequence[SchemaTypeSummary],
) -> tuple[list[Concept], list[Relationship], dict[str, ConceptAttribute], dict[str, str]]:
    concepts: list[Concept] = []
    relationships: list[Relationship] = []
    attr_index: dict[str, ConceptAttribute] = {}
    parent_of: dict[str, str] = {}
    type_names = {s.name for s in summaries}

    for summary in summaries:
        if summary.parent_type:
            parent_of[summary.name] = summary.parent_type
        attrs: list[ConceptAttribute] = []
        for rec in summary.attributes:
            slot = _attribute_from_record(summary.name, rec, type_names)
            if slot is None:
                continue
            attrs.append(slot)
            attr_index[f"{summary.name}.{slot.name}"] = slot
            if slot.kind == "relationship" and slot.range_type:
                relationships.append(
                    Relationship(
                        name=slot.name,
                        source=summary.name,
                        target=slot.range_type,
                        cardinality=slot.cardinality,
                        description=slot.description,
                    )
                )
        if not attrs:
            raise ExportRedactionError(
                f"concept {summary.name!r} has no exportable attributes"
            )
        identity = _infer_identity(summary.name, attrs)
        concepts.append(
            Concept(
                name=summary.name,
                label=_label_from_type(summary.name),
                description=summary.description or "",
                identity=identity,
                parent_type=summary.parent_type,
                attributes=attrs,
            )
        )
    concepts.sort(key=lambda c: c.name)
    relationships.sort(key=lambda r: (r.source, r.name))
    return concepts, relationships, attr_index, parent_of


def _attribute_from_record(
    type_name: str, rec: OntoAttrRecord, type_names: set[str]
) -> ConceptAttribute | None:
    leaf = rec.name
    if leaf in ATTR_META_SUFFIXES or leaf.endswith("_source_url") or leaf.endswith(
        "_provenance"
    ):
        raise ExportRedactionError(
            f"concepts.{type_name}.attributes.{leaf}: workspace-only leaf "
            "(citation / provenance); cannot export"
        )
    if rec.deprecated_at:
        return None
    if rec.kind == "literal":
        datatype = rec.datatype or "string"
        return ConceptAttribute(
            name=leaf,
            kind="literal",
            datatype=datatype,
            description=rec.description or "",
            optional=not rec.core_slot,
            cardinality=rec.cardinality or "1:1",
        )
    range_type = rec.range_type
    if not range_type:
        raise ExportRedactionError(
            f"concepts.{type_name}.attributes.{leaf}: relationship has no "
            "range_type; cannot classify"
        )
    if range_type not in type_names:
        # Range points outside this KG slice — skip rather than invent a concept.
        return None
    return ConceptAttribute(
        name=leaf,
        kind="relationship",
        range_type=range_type,
        description=rec.description or "",
        optional=not rec.core_slot,
        cardinality=rec.cardinality or "N:1",
    )


def _infer_identity(type_name: str, attrs: Sequence[ConceptAttribute]) -> list[str]:
    literals = {a.name: a for a in attrs if a.kind == "literal"}
    if not literals:
        raise ExportRedactionError(
            f"concepts.{type_name}: no literal attributes to use as identity"
        )
    for hint in _ID_HINTS:
        if hint in literals:
            return [hint]
    id_like = [n for n in literals if n.endswith("_id") or n.endswith("_key")]
    if id_like:
        return [id_like[0]]
    required = [a.name for a in attrs if a.kind == "literal" and not a.optional]
    if required:
        return [required[0]]
    return [next(iter(literals))]


def _label_from_type(name: str) -> str:
    spaced = _SNAKE_RE.sub(" ", name).replace("_", " ")
    return spaced[0].upper() + spaced[1:] if spaced else name


def _kebab(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "kg"


def _title_from_kg(kg: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", kg) if part) or kg


async def _load_strategies(tenant_id: str, type_names: Iterable[str]) -> dict[str, TypeStrategy]:
    out: dict[str, TypeStrategy] = {}
    for name in type_names:
        out[name] = await load_strategy(None, tenant_id, name)  # type: ignore[arg-type]
    return out


def _source_covers(spec: ApiSourceSpec, type_names: set[str], attr_names: set[str]) -> bool:
    kinds = {k.lower() for k in spec.coverage.entity_kinds}
    for type_name in type_names:
        snake = _SNAKE_RE.sub("_", type_name).lower()
        if type_name.lower() in kinds or snake in kinds:
            return True
    covered = {a.lower() for a in spec.coverage.attributes}
    if covered & {a.lower() for a in attr_names}:
        return True
    return False


def _export_sources(
    specs: Sequence[ApiSourceSpec],
    attr_index: dict[str, ConceptAttribute],
    rel_names: set[str],
) -> tuple[list[SourceDefinition], list[str]]:
    sources: list[SourceDefinition] = []
    asks: list[str] = []
    for spec in specs:
        if spec.auth.secret_ref:
            # Pointer at a per-tenant secret — drop the pointer, keep BYOK name.
            pass
        mappings = _mappings_for_spec(spec, attr_index, rel_names)
        if not mappings:
            raise ExportRedactionError(
                f"source {spec.slug!r}: no field mapping binds to the ontology slice"
            )
        url = _source_url(spec)
        credential: str
        key_env = ""
        if spec.auth.mode is AuthMode.none or spec.auth.mode == AuthMode.none:
            if spec.auth.key_env:
                raise ExportRedactionError(
                    f"source {spec.slug!r}: key_env set on an unauthenticated source"
                )
            credential = "none"
        else:
            if not spec.auth.key_env:
                raise ExportRedactionError(
                    f"source {spec.slug!r}: keyed source has no key_env NAME (BYOK)"
                )
            credential = "byok"
            key_env = spec.auth.key_env
        source_id = spec.slug if SLUG_RE.match(spec.slug) else _kebab(spec.slug)
        if not SLUG_RE.match(source_id):
            raise ExportRedactionError(f"source slug {spec.slug!r} is not a Blueprint id")
        license_id = "CC0-1.0" if credential == "none" else "NOASSERTION"
        kind = "public_registry" if credential == "none" else "api"
        sources.append(
            SourceDefinition(
                id=source_id,
                title=spec.title or spec.slug,
                kind=kind,
                publisher=spec.publisher or "unknown",
                description=spec.description or "",
                license=license_id,
                url=url,
                credential=credential,  # type: ignore[arg-type]
                key_env=key_env,
                declared_cadence="as published",
                mappings=mappings,
            )
        )
        asks.extend(q for q in spec.coverage.example_asks if q and q.strip())
    return sources, asks


def _source_url(spec: ApiSourceSpec) -> str:
    url = ""
    endpoint = spec.endpoint()
    if spec.base_url and endpoint is not None and endpoint.path:
        url = spec.base_url.rstrip("/") + "/" + endpoint.path.lstrip("/")
    elif spec.docs_url:
        url = spec.docs_url
    elif spec.base_url:
        url = spec.base_url
    if not url:
        raise ExportRedactionError(f"source {spec.slug!r}: no definition URL")
    return redact_definition_url(url)


def _mappings_for_spec(
    spec: ApiSourceSpec,
    attr_index: dict[str, ConceptAttribute],
    rel_names: set[str],
) -> list[SourceMapping]:
    mappings: list[SourceMapping] = []
    seen: set[tuple[str, str]] = set()
    for endpoint in spec.endpoints:
        for out_col, source_path in endpoint.field_mappings.items():
            bound = _bind_mapping(out_col, source_path, attr_index, rel_names)
            if bound is None:
                continue
            key = (bound.source_field, bound.lands_on)
            if key in seen:
                continue
            seen.add(key)
            mappings.append(bound)
    return mappings


def _bind_mapping(
    column: str,
    source_path: str,
    attr_index: dict[str, ConceptAttribute],
    rel_names: set[str],
) -> SourceMapping | None:
    candidates = [column, *(_MAPPING_ALIASES.get(column, ()))]
    for name in candidates:
        if name in rel_names:
            return SourceMapping(source_field=source_path, lands_on=name, kind="relationship")
        for key, slot in attr_index.items():
            if slot.name == name or key.endswith(f".{name}"):
                lands = name if slot.kind == "relationship" and name in rel_names else key
                if slot.kind == "relationship" and name in rel_names:
                    lands = name
                return SourceMapping(
                    source_field=source_path,
                    lands_on=lands,
                    kind=slot.kind,
                )
    return None


async def _list_schedules(schedule_store: Any | None, tenant_id: str, kg: str) -> list[Schedule]:
    store = schedule_store or make_schedule_store()
    try:
        rows = await store.list_for_tenant(tenant_id)
    except Exception:
        return []
    return [s for s in rows if s.kg_name == kg]


def _cadence_policies(schedules: Sequence[Schedule]) -> dict[str, str]:
    """Map action → cadence *policy* string. Never the cron row or last_run."""
    out: dict[str, str] = {}
    for sched in schedules:
        if sched.action not in USER_SCHEDULABLE_ACTIONS:
            continue
        if sched.interval_seconds:
            days = max(1, int(round(sched.interval_seconds / 86400)))
            if sched.interval_seconds < 86400:
                out[sched.action] = f"every {sched.interval_seconds} seconds"
            elif days == 1:
                out[sched.action] = "daily"
            elif days == 7:
                out[sched.action] = "weekly"
            else:
                out[sched.action] = f"every {days} days"
        elif sched.cron:
            out[sched.action] = "cron"
        # last_run / next_run / id / params (may hold secret_ref) are ignored.
    return out


def _export_rules(
    sources: Sequence[SourceDefinition], strategies: Mapping[str, TypeStrategy]
) -> Rules:
    winner = sources[0].id
    conflicts: list[ConflictRule] = []
    for type_name, strategy in strategies.items():
        for attr_name, attr_strategy in strategy.attributes.items():
            policy = (attr_strategy.conflict_policy or "").strip()
            if not policy:
                continue
            conflicts.append(
                ConflictRule(
                    attribute=f"{type_name}.{attr_name}",
                    winner=winner,
                    notes=policy,
                )
            )
    if not conflicts:
        conflicts.append(
            ConflictRule(
                attribute=f"{_first_literal_attr(strategies)}",
                winner=winner,
                notes=f"{winner} wins on conflict.",
            )
        )
    return Rules(
        conflict=conflicts,
        tombstones=TombstoneRule(disappeared_row="withdrawn", delete_forbidden=True),
    )


def _first_literal_attr(strategies: Mapping[str, TypeStrategy]) -> str:
    for type_name in strategies:
        return f"{type_name}.id"
    return "id"


def _export_acquisition(
    sources: Sequence[SourceDefinition],
    specs: Sequence[ApiSourceSpec],
    rules: Rules,
    cadence_by_action: Mapping[str, str],
) -> list[AcquisitionInstruction]:
    spec_by_id = {s.slug: s for s in specs}
    refresh = cadence_by_action.get("enrich") or "on the declared freshness window"
    out: list[AcquisitionInstruction] = []
    for source in sources:
        spec = spec_by_id.get(source.id)
        seed = _seed_query(spec)
        page_cap = _page_cap(spec)
        conflict_notes = rules.conflict[0].notes if rules.conflict else f"{source.id} wins."
        out.append(
            AcquisitionInstruction(
                source=source.id,
                seed_query=seed,
                page_cap=page_cap,
                first_pull=(
                    f"Mint entities from {source.title} using the declared field mappings."
                ),
                later_refresh=(
                    f"Re-fetch rows whose freshness window has elapsed ({refresh}). "
                    "Do not re-mint identity keys."
                ),
                disappeared_row=(
                    "If a previously acquired identity is gone from the source, "
                    "mark it withdrawn and stop refreshing it. Do not delete the node."
                ),
                conflict=conflict_notes,
            )
        )
    return out


def _seed_query(spec: ApiSourceSpec | None) -> str:
    if spec is None:
        return "as published"
    endpoint = spec.endpoint()
    if endpoint is None:
        return "as published"
    if endpoint.smoke_bindings:
        return "&".join(f"{k}={v}" for k, v in endpoint.smoke_bindings.items())
    if endpoint.params:
        first = endpoint.params[0]
        target = first.target or first.name
        return f"{target}=<query>"
    return "as published"


def _page_cap(spec: ApiSourceSpec | None) -> int:
    if spec is None:
        return 100
    endpoint = spec.endpoint()
    if endpoint is None or endpoint.pagination is None:
        return 100
    pag = endpoint.pagination
    pages = getattr(pag, "max_pages", None) or 1
    size = getattr(pag, "page_size", None) or 50
    return max(1, int(pages) * int(size))


def _export_tasks(
    schedules: Sequence[Schedule],
    concepts: Sequence[Concept],
    sources: Sequence[SourceDefinition],
) -> list[Task]:
    attaches = concepts[0].name if concepts else None
    tasks: list[Task] = []
    seen: set[str] = set()
    for sched in schedules:
        if sched.action not in USER_SCHEDULABLE_ACTIONS:
            continue
        task_id = _kebab(sched.action)
        if task_id in seen:
            continue
        seen.add(task_id)
        tasks.append(
            Task(
                id=task_id,
                description=f"Run {sched.action} on the declared cadence.",
                writes=sched.action != "notify",
                attaches_to=attaches,
                inputs=[],
            )
        )
    if not tasks:
        for source in sources:
            task_id = f"acquire_{source.id}"
            if not SLUG_RE.match(task_id):
                task_id = "acquire"
            if task_id in seen:
                continue
            seen.add(task_id)
            tasks.append(
                Task(
                    id=task_id,
                    description=f"First pull from {source.title}.",
                    writes=True,
                    attaches_to=attaches,
                    inputs=["query"],
                )
            )
    if not tasks:
        raise ExportRedactionError("cannot classify any task for this workspace")
    return tasks


def _default_validation(concepts: Sequence[Concept]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for concept in concepts:
        key = concept.identity[0]
        checks.append(
            {
                "id": _kebab(f"{concept.name}-has-{key}")[:64],
                "description": f"A {concept.label} without {key} is rejected.",
                "rule": f"{concept.name}.{key} must be a non-empty value.",
            }
        )
    return checks


def _export_freshness(
    concepts: Sequence[Concept],
    sources: Sequence[SourceDefinition],
    strategies: Mapping[str, TypeStrategy],
    cadence_by_action: Mapping[str, str],
    parent_of: dict[str, str],
) -> Freshness:
    cadence = cadence_by_action.get("enrich") or "weekly"
    policies: list[FreshnessPolicy] = []
    seen: set[str] = set()
    for concept in concepts:
        for key in concept.identity:
            target = f"{concept.name}.{key}"
            if target in seen:
                continue
            seen.add(target)
            policies.append(
                FreshnessPolicy(target=target, stale_after_days="never", cadence="on_create")
            )
        for attr in concept.attributes:
            if attr.kind != "literal" or attr.name in concept.identity:
                continue
            if "status" in attr.name or attr.name in {"phase", "enrollment"}:
                target = f"{concept.name}.{attr.name}"
                if target in seen:
                    continue
                seen.add(target)
                policies.append(
                    FreshnessPolicy(target=target, stale_after_days=14, cadence=cadence)
                )
    if sources and sources[0].id not in seen:
        policies.append(
            FreshnessPolicy(target=sources[0].id, stale_after_days=14, cadence=cadence)
        )
    if not policies:
        raise ExportRedactionError("cannot classify a freshness policy")

    er: list[EntityResolutionConfig] = []
    for concept in concepts:
        er.append(_er_for_concept(concept, parent_of, strategies.get(concept.name)))
    return Freshness(policies=policies, er=er)


def _er_for_concept(
    concept: Concept,
    parent_of: dict[str, str],
    strategy: TypeStrategy | None,
) -> EntityResolutionConfig:
    attr_names = {a.name for a in concept.attributes}
    identity = list(concept.identity)
    cfg = config_for_with_hierarchy(concept.name, parent_of)
    if strategy and strategy.match_key and strategy.match_key in attr_names:
        if strategy.match_key not in identity:
            identity = [strategy.match_key, *identity]

    if cfg is not None:
        signals: list[str] = []
        weights: list[float] = []
        for signal, weight in zip(cfg.signals, cfg.weights):
            mapped = _map_er_signal(signal, attr_names)
            if mapped is None:
                continue
            signals.append(mapped)
            weights.append(float(weight))
        if signals:
            total = sum(weights)
            weights = [w / total for w in weights]
            decisive = [
                m
                for m in (_map_er_signal(s, attr_names) for s in cfg.decisive_signals)
                if m
            ]
            return EntityResolutionConfig(
                type_name=concept.name,
                identity=identity,
                blocking=identity,
                signals=signals,
                weights=weights,
                auto_merge_threshold=cfg.auto_merge_threshold,
                review_threshold=cfg.review_threshold,
                decisive_signals=decisive,
                reversible=True,
            )

    n = len(identity)
    return EntityResolutionConfig(
        type_name=concept.name,
        identity=identity,
        blocking=identity,
        signals=identity,
        weights=[1.0 / n] * n,
        auto_merge_threshold=1.0 if n == 1 else 0.9,
        review_threshold=1.0 if n == 1 else 0.7,
        decisive_signals=list(identity),
        reversible=True,
    )


def _map_er_signal(signal: str, attr_names: set[str]) -> str | None:
    if signal in attr_names:
        return signal
    if signal == "name" and "org_name" in attr_names:
        return "org_name"
    return None


async def _export_skills(
    skill_store: Any | None, tenant_id: str, type_names: set[str]
) -> list[BlueprintSkill]:
    store = skill_store or make_type_skill_store()
    try:
        rows: list[TypeSkill] = await store.list_for_tenant(tenant_id)
    except Exception:
        return []
    out: list[BlueprintSkill] = []
    for skill in rows:
        if skill.type_name not in type_names or not skill.enabled:
            continue
        # metadata / tenant_id / timestamps stay workspace-side.
        out.append(
            BlueprintSkill(
                slug=skill.slug,
                type_name=skill.type_name,
                body=skill.body,
                title=skill.title or "",
                summary=skill.summary or "",
            )
        )
    return out


async def _export_functions(
    function_store: Any | None, tenant_id: str, type_names: set[str]
) -> list[NamedFunction]:
    store = function_store or make_function_store()
    try:
        rows: list[StoredFunction] = await store.list_for_tenant(tenant_id)
    except Exception:
        return []
    out: list[NamedFunction] = []
    for fn in rows:
        if fn.entity_type not in type_names:
            continue
        # endpoint_url is an implementation — never export (INF-560 C1).
        name = fn.name if SLUG_RE.match(fn.name) else _kebab(fn.name)
        if not SLUG_RE.match(name):
            raise ExportRedactionError(
                f"function {fn.name!r}: name is not a slug; cannot classify"
            )
        out.append(
            NamedFunction(
                name=name,
                type_name=fn.entity_type,
                description=fn.description or f"Named function {fn.name}.",
            )
        )
    return out


def _export_examples(
    asks: Sequence[str],
    concepts: Sequence[Concept],
    relationships: Sequence[Relationship],
) -> list[ExampleQuestion]:
    questions: list[str] = []
    for ask in asks:
        text = ask.strip()
        if text and text not in questions:
            questions.append(text)
    if len(questions) < 3:
        primary = concepts[0]
        questions.append(f"How many {primary.label} records does the graph hold?")
        questions.append(
            f"What is the {primary.identity[0]} of each {primary.label}?"
        )
        if relationships:
            rel = relationships[0]
            questions.append(
                f"Which {rel.source} values have a {rel.name} {rel.target}?"
            )
        elif len(concepts) > 1:
            questions.append(f"How many {concepts[1].label} nodes are linked?")
        else:
            questions.append(f"List every {primary.label} identity key.")
    return [ExampleQuestion(question=q) for q in questions[:12]]


def _export_evals(
    concepts: Sequence[Concept], relationships: Sequence[Relationship]
) -> list[EvalCheck]:
    evals: list[EvalCheck] = []
    for concept in concepts:
        key = concept.identity[0]
        evals.append(
            EvalCheck(
                id=_kebab(f"every-{concept.name}-has-{key}")[:64],
                kind="structural",
                description=f"Every {concept.label} has {key}.",
                still_works_when=f"count of {concept.name} with empty {key} = 0",
            )
        )
    for rel in relationships:
        evals.append(
            EvalCheck(
                id=_kebab(f"{rel.name}-is-{rel.target}")[:64],
                kind="structural",
                description=f"Every {rel.name} points at a {rel.target}.",
                still_works_when="no string in that slot; no dangling edge",
            )
        )
    if len(evals) < 3:
        evals.append(
            EvalCheck(
                id="refresh-does-not-split-identity",
                kind="structural",
                description="Refresh does not split an identity key.",
                still_works_when="same identity maps to one node, before and after",
            )
        )
    return evals[:12]


__all__ = [
    "BlueprintExport",
    "ExportOptions",
    "ExportRedactionError",
    "export_blueprint",
]
