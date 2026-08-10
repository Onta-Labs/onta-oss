"""A1 cell/row validators (ONTA-393) — reject nav chrome and type-invalid cells
before scraped rows become graph entities.

Discovery's **A1** stage turns scraped rows into entity MATERIAL. A positional /
loose column map (ONTA-392), or pure page chrome that a directory page carries in
its first column, can put a section title (``"About"`` / ``"About Us"``) in the
NAME column, a year (``"1971"``, ``"1969 (national)"``) in CITY, an enrolment
phrase (``"14,000 learners annually"``) in ADDRESS, or multi-campus free text with
no host in WEBSITE. Coverage chips then report a high fill % because the cells are
non-empty — fill rate is not correctness.

These are **cheap, deterministic** checks that run at the A1 boundary, BEFORE the
extract→ingest gate, so garbage never reaches the writer:

  * the NAME (key) cell is validated first — if it fails, the WHOLE ROW is dropped
    (a chrome row is not a real entity);
  * a non-key CELL that is invalid for its attribute *type* (city / website /
    address) is scrubbed on its own, leaving the rest of the (real) row intact.

Every rejection hands back a human-readable REASON so the caller can keep the Job
Trace honest about what it dropped and why.

Pure OSS: stdlib (`re`) only, no I/O, no ``from cograph.*`` — unit-testable in
isolation and importable on its own (see the boundary rules in CLAUDE.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "ROLE_NAME",
    "ROLE_CITY",
    "ROLE_WEBSITE",
    "ROLE_ADDRESS",
    "NAV_CHROME_NAMES",
    "classify_attribute",
    "validate_name",
    "validate_city",
    "validate_website",
    "validate_address",
    "validate_cell",
    "RowVerdict",
    "screen_row",
]

# Semantic roles this module knows how to validate. An attribute whose leaf maps to
# none of these is left untouched (no validator, never dropped).
ROLE_NAME = "name"
ROLE_CITY = "city"
ROLE_WEBSITE = "website"
ROLE_ADDRESS = "address"

# Any letter or digit (Unicode-aware): ``\w`` minus the underscore. Used to reject a
# key cell that is pure punctuation / whitespace.
_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)


def _norm(value: object) -> str:
    """Trim + collapse internal whitespace to a single space. ``None`` → ``""``."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


# --------------------------------------------------------------------------- #
# NAME (the key cell)
# --------------------------------------------------------------------------- #
# Nav / footer / section chrome that shows up as a first-column "name" on many
# directory and wiki pages. Matched case-insensitively against the WHOLE cell.
NAV_CHROME_NAMES = frozenset({
    "about",
    "about us",
    "contact",
    "contact us",
    "home",
    "menu",
    "search",
    "overview",
    "news",
    "events",
    "gallery",
    "references",
    "see also",
    "external links",
    "further reading",
    "navigation",
    "sitemap",
    "site map",
    "log in",
    "login",
    "sign in",
    "sign up",
    "register",
    "privacy policy",
    "terms of service",
    "terms of use",
    "cookie policy",
    "back to top",
    "read more",
    "learn more",
    "help",
    "support",
    "faq",
    "faqs",
})

# Prefix chrome — a cell that STARTS with one of these is chrome regardless of its
# tail: "Skip to content", "List of universities in British Columbia".
_NAV_CHROME_PREFIXES = (
    "skip to ",
    "list of ",
    "jump to ",
    "table of contents",
)


def validate_name(value: object) -> str | None:
    """Reason the NAME/key cell is invalid (caller drops the whole row), or ``None``.

    Rejects the nav-chrome blocklist, ``skip to…`` / ``list of…`` chrome prefixes,
    cells shorter than 2 characters, and cells with no letter or digit (pure
    punctuation)."""
    text = _norm(value)
    if len(text) < 2:
        return "name too short"
    if not _ALNUM_RE.search(text):
        return "name is punctuation-only"
    low = text.casefold()
    if low in NAV_CHROME_NAMES:
        return f"nav-chrome name {text!r}"
    for prefix in _NAV_CHROME_PREFIXES:
        if low.startswith(prefix):
            return f"nav-chrome name {text!r}"
    return None


# --------------------------------------------------------------------------- #
# Shared type signals (city / website / address)
# --------------------------------------------------------------------------- #
# A cell that STARTS with a 4-digit number is a year, not a place: "1971",
# "1969 (national)", "1975".
_YEAR_LIKE_RE = re.compile(r"^\d{4}\b")

# Enrolment / headcount prose — "14,000 learners annually", "Thousands annually",
# "full-time equivalent". Requires an enrolment-specific token so a real place like
# the town of "Hundred, WV" is not swept up by a bare magnitude word.
_ENROLMENT_RE = re.compile(
    r"\b(?:"
    r"enrol(?:l|ment|led|ling|lment|lments|ments)?"
    r"|learners?|students?|pupils?|attendees?"
    r"|full[-\s]?time\s+equivalent|fte"
    r"|head[-\s]?count"
    r"|annually|per\s+year|per\s+annum|a\s+year"
    r")\b",
    re.IGNORECASE,
)

# URL / host shape. Either an explicit scheme, or a hostname-ish token: one or more
# dot-separated labels ending in an alphabetic TLD (2-24 chars). Matches "ubc.ca",
# "www.langara.ca", "example.co.uk/admissions"; NOT "Multi-campus free text".
_URL_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9-]{1,63})*"
    r"\.[a-z]{2,24}\b",
    re.IGNORECASE,
)


def _looks_like_url(text: str) -> bool:
    return bool(_URL_SCHEME_RE.search(text) or _DOMAIN_RE.search(text))


# A common, non-exhaustive TLD set used ONLY by the stricter city URL check. The
# permissive ``_looks_like_url`` accepts any ``label.tld`` shape (right for a
# WEBSITE cell, which SHOULD be a URL), but that also matches no-space abbreviated
# place names ("St.Louis", "Mt.Royal", "Ft.Worth") whose "TLD" is just a word — a
# false positive for a CITY cell. Requiring a scheme, a ``www.``, or a real TLD
# keeps genuine URLs-as-city flagged while letting those place names through.
_KNOWN_TLD = frozenset({
    "com", "org", "net", "edu", "gov", "mil", "int", "io", "co", "us", "ca",
    "uk", "au", "nz", "ie", "de", "fr", "es", "it", "nl", "eu", "in", "cn",
    "jp", "br", "mx", "za", "info", "biz", "app", "dev", "ai", "tech", "xyz",
    "online", "site", "gov.uk", "ac.uk", "edu.au",
})
_DOMAIN_TLD_RE = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9-]{1,63})*"
    r"\.([a-z]{2,24})\b",
    re.IGNORECASE,
)
_WWW_RE = re.compile(r"\bwww\.", re.IGNORECASE)


def _looks_like_url_strict(text: str) -> bool:
    """URL detection for cells that are NOT expected to be URLs (city): an explicit
    scheme, a ``www.``, or a domain ending in a recognized TLD. Abbreviated place
    names like ``St.Louis`` — whose trailing label is an ordinary word, not a TLD —
    are NOT treated as URLs."""
    if _URL_SCHEME_RE.search(text) or _WWW_RE.search(text):
        return True
    return any(
        m.group(1).lower() in _KNOWN_TLD for m in _DOMAIN_TLD_RE.finditer(text)
    )


# Street cues — presence of any means the value is plausibly a REAL address, so an
# enrolment phrase that also carries one is kept (a real address with extra prose).
# Whole-word street-type tokens, a US ZIP, or a Canadian postal code.
_STREET_CUE_RE = re.compile(
    r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln"
    r"|way|court|ct|place|pl|square|sq|highway|hwy|parkway|pkwy|route|rte"
    r"|crescent|cres|terrace|terr|circle|cir|trail|trl"
    r"|suite|ste|floor|fl|unit|room|rm|building|bldg|apt|apartment"
    r"|po\s*box|p\.?\s*o\.?\s*box)\b"
    r"|\b\d{5}(?:-\d{4})?\b"                 # US ZIP / ZIP+4
    r"|\b[a-z]\d[a-z]\s?\d[a-z]\d\b",        # Canadian postal code (A1A 1A1)
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# CITY
# --------------------------------------------------------------------------- #
def validate_city(value: object) -> str | None:
    """Reason a CITY cell is invalid (caller scrubs the cell), or ``None``.

    Rejects year-like cells (``^\\d{4}``), URL-shaped cells, and enrolment prose —
    the residual-map-failure and chrome values the ticket calls out. Empty cells
    are not "invalid" (nothing to fill) → ``None``."""
    text = _norm(value)
    if not text:
        return None
    if _YEAR_LIKE_RE.match(text):
        return f"city looks like a year {text!r}"
    if _looks_like_url_strict(text):
        return f"city looks like a URL {text!r}"
    if _ENROLMENT_RE.search(text):
        return f"city looks like an enrolment phrase {text!r}"
    return None


# --------------------------------------------------------------------------- #
# WEBSITE
# --------------------------------------------------------------------------- #
def validate_website(value: object) -> str | None:
    """Reason a WEBSITE cell is invalid (caller scrubs the cell), or ``None``.

    Rejects anything with no URL/host shape (an explicit ``http(s)://`` scheme or a
    domain-like ``label.tld`` token). Empty cells → ``None``."""
    text = _norm(value)
    if not text:
        return None
    if _looks_like_url(text):
        return None
    return f"website has no URL/host shape {text!r}"


# --------------------------------------------------------------------------- #
# ADDRESS
# --------------------------------------------------------------------------- #
def validate_address(value: object) -> str | None:
    """Reason an ADDRESS cell is invalid (caller scrubs the cell), or ``None``.

    Rejects PURE enrolment / headcount phrases ("14,000 learners annually",
    "Thousands annually") that carry NO street cue — a value that also has a street
    cue is a real address with extra prose and is kept. Empty cells → ``None``."""
    text = _norm(value)
    if not text:
        return None
    if _ENROLMENT_RE.search(text) and not _STREET_CUE_RE.search(text):
        return f"address looks like an enrolment phrase {text!r}"
    return None


_VALIDATORS = {
    ROLE_NAME: validate_name,
    ROLE_CITY: validate_city,
    ROLE_WEBSITE: validate_website,
    ROLE_ADDRESS: validate_address,
}


def validate_cell(role: str, value: object) -> str | None:
    """Dispatch to the validator for ``role``. Unknown role → ``None`` (no check)."""
    fn = _VALIDATORS.get(role)
    return fn(value) if fn is not None else None


# --------------------------------------------------------------------------- #
# Attribute → role classification
# --------------------------------------------------------------------------- #
# Token match on the attribute leaf (split on non-alphanumerics), so "capacity"
# does NOT read as a "city" and "email_address" does NOT read as a street address.
_CITY_TOKENS = frozenset({"city", "town"})
_WEBSITE_TOKENS = frozenset({"website", "url", "homepage", "webpage"})
_ADDRESS_TOKENS = frozenset({"address", "street", "location"})
_EMAIL_TOKENS = frozenset({"email", "mail"})
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _leaf_tokens(attr: object) -> set[str]:
    return {t for t in _TOKEN_SPLIT_RE.split(str(attr or "").lower()) if t}


def classify_attribute(attr: object, key_attr: object) -> str | None:
    """Map an attribute leaf to a semantic role, or ``None`` when no validator applies.

    The key attribute is always the NAME role. Others are classified by their leaf
    tokens; an email attribute is explicitly NOT an address (so ``email_address``
    keeps its value)."""
    if attr == key_attr:
        return ROLE_NAME
    tokens = _leaf_tokens(attr)
    if not tokens or (tokens & _EMAIL_TOKENS):
        return None
    if tokens & _WEBSITE_TOKENS:
        return ROLE_WEBSITE
    if tokens & _CITY_TOKENS:
        return ROLE_CITY
    if tokens & _ADDRESS_TOKENS:
        return ROLE_ADDRESS
    return None


# --------------------------------------------------------------------------- #
# Row-level verdict
# --------------------------------------------------------------------------- #
@dataclass
class RowVerdict:
    """The screening decision for one A1 row.

    ``drop_row`` — the key/name cell is invalid; the whole row is chrome and must be
    dropped (``row_reason`` says why). When ``drop_row`` is ``False``, ``scrubbed``
    maps each invalid non-key attribute leaf to its drop reason — the caller removes
    just those cells and keeps the (real) row."""

    drop_row: bool = False
    row_reason: str = ""
    scrubbed: dict[str, str] = field(default_factory=dict)


def screen_row(
    row: object, key_attr: str, attributes: list[str]
) -> RowVerdict:
    """Validate ONE A1 row. Never mutates ``row``.

    The NAME (key) cell is checked first: if it fails, the whole row is dropped and
    no cell scrubbing is reported (the row is gone). Otherwise each confirmed
    non-key attribute whose leaf classifies to a known role is checked, and the
    invalid ones are collected into ``scrubbed``."""
    if not isinstance(row, dict):
        return RowVerdict()
    # Validate the NAME only when the key cell is actually PRESENT. Registry/API
    # pulls and LLM-projection rows may not carry the key verbatim — the name is
    # minted from other fields downstream (the extractor) — so an ABSENT/empty key
    # is "name derived elsewhere", NOT chrome, and must never drop the row. The
    # deterministic table rail already guarantees a non-empty key upstream, so its
    # chrome names ("About", "Skip to content") still hit this check.
    key_raw = row.get(key_attr)
    if key_raw is not None and _norm(key_raw) != "":
        name_reason = validate_name(key_raw)
        if name_reason is not None:
            return RowVerdict(drop_row=True, row_reason=name_reason)
    scrubbed: dict[str, str] = {}
    for attr in attributes:
        if attr == key_attr:
            continue
        role = classify_attribute(attr, key_attr)
        if role is None:
            continue
        raw = row.get(attr)
        if raw is None or _norm(raw) == "":
            continue
        reason = validate_cell(role, raw)
        if reason is not None:
            scrubbed[attr] = reason
    return RowVerdict(scrubbed=scrubbed)
