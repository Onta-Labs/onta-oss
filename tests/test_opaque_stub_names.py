"""Opaque FK stubs must not pollute attrs/name (anti-overfit: multi-domain codes)."""

import pytest

from cograph_client.resolver.csv_resolver import _is_opaque_identifier


@pytest.mark.parametrize(
    "v",
    [
        # original dogfood sales codes
        "C1001",
        "R-WEST",
        "S-ACME",
        "P77",
        "O9001",
        # healthcare variant
        "MRN-441",
        "CL-NORTH",
        "A-100",
        # logistics variant
        "V9",
        "ERP-12",
        "SKU_9A",
    ],
)
def test_opaque_ids_multi_domain(v: str):
    assert _is_opaque_identifier(v), v


@pytest.mark.parametrize(
    "v",
    [
        "West",
        "Alice Chen",
        "Acme Corp",
        "Harbor Family Clinic",
        "Northern Lights Logistics",
        "Seattle",
        "Room 101",  # spaced display with digits
        "Windows 11",
    ],
)
def test_display_names_not_opaque_multi_domain(v: str):
    assert not _is_opaque_identifier(v), v
