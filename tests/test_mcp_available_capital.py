from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_get_available_capital_aggregates_accounts_and_manual_cash(monkeypatch):
    """Test that get_available_capital aggregates broker accounts and manual cash."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None):
        return {
            "accounts": [
                {
                    "account": "upbit",
                    "currency": "KRW",
                    "orderable": 1000000.0,
                },
                {
                    "account": "kis_domestic",
                    "currency": "KRW",
                    "orderable": 2000000.0,
                },
                {
                    "account": "kis_overseas",
                    "currency": "USD",
                    "orderable": 100.0,
                },
            ],
            "summary": {"total_krw": 3000000.0, "total_usd": 100.0},
            "errors": [],
        }

    async def mock_get_usd_krw_rate():
        return 1300.0

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 15000000},
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def mock_now_kst():
        return datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", mock_get_usd_krw_rate)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", mock_now_kst)

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    assert result["accounts"][0]["account"] == "upbit"
    assert result["accounts"][1]["account"] == "kis_domestic"
    assert result["accounts"][2]["account"] == "kis_overseas"
    assert result["accounts"][2].get("krw_equivalent") == 130000.0

    assert result["manual_cash"]["amount"] == 15000000
    assert result["manual_cash"]["stale_warning"] is False

    assert (
        result["summary"]["total_orderable_krw"]
        == 1000000.0 + 2000000.0 + 130000.0 + 15000000.0
    )
    assert result["summary"]["exchange_rate_usd_krw"] == 1300.0
    assert result["summary"]["as_of"] == "2026-04-01T09:00:00+00:00"

    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_available_capital_excludes_manual_when_flag_disabled(monkeypatch):
    """Test that include_manual=False excludes manual cash from aggregation."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None):
        return {
            "accounts": [
                {"account": "upbit", "currency": "KRW", "orderable": 1000000.0},
            ],
            "summary": {"total_krw": 1000000.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 5000000},
            "updated_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl(include_manual=False)

    assert result["manual_cash"] is None
    assert result["summary"]["total_orderable_krw"] == 1000000.0


@pytest.mark.asyncio
async def test_get_available_capital_handles_missing_manual_cash(monkeypatch):
    """Test that missing manual cash is handled gracefully (amount = 0)."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None):
        return {
            "accounts": [
                {"account": "upbit", "currency": "KRW", "orderable": 1000000.0}
            ],
            "summary": {"total_krw": 1000000.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_manual_cash_setting():
        return None

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    assert result["manual_cash"] is None
    assert result["summary"]["total_orderable_krw"] == 1000000.0


@pytest.mark.asyncio
async def test_get_available_capital_marks_stale_manual_cash(monkeypatch):
    """Test that manual cash older than 3 days gets stale_warning=True."""
    from app.mcp_server.tooling import portfolio_cash

    async def mock_get_cash_balance_impl(account=None):
        return {
            "accounts": [],
            "summary": {"total_krw": 0.0, "total_usd": 0.0},
            "errors": [],
        }

    stale_date = datetime.now(UTC) - timedelta(days=5)

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 1000000},
            "updated_at": stale_date.isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl()

    assert result["manual_cash"]["stale_warning"] is True


@pytest.mark.asyncio
async def test_get_available_capital_toss_filter_uses_manual_cash_path(monkeypatch):
    """Test that account='toss' uses the manual cash path."""
    from app.mcp_server.tooling import portfolio_cash

    cash_balance_calls = []

    async def mock_get_cash_balance_impl(account=None):
        cash_balance_calls.append(account)
        return {
            "accounts": [],
            "summary": {"total_krw": 0.0, "total_usd": 0.0},
            "errors": [],
        }

    async def mock_get_manual_cash_setting():
        return {
            "key": "manual_cash",
            "value": {"amount": 5000000},
            "updated_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(
        portfolio_cash, "get_cash_balance_impl", mock_get_cash_balance_impl
    )
    monkeypatch.setattr(portfolio_cash, "get_usd_krw_rate", lambda: 1300.0)
    monkeypatch.setattr(
        portfolio_cash, "get_manual_cash_setting", mock_get_manual_cash_setting
    )
    monkeypatch.setattr(portfolio_cash, "now_kst", lambda: datetime.now(UTC))

    from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl

    result = await get_available_capital_impl(account="toss")

    assert "toss" in cash_balance_calls or any(
        call in (None, "toss") for call in cash_balance_calls
    )
    assert result["manual_cash"]["amount"] == 5000000
