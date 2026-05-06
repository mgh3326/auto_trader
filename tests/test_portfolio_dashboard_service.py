from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_journal import JournalStatus, TradeJournal


@pytest.mark.asyncio
async def test_get_latest_journal_snapshot_adds_distance_fields(monkeypatch):
    from app.services.portfolio_dashboard_service import PortfolioDashboardService

    mock_db = MagicMock(spec=AsyncSession)
    service = PortfolioDashboardService(mock_db)

    mock_journal = MagicMock(spec=TradeJournal)
    mock_journal.id = 1
    mock_journal.symbol = "AAPL"
    mock_journal.instrument_type = MagicMock()
    mock_journal.instrument_type.value = "equity_us"
    mock_journal.side = "buy"
    mock_journal.entry_price = Decimal("100.0")
    mock_journal.quantity = Decimal("10.0")
    mock_journal.amount = Decimal("1000.0")
    mock_journal.thesis = "Test thesis"
    mock_journal.strategy = "Test strategy"
    mock_journal.target_price = Decimal("110.0")
    mock_journal.stop_loss = Decimal("90.0")
    mock_journal.min_hold_days = 30
    mock_journal.hold_until = None
    mock_journal.indicators_snapshot = None
    mock_journal.status = JournalStatus.active
    mock_journal.trade_id = None
    mock_journal.exit_price = None
    mock_journal.exit_date = None
    mock_journal.exit_reason = None
    mock_journal.pnl_pct = None
    mock_journal.account = None
    mock_journal.notes = None
    mock_journal.created_at = None
    mock_journal.updated_at = None

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_journal
    mock_db.execute.return_value = mock_result

    result = await service.get_latest_journal_snapshot("AAPL", current_price=100.0)

    assert result is not None
    assert result["symbol"] == "AAPL"
    assert result["target_price"] == pytest.approx(110.0)
    assert result["stop_loss"] == pytest.approx(90.0)
    assert result["target_distance_pct"] == pytest.approx(10.0)
    assert result["stop_distance_pct"] == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_get_latest_journal_snapshot_returns_none_when_missing(monkeypatch):
    from app.services.portfolio_dashboard_service import PortfolioDashboardService

    mock_db = MagicMock(spec=AsyncSession)
    service = PortfolioDashboardService(mock_db)

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    result = await service.get_latest_journal_snapshot("NONEXISTENT")

    assert result is None


@pytest.mark.asyncio
async def test_get_cash_snapshot_maps_available_capital_to_dashboard_shape(monkeypatch):
    from app.services.portfolio_dashboard_service import PortfolioDashboardService

    mock_db = MagicMock(spec=AsyncSession)
    service = PortfolioDashboardService(mock_db)

    mock_cash_data = {
        "accounts": [
            {
                "account": "kis_domestic",
                "account_name": "기본 계좌",
                "broker": "kis",
                "currency": "KRW",
                "balance": 1000000.0,
                "orderable": 900000.0,
            },
            {
                "account": "kis_overseas",
                "account_name": "기본 계좌",
                "broker": "kis",
                "currency": "USD",
                "balance": 1000.0,
                "orderable": 900.0,
                "krw_equivalent": 1170000.0,
            },
            {
                "account": "upbit",
                "account_name": "기본 계좌",
                "broker": "upbit",
                "currency": "KRW",
                "balance": 500000.0,
                "orderable": 500000.0,
            },
        ],
        "manual_cash": {
            "amount": 1000000.0,
            "updated_at": "2026-04-01T00:00:00+00:00",
            "stale_warning": False,
        },
        "summary": {
            "total_orderable_krw": 3570000.0,
            "exchange_rate_usd_krw": 1300.0,
            "as_of": "2026-04-01T00:00:00+00:00",
        },
        "errors": [],
    }

    with patch(
        "app.services.portfolio_dashboard_service.get_available_capital_impl",
        new=AsyncMock(return_value=mock_cash_data),
    ):
        result = await service.get_cash_snapshot()

    assert result is not None
    assert "accounts" in result
    assert "manual_cash" in result
    assert "summary" in result
    assert "errors" in result

    accounts = result["accounts"]
    assert "kis_krw" in accounts
    assert "kis_usd" in accounts
    assert "upbit_krw" in accounts

    assert accounts["kis_krw"]["broker"] == "kis"
    assert accounts["kis_krw"]["currency"] == "KRW"
    assert accounts["kis_krw"]["balance"] == pytest.approx(1000000.0)
    assert accounts["kis_krw"]["orderable"] == pytest.approx(900000.0)

    assert accounts["kis_usd"]["broker"] == "kis"
    assert accounts["kis_usd"]["currency"] == "USD"
    assert accounts["kis_usd"]["balance"] == pytest.approx(1000.0)
    assert accounts["kis_usd"]["orderable"] == pytest.approx(900.0)

    assert accounts["upbit_krw"]["broker"] == "upbit"
    assert accounts["upbit_krw"]["currency"] == "KRW"
    assert accounts["upbit_krw"]["balance"] == pytest.approx(500000.0)

    assert result["manual_cash"]["amount"] == pytest.approx(1000000.0)
    assert result["manual_cash"]["updated_at"] == "2026-04-01T00:00:00+00:00"
    assert result["manual_cash"]["stale_warning"] is False

    assert result["summary"]["total_available_krw"] == pytest.approx(3570000.0)
    assert result["summary"]["exchange_rate_usd_krw"] == pytest.approx(1300.0)
    assert "as_of" in result["summary"]


@pytest.mark.asyncio
async def test_get_cash_snapshot_handles_missing_accounts(monkeypatch):
    from app.services.portfolio_dashboard_service import PortfolioDashboardService

    mock_db = MagicMock(spec=AsyncSession)
    service = PortfolioDashboardService(mock_db)

    mock_cash_data = {
        "accounts": [
            {
                "account": "kis_domestic",
                "account_name": "기본 계좌",
                "broker": "kis",
                "currency": "KRW",
                "balance": 1000000.0,
                "orderable": 900000.0,
            },
        ],
        "manual_cash": None,
        "summary": {
            "total_orderable_krw": 900000.0,
            "exchange_rate_usd_krw": None,
            "as_of": "2026-04-01T00:00:00+00:00",
        },
        "errors": [{"source": "kis_overseas", "error": "Connection failed"}],
    }

    with patch(
        "app.services.portfolio_dashboard_service.get_available_capital_impl",
        new=AsyncMock(return_value=mock_cash_data),
    ):
        result = await service.get_cash_snapshot()

    assert result is not None
    accounts = result["accounts"]
    assert accounts["kis_krw"] is not None
    assert accounts["kis_usd"] is None
    assert accounts["upbit_krw"] is None
    assert result["manual_cash"] is None
    assert len(result["errors"]) == 1


@pytest.mark.asyncio
async def test_get_journals_batch_returns_latest_active_or_draft_per_symbol() -> None:
    from app.services.portfolio_dashboard_service import PortfolioDashboardService

    mock_db = MagicMock(spec=AsyncSession)
    service = PortfolioDashboardService(mock_db)

    older = MagicMock(spec=TradeJournal)
    older.symbol = "AAPL"
    older.instrument_type = MagicMock()
    older.instrument_type.value = "equity_us"
    older.side = "buy"
    older.entry_price = Decimal("150.0")
    older.quantity = Decimal("1.0")
    older.amount = Decimal("150.0")
    older.thesis = "older"
    older.strategy = "swing"
    older.target_price = Decimal("165.0")
    older.stop_loss = Decimal("140.0")
    older.min_hold_days = None
    older.hold_until = None
    older.indicators_snapshot = None
    older.status = JournalStatus.active
    older.trade_id = None
    older.exit_price = None
    older.exit_date = None
    older.exit_reason = None
    older.pnl_pct = None
    older.account = None
    older.notes = None
    older.created_at = datetime(2026, 4, 1, tzinfo=UTC)
    older.updated_at = datetime(2026, 4, 1, tzinfo=UTC)

    newer = MagicMock(spec=TradeJournal)
    newer.symbol = "AAPL"
    newer.instrument_type = MagicMock()
    newer.instrument_type.value = "equity_us"
    newer.side = "buy"
    newer.entry_price = Decimal("155.0")
    newer.quantity = Decimal("1.0")
    newer.amount = Decimal("155.0")
    newer.thesis = "newer"
    newer.strategy = "trend"
    newer.target_price = Decimal("170.0")
    newer.stop_loss = Decimal("145.0")
    newer.min_hold_days = None
    newer.hold_until = None
    newer.indicators_snapshot = None
    newer.status = JournalStatus.draft
    newer.trade_id = None
    newer.exit_price = None
    newer.exit_date = None
    newer.exit_reason = None
    newer.pnl_pct = None
    newer.account = None
    newer.notes = None
    newer.created_at = datetime(2026, 4, 2, tzinfo=UTC)
    newer.updated_at = datetime(2026, 4, 2, tzinfo=UTC)

    msft = MagicMock(spec=TradeJournal)
    msft.symbol = "MSFT"
    msft.instrument_type = MagicMock()
    msft.instrument_type.value = "equity_us"
    msft.side = "buy"
    msft.entry_price = Decimal("300.0")
    msft.quantity = Decimal("2.0")
    msft.amount = Decimal("600.0")
    msft.thesis = "msft"
    msft.strategy = "trend"
    msft.target_price = Decimal("330.0")
    msft.stop_loss = Decimal("280.0")
    msft.min_hold_days = None
    msft.hold_until = None
    msft.indicators_snapshot = None
    msft.status = JournalStatus.active
    msft.trade_id = None
    msft.exit_price = None
    msft.exit_date = None
    msft.exit_reason = None
    msft.pnl_pct = None
    msft.account = None
    msft.notes = None
    msft.created_at = datetime(2026, 4, 3, tzinfo=UTC)
    msft.updated_at = datetime(2026, 4, 3, tzinfo=UTC)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [older, newer, msft]
    mock_db.execute.return_value = mock_result

    result = await service.get_journals_batch(["AAPL", "MSFT"])

    assert sorted(result) == ["AAPL", "MSFT"]
    assert result["AAPL"]["strategy"] == "trend"
    assert result["AAPL"]["target_price"] == pytest.approx(170.0)
    assert result["MSFT"]["target_price"] == pytest.approx(330.0)


@pytest.mark.asyncio
async def test_get_journals_batch_adds_distance_fields_when_current_prices_present() -> (
    None
):
    from app.services.portfolio_dashboard_service import PortfolioDashboardService

    mock_db = MagicMock(spec=AsyncSession)
    service = PortfolioDashboardService(mock_db)

    journal = MagicMock(spec=TradeJournal)
    journal.symbol = "AAPL"
    journal.instrument_type = MagicMock()
    journal.instrument_type.value = "equity_us"
    journal.side = "buy"
    journal.entry_price = Decimal("150.0")
    journal.quantity = Decimal("1.0")
    journal.amount = Decimal("150.0")
    journal.thesis = "test"
    journal.strategy = "trend"
    journal.target_price = Decimal("168.0")
    journal.stop_loss = Decimal("144.0")
    journal.min_hold_days = None
    journal.hold_until = None
    journal.indicators_snapshot = None
    journal.status = JournalStatus.active
    journal.trade_id = None
    journal.exit_price = None
    journal.exit_date = None
    journal.exit_reason = None
    journal.pnl_pct = None
    journal.account = None
    journal.notes = None
    journal.created_at = datetime(2026, 4, 3, tzinfo=UTC)
    journal.updated_at = datetime(2026, 4, 3, tzinfo=UTC)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [journal]
    mock_db.execute.return_value = mock_result

    result = await service.get_journals_batch(
        ["AAPL"],
        current_prices={"AAPL": 160.0},
    )

    assert result["AAPL"]["target_distance_pct"] == pytest.approx(5.0)
    assert result["AAPL"]["stop_distance_pct"] == pytest.approx(-10.0)
