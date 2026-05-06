"""Tests for paper trading analytics MCP tools."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_paper_analytics_tools_registered() -> None:
    tools = build_tools()
    assert "get_paper_performance" in tools
    assert "get_paper_trade_log" in tools
    assert "compare_paper_accounts" in tools


def test_parse_period_maps_to_start_date() -> None:
    from app.mcp_server.tooling.paper_analytics_registration import _parse_period

    today = date(2026, 4, 13)
    assert _parse_period("all", today) is None
    assert _parse_period("1d", today) == today - timedelta(days=1)
    assert _parse_period("1w", today) == today - timedelta(days=7)
    assert _parse_period("1m", today) == today - timedelta(days=30)
    assert _parse_period("3m", today) == today - timedelta(days=90)


def test_parse_period_rejects_unknown() -> None:
    from app.mcp_server.tooling.paper_analytics_registration import _parse_period

    with pytest.raises(ValueError, match="Unsupported period"):
        _parse_period("2y", date(2026, 4, 13))


class _SessionCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _patch_session(monkeypatch, db) -> None:
    factory = MagicMock()
    factory.return_value = _SessionCtx(db)
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_analytics_registration._session_factory",
        lambda: factory,
    )


def _perf_payload() -> dict:
    return {
        "total_return_pct": 10.0,
        "realized_pnl": 98635.0,
        "unrealized_pnl": 400000.0,
        "total_trades": 1,
        "win_rate": 100.0,
        "avg_holding_days": 5.0,
        "max_drawdown_pct": 1.49,
        "sharpe_ratio": 0.5,
        "best_trade": {
            "symbol": "005930",
            "entry_date": "2026-04-01T00:00:00+00:00",
            "exit_date": "2026-04-06T00:00:00+00:00",
            "holding_days": 5,
            "pnl": 98635.0,
            "return_pct": 16.44,
            "entry_reason": "",
            "exit_reason": "",
        },
        "worst_trade": {
            "symbol": "005930",
            "entry_date": "2026-04-01T00:00:00+00:00",
            "exit_date": "2026-04-06T00:00:00+00:00",
            "holding_days": 5,
            "pnl": 98635.0,
            "return_pct": 16.44,
            "entry_reason": "",
            "exit_reason": "",
        },
    }


@pytest.mark.asyncio
async def test_get_paper_performance_all_period(monkeypatch):
    from app.models.paper_trading import PaperAccount

    db = AsyncMock()
    _patch_session(monkeypatch, db)

    account = PaperAccount(
        id=1,
        name="bot",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("5000000"),
        cash_usd=Decimal("0"),
        is_active=True,
    )

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=account)
        svc.calculate_performance = AsyncMock(return_value=_perf_payload())

        tools = build_tools()
        result = await tools["get_paper_performance"](name="bot", period="all")

    svc.calculate_performance.assert_awaited_once_with(
        account_id=1, start_date=None, end_date=None
    )
    assert result["success"] is True
    assert result["account_name"] == "bot"
    assert result["period"] == "all"
    assert result["performance"]["total_return_pct"] == pytest.approx(10.0)
    assert result["performance"]["best_trade"]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_get_paper_performance_unknown_account(monkeypatch):
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=None)

        tools = build_tools()
        result = await tools["get_paper_performance"](name="ghost", period="all")

    assert result == {"success": False, "error": "Paper account 'ghost' not found"}


@pytest.mark.asyncio
async def test_get_paper_performance_invalid_period(monkeypatch):
    from app.models.paper_trading import PaperAccount

    db = AsyncMock()
    _patch_session(monkeypatch, db)

    account = PaperAccount(
        id=1,
        name="bot",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("0"),
        cash_usd=Decimal("0"),
        is_active=True,
    )

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=account)

        tools = build_tools()
        result = await tools["get_paper_performance"](name="bot", period="2y")

    assert result["success"] is False
    assert "Unsupported period" in result["error"]


@pytest.mark.asyncio
async def test_get_paper_trade_log_basic(monkeypatch):
    from app.models.paper_trading import PaperAccount

    db = AsyncMock()
    _patch_session(monkeypatch, db)

    account = PaperAccount(
        id=1,
        name="bot",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("0"),
        cash_usd=Decimal("0"),
        is_active=True,
    )

    trade_rows = [
        {
            "id": 1,
            "symbol": "005930",
            "instrument_type": "equity_kr",
            "side": "buy",
            "order_type": "market",
            "quantity": Decimal("10"),
            "price": Decimal("60000"),
            "total_amount": Decimal("600000"),
            "fee": Decimal("90"),
            "currency": "KRW",
            "reason": "entry",
            "realized_pnl": None,
            "executed_at": datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
        },
    ]

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=account)
        svc.get_trade_history = AsyncMock(return_value=trade_rows)

        tools = build_tools()
        result = await tools["get_paper_trade_log"](name="bot")

    svc.get_trade_history.assert_awaited_once_with(
        account_id=1, symbol=None, days=None, limit=50
    )
    assert result["success"] is True
    assert len(result["trades"]) == 1
    t = result["trades"][0]
    assert t["symbol"] == "005930"
    assert t["quantity"] == pytest.approx(10.0)
    assert t["price"] == pytest.approx(60000.0)
    assert t["fee"] == pytest.approx(90.0)
    assert t["realized_pnl"] is None
    assert t["executed_at"] == "2026-04-01T09:00:00+00:00"


@pytest.mark.asyncio
async def test_get_paper_trade_log_filters_forwarded(monkeypatch):
    from app.models.paper_trading import PaperAccount

    db = AsyncMock()
    _patch_session(monkeypatch, db)

    account = PaperAccount(
        id=1,
        name="bot",
        initial_capital=Decimal("0"),
        cash_krw=Decimal("0"),
        cash_usd=Decimal("0"),
        is_active=True,
    )

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=account)
        svc.get_trade_history = AsyncMock(return_value=[])

        tools = build_tools()
        result = await tools["get_paper_trade_log"](
            name="bot", symbol="AAPL", days=7, limit=10
        )

    svc.get_trade_history.assert_awaited_once_with(
        account_id=1, symbol="AAPL", days=7, limit=10
    )
    assert result == {"success": True, "account_name": "bot", "trades": []}


@pytest.mark.asyncio
async def test_get_paper_trade_log_account_not_found(monkeypatch):
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=None)

        tools = build_tools()
        result = await tools["get_paper_trade_log"](name="ghost")

    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_compare_paper_accounts_all_found(monkeypatch):
    from app.models.paper_trading import PaperAccount

    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc1 = PaperAccount(
        id=1,
        name="bot-a",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("0"),
        cash_usd=Decimal("0"),
        is_active=True,
    )
    acc2 = PaperAccount(
        id=2,
        name="bot-b",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("0"),
        cash_usd=Decimal("0"),
        is_active=True,
    )

    name_to_account = {"bot-a": acc1, "bot-b": acc2}
    perf_by_id = {
        1: {
            "total_return_pct": 5.0,
            "realized_pnl": 100.0,
            "unrealized_pnl": 50.0,
            "total_trades": 3,
            "win_rate": 66.67,
            "avg_holding_days": 4.0,
            "max_drawdown_pct": 2.0,
            "sharpe_ratio": 1.0,
            "best_trade": None,
            "worst_trade": None,
        },
        2: {
            "total_return_pct": -2.0,
            "realized_pnl": -200.0,
            "unrealized_pnl": 0.0,
            "total_trades": 1,
            "win_rate": 0.0,
            "avg_holding_days": 2.0,
            "max_drawdown_pct": 5.0,
            "sharpe_ratio": -0.5,
            "best_trade": None,
            "worst_trade": None,
        },
    }

    async def _lookup(name):
        return name_to_account.get(name)

    async def _perf(account_id, start_date=None, end_date=None):
        return perf_by_id[account_id]

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(side_effect=_lookup)
        svc.calculate_performance = AsyncMock(side_effect=_perf)

        tools = build_tools()
        result = await tools["compare_paper_accounts"](names=["bot-a", "bot-b"])

    assert result["success"] is True
    rows = result["comparison"]
    assert [r["account_name"] for r in rows] == ["bot-a", "bot-b"]
    assert rows[0]["performance"]["total_return_pct"] == pytest.approx(5.0)
    assert rows[1]["performance"]["total_return_pct"] == pytest.approx(-2.0)


@pytest.mark.asyncio
async def test_compare_paper_accounts_skips_missing(monkeypatch):
    from app.models.paper_trading import PaperAccount

    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc1 = PaperAccount(
        id=1,
        name="bot-a",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("0"),
        cash_usd=Decimal("0"),
        is_active=True,
    )

    async def _lookup(name):
        return acc1 if name == "bot-a" else None

    with patch(
        "app.mcp_server.tooling.paper_analytics_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(side_effect=_lookup)
        svc.calculate_performance = AsyncMock(
            return_value={
                "total_return_pct": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_holding_days": 0.0,
                "max_drawdown_pct": None,
                "sharpe_ratio": None,
                "best_trade": None,
                "worst_trade": None,
            }
        )

        tools = build_tools()
        result = await tools["compare_paper_accounts"](names=["bot-a", "ghost"])

    assert result["success"] is True
    assert len(result["comparison"]) == 2
    first, second = result["comparison"]
    assert first["account_name"] == "bot-a"
    assert first["performance"]["total_return_pct"] == pytest.approx(0.0)
    assert first.get("error") is None
    assert second == {
        "account_name": "ghost",
        "performance": None,
        "error": "Paper account 'ghost' not found",
    }


@pytest.mark.asyncio
async def test_compare_paper_accounts_empty_names(monkeypatch):
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    tools = build_tools()
    result = await tools["compare_paper_accounts"](names=[])
    assert result["success"] is False
    assert "at least one" in result["error"].lower()
