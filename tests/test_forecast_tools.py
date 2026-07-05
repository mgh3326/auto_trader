# tests/test_forecast_tools.py
"""ROB-650 — MCP tool envelopes + registration wiring for forecast tools."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.forecast_tools import (
    forecast_resolve,
    forecast_save,
    get_forecast_calibration,
    get_forecasts,
)
from app.models.review import TradeForecast

_EXPECTED_NAMES = {
    "forecast_save",
    "forecast_resolve",
    "get_forecasts",
    "get_forecast_calibration",
}


# --------------------------------------------------------------------------- #
# DB-free wiring tests
# --------------------------------------------------------------------------- #
def test_tool_names_set_complete():
    from app.mcp_server.tooling.forecast_registration import FORECAST_TOOL_NAMES

    assert FORECAST_TOOL_NAMES == _EXPECTED_NAMES


def test_tools_in_available_surface():
    from app.mcp_server import AVAILABLE_TOOL_NAMES

    for name in _EXPECTED_NAMES:
        assert name in AVAILABLE_TOOL_NAMES


def test_register_wires_four_tools():
    from app.mcp_server.tooling.forecast_registration import register_forecast_tools

    registered: list[str] = []

    class _FakeMCP:
        def tool(self, *, name, description):
            registered.append(name)

            def _wrap(fn):
                return fn

            return _wrap

    register_forecast_tools(_FakeMCP())
    assert set(registered) == _EXPECTED_NAMES


@pytest.mark.asyncio
async def test_save_missing_symbol_envelope():
    # Short-circuits before any DB access.
    res = await forecast_save(
        created_by="claude",
        symbol="   ",
        instrument_type="equity_kr",
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 1.0,
        },
        probability=0.5,
        review_date="2026-07-15",
    )
    assert res["success"] is False
    assert "symbol" in res["error"]


# ROB-712 — forecast_resolve must expose backfill_missing and forward it to
# resolve_forecast on both the single and the batch path.
@pytest.mark.asyncio
async def test_forecast_resolve_passes_backfill_flag(monkeypatch):
    from app.mcp_server.tooling import forecast_tools

    seen: dict[str, object] = {}

    async def fake_resolve(db, *, forecast_id, persist, backfill_missing=True, **kw):
        seen["backfill_missing"] = backfill_missing
        seen["forecast_id"] = forecast_id
        return {"status": "unresolved_no_data", "changed": False}

    monkeypatch.setattr(forecast_tools, "resolve_forecast", fake_resolve)

    # Stub the session factory so the test never opens a DB connection.
    # Production shape: _session_factory() -> sessionmaker; () -> session;
    # the session is an async context manager that yields the AsyncSession.
    class _StubSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return False

    class _StubSessionMaker:
        def __call__(self) -> _StubSession:
            return _StubSession()

    def _factory():
        return _StubSessionMaker()

    monkeypatch.setattr(forecast_tools, "_session_factory", _factory)



    res = await forecast_resolve(
        forecast_id="x", dry_run=True, backfill_missing=False
    )
    assert res["success"] is True
    assert seen["backfill_missing"] is False
    assert seen["forecast_id"] == "x"


# --------------------------------------------------------------------------- #
# DB-backed envelope tests (opt-in: request the _clean fixture explicitly so
# the wiring tests above stay DB-free)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def _clean(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
) -> AsyncSession:
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()
    return db_session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_and_resolve_envelope(_clean):
    saved = await forecast_save(
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.7,
        review_date="2026-07-15",
        session_label="s1",
    )
    assert saved["success"] is True
    assert saved["action"] == "created"
    fid = saved["data"]["forecast_id"]

    preview = await forecast_resolve(
        forecast_id=fid,
        dry_run=True,
        manual_outcome=True,
        manual_evidence=["target hit"],
    )
    assert preview["success"] is True
    assert preview["dry_run"] is True
    assert preview["status"] == "previewed"
    assert preview["changed"] is False

    committed = await forecast_resolve(
        forecast_id=fid,
        dry_run=False,
        manual_outcome=True,
        manual_evidence=["target hit"],
    )
    assert committed["success"] is True
    assert committed["status"] == "resolved"
    assert committed["changed"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_validation_error_envelope(_clean):
    res = await forecast_save(
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 1.0,
        },
        probability=1.9,
        review_date="2026-07-15",
    )
    assert res["success"] is False
    assert "probability" in res["error"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_and_calibration_envelope(_clean):
    lst = await get_forecasts(status="open")
    assert lst["success"] is True
    assert "entries" in lst

    agg = await get_forecast_calibration(group_by="created_by")
    assert agg["success"] is True
    assert agg["group_by"] == "created_by"
    assert "groups" in agg
