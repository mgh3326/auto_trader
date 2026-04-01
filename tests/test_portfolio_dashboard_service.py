from __future__ import annotations

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
    assert result["target_price"] == 110.0
    assert result["stop_loss"] == 90.0
    assert result["target_distance_pct"] == 10.0
    assert result["stop_distance_pct"] == -10.0


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
    assert accounts["kis_krw"]["balance"] == 1000000.0
    assert accounts["kis_krw"]["orderable"] == 900000.0

    assert accounts["kis_usd"]["broker"] == "kis"
    assert accounts["kis_usd"]["currency"] == "USD"
    assert accounts["kis_usd"]["balance"] == 1000.0
    assert accounts["kis_usd"]["orderable"] == 900.0

    assert accounts["upbit_krw"]["broker"] == "upbit"
    assert accounts["upbit_krw"]["currency"] == "KRW"
    assert accounts["upbit_krw"]["balance"] == 500000.0

    assert result["manual_cash"]["amount"] == 1000000.0
    assert result["manual_cash"]["updated_at"] == "2026-04-01T00:00:00+00:00"
    assert result["manual_cash"]["stale_warning"] is False

    assert result["summary"]["total_available_krw"] == 3570000.0
    assert result["summary"]["exchange_rate_usd_krw"] == 1300.0
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
