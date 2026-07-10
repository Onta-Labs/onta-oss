"""Unit tests for the NPPES colloquial-specialty → NUCC normalization (Fix C).

NPPES matches its `taxonomy_description` param against official NUCC descriptions,
so verbatim NL terms ("neurosurgery", "orthopedic surgeon") — which anchor three
UCI persona-eval cohorts — return zero records. The map corrects the known-wrong
terms while passing everything else through verbatim (so it can't regress a
working query).
"""

from __future__ import annotations

import pytest

from cograph_client.api_registry.nppes_taxonomy import normalize_taxonomy


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The three required persona service lines (+ colloquial variants).
        ("neurosurgery", "Neurological Surgery"),
        ("neurosurgeon", "Neurological Surgery"),
        ("orthopedic surgery", "Orthopaedic Surgery"),
        ("orthopedic surgeon", "Orthopaedic Surgery"),
        ("orthopaedic", "Orthopaedic Surgery"),
        ("cardiology", "Cardiovascular Disease"),
        ("cardiologist", "Cardiovascular Disease"),
        # Case / whitespace / punctuation insensitive.
        ("  Orthopedic Surgeon ", "Orthopaedic Surgery"),
        ("NEUROSURGERY", "Neurological Surgery"),
        # An already-correct NUCC description round-trips to the exact spelling.
        ("Orthopaedic Surgery", "Orthopaedic Surgery"),
        ("cardiovascular disease", "Cardiovascular Disease"),
        # Explicitly-supplied NUCC codes are honored (persona gave 207X00000X).
        ("207X00000X", "Orthopaedic Surgery"),
        ("207t00000x", "Neurological Surgery"),
    ],
)
def test_known_terms_and_codes_normalize(raw, expected):
    assert normalize_taxonomy(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["oncology", "gastroenterology", "pediatrics", "Family Medicine", "", "   "],
)
def test_unmapped_terms_pass_through_verbatim(raw):
    # The map only CORRECTS known-wrong terms; anything else is unchanged, so a
    # currently-working query can never regress.
    assert normalize_taxonomy(raw) == raw


def test_unknown_code_passes_through_verbatim():
    # An unrecognized NUCC-shaped code is left as-is (no worse than today).
    assert normalize_taxonomy("999Z99999X") == "999Z99999X"
