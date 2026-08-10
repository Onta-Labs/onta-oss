"""Brand / package rename guards (Infona)."""
from __future__ import annotations


def test_infona_client_is_canonical_import():
    import infona_client

    assert "Infona" in (infona_client.__doc__ or "")


def test_app_title_is_infona():
    from infona_client.api.app import create_app

    app = create_app()
    assert app.title == "Infona"


def test_no_cograph_client_package():
    """cograph_client shim is gone — only infona_client remains."""
    import importlib.util
    from pathlib import Path

    assert importlib.util.find_spec("cograph_client") is None
    assert not (Path(__file__).resolve().parents[1] / "cograph_client").exists()


def test_pyproject_distribution_name():
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path("pyproject.toml").read_text())
    assert data["project"]["name"] == "infona-client"
    assert "Infona" in data["project"]["description"]
    packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages == ["infona_client"]
