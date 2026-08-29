"""Dataset / split loader. Grows by adding fixture files, not by changing code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .graph_delta import GraphDelta
from .models import EntityType, Neighborhood, Ontology, Relation, Skill

TASK_FAMILIES: tuple[str, ...] = (
    "entity_typing",
    "property_schema_mapping",
    "entity_resolution",
    "relation_inference",
    "constraint_violation_repair",
    "conflict_resolution",
    "ontology_extension",
    "multi_step_ingest",
)

SPLITS: tuple[str, ...] = (
    "known_ontology_unseen_instances",
    "unseen_ontology_branches",
    "adversarial_conflicting",
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    family: str
    split: str
    input: Mapping[str, Any]
    gold: GraphDelta
    neighborhood: Neighborhood
    notes: str = ""

    def __post_init__(self) -> None:
        if self.family not in TASK_FAMILIES:
            raise ValueError(
                f"task {self.task_id!r} unknown family {self.family!r}; "
                f"allowed: {TASK_FAMILIES}"
            )
        if self.split not in SPLITS:
            raise ValueError(
                f"task {self.task_id!r} unknown split {self.split!r}; "
                f"allowed: {SPLITS}"
            )
        if not self.task_id:
            raise ValueError("task_id must be non-empty")


@dataclass(frozen=True)
class FixtureBundle:
    ontology: Ontology
    tasks: tuple[Task, ...]

    def tasks_for(
        self, *, family: str | None = None, split: str | None = None
    ) -> tuple[Task, ...]:
        out = self.tasks
        if family is not None:
            out = tuple(t for t in out if t.family == family)
        if split is not None:
            out = tuple(t for t in out if t.split == split)
        return out


def load_ontology(path: Path | None = None) -> Ontology:
    path = path or (FIXTURES_DIR / "ontology.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    types = {
        item["type_id"]: EntityType(
            type_id=item["type_id"],
            label=item.get("label", item["type_id"]),
            parent_ids=tuple(item.get("parent_ids") or ()),
        )
        for item in raw["types"]
    }
    relations = {
        item["relation_id"]: Relation(
            relation_id=item["relation_id"],
            label=item.get("label", item["relation_id"]),
            source_type=item["source_type"],
            target_type=item["target_type"],
            parent_ids=tuple(item.get("parent_ids") or ()),
        )
        for item in raw["relations"]
    }
    skills = tuple(
        Skill(
            skill_id=item["skill_id"],
            title=item.get("title", item["skill_id"]),
            body=item["body"],
            attached_to=item["attached_to"],
            kind=item["kind"],
            provenance=item.get("provenance", "curated"),
            enabled=bool(item.get("enabled", True)),
            version=int(item.get("version", 1) or 1),
        )
        for item in raw.get("skills") or ()
    )
    return Ontology(types=types, relations=relations, skills=skills)


def load_tasks(path: Path | None = None) -> tuple[Task, ...]:
    path = path or (FIXTURES_DIR / "tasks.jsonl")
    tasks = tuple(_parse_task(obj) for obj in _read_jsonl(path))
    ids = [t.task_id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task_id in dataset")
    return tasks


def load_fixture_bundle(
    ontology_path: Path | None = None, tasks_path: Path | None = None
) -> FixtureBundle:
    return FixtureBundle(
        ontology=load_ontology(ontology_path),
        tasks=load_tasks(tasks_path),
    )


def _parse_task(raw: dict[str, Any]) -> Task:
    nb_raw = raw.get("neighborhood") or {}
    neighborhood = Neighborhood(
        type_ids=tuple(nb_raw.get("type_ids") or ()),
        relation_ids=tuple(nb_raw.get("relation_ids") or ()),
        include_ancestors=bool(nb_raw.get("include_ancestors", True)),
        include_incident_relations=bool(
            nb_raw.get("include_incident_relations", True)
        ),
    )
    gold_raw = raw.get("gold") or {}
    if "delta" in gold_raw:
        delta = GraphDelta.from_dict(gold_raw["delta"])
    else:
        delta = GraphDelta.from_dict(gold_raw)
    return Task(
        task_id=str(raw["task_id"]),
        family=str(raw["family"]),
        split=str(raw["split"]),
        input=dict(raw.get("input") or {}),
        gold=delta,
        neighborhood=neighborhood,
        notes=str(raw.get("notes") or ""),
    )


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            obj = json.loads(stripped)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{line_no} is not a JSON object")
            yield obj
