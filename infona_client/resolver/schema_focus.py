from __future__ import annotations

"""SOFT-mode focus-type floor + type-token helpers (ONTA-255).

Job: keep a numeric cost/latency metric off an off-brief standards node.
Do not fork this floor in ingest or per-chunk extract — ``ingest`` is the
authoritative full-batch pass (allow_strip=True); the per-chunk backstop
calls here with allow_strip=False.
"""

import re

from infona_client.resolver.models import ExtractionResult
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr

# --- ONTA-255: SOFT-mode focus-type floor + metric-misattachment guard -------
# SOFT extraction (the seed prior) deliberately lets the model decompose freely
# — that faithful decomposition IS the desired output, so the soft post-guard
# stays a no-op for everything EXCEPT one drift failure it must still assert
# against. When a multi-type brief ("<subject> records … with pricing, latency,
# AND compliance") is collapsed to a single focus type + a flat attribute list,
# and a source page interleaves subject rows with certification / standard rows,
# the extractor can latch onto a fact-ABOUT-the-subject (a Compliance / Standard
# concept) as the DOMINANT type and mint the subject's records under it — folding
# the subject's own cost / latency / price metrics onto a standards-body node.
# The confirmed focus type is a CONTRACT about what the records ARE, so a numeric
# metric-shaped attribute (cost / price / fee / latency / throughput …) must not
# sit on an entity whose TYPE reads as a standards / certification / regulation
# concept — the metric belongs to the SUBJECT. What the guard actually does with
# a misattached metric, honestly (not "always re-homed"):
#   * RE-HOME when a subject can be identified — the concept entity is linked to a
#     focus subject by a surviving edge, OR there is exactly ONE focus subject in
#     the batch (see the single-subject caveat at the `sole_focus` attach below).
#   * Otherwise (no identifiable subject) STRIP the metric off the concept node so
#     a cost/latency triple can never persist on a standards entity, and COUNT +
#     LOG every removed value so nothing is silent. When the focus type minted
#     ~zero entities at all, that strip is the FOCUS-TYPE FLOOR breach and is
#     logged as `discovery_focus_type_starved`.
# `allow_strip` gates the destructive half: the PER-CHUNK backstop
# (`_apply_extraction_constraint`) runs with allow_strip=False so a partial view
# can RE-HOME within its own chunk but NEVER strip-and-declare-starved (the
# subject may live in another chunk); only the MERGED full-batch pass in `ingest`
# runs with allow_strip=True and is trusted to strip / judge starvation.
# A compliance-FOCUSED KG is safe: its confirmed focus IS the cert/standard, so
# those entities are focus-lineage and never treated as a misattachment target.

# Whole-token allowlist for a type whose NAME reads as a genuine standards / cert
# / regulation concept. Deliberately NARROW and matched as WHOLE tokens (never as
# a loose stem): loose stems like "standard"/"license"/"audit"/"governance" occur
# inside ordinary SUBJECT types (StandardRoom, SoftwareLicense, AuditLog,
# GovernanceBoard) and would mis-yank their legitimate metrics. The tokenizer
# de-pluralizes ("Certifications" -> "certification", "Certs" -> "cert").
_STANDARDS_CONCEPT_TOKENS = frozenset(
    {
        "compliance",
        "certification",
        "certificate",
        "cert",
        "regulation",
        "regulatory",
        "accreditation",
        "attestation",
    }
)
# "standard" is compound-prone, so it signals a standards concept ONLY as a BARE
# type (Standard / Standards) or when combined with a token above (which already
# matches). Any Standard-COMPOUND without a concept token (StandardRoom,
# StandardPlan, StandardEdition) is a subject and keeps its metrics.
_BARE_STANDARD_TOKENS = frozenset({"standard", "standards"})

# Substrings that mark an attribute NAME as a cost / price / latency-shaped
# metric. Combined with a numeric-value check so a non-numeric attribute whose
# name merely contains one of these (e.g. `pricing_model: "usage-based"`) is
# never touched.
_METRIC_NAME_SUBSTRINGS = (
    "cost",
    "price",
    "pricing",
    "fee",
    "latency",
    "throughput",
    "bandwidth",
    "per_minute",
    "per_second",
    "per_hour",
    "per_token",
    "per_unit",
    "_ms",
)

_NUMERIC_DATATYPES = frozenset(
    {"integer", "int", "float", "number", "double", "decimal", "long"}
)
# A leading number (optionally signed / currency-prefixed), so "0.30", "$0.30",
# "200", and "200ms" read as numeric while "SprocketSafe" / "GDPR" / "yes" do not.
_LEADING_NUMBER_RE = re.compile(r"^\s*[-+]?\s*\$?\s*\d[\d,]*(?:\.\d+)?")


def _split_type_tokens(name: str) -> set[str]:
    """Lowercased word tokens of a type name, splitting camelCase and separators,
    with a crude de-pluralization so "Standards" matches "standard"."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    tokens: set[str] = set()
    for part in re.split(r"[^A-Za-z0-9]+", spaced):
        if not part:
            continue
        low = part.lower()
        tokens.add(low)
        if low.endswith("s") and len(low) > 1:
            tokens.add(low[:-1])
    return tokens


def _is_standards_concept_type(type_name: str) -> bool:
    """True when ``type_name`` reads as a genuine standards / certification /
    regulation concept (a fact ABOUT a subject, not a subject itself).

    Whole-token match against a narrow allowlist, so compound SUBJECT types that
    merely CONTAIN a loose stem — StandardRoom, SoftwareLicense, License,
    AuditLog / AuditTrail, LicensePlate, GovernanceBoard, DataGovernance — are
    correctly classified as NON-concept and keep their own metrics. "standard"
    counts only as a bare type (Standard / Standards) or paired with a concept
    token (ComplianceStandard / RegulatoryStandard, matched by the token above).
    """
    tokens = _split_type_tokens(type_name)
    if tokens & _STANDARDS_CONCEPT_TOKENS:
        return True
    return bool(tokens) and tokens <= _BARE_STANDARD_TOKENS


def _is_metric_attribute(attr) -> bool:
    """True when ``attr`` is a numeric metric (cost / price / latency-shaped NAME
    AND a numeric value/datatype). Requires BOTH so non-numeric look-alikes stay."""
    name = (getattr(attr, "name", "") or "").lower()
    if not any(sub in name for sub in _METRIC_NAME_SUBSTRINGS):
        return False
    if (getattr(attr, "datatype", "") or "").lower() in _NUMERIC_DATATYPES:
        return True
    return bool(_LEADING_NUMBER_RE.match(str(getattr(attr, "value", "") or "")))


def _primary_entity_ids(extraction) -> set[str]:
    """Entity ids that are PRIMARY records vs dimension-only nodes (ONTA-383).

    Domain-free structural split used when consolidating under a soft focus type:
      * **primary** — appears as a relationship SOURCE, or has no relationships at
        all (orphan flat record). These are the subject records the focus names.
      * **dimension** — appears ONLY as a relationship TARGET (City, Specialty,
        Certification, …). Free type minting stays allowed for them.

    An entity that is both source and target counts as primary (it owns edges).
    """
    if extraction is None or not getattr(extraction, "entities", None):
        return set()
    sources = {r.source_id for r in (extraction.relationships or ())}
    targets = {r.target_id for r in (extraction.relationships or ())}
    only_targets = targets - sources
    all_ids = {e.id for e in extraction.entities}
    # Primary = everything that is NOT exclusively a relationship target.
    return all_ids - only_targets


def _apply_soft_focus_floor(result, constraint, *, allow_strip: bool = True):
    """SOFT-mode focus-type floor + metric-misattachment guard (ONTA-255).

    Runs only for an ACTIVE, SOFT constraint. Returns ``result`` UNCHANGED unless
    a numeric metric-shaped attribute has landed on an off-brief standards /
    certification / regulation-typed entity — the drift signature. When it has,
    each such metric is handled by identifiability, NOT unconditionally re-homed:

      * RE-HOMED onto a focus-lineage subject when one can be identified — the
        concept entity is linked to a focus subject by a surviving edge, or there
        is exactly ONE focus subject in the batch.
      * Otherwise STRIPPED off the concept node (so a cost/latency triple can never
        persist on a standards entity) and COUNTED. When no focus subject survives
        at all, that strip is the floor breach: ``discovery_focus_type_starved``.

    ``allow_strip`` gates only the destructive half. The PER-CHUNK backstop passes
    ``allow_strip=False``: on a partial view it re-homes within its own chunk but
    NEVER strips or declares starvation (the subject may be in another chunk), so
    the metric survives for the merged full-batch pass to re-home. The merged pass
    (``allow_strip=True``, the default) is the only one trusted to strip / judge
    starvation. A same-named metric that collides on the subject is COUNTED and
    logged (`discovery_metric_collision`), never silently dropped.

    Never drops an entity, a relationship, or a non-metric attribute. Idempotent:
    a second pass finds no misattached metric and returns the input untouched.
    """
    if constraint is None or not getattr(constraint, "is_active", False):
        return result
    if not getattr(constraint, "soft", False):
        return result

    focus_types = set(constraint.types)

    def _is_focus_lineage(e) -> bool:
        if e.type_name in focus_types:
            return True
        lineage = set(e.parent_chain or []) | set(e.also_types or [])
        if e.parent_type:
            lineage.add(e.parent_type)
        return bool(lineage & focus_types)

    focus_entities = [e for e in result.entities if _is_focus_lineage(e)]
    # Concept entities: standards/cert/regulation-typed AND not themselves the
    # confirmed focus (a compliance-focused KG's own records are never targets).
    concept_entities = [
        e
        for e in result.entities
        if not _is_focus_lineage(e) and _is_standards_concept_type(e.type_name)
    ]

    misattached = [
        (e, [a for a in e.attributes if _is_metric_attribute(a)])
        for e in concept_entities
    ]
    misattached = [(e, metrics) for e, metrics in misattached if metrics]
    if not misattached:
        return result  # no drift — soft decomposition passes through untouched

    # Map a concept entity to a focus subject it is directly linked to, so a
    # re-homed metric lands on the RIGHT subject when the edge survived extraction.
    focus_by_id = {e.id: e for e in focus_entities}
    linked_focus_of: dict[str, object] = {}
    for r in result.relationships:
        if r.source_id in focus_by_id and r.target_id not in focus_by_id:
            linked_focus_of.setdefault(r.target_id, focus_by_id[r.source_id])
        if r.target_id in focus_by_id and r.source_id not in focus_by_id:
            linked_focus_of.setdefault(r.source_id, focus_by_id[r.target_id])
    # Single-subject fallback: when exactly one focus subject exists and the
    # concept carries no surviving link, attach to that subject. CAVEAT: a metric
    # that truly belonged to an ABSENT subject would attach to the present one —
    # accepted because a discovery micro-batch is homogeneous per source_url, so
    # the single surviving subject is almost always the right owner, and the
    # alternative (dropping the metric) is worse.
    sole_focus = focus_entities[0] if len(focus_entities) == 1 else None

    # Only the full-batch pass (allow_strip=True) may declare the floor breached;
    # a per-chunk partial view must never call starvation.
    starved = allow_strip and len(focus_entities) == 0
    stripped_attrs: dict[str, list] = {}   # concept id -> surviving (non-metric) attrs
    add_to_focus: dict[str, list] = {}     # focus id -> metrics moved onto it
    reattributed = 0
    stripped = 0
    collisions = 0
    for concept_entity, metrics in misattached:
        dest = linked_focus_of.get(concept_entity.id) or sole_focus
        if dest is None:
            if not allow_strip:
                # PER-CHUNK partial view: the subject may live in another chunk.
                # Leave the metric in place — the merged pass re-homes it. Never
                # strip here (would destroy it) and never declare starvation.
                continue
            # MERGED pass, no subject anywhere → assert the floor. Count the loss.
            stripped_attrs[concept_entity.id] = [
                a for a in concept_entity.attributes if not _is_metric_attribute(a)
            ]
            stripped += len(metrics)
            continue
        # A subject was identified → move the metrics off the concept onto it.
        stripped_attrs[concept_entity.id] = [
            a for a in concept_entity.attributes if not _is_metric_attribute(a)
        ]
        existing = {a.name for a in dest.attributes} | {
            a.name for a in add_to_focus.get(dest.id, [])
        }
        for m in metrics:
            if m.name in existing:
                # The subject already holds this metric slot (its own value, or an
                # earlier re-home). Do NOT silently drop the second value — count
                # and log it so the collision is visible.
                collisions += 1
                _sr.logger.warning(
                    "discovery_metric_collision",
                    focus_types=sorted(focus_types),
                    concept_type=concept_entity.type_name,
                    subject_id=dest.id,
                    attribute=m.name,
                    dropped_value=str(getattr(m, "value", "")),
                )
                continue
            add_to_focus.setdefault(dest.id, []).append(m)
            existing.add(m.name)
            reattributed += 1

    if not stripped_attrs and not add_to_focus:
        # PER-CHUNK partial view could not identify any subject → nothing acted
        # on; leave the batch for the merged pass. (Cannot happen when
        # allow_strip=True, which always strips an un-re-homable metric.)
        return result

    new_entities = []
    for e in result.entities:
        update: dict = {}
        if e.id in stripped_attrs:
            update["attributes"] = stripped_attrs[e.id]
        if e.id in add_to_focus:
            base = update.get("attributes", list(e.attributes))
            update["attributes"] = base + add_to_focus[e.id]
        if update:
            e = e.model_copy(update=update)
        new_entities.append(e)

    concept_type_names = sorted({e.type_name for e, _ in misattached})
    if starved:
        _sr.logger.error(
            "discovery_focus_type_starved",
            focus_types=sorted(focus_types),
            concept_types=concept_type_names,
            metrics_reattributed=reattributed,
            metrics_stripped=stripped,
            metrics_collision=collisions,
            focus_entities=len(focus_entities),
        )
    else:
        _sr.logger.warning(
            "discovery_metric_reattributed",
            focus_types=sorted(focus_types),
            concept_types=concept_type_names,
            metrics_reattributed=reattributed,
            metrics_stripped=stripped,
            metrics_collision=collisions,
            focus_entities=len(focus_entities),
            partial_view=not allow_strip,
        )

    return ExtractionResult(
        entities=new_entities,
        relationships=result.relationships,
        source_text=result.source_text,
    )
