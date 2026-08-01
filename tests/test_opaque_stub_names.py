"""Opaque FK stubs must not pollute attrs/name (dogfood S5 dual-name SUM)."""

from cograph_client.resolver.csv_resolver import _is_opaque_identifier


def test_opaque_ids():
    for v in ("C1001", "R-WEST", "S-ACME", "P77", "ERP-1", "O9001"):
        assert _is_opaque_identifier(v), v


def test_display_names_not_opaque():
    for v in ("West", "Alice Chen", "Acme Corp", "Austin", "Beta Industries"):
        assert not _is_opaque_identifier(v), v
