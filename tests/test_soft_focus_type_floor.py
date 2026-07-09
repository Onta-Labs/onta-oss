"""SOFT-mode focus-type floor + metric-misattachment guard (ONTA-255).

Web discovery collapses a multi-type brief ("<subject> records … with pricing,
latency, AND compliance") into a single confirmed focus type + a flat attribute
list, then extracts in SOFT (seed) mode. When a source page interleaves subject
rows with certification / standard rows, the soft extractor can latch onto a
fact-ABOUT-the-subject (a Compliance / Standard concept) as the DOMINANT type,
mint the subject's records under it, and fold the subject's own cost / latency
metrics onto that standards node — leaving the KG unusable for its purpose. The
old soft post-guard was a deliberate no-op, so nothing caught the drift.

`_apply_soft_focus_floor` asserts the floor NON-DESTRUCTIVELY: a numeric metric
on an off-brief standards/cert-typed entity is RE-HOMED onto the focus subject;
when no focus subject survives (the floor is breached) it is removed from the
concept node and `discovery_focus_type_starved` is logged.

Anti-overfit: these tests use ONLY invented tokens — Widget / Sprocket / Gadget —
and assert the MECHANISM (the floor fires / no cert-typed entity keeps a numeric
metric), so they hold for ANY domain, not just the originating example.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import cograph_client.resolver.schema_resolver as sr
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionConstraint,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.verdict_cache import JsonVerdictCache


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _soft(*attrs: str) -> ExtractionConstraint:
    """A SOFT constraint focused on the invented `Widget` type."""
    return ExtractionConstraint(
        types=["Widget"], attributes={"Widget": list(attrs)}, soft=True
    )


def _num_metric_on_concept_entity(result: ExtractionResult) -> bool:
    """True iff ANY non-focus, standards/cert-typed entity still carries a numeric
    cost/latency-shaped triple — the invariant the guard must make false."""
    for e in result.entities:
        lineage = {e.type_name} | set(e.parent_chain or []) | set(e.also_types or [])
        if "Widget" in lineage:
            continue  # focus-lineage subject legitimately owns its metrics
        if not sr._is_standards_concept_type(e.type_name):
            continue
        if any(sr._is_metric_attribute(a) for a in e.attributes):
            return True
    return False


def _drifted_batch_with_subject() -> ExtractionResult:
    """The drift shape WITH a surviving subject: a Widget row, plus a cert-typed
    "SprocketSafe" entity that wrongly absorbed the Widget's cost/latency."""
    widget = ExtractedEntity(
        type_name="Widget",
        id="w1",
        attributes=[ExtractedAttribute(name="name", value="Widget One")],
    )
    cert = ExtractedEntity(
        type_name="Compliance",
        id="SprocketSafe",
        attributes=[
            ExtractedAttribute(name="label", value="SprocketSafe"),
            ExtractedAttribute(name="cost_per_unit", value="4.20", datatype="float"),
            ExtractedAttribute(name="latency_ms", value="180"),
        ],
    )
    rel = ExtractedRelationship(
        source_id="w1", predicate="certified_for", target_id="SprocketSafe"
    )
    return ExtractionResult(entities=[widget, cert], relationships=[rel])


def _fully_drifted_batch() -> ExtractionResult:
    """The floor-breach shape: NO subject survives — every record was minted as a
    standards concept and carries the subject's metrics."""
    cert_a = ExtractedEntity(
        type_name="Certification",
        id="GadgetCert",
        attributes=[
            ExtractedAttribute(name="label", value="GadgetCert"),
            ExtractedAttribute(name="cost_per_unit", value="1.10", datatype="float"),
        ],
    )
    cert_b = ExtractedEntity(
        type_name="RegulatoryStandard",
        id="SprocketReg",
        attributes=[
            ExtractedAttribute(name="label", value="SprocketReg"),
            ExtractedAttribute(name="latency_ms", value="90"),
        ],
    )
    return ExtractionResult(entities=[cert_a, cert_b], relationships=[])


# --------------------------------------------------------------------------- #
# Classifier units (domain-neutral)                                           #
# --------------------------------------------------------------------------- #
def test_standards_concept_type_classifier():
    for t in ("Compliance", "Certification", "Standards", "RegulatoryStandard",
              "ComplianceRegime", "Accreditation", "Cert"):
        assert sr._is_standards_concept_type(t), t
    for t in ("Widget", "Gadget", "Sprocket", "Provider", "Model", "City"):
        assert not sr._is_standards_concept_type(t), t


def test_metric_attribute_requires_name_and_number():
    # metric-named AND numeric → yes
    assert sr._is_metric_attribute(ExtractedAttribute(name="cost_per_unit", value="4.2"))
    assert sr._is_metric_attribute(ExtractedAttribute(name="latency_ms", value="200ms"))
    assert sr._is_metric_attribute(
        ExtractedAttribute(name="price", value="9", datatype="integer")
    )
    # metric-named but NOT numeric → no (a look-alike must not be touched)
    assert not sr._is_metric_attribute(
        ExtractedAttribute(name="pricing_model", value="usage-based")
    )
    # numeric but NOT metric-named → no
    assert not sr._is_metric_attribute(ExtractedAttribute(name="year", value="2026"))
    # neither → no
    assert not sr._is_metric_attribute(
        ExtractedAttribute(name="certification", value="SprocketSafe")
    )


# --------------------------------------------------------------------------- #
# The floor guard — pure-function mechanism tests                             #
# --------------------------------------------------------------------------- #
def test_floor_reattributes_metrics_to_the_focus_subject():
    """With a surviving Widget subject, the misplaced cost/latency metrics are
    MOVED onto the Widget and removed from the cert-typed entity — no data lost."""
    out = sr._apply_soft_focus_floor(_drifted_batch_with_subject(), _soft("cost_per_unit"))

    # MECHANISM: no cert-typed entity keeps a numeric metric triple.
    assert not _num_metric_on_concept_entity(out)

    widget = next(e for e in out.entities if e.type_name == "Widget")
    names = {a.name for a in widget.attributes}
    assert {"cost_per_unit", "latency_ms"} <= names   # re-homed onto the subject
    assert "name" in names                            # original attr preserved

    cert = next(e for e in out.entities if e.type_name == "Compliance")
    cert_names = {a.name for a in cert.attributes}
    assert "label" in cert_names                       # non-metric attr kept
    assert not (cert_names & {"cost_per_unit", "latency_ms"})  # metrics gone

    # Entities + relationships are never dropped (non-destructive).
    assert {e.id for e in out.entities} == {"w1", "SprocketSafe"}
    assert len(out.relationships) == 1


def test_floor_reports_starvation_when_no_subject_survives():
    """When the whole batch drifted onto standards concepts (focus type starved),
    the guard logs `discovery_focus_type_starved` and strips the metrics off the
    concept nodes so a cost/latency triple can never persist on a standards node."""
    mock_logger = MagicMock()
    original = sr.logger
    sr.logger = mock_logger
    try:
        out = sr._apply_soft_focus_floor(_fully_drifted_batch(), _soft("cost_per_unit"))
    finally:
        sr.logger = original

    # MECHANISM: no cert-typed entity carries a numeric metric after the guard.
    assert not _num_metric_on_concept_entity(out)
    # Concept entities themselves are kept (only the misplaced metric is removed).
    assert {e.id for e in out.entities} == {"GadgetCert", "SprocketReg"}

    starved = [
        c for c in mock_logger.error.call_args_list
        if c.args and c.args[0] == "discovery_focus_type_starved"
    ]
    assert starved, "expected a discovery_focus_type_starved error log"
    assert starved[0].kwargs["focus_types"] == ["Widget"]
    assert starved[0].kwargs["focus_entities"] == 0
    assert starved[0].kwargs["metrics_stripped"] == 2


def test_floor_is_noop_without_drift():
    """A healthy soft batch (metrics live on the subject, cert is a linked node
    with no metrics) is returned UNCHANGED — identity preserved."""
    widget = ExtractedEntity(
        type_name="Widget",
        id="w1",
        attributes=[
            ExtractedAttribute(name="name", value="Widget One"),
            ExtractedAttribute(name="cost_per_unit", value="4.20", datatype="float"),
        ],
    )
    cert = ExtractedEntity(
        type_name="Certification",
        id="SprocketSafe",
        attributes=[ExtractedAttribute(name="label", value="SprocketSafe")],
    )
    rel = ExtractedRelationship(
        source_id="w1", predicate="certified_for", target_id="SprocketSafe"
    )
    inp = ExtractionResult(entities=[widget, cert], relationships=[rel])
    assert sr._apply_soft_focus_floor(inp, _soft("cost_per_unit")) is inp


def test_floor_does_not_misfire_on_a_standards_focused_kg():
    """When the CONFIRMED focus IS a standards concept (a compliance KG), its own
    records are focus-lineage and must keep their metrics — never treated as a
    misattachment target."""
    comp = ExtractedEntity(
        type_name="Compliance",
        id="c1",
        attributes=[ExtractedAttribute(name="cost_per_unit", value="9.9", datatype="float")],
    )
    inp = ExtractionResult(entities=[comp], relationships=[])
    constraint = ExtractionConstraint(types=["Compliance"], attributes={}, soft=True)
    assert sr._apply_soft_focus_floor(inp, constraint) is inp


def test_floor_is_idempotent():
    """A second pass over the guard's own output finds no drift → no-op."""
    once = sr._apply_soft_focus_floor(_drifted_batch_with_subject(), _soft("cost_per_unit"))
    twice = sr._apply_soft_focus_floor(once, _soft("cost_per_unit"))
    assert twice is once


def test_floor_untouched_for_hard_and_inactive_constraints():
    """HARD mode and inactive/None constraints never enter the floor guard."""
    drift = _drifted_batch_with_subject()
    hard = ExtractionConstraint(types=["Widget"], attributes={}, soft=False)
    assert sr._apply_soft_focus_floor(drift, hard) is drift
    assert sr._apply_soft_focus_floor(drift, None) is drift
    inactive = ExtractionConstraint(types=[], attributes={}, soft=True)
    assert sr._apply_soft_focus_floor(drift, inactive) is drift


# --------------------------------------------------------------------------- #
# Prompt guard (fix #2) — concept-level, reword-tolerant                       #
# --------------------------------------------------------------------------- #
def test_target_prompt_teaches_subject_vs_fact_and_metric_placement():
    block = sr.EXTRACTION_TARGET_SYSTEM.lower()
    # A requested attribute like compliance is a FACT/EDGE about the subject,
    # reached via a relationship — NOT a type to mint records under.
    assert "certif" in block or "compliance" in block or "standard" in block
    assert "certified_for" in block or "complies_with" in block or "conforms_to" in block
    # Metrics belong on the subject, never on a standards/cert entity.
    assert "measurement" in block or "metric" in block or "cost" in block
    assert "never" in block
    # The neutral Widget/Sprocket worked example is present (anti-domain-lock).
    assert "widget" in block and "sprocketsafe" in block


# --------------------------------------------------------------------------- #
# Integration — the floor runs inside SchemaResolver.ingest                    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ingest_applies_floor_before_resolve(tmp_path, monkeypatch):
    """Driving `ingest` in SOFT mode with a drifted extraction, the batch that
    reaches `_resolve_and_insert` has NO cert-typed entity carrying a numeric
    metric — the floor ran before any write decision. Uses only invented tokens."""
    neptune = AsyncMock()
    resolver = SchemaResolver(
        neptune, "fake-key", JsonVerdictCache(tmp_path / "cache.json")
    )

    # Avoid all network/ontology I/O.
    resolver._fetch_ontology = AsyncMock(return_value=({}, {}))
    resolver._fetch_parent_map = AsyncMock(return_value={})
    # The extractor drifts: the Widget's metrics land on a cert-typed node.
    resolver._extract = AsyncMock(return_value=_drifted_batch_with_subject())

    captured: dict = {}

    async def _capture_resolve(extraction, *args, **kwargs):
        captured["extraction"] = extraction
        return IngestResult(entities_extracted=len(extraction.entities))

    resolver._resolve_and_insert = AsyncMock(side_effect=_capture_resolve)

    await resolver.ingest(
        "widget rows with pricing, latency, and compliance",
        tenant_id="test-tenant",
        content_type="text",
        constrain_types=["Widget"],
        constrain_attributes={"Widget": ["cost_per_unit", "latency_ms"]},
        constrain_soft=True,
    )

    out = captured["extraction"]
    assert not _num_metric_on_concept_entity(out)
    widget = next(e for e in out.entities if e.type_name == "Widget")
    assert {"cost_per_unit", "latency_ms"} <= {a.name for a in widget.attributes}
