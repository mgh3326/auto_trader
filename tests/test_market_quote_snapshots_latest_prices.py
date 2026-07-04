from __future__ import annotations

import datetime as dt

import pytest

from app.services.market_quote_snapshots.repository import (
    MarketQuoteSnapshotsRepository,
    MarketQuoteSnapshotUpsert,
)

# DB-backed helper test — uses the real-PostgreSQL ``db_session`` fixture (DISTINCT
# ON is Postgres-only). Marked bare ``asyncio`` to match the neighbouring
# snapshot-repository tests in ``tests/test_invest_coverage_valuation.py`` (they
# are NOT tagged ``unit``); ``make test-unit`` (``-m "not integration and not
# live"``) still runs it.
pytestmark = [pytest.mark.asyncio]


async def _seed(db_session):
    now = dt.datetime.now(dt.UTC)
    repo = MarketQuoteSnapshotsRepository(db_session)
    await repo.upsert(
        [
            # 005930: two rows — the later snapshot_at (70500) must win
            MarketQuoteSnapshotUpsert(
                market="kr",
                symbol="005930",
                source="kis",
                snapshot_at=now - dt.timedelta(hours=3),
                price="69000",
            ),
            MarketQuoteSnapshotUpsert(
                market="kr",
                symbol="005930",
                source="naver_finance",
                snapshot_at=now - dt.timedelta(minutes=5),
                price="70500",
            ),
            # 034020: single row
            MarketQuoteSnapshotUpsert(
                market="kr",
                symbol="034020",
                source="kis",
                snapshot_at=now - dt.timedelta(minutes=1),
                price="18000",
            ),
            # AAPL is US — must NOT leak into a kr query
            MarketQuoteSnapshotUpsert(
                market="us",
                symbol="AAPL",
                source="yahoo",
                snapshot_at=now,
                price="222.5",
            ),
        ]
    )
    await db_session.commit()
    return repo


async def test_latest_prices_returns_latest_close_per_symbol(db_session):
    repo = await _seed(db_session)
    out = await repo.latest_prices("kr", ["005930", "034020"])
    assert out == pytest.approx({"005930": 70500.0, "034020": 18000.0})


async def test_latest_prices_omits_symbols_without_snapshot(db_session):
    repo = await _seed(db_session)
    out = await repo.latest_prices("kr", ["005930", "999999"])
    assert set(out) == {"005930"}


async def test_latest_prices_is_market_scoped(db_session):
    repo = await _seed(db_session)
    # AAPL only exists under market="us"; a kr query must not find it
    assert await repo.latest_prices("kr", ["AAPL"]) == {}
    assert await repo.latest_prices("us", ["AAPL"]) == pytest.approx({"AAPL": 222.5})


async def test_latest_prices_empty_symbols_returns_empty(db_session):
    repo = await _seed(db_session)
    assert await repo.latest_prices("kr", []) == {}
