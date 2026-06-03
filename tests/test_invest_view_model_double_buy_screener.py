from __future__ import annotations

import datetime as dt
import decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_view_model.double_buy_screener import (
    load_double_buy_from_snapshots,
)

# Test symbols use a 9-prefix range to stay isolated from real KR symbols and
# from sibling tests that already claim "900xxx" ranges.
_TEST_SYMBOLS = ["911000", "910001", "919999", "912000"]


@pytest_asyncio.fixture(autouse=True)
async def _clean_double_buy_test_rows(db_session):
    """Wipe just the rows this test owns before and after each test.

    The persistent test DB is shared with other suites, so we never TRUNCATE —
    we only delete rows scoped to the synthetic symbols below.
    """

    async def _purge() -> None:
        await db_session.execute(
            sa.delete(InvestorFlowSnapshot).where(
                InvestorFlowSnapshot.symbol.in_(_TEST_SYMBOLS)
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
async def test_returns_rows_filtered_by_double_buy_and_positive_change_rate(
    db_session,
):
    # Use a far-future date to ensure our rows form the latest partition in
    # the shared persistent test DB (other suites seed dates as late as 2099).
    today = dt.date(2099, 12, 31)
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="911000",
                name="진원생명과학",
                exchange="KOSPI",
                is_active=True,
            ),
            KRSymbolUniverse(
                symbol="910001",
                name="제외종목ETF",
                exchange="KOSPI",
                is_active=True,
            ),
        ]
    )
    db_session.add_all(
        [
            InvestorFlowSnapshot(
                market="kr",
                symbol="911000",
                snapshot_date=today,
                foreign_net=1_000_000,
                institution_net=2_000_000,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="910001",
                snapshot_date=today,
                foreign_net=-1,
                institution_net=-1,
                double_buy=False,
                double_sell=True,
                source="naver_finance",
            ),
        ]
    )
    db_session.add_all(
        [
            InvestScreenerSnapshot(
                market="kr",
                symbol="911000",
                snapshot_date=today,
                latest_close=decimal.Decimal("12000"),
                prev_close=decimal.Decimal("10000"),
                change_rate=decimal.Decimal("20.0"),
                daily_volume=100_000,
                closes_window=[10000, 12000],
                source="kis",
            ),
            InvestScreenerSnapshot(
                market="kr",
                symbol="910001",
                snapshot_date=today,
                latest_close=decimal.Decimal("900"),
                prev_close=decimal.Decimal("1000"),
                change_rate=decimal.Decimal("-10.0"),
                daily_volume=50_000,
                closes_window=[1000, 900],
                source="kis",
            ),
        ]
    )
    await db_session.commit()

    result = await load_double_buy_from_snapshots(db_session, market="kr", limit=20)

    assert result is not None
    rows = result.rows
    # Cross-test contamination guard: if other test data exists in the same
    # snapshot date partition, we still expect 911000 to be present and
    # 910001 (negative change_rate) to be filtered out.
    symbols = [r["symbol"] for r in rows]
    assert "911000" in symbols
    assert "910001" not in symbols
    target = next(r for r in rows if r["symbol"] == "911000")
    assert target["change_rate"] == pytest.approx(20.0)
    assert target["double_buy"] is True
    assert target["_screener_snapshot_state"] in {"fresh", "stale"}


@pytest.mark.asyncio
async def test_returns_none_when_no_snapshots():
    """When the latest-date lookup yields NULL, helper must signal `missing`.

    Uses a mocked AsyncSession because the shared test DB has rows owned by
    other suites that we must not delete to satisfy this case.
    """
    from unittest.mock import AsyncMock, MagicMock

    null_scalar = MagicMock()
    null_scalar.scalar_one_or_none.return_value = None

    session = MagicMock()
    session.execute = AsyncMock(return_value=null_scalar)

    result = await load_double_buy_from_snapshots(session, market="kr", limit=20)
    assert result is None


@pytest.mark.asyncio
async def test_excludes_non_common_stock_by_name_heuristic(db_session):
    # Use a far-future date to ensure our rows form the latest partition in
    # the shared persistent test DB (other suites seed dates as late as 2099).
    today = dt.date(2099, 12, 31)
    db_session.add(
        KRSymbolUniverse(
            symbol="919999",
            name="KODEX 200 ETF",
            exchange="KOSPI",
            is_active=True,
        )
    )
    db_session.add(
        InvestorFlowSnapshot(
            market="kr",
            symbol="919999",
            snapshot_date=today,
            foreign_net=10,
            institution_net=10,
            double_buy=True,
            double_sell=False,
            source="naver_finance",
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="919999",
            snapshot_date=today,
            latest_close=decimal.Decimal("10000"),
            prev_close=decimal.Decimal("9000"),
            change_rate=decimal.Decimal("11.0"),
            daily_volume=1,
            closes_window=[9000, 10000],
            source="kis",
        )
    )
    await db_session.commit()

    result = await load_double_buy_from_snapshots(db_session, market="kr", limit=20)
    assert result is not None
    rows = result.rows
    # ETF must not appear; other symbols already in the DB are allowed but
    # 919999 must be excluded by the name heuristic.
    assert all(r["symbol"] != "919999" for r in rows)


@pytest.mark.asyncio
async def test_returns_none_when_market_is_not_kr(db_session):
    # No DB hit expected — short-circuits at the market guard
    result = await load_double_buy_from_snapshots(db_session, market="us", limit=20)
    assert result is None


@pytest.mark.asyncio
async def test_state_is_stale_when_price_snapshot_date_differs_from_flow_snapshot_date(
    db_session,
):
    # Construct a scenario where flow_snapshot_date < price_snapshot_date so the
    # latest-partition lookups return different dates → row should be tagged "stale".
    flow_date = dt.date(2099, 12, 30)  # earlier
    price_date = dt.date(2099, 12, 31)  # later (latest price partition)
    symbol = "912000"

    db_session.add(
        KRSymbolUniverse(
            symbol=symbol, name="테스트종목", is_active=True, exchange="KOSPI"
        )
    )
    db_session.add(
        InvestorFlowSnapshot(
            market="kr",
            symbol=symbol,
            snapshot_date=flow_date,
            foreign_net=1,
            institution_net=1,
            double_buy=True,
            double_sell=False,
            source="naver_finance",
        )
    )
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol=symbol,
            snapshot_date=price_date,
            latest_close=decimal.Decimal("10000"),
            prev_close=decimal.Decimal("9000"),
            change_rate=decimal.Decimal("11.0"),
            daily_volume=1,
            closes_window=[9000, 9500, 9800, 9900, 10000],
            source="kis",
        )
    )
    await db_session.commit()

    result = await load_double_buy_from_snapshots(db_session, market="kr", limit=20)

    assert result is not None
    rows = result.rows
    # find our test symbol (shared DB may have other rows)
    target = next((r for r in rows if r["symbol"] == symbol), None)
    assert target is not None, (
        f"expected {symbol} in results, got {[r['symbol'] for r in rows]}"
    )
    assert target["_screener_snapshot_state"] == "stale"


@pytest.mark.asyncio
async def test_double_buy_returns_snapshot_load_result_with_reason(monkeypatch):
    """Drive flow_hp healthy/non-fallback + price_hp healthy/non-fallback, but
    candidate query yields 0 qualifiers -> healthy_no_matches."""
    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.services.invest_screener_snapshots.partition_health import HealthyPartition
    from app.services.invest_view_model.double_buy_screener import (
        load_double_buy_from_snapshots,
    )
    from tests.test_invest_view_model_screener_service import (
        _FakeExecuteResult,
        _FakeSession,
    )

    flow_hp = HealthyPartition(
        partition_date=dt.date(2026, 6, 3),
        row_count=3800,
        coverage_ratio=1.0,
        is_fallback=False,
        healthy=True,
    )
    price_hp = HealthyPartition(
        partition_date=dt.date(2026, 6, 3),
        row_count=3800,
        coverage_ratio=1.0,
        is_fallback=False,
        healthy=True,
    )

    async def _fake_resolve(session, model, **kwargs):
        if model == InvestorFlowSnapshot:
            return flow_hp
        elif model == InvestScreenerSnapshot:
            return price_hp
        return None

    import app.services.invest_screener_snapshots.partition_health as ph

    monkeypatch.setattr(ph, "resolve_healthy_partition", _fake_resolve)

    # session execute returns 2000 count for universe_count, and then empty list for candidate_stmt
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[2000]),  # active_universe_count
            _FakeExecuteResult(rows=[]),  # candidate query (empty)
        ]
    )

    result = await load_double_buy_from_snapshots(session, market="kr", limit=10)
    assert result is not None
    assert result.rows == []
    assert result.partition_date == dt.date(2026, 6, 3)
    assert result.degradation_reason == "healthy_no_matches"
    assert result.coverage_label is None
