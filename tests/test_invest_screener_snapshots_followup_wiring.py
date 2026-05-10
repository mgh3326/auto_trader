"""ROB-170 follow-up — verify build_screener_results threads session into snapshot hydration."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)
from app.services.invest_view_model.screener_service import build_screener_results


class _StubResolver:
    def relation(self, market: str, symbol: str) -> str:
        return "neither"


class _StubScreeningService:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def list_screening(self, **kwargs):
        return {
            "results": list(self._rows),
            "total_count": len(self._rows),
            "filters_applied": kwargs,
            "timestamp": "2026-05-10T05:00:00+00:00",
            "cache_hit": False,
        }


@pytest.mark.asyncio
async def test_build_screener_results_data_state_not_missing_when_snapshot_present(
    db_session, monkeypatch
):
    """When snapshots exist for all candidate symbols, dataState is NOT 'missing'.

    The key invariant is that wiring the session through _enrich_consecutive_up_days
    changes dataState away from 'missing'. The exact state (fresh/stale) depends on
    snapshot age vs STALE_AFTER_HOURS and is not asserted here.
    """
    repo = InvestScreenerSnapshotsRepository(db_session)
    today = dt.date(2026, 5, 8)

    for symbol in ["005930", "000660"]:
        await repo.upsert(
            SnapshotUpsert(
                market="kr",
                symbol=symbol,
                snapshot_date=today,
                latest_close=Decimal("70000"),
                prev_close=Decimal("69000"),
                change_amount=Decimal("1000"),
                change_rate=Decimal("1.45"),
                consecutive_up_days=6,
                week_change_rate=Decimal("3.5"),
                closes_window=[68000, 68500, 69000, 69500, 70000, 70000],
                source="kis",
            )
        )
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.freshness.today_trading_date",
        lambda market, now=None: today,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.screening.enrichment.today_trading_date",
        lambda market, now=None: today,
    )

    rows = [
        {
            "market": "kr",
            "code": "005930",
            "consecutive_up_days": 6,
            "change_rate": 1.45,
            "close": 70000,
        },
        {
            "market": "kr",
            "code": "000660",
            "consecutive_up_days": 6,
            "change_rate": 1.45,
            "close": 70000,
        },
    ]
    service = _StubScreeningService(rows)

    result = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=service,
        resolver=_StubResolver(),
        market="kr",
        session=db_session,
    )
    # The critical invariant: snapshot wiring worked → state is NOT "missing".
    # (exact state depends on snapshot age vs STALE_AFTER_HOURS)
    assert result.freshness.dataState != "missing"


@pytest.mark.asyncio
async def test_build_screener_results_data_state_missing_without_snapshots(
    db_session, monkeypatch
):
    """When no snapshots exist, dataState=='missing' even though session is supplied."""
    monkeypatch.setattr(
        "app.mcp_server.tooling.screening.enrichment.today_trading_date",
        lambda market, now=None: dt.date(2026, 5, 10),
    )

    rows = [{"market": "kr", "code": "999999", "consecutive_up_days": 6}]
    service = _StubScreeningService(rows)

    result = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=service,
        resolver=_StubResolver(),
        market="kr",
        session=db_session,
    )
    assert result.freshness.dataState == "missing"


@pytest.mark.asyncio
async def test_build_screener_results_data_state_missing_when_no_session():
    """When session is None, hydration is skipped and dataState defaults to 'missing'."""
    rows = [{"market": "kr", "code": "005930", "consecutive_up_days": 6}]
    service = _StubScreeningService(rows)

    result = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=service,
        resolver=_StubResolver(),
        market="kr",
        session=None,
    )
    assert result.freshness.dataState == "missing"
