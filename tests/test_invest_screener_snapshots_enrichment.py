"""TDD tests for snapshot-first read path in enrichment._enrich_consecutive_up_days."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling.screening import enrichment
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)


@pytest.mark.asyncio
async def test_enrichment_reads_from_snapshot_when_fresh(db_session, monkeypatch):
    repo = InvestScreenerSnapshotsRepository(db_session)
    today = dt.date(2026, 5, 9)
    symbol = "900001"
    await repo.upsert(
        SnapshotUpsert(
            market="kr",
            symbol=symbol,
            snapshot_date=today,
            latest_close=Decimal("78500"),
            prev_close=Decimal("77900"),
            change_amount=Decimal("600"),
            change_rate=Decimal("0.7702"),
            consecutive_up_days=4,
            week_change_rate=Decimal("2.1"),
            closes_window=[77000, 77100, 77400, 77900, 78500],
            source="kis",
        )
    )
    await db_session.commit()

    fetcher = AsyncMock()
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)
    # Patch the name as imported into enrichment module
    monkeypatch.setattr(
        enrichment, "today_trading_date", lambda market, now=None: today
    )

    rows = [{"market": "kr", "code": symbol}]
    await enrichment._enrich_consecutive_up_days(rows, market="kr", session=db_session)
    assert rows[0]["consecutive_up_days"] == 4
    assert rows[0]["_screener_snapshot_state"] == "fresh"
    fetcher.assert_not_called()


@pytest.mark.asyncio
async def test_enrichment_uses_latest_snapshot_when_history_exists(
    db_session, monkeypatch
):
    repo = InvestScreenerSnapshotsRepository(db_session)
    older = dt.date(2026, 5, 8)
    latest = dt.date(2026, 5, 9)
    symbol = "900002"
    for snapshot_date, up_days in [(older, 2), (latest, 5)]:
        await repo.upsert(
            SnapshotUpsert(
                market="kr",
                symbol=symbol,
                snapshot_date=snapshot_date,
                latest_close=Decimal("78500"),
                prev_close=Decimal("77900"),
                change_amount=Decimal("600"),
                change_rate=Decimal("0.7702"),
                consecutive_up_days=up_days,
                week_change_rate=Decimal("2.1"),
                closes_window=[77000, 77100, 77400, 77900, 78500],
                source="kis",
            )
        )
    await db_session.commit()

    fetcher = AsyncMock()
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)
    monkeypatch.setattr(
        enrichment, "today_trading_date", lambda market, now=None: latest
    )

    rows = [{"market": "kr", "code": symbol}]
    await enrichment._enrich_consecutive_up_days(rows, market="kr", session=db_session)
    assert rows[0]["consecutive_up_days"] == 5
    assert rows[0]["_screener_snapshot_state"] == "fresh"
    fetcher.assert_not_called()


@pytest.mark.asyncio
async def test_enrichment_falls_back_when_snapshot_missing(db_session, monkeypatch):
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-04-29", periods=5),
            "close": [100, 101, 102, 103, 104],
        }
    )
    fetcher = AsyncMock(return_value=df)
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)
    # Patch the name as imported into enrichment module
    monkeypatch.setattr(
        enrichment, "today_trading_date", lambda market, now=None: dt.date(2026, 5, 9)
    )

    rows = [{"market": "kr", "code": "999999"}]  # symbol not in DB
    await enrichment._enrich_consecutive_up_days(rows, market="kr", session=db_session)
    assert rows[0]["consecutive_up_days"] == 4
    assert rows[0]["_screener_snapshot_state"] == "missing"
    fetcher.assert_awaited()


@pytest.mark.asyncio
async def test_enrichment_no_session_keeps_rob168_behavior(monkeypatch):
    """When no DB session is provided (legacy callers), behavior matches ROB-168."""
    df = pd.DataFrame(
        {"date": pd.date_range("2026-04-29", periods=5), "close": [1, 2, 3, 4, 5]}
    )
    fetcher = AsyncMock(return_value=df)
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)

    rows = [{"market": "kr", "code": "005930"}]
    await enrichment._enrich_consecutive_up_days(rows, market="kr")
    assert rows[0]["consecutive_up_days"] == 4
    fetcher.assert_awaited()


@pytest.mark.asyncio
async def test_enrichment_fallback_populates_week_change_rate(monkeypatch):
    """When snapshot is missing, the OHLCV fallback must also fill week_change_rate."""
    fake_df = pd.DataFrame(
        {
            "date": pd.date_range("2026-05-01", periods=6, freq="B"),
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        }
    )
    fetcher = AsyncMock(return_value=fake_df)
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)

    rows = [{"market": "kr", "code": "900099"}]
    await enrichment._enrich_consecutive_up_days(rows, market="kr", session=None)
    assert rows[0]["consecutive_up_days"] == 5
    assert rows[0]["week_change_rate"] == pytest.approx(
        (105.0 - 101.0) / 101.0 * 100.0, rel=1e-6
    )
