"""Verdict provenance companions for enrichment writes."""

from __future__ import annotations

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.enrichment.executor_const import REFRESH_AUTHORITY
from infona_client.enrichment.executor_helpers import (
    _attr_uri,
    _canonical_provenance_enabled,
    _now,
)
from infona_client.graph.provenance import (
    build_attribute_provenance_companions,
    build_provenance_triples,
)


class EnrichmentProvenanceMixin:
    """Display companions + canonical provenance-graph triples."""

    @staticmethod
    def _verdict_prov_string(verdict) -> str:
        """The short human citation for a verdict's `<attr>_provenance` companion:
        the source name, with the reasoning appended when present."""
        prov = (getattr(verdict, "source", None) or "")
        if getattr(verdict, "reasoning", None):
            prov = f"{prov} ({verdict.reasoning})" if prov else verdict.reasoning
        return prov

    @staticmethod
    def _verdict_as_of(verdict):
        """The as-of date to stamp for a verdict — the SOURCE's real date, not the
        write time (ONTA-245 F1). Prefer ``source_published_at`` (when the page
        stated the fact), else ``retrieved_at`` (when we fetched it), else now-UTC.
        A paid adapter that carries neither degrades to now, unchanged from before."""
        return (
            getattr(verdict, "source_published_at", None)
            or getattr(verdict, "retrieved_at", None)
            or _now()
        )

    @classmethod
    def _provenance_triples(
        cls, entity_uri: str, type_name: str, attribute: str, verdict
    ) -> list[tuple[str, str, str]]:
        """Persist where + when an enriched value came from, as queryable DISPLAY
        companions (`<attr>_source_url`, `<attr>_provenance`, `<attr>_verified_at`)
        on the entity — so the citation is visible through /ask and the Explorer,
        not just in the adapter. Audit-friendly: every enriched fact carries its
        source AND a per-fact freshness stamp.

        Built via the SHARED ``build_attribute_provenance_companions``
        (graph/provenance.py) so discovery mints the identical companion shape for
        the same fact (ONTA-245 cross-rail symmetry). `<attr>_verified_at` is the
        per-fact freshness marker (unlike the per-ENTITY `onto/ingested_at`); it is
        dated from the VERDICT's real source date (``source_published_at`` /
        ``retrieved_at``, ONTA-245 F1), NOT the write time, and is written TYPED
        ``xsd:dateTime`` so the NL planner's ``NOW()``-relative FILTER matches it
        (an untyped string would be type-incompatible → silently dropped, ONTA-247).
        Full price/value history is out of scope here (a separate deferred ticket)."""
        return build_attribute_provenance_companions(
            entity_uri,
            type_name,
            attribute,
            source_url=getattr(verdict, "source_url", None) or "",
            provenance=cls._verdict_prov_string(verdict),
            verified_at=cls._verdict_as_of(verdict),
        )

    def _canonical_provenance_triples(
        self, rows_or_decisions, type_name: str
    ) -> list[tuple[str, str, str]]:
        """Canonical companion-provenance-GRAPH triples for the applied facts
        (ONTA-245 F1) — the governance/undo substrate, keyed ``sha1(s|p|o|source)``
        with ``prov:confidence`` + a real ``prov:timestamp``, flowed through the
        shared ``insert_facts(..., provenance_triples=…)`` seam (NOT a bespoke
        writer). One record per applied (entity, attribute) fact, dated from the
        verdict's real source date so re-reading provenance shows WHEN the source
        knew the fact, not when we wrote it.

        Gated by ``INFONA_PROVENANCE_ENABLED`` (the SAME env the ingest path uses),
        so the heavier substrate only accrues when governance/undo is switched on;
        the always-on per-attribute display companions above are unaffected.

        Accepts either ``RowResult`` rows (auto-apply path) or ``ConflictReview``
        decisions (review-accept path) — both expose ``entity_uri`` + ``attribute``
        and a verdict (``.verdict`` / ``.proposed``)."""
        if not _canonical_provenance_enabled():
            return []
        out: list[tuple[str, str, str]] = []
        for item in rows_or_decisions:
            verdict = getattr(item, "verdict", None) or getattr(item, "proposed", None)
            if verdict is None or not getattr(verdict, "value", None):
                continue
            source = getattr(verdict, "source", None) or ""
            if not source:
                continue
            out.extend(
                build_provenance_triples(
                    item.entity_uri,
                    _attr_uri(type_name, item.attribute),
                    verdict.value,
                    source=source,
                    confidence=float(getattr(verdict, "confidence", 1.0) or 1.0),
                    timestamp=self._verdict_as_of(verdict),
                )
            )
        return out

    @staticmethod
    def _verdict_authority(verdict) -> AuthorityLevel:
        """The source-authority level to stamp a refreshed value with (ONTA-279).

        A registry-backed / premium adapter MAY carry an explicit
        ``AuthorityLevel`` value string on the verdict (``verdict.authority``); when
        present and valid it is threaded through verbatim so a curated
        ``source_of_truth`` API outranks a weaker web scrape at the write-time
        conflict point. Otherwise a plain machine scrape defaults to
        :data:`REFRESH_AUTHORITY` — strong but never the top ``user_assertion`` slot
        (that is the human-correction path's alone).

        The ``user_assertion`` level is CLAMPED OUT unconditionally: a machine scrape
        must never be stamped as a human correction, even if an adapter/verdict
        carries ``authority="user_assertion"`` (a bug or a spoofed source). Such a
        verdict is downgraded to :data:`REFRESH_AUTHORITY`, so it can never tie or
        beat a real user fix at the arbitration point (that would let a refresh
        clobber the very correction it is supposed to preserve)."""
        raw = getattr(verdict, "authority", None)
        if raw:
            try:
                level = AuthorityLevel(raw)
            except ValueError:
                level = None
            if level is not None:
                # Never let a machine verdict claim the human-correction slot.
                if level == AuthorityLevel.user_assertion:
                    return REFRESH_AUTHORITY
                return level
        return REFRESH_AUTHORITY
