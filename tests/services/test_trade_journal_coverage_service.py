# tests/services/test_trade_journal_coverage_service.py
from unittest.mock import AsyncMock, patch

import pytest

from app.services.trade_journal_coverage_service import TradeJournalCoverageService


@pytest.fixture(autouse=True)
def mock_external_clients():
    with (
        patch(
            "app.services.merged_portfolio_service.KISClient", autospec=True
        ) as mock_kis,
        patch(
            "app.services.trade_journal_coverage_service.upbit_client", autospec=True
        ) as mock_upbit,
        patch(
            "app.services.merged_portfolio_service.get_usd_krw_rate",
            AsyncMock(return_value=1350.0),
        ),
    ):
        mock_kis.return_value.fetch_my_stocks = AsyncMock(return_value=[])
        mock_kis.return_value.fetch_my_overseas_stocks = AsyncMock(return_value=[])
        mock_upbit.fetch_my_coins = AsyncMock(return_value=[])
        mock_upbit.fetch_multiple_current_prices = AsyncMock(return_value={})
        yield mock_kis, mock_upbit


@pytest.mark.asyncio
async def test_holding_with_active_journal_is_present(
    db_session, user, seed_holding_005930, seed_active_journal_005930
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=user.id)
    rows = {r.symbol: r for r in resp.rows}
    assert rows["005930"].journal_status == "present"
    assert rows["005930"].thesis is not None


@pytest.mark.asyncio
async def test_holding_without_journal_is_missing(
    db_session, user, seed_holding_aapl
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=user.id)
    rows = {r.symbol: r for r in resp.rows}
    row = rows["TESTAAPLNOJOURNAL"]
    assert row.journal_status == "missing"
    assert row.thesis is None


@pytest.mark.asyncio
async def test_market_filter_restricts_results(
    db_session, user, seed_holding_005930, seed_holding_aapl
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=user.id, market_filter="KR")
    assert {r.symbol for r in resp.rows} == {"005930"}


@pytest.mark.asyncio
async def test_thesis_conflict_when_summary_decision_is_sell_and_journal_active(
    db_session,
    user,
    seed_holding_005930,
    seed_active_journal_005930,
    seed_summary_sell_005930,
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=user.id)
    row = resp.rows[0]
    assert row.thesis_conflict_with_summary is True
    assert row.latest_summary_decision == "sell"


@pytest.mark.asyncio
async def test_journal_updated_before_summary_is_stale(
    db_session,
    user,
    seed_holding_005930,
    seed_active_journal_005930,
    seed_summary_sell_005930,
) -> None:
    from datetime import UTC, datetime, timedelta

    # Force summary to be newer than journal
    seed_summary_sell_005930.executed_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.flush()

    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=user.id)
    assert resp.rows[0].journal_status == "stale"
