"""Colloquial-specialty → official NUCC taxonomy normalization for NPPES.

NPPES matches its ``taxonomy_description`` search parameter against the OFFICIAL
NUCC taxonomy descriptions (e.g. "Neurological Surgery", "Orthopaedic Surgery",
"Cardiovascular Disease"). A verbatim natural-language term — "neurosurgery",
"orthopedic surgeon", "cardiologist" — or a raw NUCC *code* ("207X00000X") does
NOT match, so the query returns zero records even though the provider cohort
exists (the UCI persona-eval orthopedic / neurosurgery / cardiology blockers).

This module maps the known-wrong terms (and the persona's explicitly-supplied
NUCC codes) to their canonical NUCC description. It is deliberately CONSERVATIVE:

* An unmapped term passes through **verbatim** — the map only corrects terms we
  KNOW are wrong, so it can never regress a query that already works (e.g.
  "oncology", "gastroenterology" pass through unchanged).
* An explicitly-supplied canonical description ("Orthopaedic Surgery") or NUCC
  code ("207X00000X") is honored directly instead of being re-derived into a
  colloquial term — so a caller who already knows the right NUCC value keeps it.

Pure stdlib, no network, no ``cograph.*`` import — safe for the OSS package.
"""

from __future__ import annotations

import re

# Colloquial / derived specialty term (normalized) -> official NUCC description.
# Keys are matched case-insensitively after whitespace/punctuation normalization
# (see ``_key``). Canonical descriptions are also present as keys so an
# already-correct value (possibly re-cased by an upstream LLM) round-trips to the
# exact NUCC spelling NPPES expects.
_SYNONYM_MAP: dict[str, str] = {
    # Neurosurgery
    "neurosurgery": "Neurological Surgery",
    "neurosurgeon": "Neurological Surgery",
    "neurosurgeons": "Neurological Surgery",
    "neurological surgery": "Neurological Surgery",
    "neurological surgeon": "Neurological Surgery",
    # Orthopedic surgery (US "orthopedic" + NUCC's "orthopaedic" spelling)
    "orthopedic surgery": "Orthopaedic Surgery",
    "orthopedic surgeon": "Orthopaedic Surgery",
    "orthopedic surgeons": "Orthopaedic Surgery",
    "orthopaedic surgery": "Orthopaedic Surgery",
    "orthopaedic surgeon": "Orthopaedic Surgery",
    "orthopaedic surgeons": "Orthopaedic Surgery",
    "orthopedics": "Orthopaedic Surgery",
    "orthopaedics": "Orthopaedic Surgery",
    "orthopedic": "Orthopaedic Surgery",
    "orthopaedic": "Orthopaedic Surgery",
    # Cardiology
    "cardiology": "Cardiovascular Disease",
    "cardiologist": "Cardiovascular Disease",
    "cardiologists": "Cardiovascular Disease",
    "cardiovascular disease": "Cardiovascular Disease",
}

# Explicit NUCC taxonomy CODE -> official description, for the persona's service
# lines. When a caller supplies a code directly ("207X00000X"), honor it rather
# than dropping it. Unknown codes pass through verbatim (see ``normalize_taxonomy``).
_CODE_MAP: dict[str, str] = {
    "207T00000X": "Neurological Surgery",
    "207X00000X": "Orthopaedic Surgery",
    "207RC0000X": "Cardiovascular Disease",
}

# NUCC provider taxonomy codes are 10 chars: 3 digits, 6 alphanumerics, trailing X.
_CODE_RE = re.compile(r"^[0-9]{3}[A-Z0-9]{6}X$", re.IGNORECASE)

_WS_RE = re.compile(r"\s+")


def _key(value: str) -> str:
    """Normalize a term for map lookup: lowercase, collapse whitespace, strip
    surrounding punctuation/quotes."""
    return _WS_RE.sub(" ", value.strip().strip("\"'.,").lower()).strip()


def normalize_taxonomy(value: str) -> str:
    """Map a colloquial specialty term or NUCC code to its official NUCC
    description; pass unmapped values through unchanged.

    >>> normalize_taxonomy("neurosurgery")
    'Neurological Surgery'
    >>> normalize_taxonomy("orthopedic surgeon")
    'Orthopaedic Surgery'
    >>> normalize_taxonomy("207X00000X")
    'Orthopaedic Surgery'
    >>> normalize_taxonomy("oncology")   # unmapped -> verbatim
    'oncology'
    """
    if not value or not value.strip():
        return value
    stripped = value.strip()
    if _CODE_RE.match(stripped):
        mapped = _CODE_MAP.get(stripped.upper())
        # A recognized code resolves to its description; an unknown code passes
        # through verbatim (current behavior — can't regress).
        return mapped if mapped else value
    return _SYNONYM_MAP.get(_key(stripped), value)


__all__ = ["normalize_taxonomy"]
