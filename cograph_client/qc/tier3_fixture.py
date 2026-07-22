"""Tier-3 whole-product QC fixture schema + loader (ONTA-283-A, the OSS spine).

A **Tier-3 fixture** is the reproducible input to the whole-product QC capstone
(``docs/specs/onta_283_tier3_capstone.md``). It pins, in one JSON file, everything
needed to measure the compounding *goal in → gold-graded answer out* pipeline:

  * a raw natural-language **goal** (the A0 request a user would type);
  * a **source seed** — a bundled CSV/JSON under the fixtures dir, or a pinned URL
    list — so the *find* stage is replayable offline instead of depending on
    whatever the live web returns today;
  * a set of graded **questions**, each carrying an **execution-verified gold
    answer** in the ``eval_holdout_v2/gold`` shape (``gold_sparql`` +
    ``full_expected_items`` + ``full_result_count``) at a difficulty ``tier`` ∈
    {T1, T2, T3, T4}.

Optional ``enumeration_scope`` (ONTA-384) pins gold for an enumeration +
scoped-schema ingest goal (expected entity set, requested attribute ceiling,
allowed/forbidden types) so the profile half of the grader can score coverage /
scope-adherence / fragmentation of the produced graph offline.

This module is the *schema + loader + validation* only — it neither runs a
pipeline nor grades an answer (that is :mod:`cograph_client.qc.tier3_grade`). It is
**pure**: no I/O beyond reading the fixture file the caller names, no network, no
KG, deterministic.

Design mirror: the component-bar template (``pipeline/find_metrics.py`` /
``verification/verify_metrics.py``) — frozen dataclasses, a ``from_dict`` that
FAILS LOUD on a malformed fixture (a typo in a committed gold file must never be
silently absorbed), and a small ``__all__``.

Conservation contract (validated): ``full_result_count == len(full_expected_items)``
for every question — the gold answer set and its count are two views of the same
execution-verified result and can never disagree. A gold-empty question
(``full_result_count == 0`` with an empty ``full_expected_items``) is legal and is
the case the outcome grader's empty-answer guard is built around.

Boundary: OSS. Imports only stdlib.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "TIER_LABELS",
    "FixtureValidationError",
    "SourceSeed",
    "Tier3GoldQuestion",
    "EnumerationScope",
    "Tier3Fixture",
    "tier_index",
    "load_fixture",
    "load_fixtures",
]

# The four difficulty tiers, matching ``eval.py``'s buckets (T1 Count/Lookup,
# T2 Filter, T3 Join, T4 Multi-hop) and the ``eval_holdout_v2/gold`` ``tier`` field.
TIER_LABELS: tuple[str, ...] = ("T1", "T2", "T3", "T4")

# The two shapes a reproducible source seed can take.
_SEED_KINDS: tuple[str, ...] = ("bundled_file", "url_list")


class FixtureValidationError(ValueError):
    """A Tier-3 fixture is structurally invalid. Raised (never swallowed) so a
    malformed committed fixture fails loud in CI rather than silently mis-grading."""


def tier_index(tier: str) -> int:
    """Map a ``"T3"``-style label to its 1-based integer (``3``), the form
    ``eval.py``'s ``QuestionResult.tier`` uses. Raises on an unknown label."""
    label = str(tier).strip().upper()
    if label not in TIER_LABELS:
        raise FixtureValidationError(
            f"tier must be one of {TIER_LABELS}, got {tier!r}"
        )
    return TIER_LABELS.index(label) + 1


# --------------------------------------------------------------------------- #
# Source seed
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceSeed:
    """A reproducible pointer to the source the *find* stage consumes.

    Exactly one shape is populated, recorded by ``kind``:

    * ``kind="bundled_file"`` — ``path`` is a CSV/JSON bundled next to the fixture
      (resolved relative to the fixture file), so the run replays offline with no
      live web dependency. This is the preferred, stable shape.
    * ``kind="url_list"`` — ``urls`` is a *pinned* list of source URLs. Reproducible
      only as far as those URLs are stable; kept for live-web rotation fixtures.
    """

    kind: str
    path: Optional[str] = None
    urls: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SourceSeed":
        if not isinstance(d, Mapping):
            raise FixtureValidationError(f"source_seed must be an object, got {type(d).__name__}")
        kind = str(d.get("kind", "")).strip()
        if kind not in _SEED_KINDS:
            raise FixtureValidationError(
                f"source_seed.kind must be one of {_SEED_KINDS}, got {kind!r}"
            )
        if kind == "bundled_file":
            path = d.get("path")
            if not path or not str(path).strip():
                raise FixtureValidationError(
                    "source_seed.kind='bundled_file' requires a non-empty 'path'"
                )
            return cls(kind=kind, path=str(path))
        # url_list
        raw_urls = d.get("urls")
        if not isinstance(raw_urls, Sequence) or isinstance(raw_urls, (str, bytes)):
            raise FixtureValidationError(
                "source_seed.kind='url_list' requires 'urls' to be a list of strings"
            )
        urls = tuple(str(u) for u in raw_urls if str(u).strip())
        if not urls:
            raise FixtureValidationError(
                "source_seed.kind='url_list' requires at least one non-empty URL"
            )
        return cls(kind=kind, urls=urls)

    def resolve_path(self, base_dir: str) -> Optional[str]:
        """Absolute filesystem path of a ``bundled_file`` seed, resolved against the
        directory holding the fixture. ``None`` for a ``url_list`` seed."""
        if self.kind != "bundled_file" or not self.path:
            return None
        if os.path.isabs(self.path):
            return self.path
        return os.path.normpath(os.path.join(base_dir, self.path))

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "bundled_file":
            return {"kind": self.kind, "path": self.path}
        return {"kind": self.kind, "urls": list(self.urls)}


# --------------------------------------------------------------------------- #
# Graded question with execution-verified gold
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tier3GoldQuestion:
    """One graded A7 question with its execution-verified gold answer.

    The gold triple mirrors ``eval_holdout_v2/gold/*.json`` verbatim:
    ``gold_sparql`` (the query that produced the gold), ``full_expected_items``
    (the exact answer values it returned), and ``full_result_count`` (their count).
    The outcome grader scores a produced answer against ``full_expected_items``;
    ``gold_sparql`` is carried for provenance / stage attribution and is never
    executed by the pure grader.
    """

    id: str
    question: str
    tier: str
    gold_sparql: str
    full_expected_items: tuple[str, ...]
    full_result_count: int
    # Optional carried metadata (present in the holdout-v2 gold shape).
    expected_answer: str = ""
    reasoning: str = ""
    # Optional per-question set of legitimate citation/source references. When
    # present, the grader's citation-fabrication counter treats a produced citation
    # as SUPPORTED iff it matches one of these (or the fixture's source seed / gold
    # answer values); anything else is flagged as fabricated.
    gold_citations: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Tier3GoldQuestion":
        if not isinstance(d, Mapping):
            raise FixtureValidationError(f"question must be an object, got {type(d).__name__}")

        qid = str(d.get("id", "")).strip()
        if not qid:
            raise FixtureValidationError("question requires a non-empty 'id'")

        question = str(d.get("question", "")).strip()
        if not question:
            raise FixtureValidationError(f"question {qid!r} requires non-empty 'question' text")

        tier = str(d.get("tier", "")).strip().upper()
        if tier not in TIER_LABELS:
            raise FixtureValidationError(
                f"question {qid!r}: tier must be one of {TIER_LABELS}, got {d.get('tier')!r}"
            )

        gold_sparql = str(d.get("gold_sparql", "")).strip()
        if not gold_sparql:
            raise FixtureValidationError(
                f"question {qid!r} requires a non-empty 'gold_sparql' (execution-verified gold)"
            )

        raw_items = d.get("full_expected_items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise FixtureValidationError(
                f"question {qid!r}: 'full_expected_items' must be a list"
            )
        items = tuple(str(x) for x in raw_items)

        if "full_result_count" not in d:
            raise FixtureValidationError(
                f"question {qid!r} requires 'full_result_count'"
            )
        try:
            count = int(d["full_result_count"])
        except (TypeError, ValueError):
            raise FixtureValidationError(
                f"question {qid!r}: 'full_result_count' must be an integer, got {d['full_result_count']!r}"
            )

        # Conservation: the gold answer set and its count are two views of one
        # execution-verified result — they can never disagree.
        if count != len(items):
            raise FixtureValidationError(
                f"question {qid!r}: conservation failed — full_result_count={count} "
                f"but len(full_expected_items)={len(items)}"
            )

        raw_citations = d.get("gold_citations", ())
        if raw_citations and (
            not isinstance(raw_citations, Sequence) or isinstance(raw_citations, (str, bytes))
        ):
            raise FixtureValidationError(
                f"question {qid!r}: 'gold_citations' must be a list of strings"
            )
        gold_citations = tuple(str(c) for c in raw_citations if str(c).strip())

        return cls(
            id=qid,
            question=question,
            tier=tier,
            gold_sparql=gold_sparql,
            full_expected_items=items,
            full_result_count=count,
            expected_answer=str(d.get("expected_answer", "")),
            reasoning=str(d.get("reasoning", "")),
            gold_citations=gold_citations,
        )

    @property
    def gold_is_empty(self) -> bool:
        """True when the execution-verified gold is the empty result set — the case
        the outcome grader's empty-answer guard is built around."""
        return self.full_result_count == 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "question": self.question,
            "tier": self.tier,
            "gold_sparql": self.gold_sparql,
            "full_expected_items": list(self.full_expected_items),
            "full_result_count": self.full_result_count,
        }
        if self.expected_answer:
            out["expected_answer"] = self.expected_answer
        if self.reasoning:
            out["reasoning"] = self.reasoning
        if self.gold_citations:
            out["gold_citations"] = list(self.gold_citations)
        return out


# --------------------------------------------------------------------------- #
# Enumeration + scoped-schema gold (ONTA-384 graph-profile bar)
# --------------------------------------------------------------------------- #
# Structural attributes always treated as in-scope even when not listed in the
# goal's requested field set (type/label machinery + the key itself).
_DEFAULT_STRUCTURAL_ATTRIBUTES: tuple[str, ...] = ("name", "label", "type")


@dataclass(frozen=True)
class EnumerationScope:
    """Gold for an **enumeration + scoped-schema** ingest goal (ONTA-384).

    Optional on a :class:`Tier3Fixture`. When present, the profile half of the
    Tier-3 grader (``grade_enumeration_profile``) scores the graph the pipeline
    produced for three independent failure modes that the BC-universities
    regression compounded:

      * **coverage** (guards P1 / ONTA-379) — fraction of ``expected_entities``
        present under key-normalization;
      * **scope-adherence** (guards P2 / ONTA-380+382) — produced attributes
        must be ⊆ ``requested_attributes`` ∪ structural;
      * **fragmentation** (guards P5 / ONTA-383) — type set vs
        ``allowed_types`` / ``forbidden_types``.

    Pure schema — the scorer lives in ``tier3_grade``.
    """

    expected_entities: tuple[str, ...]
    requested_attributes: tuple[str, ...]
    allowed_types: tuple[str, ...] = ()
    forbidden_types: tuple[str, ...] = ()
    key_attribute: str = "name"
    # Variant spelling → canonical entity name (both sides free-form; the scorer
    # normalizes). Stored as a tuple of pairs so the dataclass stays frozen.
    alias_table: tuple[tuple[str, str], ...] = ()
    structural_attributes: tuple[str, ...] = _DEFAULT_STRUCTURAL_ATTRIBUTES

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EnumerationScope":
        if not isinstance(d, Mapping):
            raise FixtureValidationError(
                f"enumeration_scope must be an object, got {type(d).__name__}"
            )

        raw_entities = d.get("expected_entities")
        if not isinstance(raw_entities, Sequence) or isinstance(raw_entities, (str, bytes)):
            raise FixtureValidationError(
                "enumeration_scope.expected_entities must be a non-empty list of strings"
            )
        entities = tuple(str(x).strip() for x in raw_entities if str(x).strip())
        if not entities:
            raise FixtureValidationError(
                "enumeration_scope.expected_entities must contain at least one entity"
            )

        raw_attrs = d.get("requested_attributes")
        if not isinstance(raw_attrs, Sequence) or isinstance(raw_attrs, (str, bytes)):
            raise FixtureValidationError(
                "enumeration_scope.requested_attributes must be a non-empty list of strings"
            )
        attrs = tuple(str(x).strip() for x in raw_attrs if str(x).strip())
        if not attrs:
            raise FixtureValidationError(
                "enumeration_scope.requested_attributes must contain at least one attribute"
            )

        def _str_tuple(key: str) -> tuple[str, ...]:
            raw = d.get(key, ())
            if raw is None:
                return ()
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise FixtureValidationError(
                    f"enumeration_scope.{key} must be a list of strings"
                )
            return tuple(str(x).strip() for x in raw if str(x).strip())

        allowed = _str_tuple("allowed_types")
        forbidden = _str_tuple("forbidden_types")
        structural = _str_tuple("structural_attributes") or _DEFAULT_STRUCTURAL_ATTRIBUTES

        key_attribute = str(d.get("key_attribute", "name")).strip() or "name"

        raw_aliases = d.get("alias_table") or {}
        if not isinstance(raw_aliases, Mapping):
            raise FixtureValidationError(
                "enumeration_scope.alias_table must be an object of variant→canonical"
            )
        alias_table = tuple(
            (str(k), str(v))
            for k, v in raw_aliases.items()
            if str(k).strip() and str(v).strip()
        )

        return cls(
            expected_entities=entities,
            requested_attributes=attrs,
            allowed_types=allowed,
            forbidden_types=forbidden,
            key_attribute=key_attribute,
            alias_table=alias_table,
            structural_attributes=structural,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "expected_entities": list(self.expected_entities),
            "requested_attributes": list(self.requested_attributes),
            "key_attribute": self.key_attribute,
        }
        if self.allowed_types:
            out["allowed_types"] = list(self.allowed_types)
        if self.forbidden_types:
            out["forbidden_types"] = list(self.forbidden_types)
        if self.alias_table:
            out["alias_table"] = {k: v for k, v in self.alias_table}
        if self.structural_attributes != _DEFAULT_STRUCTURAL_ATTRIBUTES:
            out["structural_attributes"] = list(self.structural_attributes)
        return out


# --------------------------------------------------------------------------- #
# The fixture
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tier3Fixture:
    """One whole-product QC fixture: goal + reproducible source seed + graded gold
    questions. Built (and validated) via :meth:`from_dict` / :func:`load_fixture`.

    Optional ``enumeration_scope`` (ONTA-384) pins the gold for an enumeration +
    scoped-schema ingest goal so the profile grader can score coverage /
    scope-adherence / fragmentation of the produced graph offline.
    """

    id: str
    goal: str
    source_seed: SourceSeed
    questions: tuple[Tier3GoldQuestion, ...]
    # Optional metadata for reporting / grouping — never load-bearing.
    domain: str = ""
    notes: str = ""
    # Optional enumeration + scoped-schema gold (ONTA-384).
    enumeration_scope: Optional[EnumerationScope] = None
    # Directory the fixture was loaded from (for resolving a bundled source path).
    base_dir: str = field(default="", compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, base_dir: str = "") -> "Tier3Fixture":
        if not isinstance(d, Mapping):
            raise FixtureValidationError(f"fixture must be an object, got {type(d).__name__}")

        fid = str(d.get("id", "")).strip()
        if not fid:
            raise FixtureValidationError("fixture requires a non-empty 'id'")

        goal = str(d.get("goal", "")).strip()
        if not goal:
            raise FixtureValidationError(
                f"fixture {fid!r} requires a non-empty 'goal' (the A0 request)"
            )

        if "source_seed" not in d:
            raise FixtureValidationError(f"fixture {fid!r} requires a 'source_seed'")
        source_seed = SourceSeed.from_dict(d["source_seed"])

        raw_questions = d.get("questions")
        if not isinstance(raw_questions, Sequence) or isinstance(raw_questions, (str, bytes)):
            raise FixtureValidationError(f"fixture {fid!r}: 'questions' must be a list")
        if not raw_questions:
            raise FixtureValidationError(f"fixture {fid!r} requires at least one question")

        questions = tuple(Tier3GoldQuestion.from_dict(q) for q in raw_questions)

        # Question ids must be unique so answers align unambiguously by id.
        seen: set[str] = set()
        for q in questions:
            if q.id in seen:
                raise FixtureValidationError(
                    f"fixture {fid!r}: duplicate question id {q.id!r}"
                )
            seen.add(q.id)

        enumeration_scope: Optional[EnumerationScope] = None
        if d.get("enumeration_scope") is not None:
            enumeration_scope = EnumerationScope.from_dict(d["enumeration_scope"])

        return cls(
            id=fid,
            goal=goal,
            source_seed=source_seed,
            questions=questions,
            domain=str(d.get("domain", "")),
            notes=str(d.get("notes", "")),
            enumeration_scope=enumeration_scope,
            base_dir=base_dir,
        )

    @property
    def question_ids(self) -> tuple[str, ...]:
        return tuple(q.id for q in self.questions)

    def tier_distribution(self) -> dict[str, int]:
        """Count of questions per tier label — a convenience for reporting."""
        dist = {t: 0 for t in TIER_LABELS}
        for q in self.questions:
            dist[q.tier] += 1
        return dist

    def resolve_source_path(self) -> Optional[str]:
        """Absolute path of a ``bundled_file`` seed, or ``None`` for a ``url_list``."""
        return self.source_seed.resolve_path(self.base_dir)

    def source_exists(self) -> bool:
        """Whether a ``bundled_file`` seed's file is present on disk. A ``url_list``
        seed has no local file and is reported as ``True`` (nothing to check here)."""
        path = self.resolve_source_path()
        if path is None:
            return True
        return os.path.isfile(path)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "goal": self.goal,
            "source_seed": self.source_seed.to_dict(),
            "questions": [q.to_dict() for q in self.questions],
        }
        if self.domain:
            out["domain"] = self.domain
        if self.notes:
            out["notes"] = self.notes
        if self.enumeration_scope is not None:
            out["enumeration_scope"] = self.enumeration_scope.to_dict()
        return out


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
def load_fixture(path: str) -> Tier3Fixture:
    """Load and validate one Tier-3 fixture from a JSON file. Raises
    :class:`FixtureValidationError` on any structural / conservation problem."""
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise FixtureValidationError(f"{path}: invalid JSON — {e}") from e
    return Tier3Fixture.from_dict(data, base_dir=os.path.dirname(os.path.abspath(path)))


def load_fixtures(directory: str) -> list[Tier3Fixture]:
    """Load every ``*.json`` fixture directly under ``directory`` (sorted by name),
    skipping files in nested subdirectories (e.g. a ``sources/`` seed folder)."""
    out: list[Tier3Fixture] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        out.append(load_fixture(full))
    return out
