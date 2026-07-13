"""Boundary-fixture capture harness — freeze what flows BETWEEN pipeline stages.

``scenario.py`` answers "does the ingester produce a well-formed graph." ``audit.py``
answers "is an existing graph well-formed." This answers a THIRD, refactor-facing
question: **"given the same input, does each stage still hand the next stage the same
artifact?"** It snapshots the three inter-stage artifacts of the ingestion pipeline and
freezes them as CHARACTERIZATION fixtures, so a later refactor can assert *same input →
same artifact out* and a diff test flags any drift.

The three boundary artifacts (the P0–P9 stage contract's A2/A4/A5):

  * **A2 — candidate facts** (extraction output): the ``ExtractionResult`` the LLM
    extractor hands the resolver — typed entities, their attributes, and relationships.
  * **A4 — verified facts**: every literal attribute value after the REAL
    schema-on-write ``validate_triple`` — the typed, canonicalized, coerced-or-rejected
    triples the writer is allowed to persist.
  * **A5 — placement plan** (ontology mapping): where each fact LANDS — the minted type
    URIs + subclass lineage, per-entity node URIs + asserted ``rdf:type``, attribute
    DECLARATIONS (``attrs/<leaf>`` + range), and relationship INSTANCE edges
    (``onto/<leaf>``) with their materialized target nodes.

Determinism (why this can be frozen AND diffed in CI): the ONE non-deterministic stage
is the LLM extraction (A2). The tail — validation (A4) and placement (A5) — is pure,
deterministic code. So this harness renders A2 from a **canonical, dataset-derived
reference extraction** (:func:`render_extraction`, a mechanical re-render of the shipped
decomp fixtures — never hand-curation) and then pushes it through the pipeline's REAL
deterministic transforms: ``validate_triple`` (A4) and the real URI / predicate /
attribute-resolver / ontology builders (A5). The whole render is reproducible offline —
no LLM, no store — so ``--freeze`` re-generates the fixtures with one command and the
diff test (``tests/test_qc_boundary.py``) re-renders and compares.

This mirrors exactly how ``tests/test_qc_scenario.py`` handles the same LLM-in-CI
tension: the real LLM path runs out-of-band, a deterministic stand-in is what CI freezes
and checks. :func:`capture_boundary` takes ANY ``ExtractionResult``, so a real A2
recorded from a live discovery run can be dropped in and re-rendered through the identical
A4/A5 transforms.

OSS: imports only stdlib + ``cograph_client.*``. No proprietary identifiers, no endpoint,
no network. Reuses the scenario fuzzer's ``Dataset`` / ``load_fixture_datasets`` catalog
and the ONTA-199 decomp fixtures as the canonical corpus.

  # one command: re-render + freeze every domain's A2/A4/A5
  python -m cograph_client.qc.boundary --freeze

  # check the working tree against the frozen fixtures (exit 1 on drift)
  python -m cograph_client.qc.boundary --check
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cograph_client.graph.ontology_queries import (
    PRIMITIVE_TYPES,
    _datatype_to_xsd,
    attr_uri,
    entity_uri,
    ontology_version,
    type_uri,
)
from cograph_client.qc.scenario import Dataset, load_fixture_datasets
from cograph_client.resolver.attribute_resolver import resolve_attribute
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionResult,
    ValidatedTriple,
)
from cograph_client.resolver.predicate_normalizer import normalize_predicate
from cograph_client.resolver.validator import validate_triple

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
ONTO_PREFIX = "https://cograph.tech/onto/"

# Artifact tiers, named for the stage-contract slots they characterize.
TIERS = ("a2", "a4", "a5")


# --------------------------------------------------------------------------- #
# Canonical corpus — a mechanical extraction rule per representative domain.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BoundarySpec:
    """How to mechanically decompose one canonical dataset's rows into a reference
    ``ExtractionResult`` (A2). Deterministic and dataset-derived — a re-render of the
    shipped decomp fixtures, NOT a hand-authored answer key. Chosen per domain so the
    render exercises the interesting write-path branches: literal validation/typing
    (``literal`` columns), relationship edges + target-node materialization
    (``node_fields``), multi-valued split into repeated edges (``multivalued_fields``),
    and subtype minting + subclass lineage (``subtype_field`` / ``parent_chain``)."""

    domain: str
    proposed_type: str
    label_field: str  # row column that names the entity → its id + rdfs:label
    # column -> (target_type, predicate): a single node-valued edge to a shared node.
    node_fields: dict[str, tuple[str, str]] = field(default_factory=dict)
    # column -> (target_type, predicate): comma-split into REPEATED edges to shared nodes.
    multivalued_fields: dict[str, tuple[str, str]] = field(default_factory=dict)
    # column whose VALUE becomes the entity's (sub)type_name, with parent_chain lineage.
    subtype_field: Optional[str] = None
    parent_chain: list[str] = field(default_factory=list)


BOUNDARY_SPECS: dict[str, BoundarySpec] = {
    "coffee_shops": BoundarySpec(
        domain="coffee_shops",
        proposed_type="CoffeeShop",
        label_field="name",
        node_fields={"city": ("City", "located_in"), "state": ("State", "in_state")},
        multivalued_fields={"categories": ("Category", "has_category")},
    ),
    "llm_models": BoundarySpec(
        domain="llm_models",
        proposed_type="Model",
        label_field="display_name",
        node_fields={"organization": ("Organization", "developed_by")},
        multivalued_fields={"modality": ("Modality", "supports_modality")},
    ),
    "healthcare_providers": BoundarySpec(
        domain="healthcare_providers",
        proposed_type="Physician",
        label_field="full_name",
        subtype_field="provider_type",
        parent_chain=["HealthcareProvider", "Person"],
        node_fields={
            "hospital_affiliation": ("Organization", "affiliated_with"),
            "city": ("City", "located_in"),
            "state": ("State", "in_state"),
        },
        multivalued_fields={"specialty": ("Specialty", "has_specialty")},
    ),
    "sp500_companies": BoundarySpec(
        domain="sp500_companies",
        proposed_type="Company",
        label_field="company_name",
        node_fields={
            "sector": ("Sector", "in_sector"),
            "industry": ("Industry", "in_industry"),
            "ceo": ("Person", "led_by"),
        },
    ),
}

# The default corpus: every domain above (all >= the 3 the task asks for), in a stable
# order so a full freeze/diff is reproducible.
DEFAULT_DOMAINS = tuple(BOUNDARY_SPECS)


# --------------------------------------------------------------------------- #
# A2 — the reference extraction (deterministic stand-in for the LLM extractor)
# --------------------------------------------------------------------------- #
def _infer_datatype(value: Any) -> str:
    """Deterministically infer an extraction datatype from a raw JSON scalar — the same
    coarse call the extractor makes (bool/int/float/str). ``bool`` is checked before
    ``int`` (``True`` is an ``int`` in Python)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    return "string"


def _camel_type(raw: str) -> str:
    """CamelCase a free-text role into a clean type NAME identifier
    ("Nurse Practitioner" -> "NursePractitioner")."""
    return "".join(w[:1].upper() + w[1:] for w in raw.split())


def render_extraction(rows: list[dict], spec: BoundarySpec) -> ExtractionResult:
    """Mechanically decompose canonical rows into a reference ``ExtractionResult`` (A2).

    One primary entity per row (typed ``spec.proposed_type`` — or the ``subtype_field``
    value, with ``parent_chain`` lineage); the ``label_field`` becomes its id + label;
    ``node_fields`` / ``multivalued_fields`` become relationships to SHARED, deduplicated
    target nodes; every other column becomes a typed literal attribute. Deterministic:
    the same rows always yield the same extraction."""
    entities: list[ExtractedEntity] = []
    relationships: list[ExtractedRelationship] = []
    # (target_type, value) -> target entity id, so the same real-world thing (e.g. the
    # city "Portland") is ONE shared node reused across rows — the reuse the placement
    # plan should show.
    seen_targets: set[tuple[str, str]] = set()
    consumed = {spec.label_field, spec.subtype_field, *spec.node_fields, *spec.multivalued_fields}

    for row in rows:
        label = str(row.get(spec.label_field, "")).strip()
        if not label:
            continue  # a row with no name has no identity to hang facts on — skip it.
        type_name = spec.proposed_type
        parent_chain: list[str] = []
        if spec.subtype_field and row.get(spec.subtype_field):
            # CamelCase the role value into a type NAME, as the extractor does
            # ("Nurse Practitioner" -> "NursePractitioner"): type_uri does not sanitize,
            # so a type name must already be a clean identifier (the decomp spec's
            # expected subtypes are CamelCase).
            type_name = _camel_type(str(row[spec.subtype_field])) or spec.proposed_type
            parent_chain = list(spec.parent_chain)

        attrs = [
            ExtractedAttribute(name=col, value=val, datatype=_infer_datatype(val))
            for col, val in row.items()
            if col not in consumed and val is not None and str(val).strip() != ""
        ]
        entities.append(
            ExtractedEntity(
                type_name=type_name, id=label, parent_chain=parent_chain, attributes=attrs,
            )
        )

        # Single node-valued edges.
        for col, (target_type, predicate) in spec.node_fields.items():
            value = str(row.get(col, "")).strip()
            if not value:
                continue
            if (target_type, value) not in seen_targets:
                seen_targets.add((target_type, value))
                entities.append(ExtractedEntity(type_name=target_type, id=value))
            relationships.append(
                ExtractedRelationship(source_id=label, predicate=predicate, target_id=value)
            )

        # Multi-valued edges: comma-split into repeated edges to shared nodes.
        for col, (target_type, predicate) in spec.multivalued_fields.items():
            raw = str(row.get(col, "")).strip()
            if not raw:
                continue
            for piece in raw.split(","):
                value = piece.strip()
                if not value:
                    continue
                if (target_type, value) not in seen_targets:
                    seen_targets.add((target_type, value))
                    entities.append(ExtractedEntity(type_name=target_type, id=value))
                relationships.append(
                    ExtractedRelationship(source_id=label, predicate=predicate, target_id=value)
                )

    return ExtractionResult(entities=entities, relationships=relationships)


# --------------------------------------------------------------------------- #
# Capture — A2 -> {A2, A4, A5} through the REAL deterministic transforms
# --------------------------------------------------------------------------- #
@dataclass
class BoundaryArtifacts:
    """The three frozen boundary artifacts for one domain, each a JSON-ready dict."""

    domain: str
    a2: dict
    a4: dict
    a5: dict

    def tier(self, name: str) -> dict:
        return {"a2": self.a2, "a4": self.a4, "a5": self.a5}[name]


def _a2_dict(extraction: ExtractionResult) -> dict:
    """A2 = the candidate facts, canonicalized. Entities in extraction order (attributes
    sorted by name so a re-order upstream doesn't churn the fixture)."""
    return {
        "entities": [
            {
                "type_name": e.type_name,
                "id": e.id,
                "parent_chain": list(e.parent_chain),
                "also_types": list(e.also_types),
                "attributes": [
                    {"name": a.name, "value": a.value, "datatype": a.datatype}
                    for a in sorted(e.attributes, key=lambda a: a.name)
                ],
            }
            for e in extraction.entities
        ],
        "relationships": [
            {"source_id": r.source_id, "predicate": r.predicate, "target_id": r.target_id}
            for r in extraction.relationships
        ],
    }


def capture_boundary(extraction: ExtractionResult, domain: str) -> BoundaryArtifacts:
    """Render A2/A4/A5 for ``extraction`` through the pipeline's REAL deterministic
    stage transforms, into an EMPTY ontology (the discovery cold-start every decomp run
    starts from). Mirrors ``schema_resolver._resolve_and_insert`` for that case:

      * type placement is trivial (empty ontology → every proposed type is new, no LLM
        match), so ``resolved_type == entity.type_name``;
      * literal attributes flow through the real ``resolve_attribute`` (EXTEND) + the
        real ``validate_triple`` → A4;
      * relationship predicates flow through the real ``normalize_predicate`` and land
        on ``onto/<leaf>`` instance edges → A5.

    Accepts any ``ExtractionResult``, so a real captured A2 re-renders through the same
    A4/A5 transforms."""
    # entity id -> (resolved_type, uri). Ids are unique within one extraction (the
    # extractor's contract), so this is the resolver's entity_uri_map.
    resolved: dict[str, tuple[str, str]] = {}
    for e in extraction.entities:
        resolved.setdefault(e.id, (e.type_name, entity_uri(e.type_name, e.id)))

    # ---- placement accumulators (A5) ----
    types: dict[str, dict] = {}            # type name -> {uri, subclass_of, kind}
    entity_rows: list[dict] = []           # per-entity node placement
    attr_decls: dict[tuple[str, str], dict] = {}   # (type, attr) -> declaration
    rel_edges: list[dict] = []             # onto/<leaf> instance edges
    node_materializations: dict[str, dict] = {}    # target uri -> {rdf_type, label}
    # ---- verification accumulators (A4) ----
    verified: list[dict] = []
    rejections: list[dict] = []

    def _note_type(name: str, kind: str, parent: Optional[str] = None) -> None:
        entry = types.setdefault(
            name, {"uri": type_uri(name), "subclass_of": set(), "kind": kind}
        )
        if parent:
            entry["subclass_of"].add(type_uri(parent))
        # A concrete role (primary/subtype) label wins over a bare target/ancestor.
        if kind in ("primary", "subtype"):
            entry["kind"] = kind

    for e in extraction.entities:
        rtype, uri = resolved[e.id]
        # Subtype lineage: type_name subClassOf parent_chain[0] subClassOf ... (ADR 0001).
        if e.parent_chain:
            _note_type(rtype, "subtype", e.parent_chain[0])
            chain = e.parent_chain
            for i, ancestor in enumerate(chain):
                parent = chain[i + 1] if i + 1 < len(chain) else None
                _note_type(ancestor, "ancestor", parent)
        else:
            _note_type(rtype, "primary")

        entity_rows.append(
            {
                "id": e.id,
                "type": rtype,
                "uri": uri,
                "rdf_type": type_uri(rtype),
                "label": e.id,
                "also_types": [type_uri(t) for t in e.also_types if t and t != rtype],
            }
        )

        # Literal attributes → real resolve_attribute (EXTEND into empty ontology) + real
        # validate_triple. Into an empty type the resolved name is the normalized attr
        # name and the datatype is the extractor's — the write path's else-branch.
        for attr in e.attributes:
            r = resolve_attribute(attr, {})  # empty existing attrs → EXTEND
            if r.datatype not in PRIMITIVE_TYPES:
                # An attribute explicitly typed as a relationship is out of scope for the
                # canonical extractions (node-valued data is emitted as relationships, not
                # type-datatyped attributes) — guard so a future dataset can't slip a
                # silently-mismodeled edge into a "literal" fixture.
                raise ValueError(
                    f"{domain}: attribute {rtype}.{r.name} has non-primitive datatype "
                    f"{r.datatype!r}; node-valued data must be a relationship, not an attribute"
                )
            pred_uri = attr_uri(rtype, r.name)
            attr_decls[(rtype, r.name)] = {
                "type": rtype,
                "name": r.name,
                "uri": pred_uri,
                "range": _datatype_to_xsd(r.datatype),
                "kind": "literal",
            }
            v = validate_triple(
                uri, pred_uri, r.value, r.datatype, entity_id=e.id, attribute_name=r.name
            )
            if isinstance(v, ValidatedTriple):
                verified.append(
                    {
                        "subject": v.subject,
                        "predicate": v.predicate,
                        "object": v.object,
                        "outcome": v.outcome.value,
                        "original_value": v.original_value,
                    }
                )
            else:
                rejections.append(
                    {
                        "entity_id": v.entity_id,
                        "attribute": v.attribute,
                        "value": v.value,
                        "expected_datatype": v.expected_datatype,
                        "reason": v.reason,
                    }
                )

    # Relationships → onto/<canonical> instance edges + object-property declarations +
    # target-node materialization. Predicate normalization accumulates per source TYPE,
    # exactly like the resolver's relationships loop (existing edge predicates on a type
    # canonicalize the next proposal).
    declared_preds: dict[str, set[str]] = {}
    for rel in extraction.relationships:
        src = resolved.get(rel.source_id)
        tgt = resolved.get(rel.target_id)
        if not src or not tgt:
            continue
        src_type, src_uri = src
        tgt_type, tgt_uri = tgt
        existing = declared_preds.setdefault(src_type, set())
        canonical = normalize_predicate(rel.predicate, existing)
        rel_edges.append(
            {"subject": src_uri, "predicate": f"{ONTO_PREFIX}{canonical}", "object": tgt_uri}
        )
        if canonical not in existing:
            existing.add(canonical)
            attr_decls[(src_type, canonical)] = {
                "type": src_type,
                "name": canonical,
                "uri": attr_uri(src_type, canonical),
                "range": type_uri(tgt_type),
                "kind": "relationship",
            }
        # Materialize the target as a first-class node (rdf:type + label), as the writer does.
        node_materializations[tgt_uri] = {
            "uri": tgt_uri,
            "rdf_type": type_uri(tgt_type),
            "label": rel.target_id,
        }

    # rdf:type/label rows and instance edges are idempotent triples in the store, so the
    # frozen artifact is their SET — dedup by identity (a same-name merge, the coffee_shops
    # dedupe trap, otherwise emits the primary node's row twice).
    entities_by_uri = {r["uri"]: r for r in entity_rows}
    edges_by_triple = {(e["subject"], e["predicate"], e["object"]): e for e in rel_edges}
    a5 = {
        # ONTA-270: the ontology version this placement plan was computed AGAINST.
        # `capture_boundary` always renders cold-start (into an EMPTY ontology —
        # every proposed type is new), so P5 read the empty ontology; the stamp is
        # therefore the empty-ontology fingerprint (a deterministic constant, no
        # timestamp/nonce). At runtime P6 rejects/recomputes a plan whose stamp no
        # longer matches the current ontology (see schema_resolver ONTA-270).
        "ontology_version": ontology_version({}, {}),
        "types": [
            {"name": n, "uri": t["uri"], "subclass_of": sorted(t["subclass_of"]), "kind": t["kind"]}
            for n, t in sorted(types.items())
        ],
        "entities": sorted(entities_by_uri.values(), key=lambda r: r["uri"]),
        "attribute_declarations": sorted(attr_decls.values(), key=lambda d: d["uri"]),
        "relationship_edges": sorted(
            edges_by_triple.values(), key=lambda e: (e["subject"], e["predicate"], e["object"])
        ),
        "node_materializations": sorted(node_materializations.values(), key=lambda m: m["uri"]),
    }
    # Verified facts are the SET of persisted triples: identical (s,p,o) triples collapse
    # in the store, but distinct VALUES on the same predicate (multi-valued) are kept.
    facts_by_triple = {(f["subject"], f["predicate"], f["object"]): f for f in verified}
    a4 = {
        "verified_facts": sorted(
            facts_by_triple.values(), key=lambda f: (f["subject"], f["predicate"], f["object"])
        ),
        "rejections": sorted(
            rejections, key=lambda r: (r["entity_id"], r["attribute"], r["value"])
        ),
    }
    return BoundaryArtifacts(domain=domain, a2=_a2_dict(extraction), a4=a4, a5=a5)


# --------------------------------------------------------------------------- #
# Render one domain end to end (canonical dataset -> artifacts)
# --------------------------------------------------------------------------- #
def _load_rows(domain: str, fixtures_dir: Optional[str | Path] = None) -> list[dict]:
    """Load a domain's canonical rows via the scenario fuzzer's fixture catalog, so both
    harnesses read the SAME shipped decomp corpus."""
    datasets = load_fixture_datasets(fixtures_dir, names={domain})
    if not datasets:
        raise FileNotFoundError(f"canonical dataset not found for domain {domain!r}")
    return json.loads(datasets[0].content)


def render_domain(domain: str, fixtures_dir: Optional[str | Path] = None) -> BoundaryArtifacts:
    """Canonical dataset → reference extraction (A2) → captured A2/A4/A5."""
    if domain not in BOUNDARY_SPECS:
        raise KeyError(f"unknown boundary domain {domain!r}; known: {', '.join(BOUNDARY_SPECS)}")
    spec = BOUNDARY_SPECS[domain]
    rows = _load_rows(domain, fixtures_dir)
    extraction = render_extraction(rows, spec)
    return capture_boundary(extraction, domain)


# --------------------------------------------------------------------------- #
# Freeze / check against the on-disk fixtures
# --------------------------------------------------------------------------- #
def default_fixtures_dir() -> Path:
    """``tests/fixtures/boundary`` relative to the OSS repo root — where the frozen
    A2/A4/A5 snapshots live, beside the ``decomp`` canonical corpus they derive from."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "boundary"


def _fixture_path(out_dir: Path, domain: str, tier: str) -> Path:
    return out_dir / f"{domain}.{tier}.json"


def _dumps(payload: dict) -> str:
    """Canonical JSON for freeze + diff: sorted keys, 2-space indent, trailing newline —
    so the frozen file is stable and reviewable, and byte-equality is a valid diff."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def freeze(
    domains: Optional[list[str]] = None,
    out_dir: Optional[str | Path] = None,
    fixtures_dir: Optional[str | Path] = None,
) -> list[Path]:
    """Re-render each domain and WRITE its A2/A4/A5 fixtures. The 'one command' behind
    ``--freeze``: the frozen files are a re-render of the canonical datasets, never
    hand-edited. Returns the paths written."""
    out = Path(out_dir) if out_dir else default_fixtures_dir()
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for domain in domains or list(DEFAULT_DOMAINS):
        arts = render_domain(domain, fixtures_dir)
        for tier in TIERS:
            path = _fixture_path(out, domain, tier)
            path.write_text(_dumps(arts.tier(tier)))
            written.append(path)
    return written


@dataclass
class Divergence:
    """One drift between a re-render and its frozen fixture."""

    domain: str
    tier: str
    reason: str


def check(
    domains: Optional[list[str]] = None,
    out_dir: Optional[str | Path] = None,
    fixtures_dir: Optional[str | Path] = None,
) -> list[Divergence]:
    """Re-render each domain and diff against the frozen fixtures. Returns a list of
    :class:`Divergence` (empty == everything matches). A missing frozen file is itself a
    divergence (freeze was never run / a new domain)."""
    out = Path(out_dir) if out_dir else default_fixtures_dir()
    diffs: list[Divergence] = []
    for domain in domains or list(DEFAULT_DOMAINS):
        arts = render_domain(domain, fixtures_dir)
        for tier in TIERS:
            path = _fixture_path(out, domain, tier)
            rendered = _dumps(arts.tier(tier))
            if not path.exists():
                diffs.append(Divergence(domain, tier, f"frozen fixture missing: {path}"))
                continue
            frozen = path.read_text()
            if frozen != rendered:
                diffs.append(
                    Divergence(domain, tier, _summarize_diff(json.loads(frozen), arts.tier(tier)))
                )
    return diffs


def _summarize_diff(frozen: dict, rendered: dict) -> str:
    """A short, actionable description of how a frozen artifact and a re-render differ —
    which top-level lists changed size, or which scalar keys changed."""
    parts: list[str] = []
    for key in sorted(set(frozen) | set(rendered)):
        fv, rv = frozen.get(key), rendered.get(key)
        if fv == rv:
            continue
        if isinstance(fv, list) and isinstance(rv, list):
            parts.append(f"{key}: {len(fv)} frozen vs {len(rv)} rendered")
        else:
            parts.append(f"{key}: changed")
    return "; ".join(parts) or "content differs"


# --------------------------------------------------------------------------- #
# CLI — python -m cograph_client.qc.boundary
# --------------------------------------------------------------------------- #
def _summarize_domain(arts: BoundaryArtifacts) -> str:
    return (
        f"  {arts.domain}: "
        f"A2[entities={len(arts.a2['entities'])}, rels={len(arts.a2['relationships'])}]  "
        f"A4[verified={len(arts.a4['verified_facts'])}, rejected={len(arts.a4['rejections'])}]  "
        f"A5[types={len(arts.a5['types'])}, edges={len(arts.a5['relationship_edges'])}]"
    )


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m cograph_client.qc.boundary",
        description=(
            "Capture + freeze the ingestion pipeline's inter-stage artifacts (A2 candidate "
            "facts, A4 verified facts, A5 placement plan) as characterization fixtures, and "
            "diff a re-render against them. Deterministic, offline — no LLM, no store."
        ),
    )
    ap.add_argument("--freeze", action="store_true", help="re-render and WRITE the frozen fixtures")
    ap.add_argument("--check", action="store_true", help="diff a re-render against the frozen fixtures (exit 1 on drift)")
    ap.add_argument("--domain", action="append", help="only this domain (repeatable; default: all)")
    ap.add_argument("--out-dir", default=None, help="fixtures dir (default: tests/fixtures/boundary)")
    ap.add_argument("--json", action="store_true", help="emit rendered artifacts as JSON to stdout")
    args = ap.parse_args(argv)

    domains = args.domain or list(DEFAULT_DOMAINS)
    unknown = [d for d in domains if d not in BOUNDARY_SPECS]
    if unknown:
        print(
            f"!! unknown domain(s): {', '.join(unknown)}. known: {', '.join(BOUNDARY_SPECS)}",
            file=sys.stderr,
        )
        return 2

    if args.freeze:
        written = freeze(domains, out_dir=args.out_dir)
        print(f"froze {len(written)} fixture file(s) across {len(domains)} domain(s):")
        for path in written:
            print(f"  {path}")
        return 0

    if args.json:
        payload = {d: {t: render_domain(d).tier(t) for t in TIERS} for d in domains}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    # Default action is --check.
    diffs = check(domains, out_dir=args.out_dir)
    if not diffs:
        print(f"boundary fixtures OK — {len(domains)} domain(s) match their frozen A2/A4/A5.")
        for d in domains:
            print(_summarize_domain(render_domain(d)))
        return 0
    print(f"!! boundary DRIFT — {len(diffs)} artifact(s) diverge from the frozen fixtures:")
    for diff in diffs:
        print(f"  ✗ {diff.domain}.{diff.tier}: {diff.reason}")
    print("\nIf the change is intentional, re-freeze with --freeze and review the diff.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
