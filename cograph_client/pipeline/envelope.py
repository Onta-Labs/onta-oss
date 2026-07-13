"""``ArtifactEnvelope`` — the universal per-artifact metadata every pipeline
stage carries (ONTA-265).

Onta's pipeline is being decomposed into ten sub-projects (P0-P9) producing a
chain of eleven frozen inter-stage artifacts (A0-A10: Source Bundle, Candidate,
Clean, Verified, Placement Plan, Graph Delta, Answer, Refresh Delta, Run
Manifest, Correction & Feedback). Every artifact from A1 through A10 needs a
common envelope so cross-cutting concerns (which workspace, which run, how to
trace one fact through fan-out/fan-in, when it was produced, how much has been
spent) aren't reinvented per rail. Decision doc:
``docs/adr/0011-universal-artifact-envelope-schema.md`` — read that first for
the field-by-field rationale and the ``fact_id`` derivation trade-offs; this
module is the schema's executable form.

**This is a schema/type stub only.** Nothing in ``cograph_client`` constructs or
consumes an :class:`ArtifactEnvelope` yet — no pipeline stage (resolver,
enrichment, research harness, kg_writer, ...) is wired to populate or read it.
Wiring lands per-rail in later waves (tracked: ONTA-271 fact_id threading,
ONTA-273 A9 Run Manifest, ONTA-270 ontology-version stamping).

Boundary: OSS. Imports only stdlib.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

# Frozen namespace UUID for `derive_fact_id`'s uuid5 calls. Minted once
# (uuid.uuid5(uuid.NAMESPACE_DNS, "getonta.com/pipeline/fact_id")) and pinned
# as a literal forever after — regenerating it would silently change every
# fact_id a replayed run produces, breaking the "same run replayed
# deterministically produces the same ids" property the derivation exists for.
FACT_ID_NAMESPACE = uuid.UUID("ff9cd070-1ae5-5c6b-998e-9e1c7d25d72d")


def _fid_component(value: str) -> str:
    """Hash one ``derive_fact_id`` component to a fixed-width hex digest.

    The un-escaped ``"|".join(...)`` this replaced could collide across
    component boundaries: ``local_key`` legitimately carries ``|`` / ``,`` (a
    source URL, an attribute name), so ``stage="A2|x", local_key="y"`` joined to
    the SAME string as ``stage="A2", local_key="x|y"`` and minted the same id for
    two distinct facts (ONTA-271). Hashing each component to a 64-char hex digest
    — which can contain neither delimiter — makes the join injective on the
    component tuple, so distinct inputs can no longer collide.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def derive_fact_id(
    *,
    run_id: str,
    stage: str,
    parent_fact_ids: Sequence[str] = (),
    local_key: str = "",
) -> str:
    """Mint a stable, deterministic ``fact_id`` (ADR 0011 §4 — provenance-derived,
    not content-hash).

    A UUID5 over ``(run_id, stage, sorted(parent_fact_ids), local_key)``:
    deterministic (replaying the same run with the same inputs reproduces the
    same id — useful for idempotent retries and tests), independent of the
    artifact's CONTENT (so normalization/cleaning between stages never breaks
    identity), and threads lineage explicitly via ``parent_fact_ids`` rather than
    relying on content collapsing duplicates.

    ``parent_fact_ids`` sorted before hashing so fan-in from the same parent set
    in a different collection order still yields the same id. ``local_key`` is
    whatever the producing stage uses to disambiguate siblings from the same
    parent(s) — a row index, an attribute name, a source URL, a split index.
    Root artifacts (A1, or a first A2 with no parent) pass ``parent_fact_ids=()``.

    Collision-hardening (ONTA-271): every component is hashed via
    :func:`_fid_component` BEFORE the ``|``-join, so a ``|`` or ``,`` inside a
    ``local_key`` / ``stage`` (source URLs, attribute names routinely carry them)
    can no longer bleed across a boundary and mint the same id for two distinct
    facts. The ``FACT_ID_NAMESPACE`` seed is unchanged.
    """
    parents = ",".join(sorted(_fid_component(str(p)) for p in parent_fact_ids))
    name = "|".join(
        _fid_component(part)
        for part in (str(run_id), str(stage), parents, str(local_key))
    )
    return str(uuid.uuid5(FACT_ID_NAMESPACE, name))


@dataclass(frozen=True)
class ArtifactEnvelope:
    """The universal metadata carried by every A1-A10 pipeline artifact.

    ``workspace_id`` is MANDATORY, set once where a workspace-scoped request
    enters the pipeline (P0/P1), and propagated verbatim to every downstream
    artifact — NEVER re-derived from content or re-looked-up mid-pipeline (ADR
    0011 §2/§3; mirrors the route-layer ``get_tenant`` pattern, extended to
    internal plumbing). Note the deliberate naming: pipeline code says
    ``workspace_id`` (the product-facing term the A0-A10 spec uses); existing
    infra (routes, stores, auth) keeps saying ``tenant_id`` unchanged — see ADR
    0011 §3, do not blanket-rename one into the other.

    ``run_id`` is MANDATORY, minted once per pipeline run (P0 Runtime &
    Orchestration) and propagated to every artifact that run produces. A
    refresh cycle (P8) mints a NEW run_id, chaining back to the prior run via
    ``parent_fact_ids`` rather than sharing a run_id.

    ``fact_id`` is MANDATORY and stable for one traceable unit of work as it
    moves through the pipeline — mint it with :func:`derive_fact_id`, or via
    :meth:`child` for the common single-parent transform.

    ``parent_fact_ids`` is empty at the root (A1, or a first A2), holds exactly
    one id for a straight-line transform, and holds N ids for fan-in (P6
    merging several candidates into one write). Fan-out (one A2 candidate
    producing several A3 facts) mints multiple children, each with the same
    single parent.

    ``observed_at`` is when this ARTIFACT was produced (audit/freshness
    ordering for P8) — not necessarily when the underlying real-world fact was
    true; that is a separate business-time concern some payloads may carry.

    ``spend_usd`` is CUMULATIVE spend attributable to the run as of this
    artifact — monotonically non-decreasing along any one lineage path. Mirrors
    the existing ``Budget`` / ``ResearchTrace.total_cost_usd()`` pattern in
    ``cograph_client.research.types``. This is what A9 Run Manifest sums per
    item to catch the OpenRouter-402-class silent partial run.

    ``ontology_version`` is set ONLY by A5 Placement Plan and A6 Graph Delta
    producers (P5/P6) — every other artifact leaves it ``None``. Stamps which
    ontology snapshot a placement decision was made against.
    """

    workspace_id: str
    run_id: str
    fact_id: str
    parent_fact_ids: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    spend_usd: float = 0.0
    ontology_version: str | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("ArtifactEnvelope.workspace_id is mandatory")
        if not self.run_id:
            raise ValueError("ArtifactEnvelope.run_id is mandatory")
        if not self.fact_id:
            raise ValueError("ArtifactEnvelope.fact_id is mandatory")
        if not isinstance(self.parent_fact_ids, tuple):
            # Frozen dataclass: normalize a caller-supplied list/other sequence
            # via object.__setattr__ rather than plain assignment.
            object.__setattr__(self, "parent_fact_ids", tuple(self.parent_fact_ids))

    def child(
        self,
        *,
        stage: str,
        local_key: str = "",
        spend_delta_usd: float = 0.0,
        ontology_version: str | None = None,
    ) -> "ArtifactEnvelope":
        """Derive the envelope for the next artifact in a straight-line
        transform (this envelope's artifact is the sole parent).

        Propagates ``workspace_id`` / ``run_id`` unchanged, mints a new
        ``fact_id`` via :func:`derive_fact_id` with this envelope's ``fact_id``
        as the single parent, accumulates ``spend_usd``, and stamps a fresh
        ``observed_at``. ``ontology_version`` defaults to carrying this
        envelope's value forward; pass an explicit value when producing an A5/A6
        artifact.

        For fan-in (multiple parents), construct the child directly with
        :func:`derive_fact_id(parent_fact_ids=(...))` — this convenience method
        only covers the single-parent case.
        """
        new_fact_id = derive_fact_id(
            run_id=self.run_id,
            stage=stage,
            parent_fact_ids=(self.fact_id,),
            local_key=local_key,
        )
        return ArtifactEnvelope(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            fact_id=new_fact_id,
            parent_fact_ids=(self.fact_id,),
            spend_usd=self.spend_usd + spend_delta_usd,
            ontology_version=(
                ontology_version if ontology_version is not None else self.ontology_version
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "fact_id": self.fact_id,
            "parent_fact_ids": list(self.parent_fact_ids),
            "observed_at": self.observed_at.isoformat(),
            "spend_usd": self.spend_usd,
            "ontology_version": self.ontology_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArtifactEnvelope":
        observed_at = d.get("observed_at")
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at)
        elif not isinstance(observed_at, datetime):
            observed_at = datetime.now(timezone.utc)
        return cls(
            workspace_id=str(d.get("workspace_id", "") or ""),
            run_id=str(d.get("run_id", "") or ""),
            fact_id=str(d.get("fact_id", "") or ""),
            parent_fact_ids=tuple(d.get("parent_fact_ids") or ()),
            observed_at=observed_at,
            spend_usd=float(d.get("spend_usd", 0.0) or 0.0),
            ontology_version=d.get("ontology_version"),
        )


__all__ = [
    "FACT_ID_NAMESPACE",
    "ArtifactEnvelope",
    "derive_fact_id",
]
