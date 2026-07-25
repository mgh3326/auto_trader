# tests/test_forecast_tools.py
"""ROB-650 — MCP tool envelopes + registration wiring for forecast tools."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_registration_describes_terminal_close_contract():
    from app.mcp_server.tooling.forecast_registration import register_forecast_tools

    descriptions: dict[str, str] = {}

    class _FakeMCP:
        def tool(self, *, name, description):
            descriptions[name] = description

            def _wrap(fn):
                return fn

            return _wrap

    register_forecast_tools(_FakeMCP())

    save_description = descriptions["forecast_save"]
    resolve_description = descriptions["forecast_resolve"]
    assert "terminal_close" in save_description
    assert "direction in {up, down}" in save_description
    assert "terminal-close-v1-up-gte-down-lt" in save_description
    assert "window-touch-v1-high-gte-low-lte" in save_description
    assert "ROB-1043" in save_description
    assert "review-date regular-session close" in resolve_description
    assert "high/low" in resolve_description
    assert "quarantined" in resolve_description
    assert (
        "versionless price_target cannot be manually overridden" in resolve_description
    )
    assert "register a new forecast with a typed target" in resolve_description


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

    res = await forecast_resolve(forecast_id="x", dry_run=True, backfill_missing=False)
    assert res["success"] is True
    assert seen["backfill_missing"] is False
    assert seen["forecast_id"] == "x"


@pytest.mark.asyncio
async def test_due_batch_forwards_terminal_fail_closed_evidence(monkeypatch):
    from app.mcp_server.tooling import forecast_tools

    due = SimpleNamespace(forecast_id="terminal-id", symbol="SMCI")
    evidence = {
        "target_kind": "terminal_close",
        "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
        "review_date": "2026-08-20",
    }

    async def fake_due(_db, *, limit):
        assert limit == 25
        return [due]

    async def fake_quarantined(_db, *, limit):
        assert limit == 25
        return []

    async def fake_resolve(_db, **_kwargs):
        return {
            "status": "unresolved_untrusted_source",
            "changed": False,
            "reason": "source not trusted",
            "resolution_evidence": evidence,
        }

    class _StubSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return False

    class _StubSessionMaker:
        def __call__(self) -> _StubSession:
            return _StubSession()

    monkeypatch.setattr(forecast_tools, "list_due_forecasts", fake_due)
    monkeypatch.setattr(
        forecast_tools,
        "list_due_quarantined_forecasts",
        fake_quarantined,
    )
    monkeypatch.setattr(forecast_tools, "resolve_forecast", fake_resolve)
    monkeypatch.setattr(forecast_tools, "_session_factory", lambda: _StubSessionMaker())

    result = await forecast_resolve(dry_run=True, backfill_missing=False)

    assert result["success"] is True
    assert result["results"][0]["resolution_evidence"] == evidence
    assert result["quarantined_count"] == 0


@pytest.mark.asyncio
async def test_due_batch_reports_quarantine_without_consuming_due_limit(monkeypatch):
    from app.mcp_server.tooling import forecast_tools

    legacy = SimpleNamespace(forecast_id="legacy-id", symbol="OLD")
    eligible = SimpleNamespace(forecast_id="eligible-id", symbol="NEW")
    calls: list[str] = []

    async def fake_quarantined(_db, *, limit):
        assert limit == 1
        return [legacy]

    async def fake_due(_db, *, limit):
        assert limit == 1
        return [eligible]

    async def fake_resolve(_db, *, forecast_id, **_kwargs):
        calls.append(forecast_id)
        if forecast_id == "legacy-id":
            return {
                "status": "quarantined_legacy_price_target",
                "changed": False,
                "reason": "missing outcome rule",
            }
        return {"status": "requires_manual", "changed": False}

    class _StubSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class _StubSessionMaker:
        def __call__(self) -> _StubSession:
            return _StubSession()

    monkeypatch.setattr(
        forecast_tools,
        "list_due_quarantined_forecasts",
        fake_quarantined,
    )
    monkeypatch.setattr(forecast_tools, "list_due_forecasts", fake_due)
    monkeypatch.setattr(forecast_tools, "resolve_forecast", fake_resolve)
    monkeypatch.setattr(forecast_tools, "_session_factory", lambda: _StubSessionMaker())

    result = await forecast_resolve(
        dry_run=True,
        backfill_missing=False,
        limit=1,
    )

    assert result["success"] is True
    assert result["due_count"] == 1
    assert result["quarantined_count"] == 1
    assert calls == ["legacy-id", "eligible-id"]
    assert result["by_status"] == {
        "quarantined_legacy_price_target": 1,
        "requires_manual": 1,
    }


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
async def test_due_batch_dry_run_distinguishes_no_claim_auto_close(_clean):
    saved = await forecast_save(
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "no_resolvable_forecast"},
        probability=0.0,
        review_date="2020-01-01",
    )
    fid = saved["data"]["forecast_id"]

    preview = await forecast_resolve(dry_run=True, backfill_missing=False)

    assert preview["success"] is True
    assert preview["dry_run"] is True
    assert preview["by_status"] == {"would_close_no_claim": 1}
    assert preview["results"] == [
        {
            "forecast_id": fid,
            "symbol": "005930",
            "status": "would_close_no_claim",
            "changed": False,
            "auto_close": True,
            "computed": None,
            "reason": "placeholder has no resolvable claim",
        }
    ]

    committed = await forecast_resolve(dry_run=False, backfill_missing=False)
    assert committed["by_status"] == {"closed_no_claim": 1}
    assert committed["results"][0]["auto_close"] is True

    due_after_close = await forecast_resolve(dry_run=True, backfill_missing=False)
    assert due_after_close["due_count"] == 0


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
async def test_terminal_close_validation_error_envelope(_clean):
    res = await forecast_save(
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target={
            "kind": "terminal_close",
            "direction": "at_or_below",
            "target_price": 30.56,
            "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
        },
        probability=0.52,
        review_date="2026-08-20",
    )

    assert res["success"] is False
    assert "terminal_close.direction" in res["error"]


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
