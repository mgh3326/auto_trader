"""ROB-976 R3: support_proximity is a persisted, read-only pipeline."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_view_model.support_proximity_screener import (
    load_support_proximity_from_snapshots,
)

_SYMBOLS = {"976001", "976002", "976003", "976004"}


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(
        delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol.in_(_SYMBOLS)
        )
    )
    await db_session.execute(
        delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(_SYMBOLS))
    )
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(
        delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol.in_(_SYMBOLS)
        )
    )
    await db_session.execute(
        delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(_SYMBOLS))
    )
    await db_session.commit()


def _seed(
    db_session,
    symbol: str,
    *,
    snapshot_date: dt.date,
    computed_at: dt.datetime,
    close: Decimal = Decimal("50000"),
    support_price: Decimal | None = Decimal("49000"),
    distance: Decimal | None = Decimal("2.0000"),
    market_cap: Decimal = Decimal("1000000000000"),
    turnover: Decimal = Decimal("50000000000"),
    market_cap_source: str = "naver_finance",
    active: bool = True,
    suspended: bool = False,
) -> None:
    if active:
        db_session.add(
            KRSymbolUniverse(
                symbol=symbol,
                name=f"테스트{symbol}",
                exchange="KOSPI",
                is_active=True,
                krx_trading_suspended=suspended,
            )
        )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol=symbol,
            snapshot_date=snapshot_date,
            latest_close=close,
            prev_close=Decimal("49500"),
            change_amount=close - Decimal("49500"),
            change_rate=Decimal("1.0101"),
            consecutive_up_days=1,
            week_change_rate=Decimal("2.0"),
            closes_window=[49000.0 + i * 50 for i in range(30)],
            daily_volume=1_000_000,
            daily_turnover=turnover,
            market_cap=market_cap,
            market_cap_source=market_cap_source,
            market_cap_snapshot_date=snapshot_date,
            support_price=support_price,
            support_kind="bb_lower,fib_0.618" if support_price is not None else None,
            support_strength="strong" if support_price is not None else None,
            dist_to_support_pct=distance,
            support_computed_at=computed_at,
            source="kis",
            computed_at=computed_at,
        )
    )


@pytest.mark.asyncio
async def test_reads_price_support_and_distance_from_same_persisted_row(
    db_session, monkeypatch
):
    """A newer live price/support must be irrelevant to a snapshot query."""

    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    _seed(
        db_session,
        "976001",
        snapshot_date=dt.date(2026, 7, 20),
        computed_at=now - dt.timedelta(hours=1),
        close=Decimal("50000"),
        support_price=Decimal("49000"),
        distance=Decimal("2.0000"),
    )
    await db_session.commit()

    import app.mcp_server.tooling.fundamentals._support_resistance as sr_module
    import app.mcp_server.tooling.market_data_indicators as indicators

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("snapshot read attempted live/recomputed OHLCV")

    monkeypatch.setattr(sr_module, "get_support_resistance_impl", _must_not_run)
    monkeypatch.setattr(indicators, "_fetch_ohlcv_for_indicators", _must_not_run)

    result = await load_support_proximity_from_snapshots(
        db_session, market="kr", limit=10, now=lambda: now
    )

    assert result is not None
    assert result.partition_computed_at == now - dt.timedelta(hours=1)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["close"] == pytest.approx(50000.0)
    assert row["support_price"] == pytest.approx(49000.0)
    assert row["dist_to_support_pct"] == pytest.approx(2.0)
    assert row["_screener_snapshot_state"] == "fresh"


@pytest.mark.asyncio
async def test_orders_by_stored_distance_and_filters_stored_quality_values(db_session):
    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    for symbol, distance, cap, turnover in (
        ("976001", "4.0", "1000000000000", "50000000000"),
        ("976002", "1.0", "600000000000", "20000000000"),
        ("976003", "0.5", "200000000000", "50000000000"),
        ("976004", "0.2", "800000000000", "500000000"),
    ):
        _seed(
            db_session,
            symbol,
            snapshot_date=dt.date(2026, 7, 20),
            computed_at=now - dt.timedelta(hours=1),
            distance=Decimal(distance),
            market_cap=Decimal(cap),
            turnover=Decimal(turnover),
        )
    await db_session.commit()

    result = await load_support_proximity_from_snapshots(
        db_session,
        market="kr",
        min_market_cap=300_000_000_000,
        min_turnover=1_000_000_000,
        limit=10,
        now=lambda: now,
    )

    assert result is not None
    assert [row["symbol"] for row in result.rows] == ["976002", "976001"]
    assert all(
        row["market_cap_snapshot_source"] == "naver_finance" for row in result.rows
    )


@pytest.mark.asyncio
async def test_support_partition_without_qualifying_rows_is_honest_empty(db_session):
    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    _seed(
        db_session,
        "976001",
        snapshot_date=dt.date(2026, 7, 20),
        computed_at=now,
        market_cap=Decimal("200000000000"),
    )
    await db_session.commit()

    result = await load_support_proximity_from_snapshots(
        db_session, market="kr", now=lambda: now
    )

    assert result is not None
    assert result.rows == []
    assert result.degradation_reason == "healthy_no_matches"


@pytest.mark.asyncio
async def test_latest_evaluated_no_support_partition_supersedes_old_matches(db_session):
    """An honest no-match build must not resurrect an older support artifact."""

    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    _seed(
        db_session,
        "976001",
        snapshot_date=dt.date(2026, 7, 17),
        computed_at=now - dt.timedelta(days=3),
    )
    _seed(
        db_session,
        "976002",
        snapshot_date=dt.date(2026, 7, 20),
        computed_at=now,
        support_price=None,
        distance=None,
    )
    await db_session.commit()

    result = await load_support_proximity_from_snapshots(
        db_session, market="kr", now=lambda: now
    )

    assert result is not None
    assert result.partition_date == dt.date(2026, 7, 20)
    assert result.rows == []
    assert result.degradation_reason == "healthy_no_matches"


@pytest.mark.asyncio
async def test_untrusted_stored_market_cap_source_cannot_pass_quality_gate(db_session):
    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    _seed(
        db_session,
        "976001",
        snapshot_date=dt.date(2026, 7, 20),
        computed_at=now,
        market_cap=Decimal("999000000000000"),
        market_cap_source="invest_kr_fundamentals",
    )
    await db_session.commit()

    result = await load_support_proximity_from_snapshots(
        db_session, market="kr", now=lambda: now
    )

    assert result is not None
    assert result.rows == []
    assert result.degradation_reason == "healthy_no_matches"


@pytest.mark.asyncio
async def test_returns_none_when_no_persisted_support_artifact():
    from unittest.mock import AsyncMock, MagicMock

    no_partition = MagicMock()
    no_partition.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=no_partition)

    result = await load_support_proximity_from_snapshots(session, market="kr")
    assert result is None


@pytest.mark.asyncio
async def test_active_universe_membership_and_suspension_remain_fail_closed(db_session):
    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    _seed(
        db_session,
        "976001",
        snapshot_date=dt.date(2026, 7, 20),
        computed_at=now,
        active=False,
    )
    _seed(
        db_session,
        "976002",
        snapshot_date=dt.date(2026, 7, 20),
        computed_at=now,
        suspended=True,
    )
    await db_session.commit()

    result = await load_support_proximity_from_snapshots(
        db_session, market="kr", now=lambda: now
    )

    assert result is not None
    assert result.rows == []


@pytest.mark.asyncio
async def test_old_partition_is_labeled_stale_from_stored_timestamps(db_session):
    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    _seed(
        db_session,
        "976001",
        snapshot_date=dt.date(2026, 7, 17),
        computed_at=dt.datetime(2026, 7, 17, 12, 0, tzinfo=dt.UTC),
    )
    await db_session.commit()

    result = await load_support_proximity_from_snapshots(
        db_session, market="kr", now=lambda: now
    )

    assert result is not None
    assert result.rows[0]["_screener_snapshot_state"] == "stale"


@pytest.mark.asyncio
async def test_non_kr_is_not_supported(db_session):
    assert await load_support_proximity_from_snapshots(db_session, market="us") is None
