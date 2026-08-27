"""LLM extraction output and A2 zero-ontology-commitment helpers.

Extracted from ``resolver/models.py``. Public names stay importable from
``infona_client.resolver.models``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from infona_client.graph.iri import IRI_BASE

_URI_SCHEME = "://"

# ---------------------------------------------------------------------------
# LLM extraction output (non-deterministic, proposed)
# ---------------------------------------------------------------------------


class ExtractedAttribute(BaseModel):
    """A single attribute proposed by the LLM extractor."""

    name: str
    value: str
    datatype: str = "string"
    # ONTA-272: OPTIONAL evidence span / citation supporting this attribute value
    # (a source URL or the source snippet it was drawn from). Default "" keeps the
    # A2 models back-compat — existing extraction that never sets it parses and
    # validates unchanged; the pre-structured fast path populates it from the
    # per-record source_url so an A2 payload can be asserted EVIDENCE-LINKED.
    evidence: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_scalar(cls, v):
        """Stringify non-string SCALAR values the extractor returns.

        The LLM / Firecrawl JSON extraction legitimately emits a bare ``true`` /
        ``false`` or a number for a boolean- or numeric-valued attribute (e.g.
        ``streaming_support: true``, ``context_window: 8192``). ``value`` is
        typed ``str``, so Pydantic v2 would raise ``ValidationError`` on those and
        — because the extraction handler only caught JSON/Key/Type errors and
        ``ValidationError`` subclasses ``ValueError`` — the error propagated and
        failed the WHOLE discovery job with 0 records. Coerce genuine scalars to
        their string form here so extraction proceeds; the downstream validator
        (#166 ``_typed_value``) still canonicalizes the lexical form.

        ``bool`` maps to lowercase ``"true"``/``"false"`` — the canonical
        ``xsd:boolean`` lexical form the validator expects. ``None`` / dict /
        list are left untouched so they fall through to the existing validation
        (not silently swallowed).
        """
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return v


class ExtractedEntity(BaseModel):
    """An entity proposed by the LLM extractor."""

    type_name: str = Field(description="Proposed type name (e.g. 'Property', 'Address')")
    id: str = Field(description="Identifier for this entity (name, URI, or generated)")
    same_as: str | None = Field(default=None, description="Existing type name if this is the same concept")
    parent_type: str | None = Field(default=None, description="Existing type name if this is a subtype")
    parent_chain: list[str] = Field(
        default_factory=list,
        description=(
            "Full ancestor lineage of type_name, most-specific first "
            "(e.g. Condo -> ['Property', 'Asset']). Lets ingest close a brand-new "
            "multi-level subClassOf chain in one row (ADR 0001 rule 3). May include "
            "types not yet in the ontology."
        ),
    )
    also_types: list[str] = Field(
        default_factory=list,
        description=(
            "Genuine ADDITIONAL independent classifications (NOT ancestors of "
            "type_name) — e.g. a hotel employee who is also a guest: type_name="
            "'Employee', also_types=['Guest']. Each becomes a separate asserted "
            "rdf:type (ADR 0001 rule 1). Leave empty unless the entity truly IS "
            "two unrelated things."
        ),
    )
    subtype_description: str | None = Field(
        default=None,
        description=(
            "A brief, human-readable definition of type_name, set ONLY when "
            "type_name is a NEW specialized kind (a subtype) the extractor is "
            "minting — e.g. a 'HumannessIndex' subtype of Score: \"a score "
            "measuring how human a generated voice sounds\". Written as the new "
            "type's rdfs:comment so the ontology carries the definition. Leave "
            "null for pre-existing types and ordinary top-level types."
        ),
    )
    attributes: list[ExtractedAttribute] = Field(default_factory=list)
    # ONTA-272: OPTIONAL evidence span / citation supporting this candidate entity
    # (the source URL / snippet it was drawn from). Default "" keeps A2 back-compat;
    # the pre-structured fast path fills it from the per-record source_url so the
    # zero-ontology-commitment contract can assert the payload is EVIDENCE-LINKED.
    evidence: str = ""


class ExtractedRelationship(BaseModel):
    """A relationship between two extracted entities."""

    source_id: str
    predicate: str
    target_id: str
    # Optional declared types so a batch can contain Contact/17 and Purchase/17
    # without the write-path id map collapsing them. Default None keeps LLM
    # extract payloads and older CSV mappings byte-compatible (unqualified
    # lookup when the raw id is unique in the batch).
    source_type: str | None = None
    target_type: str | None = None


class ExtractionResult(BaseModel):
    """Full output of the LLM extraction step."""

    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)
    source_text: str = ""
    # ONTA-382: attribute-ceiling drops collected by the post-extraction allowlist
    # backstop (``attributes_exhaustive``). Each entry is a :class:`CleanFact`
    # (or a dict with the same shape) ready to fold into ``IngestResult.clean_report``
    # so a ceiling drop is never silent. Empty on the open / illustrative path —
    # byte-identical to pre-ONTA-382. Typed ``list[Any]`` to avoid a forward-ref
    # dance with ``CleanFact`` (defined later in this module).
    ceiling_drops: list[Any] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# A2 zero-ontology-commitment contract (ONTA-272)
# ---------------------------------------------------------------------------
#
# A2 (``ExtractionResult``) is the CANDIDATE-FACTS tier of the P2/P5 seam: the
# extractor PROPOSES soft-typed, evidence-linked candidates and the downstream
# placement layer (P5) decides their final ontology home. "Zero ontology
# commitment" means A2 must never HARD-commit a type TO the ontology — it emits
# candidate NAMES (with their soft lineage suggestions), never a resolved /
# committed ontology reference. Soft lineage (``parent_chain`` / ``also_types`` /
# subtypes) is DELIBERATELY preserved: those are SUGGESTIONS for P5, not
# commitments (ONTA-199 soft-seed extraction beat the hard cage precisely because
# it keeps them). The ONE thing A2 may not do is smuggle a COMMITTED ontology IRI
# into a type slot — a resolved reference pre-empts P5f's placement decision. The
# helpers below make that contract explicit + testable, and the pre-structured
# fast path builds a valid A2 from already-structured rows without the LLM.


class SoftContractViolation(ValueError):
    """Raised when an A2 payload breaks the zero-ontology-commitment contract."""


def _is_committed_type_ref(name: str | None) -> bool:
    """True when a type slot carries a COMMITTED ontology reference (a URI) rather
    than a soft candidate NAME. A candidate type is a bare identifier
    ("Physician", "NursePractitioner"); a committed reference is a resolved IRI
    (f"{IRI_BASE}/types/Physician") — the hard-commitment leak A2 must
    never carry."""
    return bool(name) and _URI_SCHEME in str(name)


def validate_soft_a2(
    result: ExtractionResult, *, require_evidence: bool = False
) -> list[str]:
    """Check an A2 payload (``ExtractionResult``) is SOFT-TYPED-ONLY and (opt-in)
    EVIDENCE-LINKED. Returns a list of human-readable violations — an EMPTY list
    means the payload honors the zero-ontology-commitment contract.

    Assertions (additive + back-compat — existing extraction passes unchanged):
      * every entity proposes a candidate ``type_name`` (a non-empty NAME);
      * NO type slot (``type_name`` / ``same_as`` / ``parent_type`` /
        ``parent_chain`` / ``also_types``) carries a COMMITTED ontology IRI — a
        candidate is a bare name; a URI is a hard commitment that pre-empts P5;
      * when ``require_evidence`` is True, every entity is evidence-linked — it
        carries its own ``evidence`` span or at least one attribute that does.

    Soft lineage is NOT a violation — it is the correct, preserved suggestion the
    placement layer consumes. NEVER re-cage extraction to satisfy this."""
    violations: list[str] = []
    if not isinstance(result, ExtractionResult):
        return [f"A2 payload is not an ExtractionResult (got {type(result).__name__})"]
    for i, e in enumerate(result.entities):
        if not (e.type_name or "").strip():
            violations.append(f"entity[{i}] (id={e.id!r}) has no candidate type_name")
        slots: list[tuple[str, str | None]] = [
            ("type_name", e.type_name),
            ("same_as", e.same_as),
            ("parent_type", e.parent_type),
        ]
        slots += [("parent_chain", p) for p in e.parent_chain]
        slots += [("also_types", a) for a in e.also_types]
        for slot, val in slots:
            if _is_committed_type_ref(val):
                violations.append(
                    f"entity[{i}] (id={e.id!r}) {slot}={val!r} is a committed "
                    "ontology reference (URI), not a soft candidate — A2 must "
                    "emit candidate type NAMES only (zero ontology commitment)"
                )
        if require_evidence:
            linked = bool((e.evidence or "").strip()) or any(
                (a.evidence or "").strip() for a in e.attributes
            )
            if not linked:
                violations.append(
                    f"entity[{i}] (id={e.id!r}) is not evidence-linked (no evidence "
                    "span on the entity or any of its attributes)"
                )
    return violations


def assert_soft_a2(
    result: ExtractionResult, *, require_evidence: bool = False
) -> None:
    """Raise :class:`SoftContractViolation` if ``result`` breaks the A2 contract
    (soft-typed-only + optionally evidence-linked). The fatal enforcement seam for
    the DETERMINISTIC pre-structured fast path, where a violation can only mean a
    code bug (structured rows are provably soft), so failing fast is correct. The
    non-deterministic LLM discovery path uses :func:`validate_soft_a2` and only
    LOGS — imperfect model output must never hard-fail a run."""
    violations = validate_soft_a2(result, require_evidence=require_evidence)
    if violations:
        raise SoftContractViolation("; ".join(violations))


def soft_a2_from_structured_rows(
    rows: list[dict],
    type_name: str,
    *,
    key_field: str | None = None,
    source_url_field: str = "source_url",
) -> ExtractionResult:
    """Build a SOFT-TYPED, evidence-linked A2 (``ExtractionResult``) from
    already-structured rows — DETERMINISTICALLY, NO LLM (ONTA-272 fast path).

    Each row becomes ONE candidate entity typed ``type_name`` (a soft SUGGESTION —
    the pre-structured source confirmed the type, but A2 still only PROPOSES it for
    P5). Every non-empty field except the source-URL becomes a literal
    ``ExtractedAttribute``; the row's ``source_url`` (when present) is carried as
    the entity's + its attributes' ``evidence`` link (the per-record citation). The
    id is the ``key_field`` value, else the row's ``name``, else its positional
    index. Pre-structured rows are inherently soft — flat literal candidates with
    no minted ontology commitment — so the result always passes
    :func:`validate_soft_a2` (and passes ``require_evidence`` when the rows carry a
    source_url)."""
    entities: list[ExtractedEntity] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        evidence = str(row.get(source_url_field) or "").strip()
        rid_raw = (row.get(key_field) if key_field else None) or row.get("name") or str(i)
        rid = str(rid_raw).strip() or str(i)
        attrs: list[ExtractedAttribute] = []
        for k, v in row.items():
            if k == source_url_field:
                continue
            if v is None or str(v).strip() == "":
                continue
            attrs.append(ExtractedAttribute(name=str(k), value=v, evidence=evidence))
        entities.append(
            ExtractedEntity(
                type_name=type_name, id=rid, attributes=attrs, evidence=evidence
            )
        )
    return ExtractionResult(entities=entities)


class ExtractionConstraint(BaseModel):
    """Opt-in constraint that narrows extraction to a confirmed type + attributes.

    Default document / CSV / text ingestion passes ``None`` and stays fully
    open-ended (discovering every type the source justifies — that is its job).
    WEB DISCOVERY (ONTA-199), by contrast, has already CONFIRMED the single
    target type and the exact attribute set with the user, so re-running the
    open-ended multi-type reifier over a rich source payload just mints ~20
    unwanted sub-entities (Address, Taxonomy, Organization, …) and ~3x output
    tokens, which is what blew the extraction-time watchdog. When present, this
    constraint tells the extractor to emit ONLY records of ``types`` with ONLY
    the listed attributes (the key attribute always allowed), and drives a light
    post-extraction guard that drops off-type entities / unrequested attributes.

    A single-type constraint (the discovery case) is the common shape:
    ``types=["Physician"]`` +
    ``attributes={"Physician": ["name", "specialty", "city", "phone"]}``.
    """

    types: list[str] = Field(
        default_factory=list,
        description="The confirmed target type(s) the extractor may emit.",
    )
    attributes: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Per-type allowed attribute names (snake_case). A type absent from "
            "this map has no attribute restriction (all attributes allowed)."
        ),
    )
    soft: bool = Field(
        default=False,
        description=(
            "SEED vs CAGE. False (default, ONTA-199): a HARD constraint — extract "
            "ONLY the flat focus type + listed attributes, drop off-type entities, "
            "strip lineage, emit no relationships. True: a SOFT prior — the focus "
            "type + attributes are a hint, but the extractor decomposes faithfully "
            "(most-specific subtypes, real-world values as nodes, multi-valued "
            "splits, reuse-first) and the post-extraction type guard is a no-op. Soft "
            "restores correct ontology shape on discovery while the prior keeps "
            "extraction focused + compact (measurements stay literal, no per-column "
            "type explosion). Pair with ``attributes_exhaustive`` (ONTA-382) when "
            "the listed attributes must still act as a hard CEILING even in soft "
            "mode — soft type decomposition stays, unlisted attributes do not."
        ),
    )
    attributes_exhaustive: bool = Field(
        default=False,
        description=(
            "ONTA-382 — EXHAUSTIVE vs ILLUSTRATIVE attribute set. False (default): "
            "the listed attributes are a FLOOR / prior (illustrative) — soft mode "
            "may keep extra attributes the source justifies. True: the listed "
            "attributes are a CEILING (allowlist) — even in soft mode, focus-type "
            "records keep ONLY those attributes (plus name/label/title identity); "
            "unlisted attributes are dropped and recorded on the CleanReport ledger. "
            "Hard mode already enforces a ceiling regardless of this flag. Open when "
            "the user never named a closed field list."
        ),
    )

    @property
    def is_active(self) -> bool:
        """True only when the constraint actually restricts something."""
        return bool(self.types)

    def allowed_attributes(self, type_name: str) -> set[str] | None:
        """Allowed attribute names for ``type_name``, or ``None`` = unrestricted."""
        attrs = self.attributes.get(type_name)
        return set(attrs) if attrs else None

    def ceiling_attributes_for(
        self, type_name: str, parent_chain: list[str] | None = None
    ) -> set[str] | None:
        """Attribute allowlist for a focus-related entity under an exhaustive ceiling.

        Applies to the confirmed focus type(s) and to soft-mode subtypes of those
        focus types (detected via ``parent_chain``). Off-type entities the soft
        decomposer lifts out (City, Organization, …) are unrestricted — ``None``.
        Direct map hit wins; otherwise the (single) focus type's list is inherited.
        """
        focus_types = set(self.types)
        chain = list(parent_chain or [])
        is_focus_related = type_name in focus_types or any(p in focus_types for p in chain)
        if not is_focus_related:
            return None
        attrs = self.attributes.get(type_name)
        if attrs:
            return set(attrs)
        for focus in self.types:
            focus_attrs = self.attributes.get(focus)
            if focus_attrs:
                return set(focus_attrs)
        if len(self.attributes) == 1:
            only = next(iter(self.attributes.values()))
            return set(only) if only else None
        return None

