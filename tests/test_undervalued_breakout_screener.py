# tests/test_undervalued_breakout_screener.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.invest_view_model.undervalued_breakout_screener import (
    _near_high_proximity,
    _passes_near_high,
    load_undervalued_breakout_from_snapshots,
)

# All symbols used in this file's integration tests. They MUST all be in the
# autouse purge list — these tests seed a far-future snapshot_date (2099-12-31),
# so any leaked row poisons MAX(snapshot_date) for KR valuation-partition queries
# in OTHER test files (persistent shared test DB).
_TEST_SYMBOLS = [
    "907001",
    "907002",
    "907003",
    "907004",
    "907021",
    "907022",
    "907023",
    "907024",
    "907031",
]


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


def test_near_high_proximity_and_pass():
    # within 5% of 52w high → passes
    assert _near_high_proximity(Decimal("95"), Decimal("100")) == Decimal("0.95")
    assert _passes_near_high(Decimal("95"), Decimal("100"), Decimal("0.95")) is True
    # 10% below high → fails
    assert _passes_near_high(Decimal("90"), Decimal("100"), Decimal("0.95")) is False
    # NULL close or high → fail-closed (cannot judge 신고가)
    assert _near_high_proximity(None, Decimal("100")) is None
    assert _passes_near_high(None, Decimal("100"), Decimal("0.95")) is False
    assert _passes_near_high(Decimal("95"), None, Decimal("0.95")) is False
    # close above 52w high (new high) → passes
    assert _passes_near_high(Decimal("105"), Decimal("100"), Decimal("0.95")) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_filters_per_pbr_and_near_high(db_session):
    vd = dt.date(2099, 12, 31)
    syms = ["907001", "907002", "907003", "907004"]
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(InvestScreenerSnapshot.symbol.in_(syms))
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()
    # 907001: per 8, pbr 0.8, close 96/high 100 → near high (0.96) → INCLUDED
    # 907002: per 8, pbr 0.8, close 80/high 100 → 0.80 < 0.95 → excluded (not near high)
    # 907003: per 20 (> 10) → excluded at SQL candidate stage
    # 907004: per 8, pbr 0.8, NO price row → close NULL → fail-closed excluded
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol="907001",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("8"),
                pbr=Decimal("0.8"),
                high_52w=Decimal("100"),
                market_cap=Decimal("5e11"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="907002",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("8"),
                pbr=Decimal("0.8"),
                high_52w=Decimal("100"),
                market_cap=Decimal("4e11"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="907003",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("20"),
                pbr=Decimal("0.8"),
                high_52w=Decimal("100"),
                market_cap=Decimal("3e11"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="907004",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("8"),
                pbr=Decimal("0.8"),
                high_52w=Decimal("100"),
                market_cap=Decimal("2e11"),
            ),
        ]
    )
    db_session.add_all(
        [
            InvestScreenerSnapshot(
                market="kr",
                symbol="907001",
                snapshot_date=vd,
                latest_close=Decimal("96"),
                closes_window=[],
                source="kis",
            ),
            InvestScreenerSnapshot(
                market="kr",
                symbol="907002",
                snapshot_date=vd,
                latest_close=Decimal("80"),
                closes_window=[],
                source="kis",
            ),
            InvestScreenerSnapshot(
                market="kr",
                symbol="907003",
                snapshot_date=vd,
                latest_close=Decimal("99"),
                closes_window=[],
                source="kis",
            ),
        ]
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=s, name=f"종목{s}", exchange="KOSPI", is_active=True
            )
            for s in syms
        ]
    )
    await db_session.commit()

    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="kr", limit=20, today_market_date=vd
    )
    assert rows is not None
    assert [r["symbol"] for r in rows] == [
        "907001"
    ]  # only near-high cheap value survives


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_returns_none_without_valuation_partition(db_session):
    # ROB-440 Part 2: kr+us are valid now; crypto (unsupported market) short-circuits.
    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="crypto", limit=20, today_market_date=dt.date(2026, 6, 2)
    )
    assert rows is None  # non-{kr,us} short-circuits


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_ranks_by_proximity_desc_then_per_asc(db_session):
    # spec §5.2: rank by 신고가 근접도(close/high_52w) desc, tiebreak per asc.
    vd = dt.date(2099, 12, 31)
    syms = ["907021", "907022", "907023", "907024"]
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(InvestScreenerSnapshot.symbol.in_(syms))
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()
    # proximity = close/high(=100): A 0.99, B 0.96, C 0.97 (per 8), D 0.97 (per 5)
    # expected order: A(0.99) > {D,C both 0.97 → per asc: D(5) then C(8)} > B(0.96)
    specs = [
        ("907021", "8", "99"),
        ("907022", "8", "96"),
        ("907023", "8", "97"),
        ("907024", "5", "97"),
    ]
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol=s,
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal(per),
                pbr=Decimal("0.8"),
                high_52w=Decimal("100"),
                market_cap=Decimal("5e11"),
            )
            for s, per, _close in specs
        ]
    )
    db_session.add_all(
        [
            InvestScreenerSnapshot(
                market="kr",
                symbol=s,
                snapshot_date=vd,
                latest_close=Decimal(close),
                closes_window=[],
                source="kis",
            )
            for s, _per, close in specs
        ]
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=s, name=f"종목{s}", exchange="KOSPI", is_active=True
            )
            for s in syms
        ]
    )
    await db_session.commit()

    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="kr", limit=20, today_market_date=vd
    )
    assert rows is not None
    assert [r["symbol"] for r in rows] == ["907021", "907024", "907023", "907022"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_us_date_recency_passes(db_session, monkeypatch):
    # ROB-440 PR3: US uses date-recency (a NEW 52-week high within 20 XNYS trading
    # sessions of the partition), NOT the price-proximity proxy. latest_close=80 vs
    # high_52w=100 (proximity 0.80) would FAIL the old >=0.95 rule — so passing here
    # proves the switch to date-recency. In-range XNYS dates (2099 is out of range).
    vd = dt.date(2026, 6, 2)
    high_date = dt.date(2026, 5, 20)  # ~9 XNYS sessions before vd → recent new high
    sym = "907031"
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == sym)
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(InvestScreenerSnapshot.symbol == sym)
    )
    await db_session.commit()
    db_session.add(
        MarketValuationSnapshot(
            market="us",
            symbol=sym,
            snapshot_date=vd,
            source="yahoo",
            per=Decimal("8"),
            pbr=Decimal("0.8"),
            high_52w=Decimal("100"),
            high_52w_date=high_date,
            market_cap=Decimal("5e11"),
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="us",
            symbol=sym,
            snapshot_date=vd,
            latest_close=Decimal("80"),  # proximity 0.80 < 0.95 (old rule would reject)
            closes_window=[],
            source="kis",
        )
    )
    await db_session.commit()

    from app.services.invest_screener_snapshots import partition_health
    from app.services.invest_screener_snapshots.partition_health import (
        HealthyPartition,
    )

    async def _resolve_test_partition(*_args, **_kwargs):
        return HealthyPartition(
            partition_date=vd,
            row_count=1,
            coverage_ratio=1.0,
            is_fallback=False,
            healthy=True,
        )

    monkeypatch.setattr(
        partition_health,
        "resolve_healthy_partition",
        _resolve_test_partition,
    )

    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="us", limit=20, today_market_date=vd
    )
    assert rows is not None
    row = next(r for r in rows if r["symbol"] == sym)
    assert row["market"] == "us"  # market threaded through
    assert row["new_high_age_trading_days"] is not None
    assert row["new_high_age_trading_days"] <= 20  # recent new high


@pytest.mark.unit
def test_new_high_age_trading_days_xnys() -> None:
    from app.services.invest_view_model.undervalued_breakout_screener import (
        _new_high_age_trading_days,
    )

    part = dt.date(2026, 6, 2)
    # ~9 trading sessions earlier → small, in-range age.
    age = _new_high_age_trading_days(dt.date(2026, 5, 20), part, "us")
    assert age is not None and 0 < age <= 20
    # same day → age 0 (a new high made today is the most recent).
    assert _new_high_age_trading_days(part, part, "us") == 0
    # future high date → None (fail-closed, not "recent").
    assert _new_high_age_trading_days(dt.date(2026, 6, 30), part, "us") is None
    # missing date → None.
    assert _new_high_age_trading_days(None, part, "us") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_dedups_symbol_across_multiple_sources(db_session):
    # Defensive: same KR symbol under two valuation sources must yield ONE row.
    vd = dt.date(2099, 12, 31)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol == "907031"
        )
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol == "907031"
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == "907031")
    )
    await db_session.commit()
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol="907031",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("8"),
                pbr=Decimal("0.8"),
                high_52w=Decimal("100"),
                market_cap=Decimal("5e11"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="907031",
                snapshot_date=vd,
                source="yahoo",
                per=Decimal("8"),
                pbr=Decimal("0.8"),
                high_52w=Decimal("100"),
                market_cap=Decimal("5e11"),
            ),
        ]
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="907031",
            snapshot_date=vd,
            latest_close=Decimal("99"),
            closes_window=[],
            source="kis",
        )
    )
    db_session.add(
        KRSymbolUniverse(
            symbol="907031", name="종목907031", exchange="KOSPI", is_active=True
        )
    )
    await db_session.commit()

    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="kr", limit=20, today_market_date=vd
    )
    assert rows is not None
    assert [r["symbol"] for r in rows].count("907031") == 1  # deduped


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rows_carry_close_alias_for_price_label(db_session) -> None:
    """ROB-508: priceLabel은 row['close']를 읽으므로 latest_close와 동일 값의
    close 키가 있어야 한다."""
    val_date = dt.date(2026, 6, 2)
    db_session.add_all([
        MarketValuationSnapshot(
            market="us",
            symbol="ZZUSHI",
            snapshot_date=val_date,
            source="yahoo",
            per=Decimal("7.0"),
            pbr=Decimal("0.8"),
            high_52w=Decimal("100"),
            high_52w_date=val_date,
            market_cap=Decimal("5000000000"),
        ),
        InvestScreenerSnapshot(
            market="us",
            symbol="ZZUSHI",
            snapshot_date=val_date,
            latest_close=Decimal("99"),
            closes_window=[99],
            source="yahoo",
        )
    ])
    await db_session.commit()
    rows = await load_undervalued_breakout_from_snapshots(
        db_session, market="us", limit=20, today_market_date=val_date
    )
    # clean up
    await db_session.execute(
        MarketValuationSnapshot.__table__.delete().where(
            MarketValuationSnapshot.symbol == "ZZUSHI"
        )
    )
    await db_session.execute(
        InvestScreenerSnapshot.__table__.delete().where(
            InvestScreenerSnapshot.symbol == "ZZUSHI"
        )
    )
    await db_session.commit()

    assert rows
    assert rows[0]["close"] == rows[0]["latest_close"]
