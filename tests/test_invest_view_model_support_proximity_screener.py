from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.invest_view_model.support_proximity_screener import (
    load_support_proximity_from_snapshots,
)

# Test symbols use a 976-prefix range (ROB-976), unclaimed by sibling suites.
_TEST_SYMBOLS = ["976001", "976002", "976003", "976004", "976005"]


@pytest_asyncio.fixture(autouse=True)
async def _clean_support_proximity_test_rows(db_session, monkeypatch):
    """Wipe just the rows this test owns before and after each test, and pin
    partition resolution to the raw latest partition (see
    test_invest_view_model_double_buy_screener.py for the same trick — the
    shared persistent test DB may have other suites' partitions/coverage that
    would otherwise make resolve_healthy_partition pick a different date)."""
    from app.services.invest_screener_snapshots import partition_health
    from app.services.invest_screener_snapshots.partition_health import (
        HealthyPartition,
    )

    async def _resolve_raw_latest_partition(
        session, *, model, date_col, market_col, market, **_kwargs
    ):
        newest = (
            await session.execute(
                sa.select(sa.func.max(date_col)).where(market_col == market)
            )
        ).scalar_one_or_none()
        if newest is None:
            return None
        row_count = int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(model)
                    .where(market_col == market, date_col == newest)
                )
            ).scalar()
            or 0
        )
        return HealthyPartition(
            partition_date=newest,
            row_count=row_count,
            coverage_ratio=1.0,
            is_fallback=False,
            healthy=True,
        )

    monkeypatch.setattr(
        partition_health, "resolve_healthy_partition", _resolve_raw_latest_partition
    )

    async def _purge() -> None:
        await db_session.execute(
            sa.delete(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.symbol.in_(_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            sa.delete(MarketValuationSnapshot).where(
                MarketValuationSnapshot.symbol.in_(_TEST_SYMBOLS)
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


def _patch_support_resistance(
    monkeypatch, fake_supports: dict[str, list[dict[str, Any]]]
):
    """Patch get_support_resistance_impl to a canned per-symbol response and
    return the list of symbols it was actually called for (so tests can assert
    the live fan-out was bounded to the quality-filtered candidates only)."""
    import app.mcp_server.tooling.fundamentals._support_resistance as sr_module

    called: list[str] = []

    async def _fake_impl(symbol: str, market: str | None = None, preloaded_df=None):
        called.append(symbol)
        supports = fake_supports.get(symbol, [])
        return {
            "symbol": symbol,
            "current_price": 50000.0,
            "supports": supports,
            "resistances": [],
        }

    monkeypatch.setattr(sr_module, "get_support_resistance_impl", _fake_impl)
    return called


def _seed_common(db_session, symbol: str, *, today: dt.date, market_cap: float) -> None:
    db_session.add(
        KRSymbolUniverse(
            symbol=symbol, name=f"테스트근접{symbol}", exchange="KOSPI", is_active=True
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol=symbol,
            snapshot_date=today,
            latest_close=decimal.Decimal("50000"),
            prev_close=decimal.Decimal("49000"),
            change_rate=decimal.Decimal("2.0"),
            change_amount=decimal.Decimal("1000"),
            daily_volume=1_000_000,  # turnover = 50,000,000,000 (500억) — above floor
            closes_window=[49000, 50000],
            source="kis",
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol=symbol,
            snapshot_date=today,
            market_cap=decimal.Decimal(str(market_cap)),
            per=decimal.Decimal("10.0"),
            source="naver_finance",
        )
    )


@pytest.mark.asyncio
async def test_ranks_by_distance_and_applies_quality_and_empty_support_filters(
    db_session, monkeypatch
):
    today = dt.date(2099, 12, 31)

    # 976001: 1조 market cap, nearest support -2.0% away -> closest, ranked first.
    _seed_common(db_session, "976001", today=today, market_cap=1_000_000_000_000.0)
    # 976002: 5천억 market cap, nearest support -8.0% away -> ranked second.
    _seed_common(db_session, "976002", today=today, market_cap=500_000_000_000.0)
    # 976003: below the 3천억 market-cap floor -> excluded before the live fan-out.
    _seed_common(db_session, "976003", today=today, market_cap=100_000_000_000.0)
    # 976004: passes quality filters but has no support below current price
    # (ROB-976: the supports=[] case observed live on 07-20) -> excluded, no crash.
    _seed_common(db_session, "976004", today=today, market_cap=400_000_000_000.0)
    # 976005: high market cap but below the turnover floor -> excluded.
    db_session.add(
        KRSymbolUniverse(
            symbol="976005", name="테스트근접976005", exchange="KOSPI", is_active=True
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="976005",
            snapshot_date=today,
            latest_close=decimal.Decimal("50000"),
            prev_close=decimal.Decimal("49000"),
            change_rate=decimal.Decimal("2.0"),
            change_amount=decimal.Decimal("1000"),
            daily_volume=100,  # turnover = 5,000,000 — far below the 10억 floor
            closes_window=[49000, 50000],
            source="kis",
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="976005",
            snapshot_date=today,
            market_cap=decimal.Decimal("1000000000000"),
            per=decimal.Decimal("10.0"),
            source="naver_finance",
        )
    )
    await db_session.commit()

    called = _patch_support_resistance(
        monkeypatch,
        {
            "976001": [
                {
                    "price": 49000.0,
                    "strength": "strong",
                    "sources": ["bb_lower", "fib_0.618"],
                    "distance_pct": -2.0,
                }
            ],
            "976002": [
                {
                    "price": 46000.0,
                    "strength": "weak",
                    "sources": ["fib_0.5"],
                    "distance_pct": -8.0,
                }
            ],
            "976004": [],  # empty supports — must be excluded, never crash
        },
    )

    result = await load_support_proximity_from_snapshots(
        db_session,
        market="kr",
        limit=10,
        min_market_cap=300_000_000_000.0,
        min_turnover=1_000_000_000.0,
    )

    assert result is not None
    rows = result.rows
    symbols = [r["symbol"] for r in rows]
    assert symbols == ["976001", "976002"]

    first = rows[0]
    assert first["dist_to_support_pct"] == pytest.approx(2.0)
    assert first["support_price"] == pytest.approx(49000.0)
    assert first["support_strength"] == "strong"
    assert "bb_lower" in first["support_kind"]
    assert first["close"] == pytest.approx(50000.0)
    assert first["market_cap"] == pytest.approx(1_000_000_000_000.0)

    second = rows[1]
    assert second["dist_to_support_pct"] == pytest.approx(8.0)

    # The quality pre-filter (market cap / turnover) must bound the live
    # get_support_resistance fan-out — 976003/976005 should never be checked.
    assert "976003" not in called
    assert "976005" not in called
    assert set(called) == {"976001", "976002", "976004"}


@pytest.mark.asyncio
async def test_returns_none_when_market_is_not_kr(db_session):
    result = await load_support_proximity_from_snapshots(
        db_session, market="us", limit=20
    )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_no_snapshots():
    """When the latest-date lookup yields NULL, the loader must signal `missing`
    (None), mirroring double_buy's mocked-session test."""
    from unittest.mock import AsyncMock, MagicMock

    null_scalar = MagicMock()
    null_scalar.scalar_one_or_none.return_value = None

    session = MagicMock()
    session.execute = AsyncMock(return_value=null_scalar)

    result = await load_support_proximity_from_snapshots(session, market="kr", limit=20)
    assert result is None


@pytest.mark.asyncio
async def test_returns_empty_when_all_candidates_lack_a_support(
    db_session, monkeypatch
):
    today = dt.date(2099, 12, 31)
    _seed_common(db_session, "976001", today=today, market_cap=1_000_000_000_000.0)
    await db_session.commit()

    called = _patch_support_resistance(monkeypatch, {"976001": []})

    result = await load_support_proximity_from_snapshots(
        db_session,
        market="kr",
        limit=10,
        min_market_cap=300_000_000_000.0,
        min_turnover=1_000_000_000.0,
    )

    assert result is not None
    assert result.rows == []
    assert result.degradation_reason == "healthy_no_matches"
    assert called == ["976001"]


@pytest.mark.asyncio
async def test_displayed_price_uses_live_reverified_basis_not_stale_snapshot(
    db_session, monkeypatch
):
    """ROB-976 verify R1 [BLOCKER]: price/support/distance must all come from
    the SAME live get_support_resistance call. Using the stale snapshot price
    for display while the distance is computed against a different live price
    caused a real cross-checked bug (a support above the displayed price still
    showing as a "support" — KT&G 033780 in the 07-20 verify report)."""
    today = dt.date(2099, 12, 31)
    _seed_common(db_session, "976001", today=today, market_cap=1_000_000_000_000.0)
    await db_session.commit()

    import app.mcp_server.tooling.fundamentals._support_resistance as sr_module

    async def _fake_impl(symbol: str, market: str | None = None, preloaded_df=None):
        # Live price (48000) deliberately differs from the seeded snapshot's
        # latest_close (50000) — simulates intraday drift since the snapshot.
        return {
            "symbol": symbol,
            "current_price": 48000.0,
            "supports": [
                {
                    "price": 47000.0,
                    "strength": "moderate",
                    "sources": ["bb_lower"],
                    "distance_pct": -2.08,
                }
            ],
            "resistances": [],
        }

    monkeypatch.setattr(sr_module, "get_support_resistance_impl", _fake_impl)

    result = await load_support_proximity_from_snapshots(
        db_session,
        market="kr",
        limit=10,
        min_market_cap=300_000_000_000.0,
        min_turnover=1_000_000_000.0,
    )

    assert result is not None
    assert len(result.rows) == 1
    row = result.rows[0]
    # Displayed price must be the LIVE price, not the stale snapshot price —
    # otherwise a level below the live price could render as "above" the
    # (stale) displayed price, or vice versa.
    assert row["close"] == pytest.approx(48000.0)
    assert row["latest_close"] == pytest.approx(48000.0)
    assert row["close"] != pytest.approx(50000.0)
    assert row["support_price"] == pytest.approx(47000.0)
    assert row["dist_to_support_pct"] == pytest.approx(2.08)
    # change_amount/change_rate recomputed against the live price, anchored on
    # the snapshot's prev_close (yesterday's settled close — not time-sensitive).
    assert row["change_amount"] == pytest.approx(48000.0 - 49000.0)
    assert row["computed_at"] is not None
    assert row["_screener_snapshot_state"] == "fresh"
    assert result.partition_computed_at is not None


@pytest.mark.asyncio
async def test_excludes_candidates_not_in_active_universe(db_session, monkeypatch):
    """ROB-976 verify R1 [HIGH]: a symbol absent from the ACTIVE KRSymbolUniverse
    query (empty/stale active universe, delisted, etc.) must be excluded — never
    fall through to the permissive 'unknown name -> allow' heuristic, which
    previously let a stale snapshot-only symbol appear as a candidate even when
    the active universe was empty."""
    today = dt.date(2099, 12, 31)
    # Price + valuation snapshot rows exist, but NO active KRSymbolUniverse row
    # is seeded for this symbol.
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="976001",
            snapshot_date=today,
            latest_close=decimal.Decimal("50000"),
            prev_close=decimal.Decimal("49000"),
            change_rate=decimal.Decimal("2.0"),
            change_amount=decimal.Decimal("1000"),
            daily_volume=1_000_000,
            closes_window=[49000.0, 50000.0],
            source="kis",
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="976001",
            snapshot_date=today,
            market_cap=decimal.Decimal("1000000000000"),
            per=decimal.Decimal("10.0"),
            source="naver_finance",
        )
    )
    await db_session.commit()

    called = _patch_support_resistance(monkeypatch, {})

    result = await load_support_proximity_from_snapshots(
        db_session,
        market="kr",
        limit=10,
        min_market_cap=300_000_000_000.0,
        min_turnover=1_000_000_000.0,
    )

    assert result is not None
    assert result.rows == []
    # Never reached the live fan-out — excluded before stage 2.
    assert called == []


@pytest.mark.asyncio
async def test_excludes_krx_trading_suspended_symbol(db_session, monkeypatch):
    """ROB-976 verify R1 [HIGH]: KRSymbolUniverse.krx_trading_suspended must
    gate candidates (via _is_toss_common_stock_row), not just the bare
    name-heuristic that ignores suspension entirely."""
    today = dt.date(2099, 12, 31)
    db_session.add(
        KRSymbolUniverse(
            symbol="976001",
            name="정지종목",
            exchange="KOSPI",
            is_active=True,
            krx_trading_suspended=True,
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="976001",
            snapshot_date=today,
            latest_close=decimal.Decimal("50000"),
            prev_close=decimal.Decimal("49000"),
            change_rate=decimal.Decimal("2.0"),
            change_amount=decimal.Decimal("1000"),
            daily_volume=1_000_000,
            closes_window=[49000.0, 50000.0],
            source="kis",
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="976001",
            snapshot_date=today,
            market_cap=decimal.Decimal("1000000000000"),
            per=decimal.Decimal("10.0"),
            source="naver_finance",
        )
    )
    await db_session.commit()

    called = _patch_support_resistance(monkeypatch, {})

    result = await load_support_proximity_from_snapshots(
        db_session,
        market="kr",
        limit=10,
        min_market_cap=300_000_000_000.0,
        min_turnover=1_000_000_000.0,
    )

    assert result is not None
    assert result.rows == []
    assert called == []


@pytest.mark.asyncio
async def test_live_verification_pool_is_selected_by_snapshot_proxy_not_market_cap(
    db_session, monkeypatch
):
    """ROB-976 verify R1 [BLOCKER]: stage-1 candidate ranking (who gets the
    expensive live re-check) must come from the snapshot-only Bollinger proxy —
    not simply 'top by market cap' — so 'live-verify only the top candidates'
    genuinely means the most support-proximate ones, not just the biggest."""
    today = dt.date(2099, 12, 31)

    # Candidate A: smaller market cap, but flat closes put it right on its
    # snapshot-only Bollinger lower band (near-zero cheap-proxy distance).
    db_session.add(
        KRSymbolUniverse(
            symbol="976001", name="테스트A", exchange="KOSPI", is_active=True
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="976001",
            snapshot_date=today,
            latest_close=decimal.Decimal("50000"),
            prev_close=decimal.Decimal("50000"),
            change_rate=decimal.Decimal("0.0"),
            change_amount=decimal.Decimal("0"),
            daily_volume=1_000_000,
            closes_window=[50000.0] * 20,
            source="kis",
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="976001",
            snapshot_date=today,
            market_cap=decimal.Decimal("500000000000"),
            per=decimal.Decimal("10.0"),
            source="naver_finance",
        )
    )

    # Candidate B: bigger market cap, but a volatile closes_window puts the
    # Bollinger band far from the latest close (poor snapshot-only proxy).
    db_session.add(
        KRSymbolUniverse(
            symbol="976002", name="테스트B", exchange="KOSPI", is_active=True
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="976002",
            snapshot_date=today,
            latest_close=decimal.Decimal("59000"),
            prev_close=decimal.Decimal("58000"),
            change_rate=decimal.Decimal("1.7"),
            change_amount=decimal.Decimal("1000"),
            daily_volume=1_000_000,
            closes_window=[float(40000 + i * 1000) for i in range(20)],
            source="kis",
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="976002",
            snapshot_date=today,
            market_cap=decimal.Decimal("2000000000000"),
            per=decimal.Decimal("10.0"),
            source="naver_finance",
        )
    )
    await db_session.commit()

    called = _patch_support_resistance(
        monkeypatch,
        {
            "976001": [
                {
                    "price": 49500.0,
                    "strength": "strong",
                    "sources": ["bb_lower"],
                    "distance_pct": -1.0,
                }
            ],
            "976002": [
                {
                    "price": 55000.0,
                    "strength": "weak",
                    "sources": ["bb_lower"],
                    "distance_pct": -6.8,
                }
            ],
        },
    )

    result = await load_support_proximity_from_snapshots(
        db_session,
        market="kr",
        limit=1,
        min_market_cap=300_000_000_000.0,
        min_turnover=1_000_000_000.0,
        candidate_pool_limit=1,
    )

    assert result is not None
    # Only the better snapshot-only-proxy candidate (976001) should have been
    # live-checked, even though 976002 has 4x the market cap. Pool size is
    # max(candidate_pool_limit, limit); both are 1 here so the bound is exact.
    assert called == ["976001"]
    assert [r["symbol"] for r in result.rows] == ["976001"]
