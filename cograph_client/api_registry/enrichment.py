"""Registry-backed enrichment source adapter (ONTA-194, phase 3).

The parallel of the phase-2 discovery projection: where ``RegistryDiscoverySource``
adapts the declarative executor to the ``WebSourceProvider`` seam, this adapts it
to the enrichment ``SourceAdapter`` seam so a registered authoritative API can
**fill an attribute on an existing entity** — with **authority precedence**.

Given ``lookup(entity_label, attribute, context)`` it:

1. self-gates — the entry must be able to *produce* the requested ``attribute``
   (it's one of the entry's field-mapping columns) and cover the entity's type
   (``context["entity_type"]`` matches the entry's coverage); otherwise ``[]`` so
   the chain falls through to wikidata / web adapters,
2. derives request bindings **deterministically** from the entity label via each
   param's catalog-authored ``enrich_from`` recipe (no per-lookup LLM),
3. runs ``RegistryApiSource.execute`` and builds a ``Verdict`` **directly** from
   the top row (like the wikidata adapter — no LLM extraction needed for a
   structured value), with a calibrated confidence set by the entry's
   ``authority_level``.

**Authority** is expressed purely by chain ORDER + confidence (never by importing
the proprietary ``WIKIDATA_BETTER_ATTRIBUTES`` set): ``register_registry_enrichment``
registers a chain-prefix provider (``tiers.register_chain_prefix_provider``) so
the registry adapters LEAD every tier chain, and a ``source_of_truth`` entry
returns a high-confidence verdict that the executor's first-sufficient-verdict
short-circuit lets win over wikidata and every web adapter.

Boundary: OSS. Imports only ``cograph_client.*`` — no ``from cograph.*``.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..enrichment.extraction import ExtractorFn, extract_value
from ..enrichment.models import Verdict
from ..enrichment.sources.base import register_adapter
from ..enrichment.tiers import register_chain_prefix_provider
from .catalog import ApiSourceCatalog, get_api_source_catalog
from .executor import RegistryApiSource
from .spec import (
    AUTHORITY_CONFIDENCE as _AUTHORITY_CONFIDENCE,
    AUTHORITY_RANK as _AUTHORITY_RANK,
    ENRICH_FROM_ATTRIBUTE_PREFIX,
    ApiSourceSpec,
    AuthorityLevel,
    EndpointSpec,
)

logger = logging.getLogger(__name__)

# Authority ranking + calibrated confidences now live canonically on ``spec.py``
# next to ``AuthorityLevel`` (ONE scale, shared with the write-time conflict
# policy — see spec.AUTHORITY_RANK / AUTHORITY_CONFIDENCE). Aliased here under
# the original names so the chain-lead sort + confidence calibration below are
# byte-identical: source_of_truth (rank 0, conf 0.95) leads authoritative
# (1, 0.85) leads supplementary (2, 0.6); the first two clear the default
# confidence bar, supplementary only augments a gap.
_MAX_ROWS = 5
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Word boundaries INSIDE a camelCase / PascalCase identifier: a lower/digit→upper
# transition ("LineItem" → "Line|Item"), and an acronym→word transition
# ("BLSItem" → "BLS|Item"). Split on these BEFORE lowercasing so a PascalCase
# ontology type name tokenizes to the same words a snake_case coverage kind does
# — otherwise "LineItem" collapses to the single token {"lineitem"}, never
# overlaps "line_item"/"food_item"/…, and the registry source is silently skipped
# for exactly the multi-word type names auto-ontology tends to mint.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# Generic tokens that must not, ALONE, make an entity type match an entry's
# coverage — otherwise a bare "Organization" would match "health_organization"
# on the shared "organization" token and fire a spurious API call.
_GENERIC_TYPE_TOKENS = frozenset({
    "organization", "org", "provider", "company", "business", "entity",
    "person", "record", "item", "thing", "group", "service",
})


# Candidate-select (ONTA-360): cap on the prompt text sent to the LLM selector
# (instruction + numbered candidate lines) — keeps cost bounded and avoids
# token-limit truncation mid-candidate.
_CANDIDATE_TEXT_BUDGET = 8000
# A token carrying a digit or "/" is pack-size / count / unit noise for a
# name-search query ("Ground beef 80/20" → "80/20"): the relaxation ladder
# drops such tokens before trying broader word-drop relaxations.
_NOISY_QUERY_TOKEN_RE = re.compile(r"[\d/]")


def _relax_ladder(label: str) -> list[str]:
    """Progressive query-relaxation candidates for an ``enrich_from: entity_name``
    search param (ONTA-360), broadest-preserving first:

    (a) the original label;
    (b) the label with digit-/slash-bearing tokens removed
        ("Ground beef 80/20" → "Ground beef");
    (c) the (cleaned) label minus its FIRST word when it has >= 2 words
        ("Roma tomatoes" → "tomatoes") — the leading word is usually a
        variety/brand qualifier a source's search index may not know;
    (d) the last word alone.

    Steps (c)/(d) operate on the digit-cleaned token list (falling back to the
    original tokens when cleaning removed everything) — dropping qualifier words
    only helps once the numeric noise is already gone. Deduplicated, order
    preserved; the caller stops at the first query that yields candidates.
    """
    original = " ".join((label or "").split())
    if not original:
        return []
    tokens = original.split()
    clean_tokens = [t for t in tokens if not _NOISY_QUERY_TOKEN_RE.search(t)]
    base_tokens = clean_tokens or tokens
    ladder = [original, " ".join(clean_tokens)]
    if len(base_tokens) >= 2:
        ladder.append(" ".join(base_tokens[1:]))
    ladder.append(base_tokens[-1] if base_tokens else "")
    out: list[str] = []
    for q in ladder:
        q = q.strip()
        if q and q not in out:
            out.append(q)
    return out


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _tokens(s: str) -> set[str]:
    # Insert spaces at camelCase/PascalCase word boundaries first, so both a
    # PascalCase entity type ("LineItem") and a snake_case coverage kind
    # ("line_item") reduce to the same word set {"line", "item"}.
    split = _CAMEL_BOUNDARY_RE.sub(" ", s or "")
    return set(_TOKEN_RE.findall(split.lower()))


def _has_enrich_params(spec: ApiSourceSpec) -> bool:
    return any(p.enrich_from for ep in spec.endpoints for p in ep.params)


class RegistrySourceAdapter:
    """A ``SourceAdapter`` backed by one declarative catalog entry."""

    def __init__(
        self,
        spec: ApiSourceSpec,
        *,
        executor: Optional[RegistryApiSource] = None,
        extractor: Optional[ExtractorFn] = None,
    ) -> None:
        self._spec = spec
        self._executor = executor or RegistryApiSource()
        # Candidate-select LLM seam (ONTA-360): None ⇒ extract_value falls back
        # to get_default_extractor() (the OSS OpenRouter extractor when a key is
        # configured, the deterministic offline one otherwise). Tests inject a
        # fake here so no network / real LLM is ever needed.
        self._extractor = extractor
        self.name = f"api:{spec.slug}"
        self.is_paid = spec.is_paid
        self.cost_per_call = spec.cost_per_call
        # normalized(field-mapping column) -> canonical column name, across endpoints
        self._columns: dict[str, str] = {}
        for ep in spec.endpoints:
            for col in ep.field_mappings:
                self._columns.setdefault(_norm(col), col)

    @property
    def authority_level(self) -> AuthorityLevel:
        return self._spec.authority_level

    @property
    def binding_source_attributes(self) -> frozenset[str]:
        """Attribute leaves this adapter binds a request param FROM (the
        `attribute:<attr>` enrich_from recipe) — the executor pre-loads these
        onto the entity so lookup() can read them from context."""
        out = set()
        for ep in self._spec.endpoints:
            for p in ep.params:
                ef = p.enrich_from or ""
                if ef.startswith(ENRICH_FROM_ATTRIBUTE_PREFIX):
                    leaf = ef[len(ENRICH_FROM_ATTRIBUTE_PREFIX):]
                    if leaf:
                        out.add(leaf)
        return frozenset(out)

    def _confidence(self) -> float:
        return _AUTHORITY_CONFIDENCE.get(self._spec.authority_level, 0.6)

    def _fillable_column(self, attribute: str) -> Optional[str]:
        return self._columns.get(_norm(attribute))

    def _type_matches(self, entity_type: str) -> bool:
        # Missing type -> don't over-exclude (ONTA-191): rely on the attribute +
        # binding gates. Present type -> require a token overlap with coverage on
        # a NON-generic token, so a bare "Organization" doesn't match
        # "health_organization" (and fire a spurious API call) on the shared
        # generic "organization" token alone.
        if not entity_type:
            return True
        want = _tokens(entity_type)
        if not want:
            return True
        if not (want - _GENERIC_TYPE_TOKENS):
            # The type name is ENTIRELY generic ("Item", "Organization" — the
            # shapes auto-ontology mints when a dataset has no domain
            # vocabulary). It cannot supply a distinguishing token, so a
            # partial overlap with a multi-word kind ("health_organization")
            # stays a NON-match — the guard's whole point. It matches only a
            # coverage kind declared at the same generic level (every token of
            # the kind present in the type name, e.g. kind "item" for type
            # "Item"): an explicit author opt-in to serve generic types.
            return any(
                kt and kt <= want
                for kt in (_tokens(k) for k in self._spec.coverage.entity_kinds)
            )
        for kind in self._spec.coverage.entity_kinds:
            overlap = _tokens(kind) & want
            if overlap and (overlap - _GENERIC_TYPE_TOKENS):
                return True
        return False

    def _build_bindings(
        self, ep: EndpointSpec, entity_label: str, entity_attrs: dict,
    ) -> dict[str, str]:
        # Deterministic, no-LLM derivation. Naive token split: a label carrying a
        # title/suffix ("Dr. Jane Smith MD") yields imperfect first/last tokens;
        # it degrades gracefully (the API returns nothing → the chain falls
        # through), and richer parsing is a tracked follow-up.
        # ``attribute:<attr>`` binds from another attribute already resolved on
        # the entity (``entity_attrs``, keyed by attribute leaf name) — e.g. a
        # ``bls_series_id`` resolved by a prior enrichment step feeding a price
        # lookup. Missing/empty attr ⇒ no binding ⇒ the lookup no-ops (falls
        # through), same graceful-degrade contract as the label recipes.
        label = (entity_label or "").strip()
        parts = label.split()
        attrs = entity_attrs or {}
        bindings: dict[str, str] = {}
        for p in ep.params:
            ef = p.enrich_from
            if not ef:
                continue
            if ef == "entity_name":
                val = label
            elif ef == "entity_name_first":
                val = parts[0] if parts else ""
            elif ef == "entity_name_last":
                val = parts[-1] if parts else ""
            elif ef.startswith(ENRICH_FROM_ATTRIBUTE_PREFIX):
                attr = ef[len(ENRICH_FROM_ATTRIBUTE_PREFIX):]
                val = str(attrs.get(attr, "") or "").strip()
            else:
                val = ""
            if val:
                bindings[p.name] = val
        return bindings

    def _source_url(self, res) -> Optional[str]:
        if res.sources:
            return res.sources[0]
        if res.provenance:
            return next(iter(res.provenance.values()), None)
        return None

    def _secret_resolver(self, context: dict):
        """A per-tenant secret resolver iff this entry uses a secret_ref AND the
        lookup context carries a tenant_id; else ``None`` (env-var auth needs no
        resolver). The tenant flows in via the enrichment executor's ctx, so a
        tenant_custom adapter decrypts only THIS tenant's secret for THIS source."""
        if not self._spec.auth.secret_ref:
            return None
        tenant_id = (context or {}).get("tenant_id") or ""
        if not tenant_id:
            return None
        from .secret_store import make_secret_resolver

        return make_secret_resolver(tenant_id, self._spec.slug)

    async def lookup(self, entity_label: str, attribute: str, context: dict) -> list[Verdict]:
        try:
            entity_type = (context or {}).get("entity_type") or ""
            col = self._fillable_column(attribute)
            if col is None or not self._type_matches(entity_type):
                return []  # this entry can't answer -> fall through to the next adapter
            ep = self._spec.endpoint()
            if ep is None:
                return []
            entity_attrs = (context or {}).get("entity_attributes") or {}
            bindings = self._build_bindings(ep, entity_label, entity_attrs)
            if not bindings:
                return []  # not enrichment-configured (no enrich_from) or empty binding source
            cs = ep.candidate_select or {}
            if cs and str(cs.get("mode", "")).strip().lower() == "llm":
                # ONTA-360: many-candidate fetch + LLM selection (+ optional
                # query relaxation). The default single-row path below is
                # untouched for every entry without a candidate_select recipe.
                return await self._lookup_candidate_select(
                    ep, entity_label, attribute, col, bindings, context or {},
                )
            res = await self._executor.execute(
                self._spec, bindings, endpoint_name=ep.name, max_rows=_MAX_ROWS,
                sample=True, secret_resolver=self._secret_resolver(context),
            )
            if res.dormant or res.error or not res.rows:
                return []
            for row in res.rows:
                value = row.get(col)
                if value:
                    return [Verdict(
                        value=str(value),
                        confidence=self._confidence(),
                        source=self.name,
                        source_url=self._source_url(res),
                    )]
            return []
        except Exception:  # noqa: BLE001 - an adapter must never break the chain
            logger.debug(
                "api_registry enrichment lookup failed slug=%s attr=%s",
                self._spec.slug, attribute, exc_info=True,
            )
            return []

    async def _lookup_candidate_select(
        self,
        ep: EndpointSpec,
        entity_label: str,
        attribute: str,
        col: str,
        bindings: dict[str, str],
        context: dict,
    ) -> list[Verdict]:
        """Many-candidate fetch + LLM record selection (ONTA-360).

        Instead of taking the first row with a value, fetch up to
        ``max_candidates`` rows, format them as numbered lines, and ask the OSS
        LLM extraction seam (:func:`extract_value`) to pick THE record for this
        entity. Anti-hallucination gate: the returned value must EXACTLY equal
        one of the fetched candidates' ``col`` values, otherwise no verdict.
        With ``query_relax``, the :func:`_relax_ladder` for the
        ``enrich_from: entity_name`` param is walked until a rung yields an
        ACCEPTED selection — a rung whose fetch returns zero rows AND a rung
        whose candidates the selector refuses (or answers off-list) both relax
        further, because a too-specific query can return only wrong-kind records
        (e.g. an index series for the item) that a broader query fixes.
        """
        cs = ep.candidate_select
        try:
            max_candidates = int(cs.get("max_candidates", 20))
        except (TypeError, ValueError):
            max_candidates = 20
        max_candidates = max(1, max_candidates)

        # Only a param bound from the whole entity label is relaxable.
        name_params = [p.name for p in ep.params if p.enrich_from == "entity_name"]
        queries: list[str] = [""]  # sentinel: use the bindings exactly as built
        if cs.get("query_relax") and name_params:
            queries = _relax_ladder(entity_label) or [""]

        for q in queries:
            attempt = dict(bindings)
            if q:
                for pn in name_params:
                    attempt[pn] = q
            res = await self._executor.execute(
                self._spec, attempt, endpoint_name=ep.name, max_rows=max_candidates,
                sample=True, secret_resolver=self._secret_resolver(context),
            )
            if res.dormant or res.error:
                return []
            if not res.rows:
                continue
            verdicts = await self._select_from_candidates(
                cs, res, entity_label, attribute, col,
            )
            if verdicts:
                return verdicts
            # No accepted selection on this rung: relax further. (A single
            # iteration when relaxation is disabled.)
        return []

    async def _select_from_candidates(
        self, cs: dict, res, entity_label: str, attribute: str, col: str,
    ) -> list[Verdict]:
        """LLM-select one record out of ``res.rows``; [] when nothing qualifies."""
        # Numbered candidate lines from the recipe's display fields (the
        # fillable column is always included so the selector can quote it).
        fields = [f for f in (cs.get("fields") or []) if isinstance(f, str) and f]
        if col not in fields:
            fields = [col, *fields]

        source_title = self._spec.title or self._spec.slug
        # The selection criterion comes from the RECIPE (catalog data), never
        # hardcoded here — this adapter is generic; only the entry knows what
        # "the right record" means for its API (e.g. FRED: the national/U.S.
        # city average series). The default is a source-neutral best-match ask.
        criterion = str(cs.get("instruction") or "").strip() or (
            "Pick the single record that clearly refers to this exact entity"
        )
        header = (
            f"These are candidate records from {source_title} for the entity "
            f'"{entity_label}". {criterion}; return its '
            f"{attribute} value copied exactly from that record; return null if "
            f"none clearly matches.\n\nCandidates:\n"
        )
        # The candidate lines and the anti-hallucination set are built TOGETHER
        # from the rows that fit the prompt budget, so the gate can never accept
        # a value the selector was not actually shown (a budget-truncated row).
        # ``allowed`` maps casefolded value -> canonical candidate value: a
        # case-normalized echo of a real candidate is canonicalized, not
        # rejected (the write always uses the API's own spelling).
        budget = _CANDIDATE_TEXT_BUDGET - len(header)
        kept: list[str] = []
        allowed: dict[str, str] = {}
        used = 0
        for row in res.rows:
            value = str(row.get(col, "") or "").strip()
            if not value:
                continue
            parts = [
                f"{f}={str(row.get(f)).strip()}"
                for f in fields
                if str(row.get(f, "") or "").strip()
            ]
            if not parts:
                continue
            line = f"{len(kept) + 1}. " + " · ".join(parts)
            if used + len(line) + 1 > budget:
                break
            kept.append(line)
            used += len(line) + 1
            allowed.setdefault(value.casefold(), value)
        if not kept:
            return []
        text = header + "\n".join(kept)

        verdict = await extract_value(
            text, attribute, entity_label,
            source=self.name, extractor=self._extractor,
        )
        if verdict is None:
            return []
        value = allowed.get((verdict.value or "").strip().casefold())
        if value is None:
            logger.debug(
                "api_registry candidate-select rejected non-candidate value "
                "slug=%s attr=%s", self._spec.slug, attribute,
            )
            return []
        # The anti-hallucination gate — not the selector's self-report — is the
        # trust anchor: a gate-verified selection is calibrated by the ENTRY's
        # authority level, exactly like the default first-row path. (The
        # single-pass extraction calibration ceiling is 0.8, strictly below the
        # 0.85 default confidence bar — echoing it would mean this rail silently
        # writes nothing on every surface that keeps the default.)
        return [verdict.model_copy(update={
            "value": value,
            "confidence": self._confidence(),
            "source": self.name,
            "source_url": self._source_url(res),
        })]


# --------------------------------------------------------------------------- #
# Registration + authority chain-prefix
# --------------------------------------------------------------------------- #
_registry_lead_names: list[str] = []


def _registry_prefix_provider(_tier) -> list[str]:
    return list(_registry_lead_names)


def register_registry_enrichment(
    catalog: Optional[ApiSourceCatalog] = None,
    *,
    executor: Optional[RegistryApiSource] = None,
) -> list[str]:
    """Register a ``RegistrySourceAdapter`` per enrichment-ready catalog entry and
    make them LEAD every tier chain (source-of-truth first). Idempotent — safe to
    call again to refresh after the catalog/overlay changes. Returns the ordered
    adapter names.
    """
    global _registry_lead_names
    cat = catalog or get_api_source_catalog()
    shared = executor or RegistryApiSource()
    specs = [s for s in cat.enabled() if s.endpoints and _has_enrich_params(s)]
    specs.sort(key=lambda s: (_AUTHORITY_RANK.get(s.authority_level, 9), s.slug))
    names: list[str] = []
    for spec in specs:
        register_adapter(RegistrySourceAdapter(spec, executor=shared))
        names.append(f"api:{spec.slug}")
    _registry_lead_names = names
    register_chain_prefix_provider(_registry_prefix_provider)
    logger.info("api_registry: enrichment adapters registered: %s", names)
    return names


def reset_registry_enrichment() -> None:
    """Clear the registry lead-names (tests). The chain-prefix provider stays
    registered but returns an empty list, so it's a no-op."""
    global _registry_lead_names
    _registry_lead_names = []


__all__ = [
    "RegistrySourceAdapter",
    "register_registry_enrichment",
    "reset_registry_enrichment",
]
