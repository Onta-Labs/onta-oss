"""Explorer product surfaces must not appear in the public OSS API.

Per-tenant ``GET /graphs/{tenant}/usage`` is OSS (self-hosters need their
own counters). The hosted Explorer's cross-workspace dashboard rollup is
premium (``infona/api/routes/me_dashboard.py``, mounted via
``INFONA_ROUTER_PLUGINS``). ``scripts/check_boundary.sh`` greps source
strings; this test catches FastAPI prefix+path composition that grep would
miss (``prefix="/v1/me"`` + ``@router.get("/dashboard")``).
"""

from __future__ import annotations

from infona_client.api.app import create_app

_FORBIDDEN_PATHS = ("/v1/me/dashboard", "/v1/me/summary")


def test_oss_openapi_does_not_serve_explorer_dashboard():
    paths = create_app().openapi()["paths"]
    leaked = [p for p in paths if p.rstrip("/") in _FORBIDDEN_PATHS]
    assert leaked == [], (
        "Explorer-only dashboard routes must live in infona/ and mount via "
        f"INFONA_ROUTER_PLUGINS, not infona-oss. Found: {leaked}"
    )
