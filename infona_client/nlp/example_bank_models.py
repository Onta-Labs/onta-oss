"""Example dataclass, keys, holdout / benchmark gates, and pattern tags.

Looked up on :mod:`infona_client.nlp.example_bank` at call time via ``_host()``
when a sibling needs a patchable name.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("infona_client.nlp.example_bank")


def _host():
    """Call-time lookup of the public example_bank module.

    Tests monkeypatch names on ``infona_client.nlp.example_bank`` (e.g.
    ``MAX_BANK_SIZE``, ``json.dumps``). Sibling modules must look these up at
    call time so those patches keep working.
    """
    from infona_client.nlp import example_bank as _mod

    return _mod


# Bank limits
MAX_BANK_SIZE = 500

# Similarity thresholds
ANTI_CHEAT_THRESHOLD = 0.90  # Exclude examples too similar to excluded questions
SAME_DATASET_MAX_SIM = 0.75  # Same-KG examples must be below this to avoid near-cheating

# Pattern tags detected from SPARQL text
PATTERN_DETECTORS: list[tuple[str, str]] = [
    ("count", r"COUNT\s*\("),
    ("avg", r"AVG\s*\("),
    ("max", r"MAX\s*\("),
    ("sum", r"SUM\s*\("),
    ("filter", r"FILTER\s*\("),
    ("contains", r"CONTAINS\s*\("),
    ("date_filter", r"xsd:dateTime"),
    ("group_by", r"GROUP\s+BY"),
]

# Pattern tags for Cypher (E6). Overlap names with SPARQL so diversity logic
# still works across a mixed bank.
CYPHER_PATTERN_DETECTORS: list[tuple[str, str]] = [
    ("count", r"\bcount\s*\("),
    ("avg", r"\bavg\s*\("),
    ("max", r"\bmax\s*\("),
    ("sum", r"\bsum\s*\("),
    ("filter", r"\bWHERE\b"),
    ("contains", r"\b(?:contains|toLower|toUpper)\s*\("),
    ("group_by", r"\bWITH\b"),
    ("order", r"\bORDER\s+BY\b"),
    ("limit", r"\bLIMIT\b"),
]

# Default file paths. The few-shot bank ships with the package; eval report
# dumps stay at the repo-root eval_reports/ dir (gitignored, local-only).
DEFAULT_BANK_PATH = Path(__file__).resolve().parent / "data" / "example_bank.jsonl"
EVAL_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "eval_reports"

# Holdout v2 KG exclusion list (spec §6.1): holdout-v2 KGs must never be
# indexed into the example bank, regardless of whether they appear in
# eval_reports. The manifest is the source of truth; we fall back to a
# hardcoded list if the manifest isn't reachable (e.g. prod deploys that
# don't ship eval_holdout_v2/). Drift between the fallback and manifest
# is logged at import time so it gets noticed.
_HOLDOUT_V2_KGS_FALLBACK: frozenset[str] = frozenset({
    # healthcare
    "cms-nursing-home-compare",
    "samhsa-n-ssats",
    "medicare-part-d-pricing",
    "hrsa-hpsa",
    "cdc-fluview",
    "cdc-wonder-mortality",
    "npi-registry",
    # finance
    "sec-edgar-10k",
    "fdic-call-reports",
    "treasury-fiscaldata-securities",
    "cftc-swap-data",
    "ncua-credit-union-call-reports",
    "finra-trace-corporate-bonds",
    "ofr-financial-stability",
    # legal
    "patentsview",
    "scdb-supreme-court",
    "doj-enforcement-actions",
    "ftc-consent-decrees",
    "uspto-trademarks",
    "pacer-federal-dockets",
    "fec-enforcement",
    # scientific_public_sector
    "nsf-awards",
    "nih-reporter-non-clinical",
    "fema-disaster-declarations",
    "epa-water-quality-portal",
    "noaa-storm-events",
    "usda-agricultural-statistics",
    "doe-energy-research-grants",
})


def _load_holdout_v2_kgs() -> frozenset[str]:
    """Load holdout-v2 KG IDs from eval_holdout_v2/HOLDOUT_V2_MANIFEST.json.

    Searches a few plausible locations (the infona-oss submodule lives inside
    the parent infona repo, so the manifest is typically two or three
    parents up from this file). On any failure, returns the hardcoded
    fallback set and logs a warning so drift gets noticed.
    """
    # __file__ = .../infona-oss/infona/nlp/example_bank.py
    here = Path(__file__).resolve()
    candidates = [
        # infona parent (submodule layout): .../infona/infona-oss/infona/nlp/
        here.parent.parent.parent.parent / "eval_holdout_v2" / "HOLDOUT_V2_MANIFEST.json",
        # alt: one level up (standalone)
        here.parent.parent.parent / "eval_holdout_v2" / "HOLDOUT_V2_MANIFEST.json",
        # cwd fallback
        Path.cwd() / "eval_holdout_v2" / "HOLDOUT_V2_MANIFEST.json",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            with open(path) as f:
                manifest = json.load(f)
            ids = frozenset(
                kg["id"] for kg in manifest.get("kgs", []) if kg.get("id")
            )
            if not ids:
                continue
            # Drift check vs fallback
            missing_from_fallback = ids - _HOLDOUT_V2_KGS_FALLBACK
            extra_in_fallback = _HOLDOUT_V2_KGS_FALLBACK - ids
            if missing_from_fallback or extra_in_fallback:
                logger.warning(
                    "HOLDOUT_V2_KGS fallback drift vs manifest %s: "
                    "missing_from_fallback=%s extra_in_fallback=%s",
                    path, sorted(missing_from_fallback), sorted(extra_in_fallback),
                )
            logger.info("Loaded %d holdout-v2 KGs from %s", len(ids), path)
            return ids
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to read holdout-v2 manifest at %s: %s", path, exc)
            continue
    logger.warning(
        "HOLDOUT_V2_MANIFEST.json not found in any candidate path; "
        "using hardcoded HOLDOUT_V2_KGS fallback (%d entries). "
        "This is OK for prod deploys that don't ship eval_holdout_v2/.",
        len(_HOLDOUT_V2_KGS_FALLBACK),
    )
    return _HOLDOUT_V2_KGS_FALLBACK


HOLDOUT_V2_KGS: frozenset[str] = _load_holdout_v2_kgs()


# ── Benchmark-KG exclusion (ONTA-449) ────────────────────────────────────
#
# Spider4SPARQL and friends are ingested into the isolated ``spider-bench``
# tenant precisely so their schema (Singer, Stadium, Airline, Country, ...)
# never pollutes a real ontology. The example bank is a SECOND path into a
# real user's context that bypasses that isolation completely: it is scoped
# per PROCESS, not per tenant, so a benchmark answer stored during an eval run
# is few-shot injected into every LATER ``/ask``, whatever tenant asked and
# whatever KG they are querying. That is the same contamination rule being
# broken through the prompt path instead of the graph path.
#
# This is not hypothetical. The committed OSS bank carried 148 spider entries
# out of 262. Replaying production retrieval (top-10 cosine over the stored
# embeddings, then tag-diversify to 3) with each non-benchmark KG held out --
# i.e. simulating a domain the bank has never seen, which is what a real user
# looks like -- benchmark examples took **97 of 342 top-3 few-shot slots
# (28.4%)** and changed the selected set for 60 of 114 questions. Five of them
# also teach a malformed shape: a bare ``types/<T>`` IRI in PREDICATE position.
#
# Removing them is close to free. The mean cosine similarity of the selected
# set drops 0.0125 across all 114 held-out queries (0.024 if you look only at
# the 60 whose selection changed at all), because the non-benchmark replacement
# sitting just below in the ranking is almost as close. Cross-domain transfer --
# the reason the bank tolerates examples from other datasets at all -- is
# preserved by the 8 real KGs that remain.
#
# Matched by KG-name prefix rather than an exact set so a NEW benchmark split
# (``spider-tvshow``, ...) is excluded the day it is first evaluated, without
# anyone remembering to extend a list.
BENCHMARK_KG_PREFIXES: tuple[str, ...] = (
    "spider-",
    # Eval-MH v3 never-tuned holdout (whitepaper). Path-B product eval KGs —
    # must not few-shot leak into production /ask or other eval runs.
    "eval-mh-",
)

# ── Blueprint tenant isolation (INF-567) ────────────────────────────────
#
# The bank is still one process-scoped JSONL. Shared open-data examples
# (empty ``tenant_id``) stay globally visible — that is the cross-domain
# transfer the bank exists for. A Blueprint ships ``evals[]`` and supported
# questions that MUST work inside the installing tenant and MUST NOT reach
# any other tenant's retrieval. ``format_examples_for_prompt`` rewrites
# ``FROM`` to the caller's graph, so a leaked Blueprint example would look
# legitimate rather than obviously foreign.
#
# Decision: admit Blueprint examples with a ``tenant_id``, never as global
# rows. Four write/load layers refuse an unscoped Blueprint example (same
# shape as the ONTA-449 spider- prefix block). Retrieval is fail-closed:
# a row with ``tenant_id`` is visible only when the caller passes that
# same tenant. Omitting ``tenant_id`` at retrieve hides every scoped row.
BLUEPRINT_ORIGIN = "blueprint"


def normalize_example_origin(
    origin: str | None = None,
    *,
    blueprint_id: str | None = None,
) -> str:
    """Canonical origin token. ``blueprint_id`` implies Blueprint origin."""
    if (blueprint_id or "").strip():
        return BLUEPRINT_ORIGIN
    return (origin or "").strip().lower()


def is_blueprint_origin(
    origin: str | None = None,
    *,
    blueprint_id: str | None = None,
) -> bool:
    """True when the row was sourced from a Blueprint package."""
    return normalize_example_origin(origin, blueprint_id=blueprint_id) == BLUEPRINT_ORIGIN


def is_unscoped_blueprint_example(
    origin: str | None = None,
    tenant_id: str | None = None,
    *,
    blueprint_id: str | None = None,
) -> bool:
    """True when a Blueprint example would be globally visible — refuse it.

    Tolerates ``None`` the same way :func:`is_benchmark_kg` does: ``load()``
    calls this outside its malformed-line ``except``, so raising here would
    take down the whole load.
    """
    if not is_blueprint_origin(origin, blueprint_id=blueprint_id):
        return False
    return not (tenant_id or "").strip()


def example_matches_kg_purge(
    example: "Example",
    *,
    tenant_id: str,
    kg_name: str,
) -> bool:
    """True when a KG delete should drop this example.

    Global rows (empty ``tenant_id``) keep the historical kg_name-only
    match. Tenant-scoped rows — Blueprint installs — are dropped only for
    the deleting tenant, so two workspaces that installed the same
    Blueprint do not evict each other's questions.
    """
    if (example.kg_name or "").strip() != (kg_name or "").strip():
        return False
    scoped = (example.tenant_id or "").strip()
    if not scoped:
        return True
    return scoped == (tenant_id or "").strip()


def example_visible_to_tenant(
    example_tenant_id: str | None,
    caller_tenant_id: str | None,
) -> bool:
    """Whether a stored example may enter this caller's retrieval pool.

    Empty ``example_tenant_id`` is the shared bank (visible to every tenant).
    A scoped row is visible only to the matching caller. Missing caller
    tenant is fail-closed for scoped rows so a forgotten ``tenant_id`` at
    retrieve cannot leak a Blueprint question.
    """
    scoped = (example_tenant_id or "").strip()
    if not scoped:
        return True
    caller = (caller_tenant_id or "").strip()
    if not caller:
        return False
    return scoped == caller


def is_benchmark_kg(kg_name: str) -> bool:
    """True if ``kg_name`` is an external-benchmark KG barred from the bank.

    Benchmark corpora live in a disposable tenant and must not reach any real
    user's prompt. See :data:`BENCHMARK_KG_PREFIXES`.

    Tolerates ``None``: ``Example.from_dict`` does ``d.get("kg_name", "")``, so
    a bank line with an explicit ``"kg_name": null`` yields ``None`` rather than
    a string. This is called from ``load()`` OUTSIDE its malformed-line
    ``except``, so raising here would take down the whole load -- and because
    ``get_example_bank`` assigns the singleton before calling ``load()``, it
    would fail silently, leaving the bank empty for the process lifetime.
    """
    return (kg_name or "").strip().lower().startswith(BENCHMARK_KG_PREFIXES)


def example_key(
    question: str,
    kg_name: str | None,
    tenant_id: str | None = None,
) -> tuple[str, str, str]:
    """Identity of an example for dedup/refresh: question, KG, tenant.

    This used to be the question text ALONE, which conflated two different
    things and got both wrong:

    - The same question asked of two different KGs ("how many rows?") is two
      different examples -- different ontology, different ``FROM`` graph. Keying
      on question alone dropped whichever the eval happened to reach second, so
      the bank's contents depended on eval ORDER.
    - The same question asked of the SAME KG is one example that may have a
      better answer now (reingest changed the predicate URIs; a later run
      produced a corrected SPARQL). Keying on question alone made that a
      "duplicate" to be dropped, so fresh data lost to whatever was in the bank
      first. See :meth:`ExampleBank.add_batch`, which now refreshes instead.

    ``tenant_id`` is the INF-567 third coordinate: two tenants that install
    the same Blueprint (same question, same ``kg_name``) must not refresh
    each other's row. Empty ``tenant_id`` is the shared bank; existing
    callers that omit it keep the previous ``(question, kg_name)`` identity.

    ``question`` is normalized (strip + lowercase) because it is free text a
    human typed; ``kg_name`` and ``tenant_id`` are slugs and are only stripped.
    """
    return (
        question.strip().lower(),
        (kg_name or "").strip(),
        (tenant_id or "").strip(),
    )


@dataclass
class Example:
    """A single (question, query) example with metadata.

    SPARQL remains the primary few-shot language for Neptune. Cypher is an
    **optional** sibling field (E6): mixed banks load without error; Cypher
    mode only formats rows that carry a non-empty ``cypher`` string.
    """

    question: str
    sparql: str = ""
    kg_name: str = ""
    ontology_context: str = ""
    pattern_tags: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    cypher: str = ""
    tenant_id: str = ""
    origin: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """See :func:`example_key`."""
        return example_key(self.question, self.kg_name, self.tenant_id)

    def refresh_from(
        self,
        sparql: str = "",
        ontology_context: str = "",
        *,
        cypher: str = "",
    ) -> bool:
        """Update this example's answer in place. True if anything changed.

        The embedding is NOT recomputed: it is keyed on the question, and the
        question is part of this example's identity, so by construction it has
        not changed. A refresh therefore costs zero embedding calls, which is
        also why it stays allowed when the bank is at ``MAX_BANK_SIZE``.

        Empty inputs are ignored rather than written: a caller with no ontology
        text on hand (``populate_from_eval_reports`` reading a report that
        omitted it) must not blank out context the bank already has.
        """
        changed = False
        if sparql and sparql != self.sparql:
            self.sparql = sparql
            self.pattern_tags = detect_pattern_tags(sparql)
            changed = True
        if cypher and cypher != self.cypher:
            self.cypher = cypher
            # Prefer Cypher tags when refreshing a Cypher answer; keep SPARQL
            # tags if only sparql changed above.
            self.pattern_tags = detect_pattern_tags_cypher(cypher) or self.pattern_tags
            changed = True
        if ontology_context and ontology_context != self.ontology_context:
            self.ontology_context = ontology_context
            changed = True
        return changed

    def to_dict(self) -> dict:
        d = {
            "question": self.question,
            "sparql": self.sparql,
            "kg_name": self.kg_name,
            "ontology_context": self.ontology_context,
            "pattern_tags": self.pattern_tags,
            "embedding": self.embedding,
        }
        # Omit empty cypher so SPARQL-only bank lines stay byte-stable.
        if self.cypher:
            d["cypher"] = self.cypher
        # Omit empty isolation fields so the shared bank stays byte-stable.
        if self.tenant_id:
            d["tenant_id"] = self.tenant_id
        if self.origin:
            d["origin"] = self.origin
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Example":
        sparql = d.get("sparql", "") or ""
        cypher = d.get("cypher", "") or ""
        if not sparql and not cypher:
            raise KeyError("example requires sparql and/or cypher")
        origin = normalize_example_origin(
            d.get("origin", "") or "",
            blueprint_id=d.get("blueprint_id") or "",
        )
        return cls(
            question=d["question"],
            sparql=sparql,
            kg_name=d.get("kg_name", ""),
            ontology_context=d.get("ontology_context", ""),
            pattern_tags=d.get("pattern_tags", []),
            embedding=d.get("embedding", []),
            cypher=cypher,
            tenant_id=d.get("tenant_id", "") or "",
            origin=origin,
        )


def detect_pattern_tags(sparql: str) -> list[str]:
    """Auto-detect query pattern tags from SPARQL text.

    Detects aggregation functions (COUNT, AVG, MAX, SUM), filtering patterns
    (FILTER, CONTAINS, date), structural patterns (JOIN, GROUP BY, multi-hop).
    """
    tags: list[str] = []

    for tag, pattern in PATTERN_DETECTORS:
        if re.search(pattern, sparql, re.IGNORECASE):
            tags.append(tag)

    # "join" — 2+ triple patterns with different subjects
    subjects = set(re.findall(r"\?\w+\s+<", sparql))
    if len(subjects) >= 2:
        tags.append("join")

    # "multi_hop" — 3+ triple patterns (lines ending with ' .')
    triple_count = len(re.findall(r"\.\s*(?:\n|$|\})", sparql))
    # Also count triples separated by ' . '
    triple_count += sparql.count(" . ")
    if triple_count >= 3:
        tags.append("multi_hop")

    return sorted(set(tags))


def detect_pattern_tags_cypher(cypher: str) -> list[str]:
    """Auto-detect pattern tags from Cypher text (E6 example bank)."""
    tags: list[str] = []
    for tag, pattern in CYPHER_PATTERN_DETECTORS:
        if re.search(pattern, cypher or "", re.IGNORECASE):
            tags.append(tag)
    # join: 2+ MATCH clauses
    if len(re.findall(r"\bMATCH\b", cypher or "", re.IGNORECASE)) >= 2:
        tags.append("join")
    return sorted(set(tags))
