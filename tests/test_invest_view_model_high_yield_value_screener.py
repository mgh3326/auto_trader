from __future__ import annotations

import datetime as dt
import decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.invest_view_model.high_yield_value_screener import (
    load_high_yield_value_from_snapshots,
)

# 9-prefix synthetic symbols, isolated from real KR symbols and sibling suites.
# ZZUS* are synthetic US tickers for the ROB-427 PR3 US-market path.
_TEST_SYMBOLS = ["921000", "920001", "929999", "ZZUSHI", "ZZUSLO"]


@pytest.fixture(autouse=True)
def mock_partition_health_always_healthy(monkeypatch):
    from app.services.invest_screener_snapshots import partition_health
    from app.services.invest_screener_snapshots.partition_health import (
        HealthyPartition,
        resolve_healthy_partition,
    )

    orig_resolve = resolve_healthy_partition

    async def _fake_resolve(*args, **kwargs):
        hp = await orig_resolve(*args, **kwargs)
        if hp:
            return HealthyPartition(
                partition_date=hp.partition_date,
                row_count=hp.row_count,
                coverage_ratio=hp.coverage_ratio,
                is_fallback=hp.is_fallback,
                healthy=True,
            )
        return None

    monkeypatch.setattr(
        partition_health,
        "resolve_healthy_partition",
        _fake_resolve,
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows(db_session):
    async def _purge() -> None:
        await db_session.execute(
            sa.delete(MarketValuationSnapshot).where(
                MarketValuationSnapshot.symbol.in_(_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            sa.delete(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.symbol.in_(_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol.in_(_TEST_SYMBOLS)
            )
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
async def test_filters_by_roe_and_per(db_session):
    val_date = dt.date(2099, 12, 31)
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="921000", name="고ROE저PER", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="920001", name="저ROE", exchange="KOSPI", is_active=True
            ),
        ]
    )
    db_session.add_all(
        [
            # qualifies: ROE 18 >= 15, PER 8 in (0, 10]
            MarketValuationSnapshot(
                market="kr",
                symbol="921000",
                snapshot_date=val_date,
                source="naver_finance",
                per=decimal.Decimal("8.0"),
                roe=decimal.Decimal("18.0"),
            ),
            # excluded: ROE 9 < 15
            MarketValuationSnapshot(
                market="kr",
                symbol="920001",
                snapshot_date=val_date,
                source="naver_finance",
                per=decimal.Decimal("5.0"),
                roe=decimal.Decimal("9.0"),
            ),
        ]
    )
    await db_session.commit()

    rows = await load_high_yield_value_from_snapshots(
        db_session, market="kr", limit=20, today_market_date=val_date
    )

    assert rows is not None
    symbols = [r["symbol"] for r in rows]
    assert "921000" in symbols
    assert "920001" not in symbols
    target = next(r for r in rows if r["symbol"] == "921000")
    assert target["roe"] == pytest.approx(18.0)
    assert target["per"] == pytest.approx(8.0)
    assert target["_screener_snapshot_state"] == "fresh"


@pytest.mark.asyncio
async def test_us_market_filters_by_roe_and_per(db_session):
    """ROB-427 PR3: US runs on Yahoo valuation snapshots (market=us), same ROE>=15 /
    PER 0~10 rule, no KR universe / common-stock filter. row.market == 'us'."""
    val_date = dt.date(2099, 12, 31)
    db_session.add_all(
        [
            # qualifies: ROE 22 >= 15, PER 7 in (0, 10]
            MarketValuationSnapshot(
                market="us",
                symbol="ZZUSHI",
                snapshot_date=val_date,
                source="yahoo",
                per=decimal.Decimal("7.0"),
                roe=decimal.Decimal("22.0"),
            ),
            # excluded: ROE 8 < 15
            MarketValuationSnapshot(
                market="us",
                symbol="ZZUSLO",
                snapshot_date=val_date,
                source="yahoo",
                per=decimal.Decimal("4.0"),
                roe=decimal.Decimal("8.0"),
            ),
        ]
    )
    await db_session.commit()

    rows = await load_high_yield_value_from_snapshots(
        db_session, market="us", limit=20, today_market_date=val_date
    )

    assert rows is not None
    symbols = [r["symbol"] for r in rows]
    assert "ZZUSHI" in symbols
    assert "ZZUSLO" not in symbols
    target = next(r for r in rows if r["symbol"] == "ZZUSHI")
    assert target["market"] == "us"
    assert target["roe"] == pytest.approx(22.0)
    assert target["per"] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_excludes_high_per_and_null_metrics(db_session):
    val_date = dt.date(2099, 12, 31)
    db_session.add(
        KRSymbolUniverse(
            symbol="921000", name="PER초과", exchange="KOSPI", is_active=True
        )
    )
    db_session.add_all(
        [
            # excluded: PER 12 > 10 though ROE qualifies
            MarketValuationSnapshot(
                market="kr",
                symbol="921000",
                snapshot_date=val_date,
                source="naver_finance",
                per=decimal.Decimal("12.0"),
                roe=decimal.Decimal("20.0"),
            ),
            # excluded: NULL roe must fail closed (never fabricated as a qualifier)
            MarketValuationSnapshot(
                market="kr",
                symbol="920001",
                snapshot_date=val_date,
                source="naver_finance",
                per=decimal.Decimal("5.0"),
                roe=None,
            ),
        ]
    )
    await db_session.commit()

    rows = await load_high_yield_value_from_snapshots(
        db_session, market="kr", limit=20, today_market_date=val_date
    )
    assert rows is not None
    assert "921000" not in [r["symbol"] for r in rows]
    assert "920001" not in [r["symbol"] for r in rows]


@pytest.mark.asyncio
async def test_excludes_non_common_stock(db_session):
    val_date = dt.date(2099, 12, 31)
    db_session.add(
        KRSymbolUniverse(
            symbol="929999", name="KODEX 고배당 ETF", exchange="KOSPI", is_active=True
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="929999",
            snapshot_date=val_date,
            source="naver_finance",
            per=decimal.Decimal("6.0"),
            roe=decimal.Decimal("22.0"),
        )
    )
    await db_session.commit()

    rows = await load_high_yield_value_from_snapshots(
        db_session, market="kr", limit=20, today_market_date=val_date
    )
    assert rows is not None
    assert all(r["symbol"] != "929999" for r in rows)


@pytest.mark.asyncio
async def test_stale_when_partition_is_not_todays_trading_date(db_session):
    val_date = dt.date(2099, 12, 31)
    db_session.add(
        KRSymbolUniverse(
            symbol="921000", name="구밸류에이션", exchange="KOSPI", is_active=True
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="921000",
            snapshot_date=val_date,
            source="naver_finance",
            per=decimal.Decimal("7.0"),
            roe=decimal.Decimal("16.0"),
        )
    )
    await db_session.commit()

    rows = await load_high_yield_value_from_snapshots(
        db_session,
        market="kr",
        limit=20,
        today_market_date=dt.date(2100, 1, 2),  # later than the partition
    )
    assert rows is not None
    target = next((r for r in rows if r["symbol"] == "921000"), None)
    assert target is not None
    assert target["_screener_snapshot_state"] == "stale"


@pytest.mark.asyncio
async def test_returns_none_for_non_kr_market(db_session):
    # ROB-427 PR3: US is now SUPPORTED (Yahoo valuation). crypto / unknown markets
    # remain unsupported by this loader → None.
    rows = await load_high_yield_value_from_snapshots(
        db_session, market="crypto", limit=20
    )
    assert rows is None


@pytest.mark.asyncio
async def test_returns_none_when_no_valuation_partition():
    from unittest.mock import AsyncMock, MagicMock

    null_scalar = MagicMock()
    null_scalar.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=null_scalar)

    rows = await load_high_yield_value_from_snapshots(session, market="kr", limit=20)
    assert rows is None


@pytest.mark.unit
def test_metric_value_label_formats_roe_as_percent():
    from app.services.invest_view_model.screener_service import _metric_value_label

    label, warnings = _metric_value_label("high_yield_value", {"roe": 18.3})
    assert label == "18.3%"
    assert warnings == []
