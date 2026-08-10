"""Brand / package rename guards (Infona)."""
from __future__ import annotations

import importlib
import warnings


def test_infona_client_is_canonical_import():
    import infona_client

    assert "Infona" in (infona_client.__doc__ or "")


def test_app_title_is_infona():
    from infona_client.api.app import create_app

    app = create_app()
    assert app.title == "Infona"


def test_cograph_client_shim_deprecated_and_aliases():
    # Drop any prior alias modules so the deprecation fires.
    import sys

    for k in list(sys.modules):
        if k == "cograph_client" or k.startswith("cograph_client."):
            del sys.modules[k]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Top-level package import path (filesystem shim).
        import cograph_client  # noqa: F401

        # Ensure finder is installed; submodule import should resolve.
        from cograph_client.api.app import create_app as via_legacy
        from infona_client.api.app import create_app as via_new

    assert any(
        issubclass(w.category, DeprecationWarning)
        and "cograph_client" in str(w.message)
        for w in caught
    )
    # Legacy-only identity: both names end up usable.
    assert via_legacy is not None and via_new is not None
    assert via_legacy().title == "Infona"


def test_pyproject_distribution_name():
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path("pyproject.toml").read_text())
    assert data["project"]["name"] == "infona-client"
    assert "Infona" in data["project"]["description"]
