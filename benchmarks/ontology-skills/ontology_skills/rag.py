"""Condition 9: retrieve fixture skills by embedding similarity.

This is not the compiler. k matches the routed compiled skill count for the
same task so the token budget is comparable. Retrieval scores are logged,
never written as skill provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .compiler import compile_routed
from .dataset import Task
from .embedder import Embedder
from .models import CompiledSkillSet, Ontology, Skill


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    skill_id: str
    score: float

    def to_dict(self) -> dict[str, object]:
        return {"skill_id": self.skill_id, "score": self.score}


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    compiled: CompiledSkillSet
    hits: tuple[RetrievalHit, ...]
    embedder_id: str
    k: int

    def to_log_dict(self) -> dict[str, object]:
        return {
            "embedder_id": self.embedder_id,
            "k": self.k,
            "hits": [hit.to_dict() for hit in self.hits],
        }


def empty_retrieval_log() -> dict[str, object]:
    return {"embedder_id": None, "k": None, "hits": None}


def retrieve_skills(
    ontology: Ontology, task: Task, embedder: Embedder
) -> RetrievalResult:
    """Cosine top-k over enabled skill bodies vs json.dumps(task.input)."""
    routed = compile_routed(ontology, task.neighborhood)
    k = len(routed.skills)
    corpus = _enabled_skills(ontology)
    query = json.dumps(dict(task.input), sort_keys=True, ensure_ascii=False)
    if k == 0 or not corpus:
        compiled = CompiledSkillSet(
            mode="rag",
            skills=(),
            type_lineage=(),
            relation_ids=(),
        )
        return RetrievalResult(
            compiled=compiled, hits=(), embedder_id=embedder.embedder_id, k=k
        )
    texts = [query] + [skill.body for skill in corpus]
    vectors = embedder.embed(texts)
    if len(vectors) != len(texts):
        raise RuntimeError("embedder returned the wrong number of vectors")
    query_vec = vectors[0]
    ranked: list[tuple[float, str, Skill]] = []
    for skill, vec in zip(corpus, vectors[1:]):
        ranked.append((_cosine(query_vec, vec), skill.skill_id, skill))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    picked = ranked[:k]
    compiled = CompiledSkillSet(
        mode="rag",
        skills=tuple(row[2] for row in picked),
        type_lineage=(),
        relation_ids=(),
    )
    hits = tuple(RetrievalHit(skill_id=row[1], score=row[0]) for row in picked)
    return RetrievalResult(
        compiled=compiled,
        hits=hits,
        embedder_id=embedder.embedder_id,
        k=k,
    )


def _enabled_skills(ontology: Ontology) -> tuple[Skill, ...]:
    enabled = [s for s in ontology.skills if s.enabled]
    enabled.sort(key=lambda s: (s.kind, s.attached_to, s.skill_id))
    return tuple(enabled)


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimension mismatch")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for a, b in zip(left, right):
        dot += a * b
        na += a * a
        nb += b * b
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))
