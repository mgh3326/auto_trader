from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
    cap_degraded,
    resolve_healthy_partition,
)

_TEST_DATES = {
    dt.date(2040, 5, 1),
    dt.date(2040, 5, 19),
    dt.date(2040, 5, 20),
    dt.date(2040, 5, 22),
}


async def _cleanup(session) -> None:
    await session.execute(
        sa.delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.snapshot_date.in_(_TEST_DATES)
        )
    )
    await session.flush()


def test_cap_degraded_floors_fresh_and_partial_to_stale():
    assert cap_degraded("fresh") == "stale"
    assert cap_degraded("partial") == "stale"
    assert cap_degraded("stale") == "stale"
    assert cap_degraded("missing") == "missing"
    assert cap_degraded("fallback") == "fallback"


def _snap(symbol: str, snapshot_date: dt.date) -> InvestScreenerSnapshot:
    return InvestScreenerSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=snapshot_date,
        consecutive_up_days=5,
        week_change_rate=1.0,
        change_rate=1.0,
        closes_window=[1, 2, 3, 4, 5],
        computed_at=dt.datetime(2040, 5, 22, 0, 30, tzinfo=dt.UTC),
        source="kis",
        latest_close=10000.0,
    )


async def _seed(session, *, date_counts: dict[dt.date, int]) -> None:
    n = 0
    for d, cnt in date_counts.items():
        for _ in range(cnt):
            n += 1
            session.add(_snap(f"{n:06d}", d))
    await session.flush()


_KW = {
    "model": InvestScreenerSnapshot,
    "date_col": InvestScreenerSnapshot.snapshot_date,
    "market_col": InvestScreenerSnapshot.market,
    "market": "kr",
}


@pytest.mark.asyncio
async def test_resolve_latest_healthy(db_session):
    await _cleanup(db_session)
    d = dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={d: 60})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == d
    assert hp.healthy is True and hp.is_fallback is False


@pytest.mark.asyncio
async def test_resolve_thin_latest_falls_back_to_older_healthy(db_session):
    await _cleanup(db_session)
    older, newer = dt.date(2040, 5, 19), dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={older: 60, newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == older
    assert hp.healthy is True and hp.is_fallback is True


@pytest.mark.asyncio
async def test_resolve_all_thin_serves_newest_as_last_resort(db_session):
    await _cleanup(db_session)
    older, newer = dt.date(2040, 5, 19), dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={older: 3, newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == newer  # NOT None
    assert hp.healthy is False and hp.is_fallback is False
    assert hp.row_count == 5


# ── ROB-551: source-aware partition selection for MarketValuationSnapshot ──

_VAL_DATES = {dt.date(2041, 6, 11), dt.date(2041, 6, 12)}


async def _cleanup_val(session) -> None:
    from app.models.market_valuation_snapshot import MarketValuationSnapshot

    await session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.snapshot_date.in_(_VAL_DATES)
        )
    )
    await session.flush()


async def _seed_val(
    session, *, snapshot_date: dt.date, count: int, sparse: bool, source: str
) -> None:
    from decimal import Decimal

    from app.models.market_valuation_snapshot import MarketValuationSnapshot

    for i in range(count):
        kwargs = {
            "market": "kr",
            "symbol": f"{source[:2]}{snapshot_date.day:02d}{i:04d}",
            "snapshot_date": snapshot_date,
            "source": source,
            "market_cap": Decimal("1000000"),
        }
        if not sparse:
            kwargs.update(
                per=Decimal("11"),
                pbr=Decimal("1.2"),
                roe=Decimal("0.15"),
                dividend_yield=Decimal("0.02"),
            )
        session.add(MarketValuationSnapshot(**kwargs))
    await session.flush()


@pytest.mark.asyncio
async def test_metric_rich_row_filter_skips_toss_only_partition(db_session):
    """ROB-551: a newer metric-sparse toss-only partition (market_cap only) must
    not be selected as the screener val_date; the metric-rich naver partition on
    the older date wins when row_filter counts only metric-rich rows."""
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.market_valuation_snapshots.repository import metric_rich_filter

    await _cleanup_val(db_session)
    older, newer = dt.date(2041, 6, 11), dt.date(2041, 6, 12)
    # older: 60 metric-rich naver rows (clears floor). newer: 60 metric-sparse
    # toss rows (clears floor by TOTAL count, but 0 metric-rich).
    await _seed_val(
        db_session, snapshot_date=older, count=60, sparse=False, source="naver_finance"
    )
    await _seed_val(
        db_session, snapshot_date=newer, count=60, sparse=True, source="toss_openapi"
    )

    kw = {
        "model": MarketValuationSnapshot,
        "date_col": MarketValuationSnapshot.snapshot_date,
        "market_col": MarketValuationSnapshot.market,
        "market": "kr",
    }

    # Without the filter (legacy behavior), the newer toss-only partition wins.
    hp_default = await resolve_healthy_partition(db_session, universe_count=100, **kw)
    assert hp_default is not None and hp_default.partition_date == newer

    # With the metric-rich filter, the toss-only partition has 0 qualifying rows
    # and is skipped; the metric-rich older partition is served.
    hp_filtered = await resolve_healthy_partition(
        db_session, universe_count=100, row_filter=metric_rich_filter(), **kw
    )
    assert hp_filtered is not None and hp_filtered.partition_date == older
    assert hp_filtered.healthy is True and hp_filtered.is_fallback is True


@pytest.mark.asyncio
async def test_resolve_empty_table_returns_none():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.execute.return_value = mock_result

    hp = await resolve_healthy_partition(mock_session, universe_count=100, **_KW)
    assert hp is None


@pytest.mark.asyncio
async def test_resolve_universe_zero_disables_gate(db_session):
    await _cleanup(db_session)
    newer = dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=0, **_KW)
    assert hp is not None and hp.partition_date == newer
    assert hp.healthy is True


@pytest.mark.asyncio
async def test_resolve_scan_back_bound_does_not_reach_distant_healthy(db_session):
    await _cleanup(db_session)
    # Newest 2 are thin; a healthy partition exists but beyond max_scan_back=2.
    healthy_far = dt.date(2040, 5, 1)
    thin1, thin2 = dt.date(2040, 5, 20), dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={healthy_far: 60, thin1: 5, thin2: 5})
    hp = await resolve_healthy_partition(
        db_session, universe_count=100, max_scan_back=2, **_KW
    )
    assert hp is not None and hp.partition_date == thin2  # last resort, not healthy_far
    assert hp.healthy is False


@pytest.mark.asyncio
async def test_active_universe_count_counts_active_kr(db_session):
    from app.models.kr_symbol_universe import KRSymbolUniverse

    initial_count = await active_universe_count(db_session, market="kr")

    symbol_1 = "999901"
    symbol_2 = "999902"

    # Cleanup these symbols in case they exist from aborted runs
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_({symbol_1, symbol_2})
        )
    )
    await db_session.flush()

    db_session.add(
        KRSymbolUniverse(symbol=symbol_1, name="A", exchange="KRX", is_active=True)
    )
    db_session.add(
        KRSymbolUniverse(symbol=symbol_2, name="B", exchange="KRX", is_active=False)
    )
    await db_session.flush()

    assert await active_universe_count(db_session, market="kr") == initial_count + 1

    # Cleanup after test
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_({symbol_1, symbol_2})
        )
    )
    await db_session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_universe_count_us_counts_common_stock_only(db_session):
    # ROB-440: US coverage denominator must count common stocks (the build scope),
    # not the full active universe (incl ETFs). Otherwise a complete common-stock
    # build is mislabeled below-floor → partition degraded → spurious stale/"준비중".
    from app.models.us_symbol_universe import USSymbolUniverse

    syms = ["ZZC1", "ZZC2", "ZZETF", "ZZINA"]
    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()

    before = await active_universe_count(db_session, market="us")
    db_session.add_all(
        [
            USSymbolUniverse(
                symbol="ZZC1", exchange="NASDAQ", is_active=True, is_common_stock=True
            ),
            USSymbolUniverse(
                symbol="ZZC2", exchange="NYSE", is_active=True, is_common_stock=True
            ),
            USSymbolUniverse(
                symbol="ZZETF", exchange="NYSE", is_active=True, is_common_stock=False
            ),
            USSymbolUniverse(
                symbol="ZZINA", exchange="NASDAQ", is_active=False, is_common_stock=True
            ),
        ]
    )
    await db_session.commit()

    after = await active_universe_count(db_session, market="us")
    # only the 2 active common stocks add to the count (ETF + inactive excluded)
    assert after == before + 2

    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()


def test_served_partition_degraded_healthy_fallback_not_degraded():
    # ROB-440: a HEALTHY fallback (is_fallback=True, healthy=True) is NOT degraded —
    # resolver correctly served the latest healthy partition over a thin raw-latest.
    from app.services.invest_screener_snapshots.partition_health import (
        HealthyPartition,
        served_partition_degraded,
    )

    healthy_fallback = HealthyPartition(
        partition_date=dt.date(2026, 6, 5),
        row_count=4926,
        coverage_ratio=0.96,
        is_fallback=True,
        healthy=True,
    )
    assert served_partition_degraded(healthy_fallback) is False

    unhealthy = HealthyPartition(
        partition_date=dt.date(2026, 6, 6),
        row_count=388,
        coverage_ratio=0.0758,
        is_fallback=False,
        healthy=False,
    )
    assert served_partition_degraded(unhealthy) is True
    assert served_partition_degraded(None) is False
