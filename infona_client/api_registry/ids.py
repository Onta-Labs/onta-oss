"""Well-known external identifier formats for registry enrichment bindings.

Demo / ingest pipelines sometimes mint placeholder IDs (e.g. ``NO-TRIAL-R053``
for ClinicalTrials.gov). Binding those into an authoritative API call wastes a
round-trip and surfaces as a confusing ``no_match``. Helpers here let
``RegistrySourceAdapter._build_bindings`` reject non-conforming values the same
way a missing attribute is handled: omit the binding → empty bindings → graceful
no-op fall-through.

Pure stdlib — no network, no ``infona.*``.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

# ClinicalTrials.gov NCT identifiers: NCT + exactly 8 digits (case-insensitive).
# See https://clinicaltrials.gov/data-api/about-api/study-data-structure
_NCT_ID_RE = re.compile(r"^NCT\d{8}$", re.IGNORECASE)


def is_valid_nct_id(value: str) -> bool:
    """True iff ``value`` is a well-formed ClinicalTrials.gov NCT identifier."""
    return bool(_NCT_ID_RE.match((value or "").strip()))


def normalize_nct_id(value: str) -> Optional[str]:
    """Return the canonical uppercase ``NCT########`` form, or ``None`` if invalid.

    Placeholders like ``NO-TRIAL-R053``, bare numbers, empty strings, and
    truncated IDs all return ``None`` so callers can treat them as "no binding".
    """
    v = (value or "").strip()
    if not is_valid_nct_id(v):
        return None
    return v.upper()


# Attribute leaves whose enrich_from values must pass a well-known id format
# before they become request bindings. Key = attribute leaf in
# ``enrich_from: attribute:<leaf>``; value = normalizer returning the canonical
# string to bind, or None to omit (same as missing/empty).
_ATTR_BIND_NORMALIZERS: dict[str, Callable[[str], Optional[str]]] = {
    "nct_id": normalize_nct_id,
}


def normalize_attribute_binding(attr_leaf: str, value: str) -> str:
    """Apply a format normalizer for ``attr_leaf`` when one is registered.

    Returns the canonical value to bind, or ``""`` when the value is empty,
    missing a normalizer (pass-through strip), or fails format validation.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalizer = _ATTR_BIND_NORMALIZERS.get(attr_leaf)
    if normalizer is None:
        return raw
    return normalizer(raw) or ""


__all__ = [
    "is_valid_nct_id",
    "normalize_nct_id",
    "normalize_attribute_binding",
]
