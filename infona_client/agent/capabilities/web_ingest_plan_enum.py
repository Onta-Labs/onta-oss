"""Enumeration partition + cross-batch row dedupe for discovery.

Deterministic backstop so a population inventory ask fans out instead of
collapsing to one thin page (ONTA-379). Dedupe binds ``source_url``
BEFORE dropping rows (ONTA-256).
"""
from __future__ import annotations

import re
from typing import Optional

from infona_client.agent.capabilities.web_ingest_fetch import _attach_source_urls
from infona_client.agent.capabilities.web_ingest_text import (
    _clean_query,
    _current_request,
    _dedupe,
    _slug,
)

# Hard ceiling on the enumeration fan-out — the LLM is asked for 2-6 sub-queries;
# this guards against an over-eager reply multiplying paid calls.
_MAX_SUBQUERIES = 6


# Secondary identity signals (checked in order) that distinguish same-NAME rows:
# "Starbucks" per branch, "Dr. John Smith" per city. A bare-name dedupe key would
# collapse all of them to one record (adversarial-review F1).
_DEDUPE_SIGNAL_COLS = (
    "address", "street_address", "city", "location", "phone", "phone_number",
)


def _norm_key_part(v) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip() if v else ""


def _row_key(row: dict, key_attr: str) -> str:
    """Composite dedupe key: normalized KEY attribute + the first present
    identity signal (address/city/phone). Same name + same signal → duplicate;
    same name + DIFFERENT signal → distinct records (two branches, two cities) —
    both kept, with downstream entity resolution as the deeper merge net. A row
    with no key value returns "" (never deduped)."""
    name = _norm_key_part(row.get(key_attr))
    if not name:
        return ""
    for col in _DEDUPE_SIGNAL_COLS:
        sig = _norm_key_part(row.get(col))
        if sig:
            return f"{name}|{sig}"
    return name


def _dedupe_rows(
    rows: list[dict], key_attr: str, seen: set[str]
) -> list[dict]:
    """Drop rows whose composite key (see :func:`_row_key`) was already seen
    (mutating ``seen``) — the cross-batch merge for the fan-out/ensemble. Keys
    are normalized (lowercased, punctuation collapsed) so "Dr. Alina Reyes" and
    "dr alina reyes" dedupe; rows with NO key value are kept (nothing to match
    on)."""
    out: list[dict] = []
    for r in rows:
        key = _row_key(r, key_attr)
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def _dedupe_rows_with_source_urls(
    rows: list[dict],
    key_attr: str,
    seen: set[str],
    provenance: dict[str, str],
) -> list[dict]:
    """Bind each row's per-record ``source_url`` provenance BEFORE deduping, then
    dedupe — the ORDER is the whole fix (ONTA-256).

    :func:`_dedupe_rows` drops already-seen rows, which SHIFTS every surviving
    row's positional index. The provider's ``provenance`` map is keyed by each
    row's ORIGINAL position (or name), so re-deriving a URL by position AFTER the
    drop binds a surviving row to a DROPPED neighbour's page — the citation
    mis-binds (a row shows a source URL that isn't its own). Stamping first, while
    indices are still original, and carrying the URL ON the row object itself makes
    the citation immune to the reindex: :func:`_dedupe_rows` returns the SAME row
    objects, so each survivor keeps exactly the URL that was bound to it. And
    because :func:`_attach_source_urls` never clobbers a row that already carries a
    ``source_url``, the position-based derivation is a last resort that only runs
    while indices are still faithful — never on a reindexed survivor.

    Behaviour-preserving when nothing is dropped: attach-then-dedupe and
    dedupe-then-attach are identical for an unshifted list."""
    _attach_source_urls(rows, provenance)
    return _dedupe_rows(rows, key_attr, seen)


def _norm_subqueries(v) -> list[str]:
    """Sanitize the LLM's enumeration partition: non-empty strings, stripped,
    case-insensitively deduped, capped at ``_MAX_SUBQUERIES``. Anything malformed
    (not a list, numbers, nulls) degrades to [] — single-query behavior."""
    if not isinstance(v, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in v:
        if not isinstance(item, str):
            continue
        q = item.strip()
        key = q.lower()
        if not q or key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= _MAX_SUBQUERIES:
            break
    return out


# --- enumeration intent + deterministic partition (ONTA-379) ----------------- #
#
# ONTA-192 taught the spec LLM to fan out multi-city/category asks, but a
# single-scope population inventory ("universities in British Columbia", "BC
# universities") still collapsed to ONE subquery → ONE thin source page → ~5
# rows. These helpers are the DETERMINISTIC backstop: detect inventory intent,
# synthesize 2-6 authoritative-list angles (subtype + complementary directory
# phrasings), and expand the provider ensemble so a thin Tier-0 hit cannot
# single-source the whole population. LLM-provided partitions (≥2) still win.

# STRONG completeness language — enough (with a class-shaped subject) to fan
# out even without a geographic scope. Deliberately excludes bare "list of",
# which is everyday discovery phrasing ("list of OpenRouter models") and must
# NOT alone trigger a multi-subquery partition.
_ENUM_STRONG = re.compile(
    r"\b("
    r"all|every|every\s+single|complete|entire|full\s+list|"
    r"complete\s+list|inventory|as\s+many\s+as|comprehensive"
    r")\b",
    re.IGNORECASE,
)
# Leading inventory noise stripped before building "List of …" queries.
# Includes an optional article so "a list of …" / "the complete …" both clean.
_ENUM_LEAD = re.compile(
    r"^(?:(?:the|a|an)\s+)?(?:"
    r"list\s+of|all|every|complete|full|entire|"
    r"complete\s+list\s+of|full\s+list\s+of|directory\s+of|"
    r"catalogue\s+of|catalog\s+of"
    r")\s+",
    re.IGNORECASE,
)
# ``<head> in|across|within|throughout <scope>`` — the classic population shape.
# Deliberately omits bare ``of``: "list of X" / "University of Y" are not
# geographic partitions and would false-positive every "list of …" discovery.
_POPULATION_SCOPE = re.compile(
    r"^(?P<head>.+?)\s+"
    r"(?P<prep>in|across|within|throughout)\s+"
    r"(?P<scope>.+)$",
    re.IGNORECASE,
)
# Compound category joiners inside the head: "universities and colleges".
_COMPOUND_SPLIT = re.compile(r"\s+(?:and|&|/|or)\s+", re.IGNORECASE)
# Short local scopes (city nicknames / neighborhoods) stay single-query —
# Places + one directory usually cover them. Multi-token scopes
# (``British Columbia``, ``New South Wales``) and long single-token admin
# regions (``California``) count as broad. ``Mission`` / ``Tustin`` / ``SF``
# alone stay local unless the user used strong completeness language.
_SHORT_SCOPE_MAX = 8

# HEAVY inventory classes — multi-page provincial/state populations that a
# single directory almost never returns whole. Used as a DETECTION gate so
# everyday place queries ("coffee shops in the Mission San Francisco",
# "cardiologists in Austin TX") stay single-query even when the scope is
# multi-token. Sibling expansion (below) may still widen coffee shops once an
# ask is already classified as enumeration via ``all`` / compound heads.
_HEAVY_INVENTORY = re.compile(
    r"\b("
    r"universit(?:y|ies)|colleges?|hospitals?|"
    r"schools?|polytechnics?|institutes?"
    r")\b",
    re.IGNORECASE,
)

# Lightweight inventory siblings: when the head matches a known dual-category
# population, expand both so one subtype's thin directory cannot cap the ask.
# Deliberately small + generic; unknown heads still get complementary angles.
_INVENTORY_SIBLINGS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"\buniversit(?:y|ies)\b", re.IGNORECASE),
        ("universities", "colleges", "public universities", "private universities"),
    ),
    (
        re.compile(r"\bcolleges?\b", re.IGNORECASE),
        ("colleges", "universities", "community colleges"),
    ),
    (
        re.compile(r"\bhospitals?\b", re.IGNORECASE),
        ("hospitals", "medical centers", "clinics"),
    ),
    (
        re.compile(r"\b(coffee\s+shops?|cafes?|cafés?)\b", re.IGNORECASE),
        ("coffee shops", "cafes", "bakeries"),
    ),
)


def _authoritative_list_query(q: str) -> str:
    """Prefer directory/wiki phrasing: ``List of <subject>``.

    Locate/search rank official inventory pages much higher on "List of
    universities in British Columbia" than on the bare subject, which is the
    under-collection root cause for single-query source_first runs (ONTA-379)."""
    s = (q or "").strip()
    if not s:
        return s
    if re.match(r"(?i)^list\s+of\b", s):
        return s
    body = _ENUM_LEAD.sub("", s).strip() or s
    return f"List of {body}"


def _scope_is_broad(scope: str) -> bool:
    """True when the geographic/organizational scope is large enough that a
    single page rarely holds the whole population. Multi-token scopes and long
    single tokens qualify; short nicknames (``SF``, ``NYC``) do not."""
    s = (scope or "").strip()
    if not s:
        return False
    # Drop a leading article so "the Mission" counts as one meaningful token.
    s = re.sub(r"^(?i:the|a|an)\s+", "", s).strip()
    tokens = [t for t in re.split(r"\s+", s) if t]
    if len(tokens) >= 2:
        return True
    return bool(tokens) and len(tokens[0]) >= _SHORT_SCOPE_MAX


def _split_compound_head(head: str) -> list[str]:
    """Split ``universities and colleges`` → [``universities``, ``colleges``].

    Only fires when every part is a short noun phrase (≤4 words) so prose
    ("hospitals that accept Medicaid and Medicare") is not shattered."""
    parts = [p.strip(" ,;") for p in _COMPOUND_SPLIT.split(head or "") if p.strip()]
    if len(parts) < 2:
        return []
    clean: list[str] = []
    for p in parts:
        words = [w for w in re.split(r"\s+", p) if w]
        if not words or len(words) > 4:
            return []
        clean.append(" ".join(words))
    # Dedup case-insensitively while preserving order.
    return _dedupe(clean)


def _inventory_siblings(head: str) -> list[str]:
    """Return alternate inventory labels for ``head`` (including itself when
    matched), or ``[]`` when no sibling table applies."""
    h = (head or "").strip()
    if not h:
        return []
    for pat, siblings in _INVENTORY_SIBLINGS:
        if pat.search(h):
            # Prefer the caller's own wording first, then the table.
            return _dedupe([h, *siblings])
    return []


def _is_heavy_inventory_head(head: str) -> bool:
    """True for HE / hospital-style classes whose provincial inventory no single
    page covers. Everyday place nouns (coffee shops, cardiologists, gadgets)
    return False so multi-token city scopes don't fan out by accident."""
    return bool(_HEAVY_INVENTORY.search(head or ""))


def _is_enumeration_ask(instruction: str, query: str) -> bool:
    """True when the ask is a population inventory that should fan out.

    Triggers on (any one):
    * a **compound** category head (``universities and colleges in …``),
    * **strong** completeness language (``all`` / ``every`` / ``complete``) on
      a population shape — including narrow scopes (``all coffee shops in SF``),
    * a **heavy inventory class** over a broad scope (``universities in
      British Columbia``) — HE/hospitals/schools, not everyday place finds.

    Multi-token city scopes alone are NOT enough: ``cardiologists in Austin TX``
    and ``coffee shops in the Mission San Francisco`` stay single-query (the
    P1 Find offline bar + Places path). Bare ``list of X`` catalogues
    (``list of OpenRouter models``) also stay single-query."""
    text = f"{instruction or ''}\n{query or ''}".strip()
    if not text:
        return False
    has_strong = bool(_ENUM_STRONG.search(text))
    subject = (query or "").strip() or _current_request(instruction)
    subject = _ENUM_LEAD.sub("", subject).strip() or subject
    m = _POPULATION_SCOPE.match(subject)
    if not m:
        # Also try the current request line when the cleaned query lost the scope.
        m = _POPULATION_SCOPE.match(
            _ENUM_LEAD.sub("", _current_request(instruction)).strip()
        )
    if not m:
        # No ``head prep scope`` shape. Strong completeness alone on a bare
        # catalogue name ("all OpenRouter models") is still a single source —
        # do NOT fan out without a scope or compound head to partition on.
        return False
    head = m.group("head").strip()
    scope = m.group("scope").strip()
    if _split_compound_head(head):
        return True
    if has_strong:
        return True
    # Broad scope alone is not enough (city+state is multi-token). Require a
    # heavy inventory class so BC universities fan out while Austin cardiologists
    # and Mission coffee shops do not.
    return _scope_is_broad(scope) and _is_heavy_inventory_head(head)


def _synthesize_enumeration_subqueries(query: str, instruction: str) -> list[str]:
    """Build 2-6 complementary inventory angles for a population ask.

    Prefers authoritative ``List of …`` phrasing and expands (in order):
    compound heads → inventory siblings → complementary directory angles.
    Always capped/deduped by :func:`_norm_subqueries`."""
    subject = (query or "").strip() or _clean_query(instruction)
    subject = _ENUM_LEAD.sub("", subject).strip() or subject
    if not subject:
        return []

    m = _POPULATION_SCOPE.match(subject)
    if m:
        head = _ENUM_LEAD.sub("", m.group("head")).strip()
        prep = m.group("prep")
        scope = m.group("scope").strip()
        compounds = _split_compound_head(head)
        if compounds:
            return _norm_subqueries(
                [_authoritative_list_query(f"{c} {prep} {scope}") for c in compounds]
            )
        siblings = _inventory_siblings(head)
        if siblings:
            out = [
                _authoritative_list_query(f"{sib} {prep} {scope}") for sib in siblings
            ]
            # One extra complementary angle so a 2-sibling table still reaches ≥3
            # subqueries when the acceptance fixture needs multi-source coverage.
            out.append(
                f"complete directory of {head} {prep} {scope}"
            )
            out.append(
                f"{head} {prep} {scope} accreditation or government registry"
            )
            return _norm_subqueries(out)
        return _norm_subqueries(
            [
                _authoritative_list_query(f"{head} {prep} {scope}"),
                f"complete directory of {head} {prep} {scope}",
                f"{head} {prep} {scope} accreditation or government registry",
                f"public {head} {prep} {scope}",
                f"private {head} {prep} {scope}",
            ]
        )

    # No clear ``head prep scope`` parse — still give complementary inventory
    # angles so an explicit "list of X" / "all X" ask does not collapse to one
    # thin page.
    return _norm_subqueries(
        [
            _authoritative_list_query(subject),
            f"complete directory of {subject}",
            f"{subject} official registry or accreditation list",
        ]
    )


def _ensure_enumeration_partition(
    *,
    query: str,
    instruction: str,
    llm_subqueries: list[str],
) -> list[str]:
    """Return the sub-query partition execute() should fan out over.

    * LLM already partitioned (≥2) → keep it (ONTA-192 path; do not rewrite,
      so existing multi-city plans stay byte-stable).
    * Enumeration intent + empty/singleton LLM partition → synthesize
      authoritative-list angles (ONTA-379 backstop).
    * Non-enumeration → ``[]`` (classic single-query discovery)."""
    subs = list(llm_subqueries or [])
    if len(subs) >= 2:
        return _norm_subqueries(subs)
    if not _is_enumeration_ask(instruction, query):
        return []
    synthesized = _synthesize_enumeration_subqueries(query, instruction)
    # Need a real partition (≥2). A singleton synthesis is not worth the
    # fan-out overhead — fall back to single-query.
    return synthesized if len(synthesized) >= 2 else []


def _expand_enumeration_ensemble(ensemble: list) -> list:
    """For enumeration goals, also consult nested fallback providers.

    ``source_first`` short-circuits to a thin Tier-0 page when one JSON/HTML
    directory validates — never reaching its web-search fallback. Unwrapping
    that fallback into the ensemble (specialized/primary first, fallback next)
    keeps the thin source AND the broader web harvest, with cross-batch key
    dedupe making the overlap free. Providers without a nested fallback are
    unchanged. Reads ``fallback`` (public) then ``_fallback`` (legacy private
    attr on source_first) defensively so OSS stays decoupled from the premium
    wrapper's attribute name."""
    out: list = []
    seen: set[int] = set()

    def _add(p) -> None:
        if p is None:
            return
        pid = id(p)
        if pid in seen:
            return
        seen.add(pid)
        out.append(p)

    for p in ensemble or []:
        _add(p)
        fb = getattr(p, "fallback", None)
        if fb is None:
            fb = getattr(p, "_fallback", None)
        _add(fb)
    return out or list(ensemble or [])
