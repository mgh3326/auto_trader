"""ROB-269 Phase 2 — INVESTMENT_SNAPSHOTS_MCP_ENABLED end-to-end flag tests.

These build the full FastAPI app twice (once flag off, once flag on) and
assert the snapshots router is physically absent vs present. The MCP-side
tool flag gating is covered by ``tests/mcp_server/test_investment_snapshots_tools.py``.
"""

from __future__ import annotations


def _snapshots_routes(app) -> set[str]:
    return {
        getattr(r, "path", "")
        for r in app.routes
        if "investment-snapshots" in getattr(r, "path", "")
    }


def test_main_create_app_does_not_mount_snapshots_router_when_flag_disabled(
    monkeypatch,
):
    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "INVESTMENT_SNAPSHOTS_MCP_ENABLED", False)

    app = create_app()
    assert _snapshots_routes(app) == set(), (
        "Snapshots router routes leaked with flag off"
    )


def test_main_create_app_mounts_snapshots_router_when_flag_enabled(monkeypatch):
    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "INVESTMENT_SNAPSHOTS_MCP_ENABLED", True)

    app = create_app()
    routes = _snapshots_routes(app)
    # All three GET paths present.
    assert any("/bundles/{bundle_uuid}" in r for r in routes)
    assert "/trading/api/investment-snapshots/bundles" in routes
    assert "/trading/api/investment-snapshots/snapshots" in routes
