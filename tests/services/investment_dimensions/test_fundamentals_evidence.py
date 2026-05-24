import datetime as dt
from decimal import Decimal

import pytest

from app.models.analysis import StockInfo
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.investment_dimensions.fundamentals_evidence import (
    build_fundamentals_evidence,
)
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)
from app.services.stock_info_service import StockInfoService


async def _clear(db_session):
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM market_valuation_snapshots"))
    await db_session.execute(
        text("DELETE FROM stock_info WHERE symbol IN ('AAPL','MSFT')")
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_build_fundamentals_evidence_covered(db_session):
    await _clear(db_session)
    db_session.add(
        MarketValuationSnapshot(
            market="us",
            symbol="AAPL",
            snapshot_date=dt.date(2026, 5, 23),
            source="yahoo",
            per=Decimal("28.5"),
            pbr=Decimal("45"),
            roe=Decimal("1.5"),
            dividend_yield=Decimal("0.005"),
            market_cap=Decimal("3000000000000"),
            high_52w=Decimal("260"),
            low_52w=Decimal("164"),
        )
    )
    db_session.add(
        StockInfo(
            symbol="AAPL",
            name="Apple",
            instrument_type="equity_us",
            sector="Technology",
            is_active=True,
        )
    )
    await db_session.commit()

    bundle = await build_fundamentals_evidence(
        MarketValuationSnapshotsRepository(db_session),
        StockInfoService(db_session),
        market="us",
        symbols={"AAPL", "MSFT"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["market"] == "us"
    assert bundle["data_health"] == {"requested": 2, "covered": 1}
    assert bundle["covered_count"] == 1
    row = bundle["per_symbol"][0]
    assert row["symbol"] == "AAPL"
    assert row["sector"] == "Technology"
    assert row["per"] == 28.5
    assert row["dividend_yield"] == 0.005
    assert bundle["freshness"]["status"] in {"fresh", "stale"}
    assert bundle["freshness"]["latest_snapshot_date"] == "2026-05-23"


@pytest.mark.asyncio
async def test_build_fundamentals_evidence_empty_is_unavailable(db_session):
    await _clear(db_session)
    bundle = await build_fundamentals_evidence(
        MarketValuationSnapshotsRepository(db_session),
        StockInfoService(db_session),
        market="us",
        symbols={"AAPL"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["covered_count"] == 0
    assert bundle["per_symbol"] == []
    assert bundle["freshness"]["status"] == "unavailable"
    assert bundle["data_health"] == {"requested": 1, "covered": 0}
