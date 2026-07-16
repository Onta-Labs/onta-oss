"""``SourceBundle`` — the **A1 Source Bundle** artifact materialized at the
Find→Extract boundary (ONTA-346).

Discovery ("Find") historically fused *find + extract + write* into one
``web_ingest`` call: rows streamed straight from ``discover()`` → dedupe →
``resolver.ingest`` with no artifact in between. The pipeline decomposition
(P0-P9, ADR 0011) names the first inter-stage artifact **A1 Source Bundle**: the
deduped evidence a run FOUND, with its provenance and lineage, captured BEFORE
anything extracts or writes it. This module is that artifact's executable form.

**A1 is a PRE-write artifact.** It carries evidence *toward* the extractor/writer
— it never writes a KG itself. The actual write stays on the already-converged
write path (``resolver.ingest`` / ``resolver.ingest_structured_rows`` →
``insert_facts``); materializing A1 inserts a boundary, it does not fork the
writer.

What A1 carries (per ADR 0011 + ONTA-346):

* the :class:`~cograph_client.pipeline.envelope.ArtifactEnvelope` — ``workspace_id``
  / ``run_id`` + the fact-id lineage every A1-A10 artifact threads;
* the post-dedupe rows the run found, each as a :class:`SourceRow` with its own
  derived ``fact_id`` (child of the bundle root), the ``source_url`` citation the
  row was drawn from, the source **tier** it came from (registry Tier -1
  = ``authoritative`` vs open ``web``), and the producing provider's name;
* ``secret_refs`` — **logical secret REFERENCES ONLY**. Structured API sources
  decrypt a ``secret_ref`` at FETCH time, inside the executor's fetch layer;
  plaintext credentials never leave that layer. The bundle therefore carries only
  the reference NAME (the ``tenant_secret`` logical key), never a resolved/
  decrypted value — the constructor VALIDATES this invariant (see
  :class:`SourceBundle`).

Boundary: OSS. Imports only stdlib + ``cograph_client.pipeline.envelope``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from cograph_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id

# The pipeline stage tag for this artifact (A1). Threaded into every
# ``derive_fact_id`` call so a fact_id minted here is unambiguously an A1 id.
SOURCE_BUNDLE_STAGE = "A1"

# Source tiers a row can carry. The Find rail consults two kinds of source: the
# API source REGISTRY (Tier -1 — authoritative source-of-truth, consulted before
# web search) and open WEB discovery. A bundle row records which one produced it
# so downstream stages (extract/verify/place) can rank evidence by authority
# WITHOUT re-deriving it. Kept deliberately small; the registry authority axis
# itself lives in ``api_registry.spec.AuthorityLevel`` and is NOT re-forked here.
TIER_AUTHORITATIVE = "authoritative"  # registry source-of-truth (Tier -1)
TIER_WEB = "web"                      # open-web discovery

KNOWN_TIERS: frozenset[str] = frozenset({TIER_AUTHORITATIVE, TIER_WEB})

# The row attribute carrying a record's per-record source-URL citation — the same
# ``source_url`` key discovery stamps on each row before extraction
# (``web_ingest_cap.SOURCE_URL_ATTR``). Named here so the builder reads it without
# importing the capability (which imports this module).
SOURCE_URL_ATTR = "source_url"

# A LOGICAL secret reference — the ``tenant_secret`` name, NOT a secret value.
# Mirrors ``api_registry.spec._SECRET_REF_RE`` (the pattern that module enforces
# when an entry declares ``auth.secret_ref``). A resolved/decrypted credential —
# uppercase, ``+`` / ``/`` / ``=`` / ``.`` / ``-``, whitespace, an ``sk-…`` prefix,
# or simply longer than 64 chars — CANNOT match this shape, so validating each
# ``secret_ref`` against it turns "the bundle carries references only" from a
# convention into a checked invariant.
_SECRET_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


def is_secret_ref(value: Any) -> bool:
    """True iff ``value`` is a well-formed LOGICAL secret reference (not a resolved
    credential). Used by :class:`SourceBundle` to enforce the secret_refs-only
    invariant."""
    return isinstance(value, str) and bool(_SECRET_REF_RE.match(value))


@dataclass(frozen=True)
class SourceRow:
    """One post-dedupe discovered row + its Find→Extract provenance.

    ``data`` is a snapshot COPY of the row the provider returned (so the artifact
    is immutable evidence, decoupled from the mutable batch the writer consumes).
    ``fact_id`` is this row's stable lineage id — a child of the bundle root, so a
    single fact is traceable from A1 through every later stage. ``source_url`` is
    the citation the row was drawn from (``None`` when the provider supplied no
    provenance — a free/stub source). ``tier`` is the source authority
    (:data:`TIER_AUTHORITATIVE` for a registry Tier -1 source, :data:`TIER_WEB`
    for open web). ``provider`` names the source that produced it.
    """

    fact_id: str
    data: Mapping[str, Any]
    source_url: str | None
    tier: str
    provider: str

    def __post_init__(self) -> None:
        if not self.fact_id:
            raise ValueError("SourceRow.fact_id is mandatory")
        if self.tier not in KNOWN_TIERS:
            raise ValueError(
                f"SourceRow.tier {self.tier!r} is not a known tier "
                f"(expected one of {sorted(KNOWN_TIERS)})"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "data": dict(self.data),
            "source_url": self.source_url,
            "tier": self.tier,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class SourceBundle:
    """The A1 Source Bundle — deduped evidence + lineage + provenance, PRE-write.

    ``envelope`` is the run/lineage metadata (``workspace_id`` / ``run_id`` + the
    bundle's root ``fact_id``); each :class:`SourceRow` in ``rows`` derives its own
    ``fact_id`` as a child of that root. ``secret_refs`` are the LOGICAL secret
    references the producing source(s) used — **never** a resolved/decrypted
    credential.

    INVARIANT (validated in ``__post_init__``): ``secret_refs`` holds references
    ONLY. The bundle has no field that can hold a plaintext credential, and every
    ``secret_refs`` entry MUST match the logical-reference shape
    (:func:`is_secret_ref`) — a resolved credential fails that check and raises, so
    a decrypted secret can never be smuggled through this artifact.
    """

    envelope: ArtifactEnvelope
    rows: tuple[SourceRow, ...] = ()
    secret_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, ArtifactEnvelope):
            raise TypeError("SourceBundle.envelope must be an ArtifactEnvelope")
        if not isinstance(self.rows, tuple):
            object.__setattr__(self, "rows", tuple(self.rows))
        for row in self.rows:
            if not isinstance(row, SourceRow):
                raise TypeError("SourceBundle.rows must contain SourceRow instances")
        # secret_refs-only invariant: dedupe (order-preserving) and validate each
        # is a logical reference, NEVER a resolved credential.
        refs = tuple(dict.fromkeys(self.secret_refs))
        for ref in refs:
            if not is_secret_ref(ref):
                raise ValueError(
                    f"SourceBundle.secret_refs entry {ref!r} is not a logical secret "
                    "reference — the bundle carries secret_refs ONLY, never a "
                    "resolved/decrypted credential"
                )
        if refs != self.secret_refs:
            object.__setattr__(self, "secret_refs", refs)

    # --- convenience views (read-only) --------------------------------------- #
    @property
    def workspace_id(self) -> str:
        return self.envelope.workspace_id

    @property
    def run_id(self) -> str:
        return self.envelope.run_id

    @property
    def fact_ids(self) -> tuple[str, ...]:
        """Per-row lineage ids, in row order."""
        return tuple(r.fact_id for r in self.rows)

    @property
    def tiers(self) -> frozenset[str]:
        """The distinct source tiers represented in this bundle."""
        return frozenset(r.tier for r in self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope.to_dict(),
            "rows": [r.to_dict() for r in self.rows],
            "secret_refs": list(self.secret_refs),
        }


def _row_local_key(
    data: Mapping[str, Any],
    index: int,
    key_attribute: str,
    source_url_attr: str,
    bundle_key: str,
) -> str:
    """A stable, per-row disambiguator for :func:`derive_fact_id`.

    Folds the bundle key + positional index (guaranteeing uniqueness even if two
    rows share a natural key) with the row's key-attribute value and source URL
    when present — so a replayed run mints the SAME fact_id for the SAME row.
    ``derive_fact_id`` hashes ``local_key`` as ONE component, so embedded ``|``
    separators are safe.
    """
    parts = [bundle_key, str(index)]
    if key_attribute:
        val = data.get(key_attribute)
        if val not in (None, ""):
            parts.append(str(val))
    url = data.get(source_url_attr)
    if url:
        parts.append(str(url))
    return "|".join(parts)


def build_source_bundle(
    rows: Sequence[Mapping[str, Any]],
    *,
    workspace_id: str,
    run_id: str,
    provider: str,
    tier: str,
    secret_refs: Sequence[str] = (),
    key_attribute: str = "",
    source_url_attr: str = SOURCE_URL_ATTR,
    bundle_key: str = "",
    spend_usd: float = 0.0,
) -> SourceBundle:
    """Assemble an A1 :class:`SourceBundle` from one batch of post-dedupe rows.

    The single builder discovery calls at the Find→Extract boundary. It mints the
    bundle's root :class:`ArtifactEnvelope` from ``workspace_id`` + ``run_id``
    (A1 is a ROOT artifact — no parent lineage), then derives one child
    ``fact_id`` per row so every found fact is individually traceable. ``rows`` are
    snapshot-copied into the bundle, so the artifact is decoupled from the mutable
    batch the writer goes on to consume (the write stays byte-identical).

    ``secret_refs`` are LOGICAL references only — pass a source's ``secret_ref``
    name, never a resolved credential; the constructor rejects anything that
    isn't a well-formed reference. ``bundle_key`` disambiguates bundles from the
    same run (e.g. ``"{provider}:{sub_query}"``); it defaults to ``provider``.
    """
    root_local_key = bundle_key or provider
    root_fact_id = derive_fact_id(
        run_id=run_id,
        stage=SOURCE_BUNDLE_STAGE,
        parent_fact_ids=(),
        local_key=root_local_key,
    )
    envelope = ArtifactEnvelope(
        workspace_id=workspace_id,
        run_id=run_id,
        fact_id=root_fact_id,
        spend_usd=spend_usd,
    )
    source_rows: list[SourceRow] = []
    for i, row in enumerate(rows):
        data: dict[str, Any] = dict(row) if isinstance(row, Mapping) else {"value": row}
        local_key = _row_local_key(
            data, i, key_attribute, source_url_attr, root_local_key
        )
        fact_id = derive_fact_id(
            run_id=run_id,
            stage=SOURCE_BUNDLE_STAGE,
            parent_fact_ids=(root_fact_id,),
            local_key=local_key,
        )
        url = data.get(source_url_attr)
        source_rows.append(
            SourceRow(
                fact_id=fact_id,
                data=data,
                source_url=str(url) if url else None,
                tier=tier,
                provider=provider,
            )
        )
    return SourceBundle(
        envelope=envelope,
        rows=tuple(source_rows),
        secret_refs=tuple(secret_refs),
    )


__all__ = [
    "SOURCE_BUNDLE_STAGE",
    "SOURCE_URL_ATTR",
    "TIER_AUTHORITATIVE",
    "TIER_WEB",
    "KNOWN_TIERS",
    "SourceRow",
    "SourceBundle",
    "build_source_bundle",
    "is_secret_ref",
]
