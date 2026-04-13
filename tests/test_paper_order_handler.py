"""Unit tests for the paper order handler MCP shim."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling import paper_order_handler
from app.models.paper_trading import PaperAccount


def _make_account(account_id: int = 1, name: str = "default") -> PaperAccount:
    return PaperAccount(
        id=account_id,
        name=name,
        initial_capital=Decimal("100000000"),
        cash_krw=Decimal("100000000"),
        cash_usd=Decimal("0"),
        is_active=True,
    )


class TestPlacePaperOrderDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_creates_default_account_when_missing(self):
        service = MagicMock()
        service.get_account_by_name = AsyncMock(return_value=None)
        service.create_account = AsyncMock(return_value=_make_account())
        service.preview_order = AsyncMock(
            return_value={
                "success": True,
                "dry_run": True,
                "account_id": 1,
                "preview": {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": Decimal("10"),
                    "price": Decimal("70000"),
                    "gross": Decimal("700000"),
                    "fee": Decimal("105"),
                    "total_cost": Decimal("700105"),
                    "currency": "KRW",
                },
            }
        )

        fake_session_cm = AsyncMock()
        fake_session_cm.__aenter__.return_value = MagicMock()
        fake_session_cm.__aexit__.return_value = None

        with (
            patch.object(
                paper_order_handler,
                "AsyncSessionLocal",
                return_value=fake_session_cm,
            ),
            patch.object(
                paper_order_handler,
                "PaperTradingService",
                return_value=service,
            ),
        ):
            result = await paper_order_handler._place_paper_order(
                symbol="005930",
                side="buy",
                order_type="limit",
                quantity=10,
                price=70000,
                amount=None,
                dry_run=True,
                reason="",
                paper_account_name=None,
            )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["account_type"] == "paper"
        assert result["paper_account"] == "default"
        assert result["preview"]["symbol"] == "005930"
        service.create_account.assert_awaited_once()
        call_kwargs = service.create_account.await_args.kwargs
        assert call_kwargs["name"] == "default"
        assert call_kwargs["initial_capital_krw"] == Decimal("100000000")
        service.preview_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_uses_named_account(self):
        account = _make_account(account_id=7, name="swing")
        service = MagicMock()
        service.get_account_by_name = AsyncMock(return_value=account)
        service.create_account = AsyncMock()
        service.preview_order = AsyncMock(
            return_value={
                "success": True,
                "dry_run": True,
                "account_id": 7,
                "preview": {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "side": "buy",
                    "order_type": "market",
                    "quantity": Decimal("1"),
                    "price": Decimal("190"),
                    "gross": Decimal("190"),
                    "fee": Decimal("1"),
                    "total_cost": Decimal("191"),
                    "currency": "USD",
                },
            }
        )

        fake_session_cm = AsyncMock()
        fake_session_cm.__aenter__.return_value = MagicMock()
        fake_session_cm.__aexit__.return_value = None

        with (
            patch.object(
                paper_order_handler,
                "AsyncSessionLocal",
                return_value=fake_session_cm,
            ),
            patch.object(
                paper_order_handler,
                "PaperTradingService",
                return_value=service,
            ),
        ):
            result = await paper_order_handler._place_paper_order(
                symbol="AAPL",
                side="buy",
                order_type="market",
                quantity=1,
                price=None,
                amount=None,
                dry_run=True,
                reason="",
                paper_account_name="swing",
            )

        assert result["paper_account"] == "swing"
        assert result["account_id"] == 7
        service.create_account.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_named_account_errors(self):
        service = MagicMock()
        service.get_account_by_name = AsyncMock(return_value=None)
        service.create_account = AsyncMock()

        fake_session_cm = AsyncMock()
        fake_session_cm.__aenter__.return_value = MagicMock()
        fake_session_cm.__aexit__.return_value = None

        with (
            patch.object(
                paper_order_handler,
                "AsyncSessionLocal",
                return_value=fake_session_cm,
            ),
            patch.object(
                paper_order_handler,
                "PaperTradingService",
                return_value=service,
            ),
        ):
            result = await paper_order_handler._place_paper_order(
                symbol="005930",
                side="buy",
                order_type="limit",
                quantity=1,
                price=70000,
                amount=None,
                dry_run=True,
                reason="",
                paper_account_name="ghost",
            )

        assert result["success"] is False
        assert result["account_type"] == "paper"
        assert result["error"].startswith("[Paper]")
        assert "ghost" in result["error"]
        service.create_account.assert_not_called()


class TestPlacePaperOrderExecute:
    @pytest.mark.asyncio
    async def test_execute_returns_preview_and_execution(self):
        account = _make_account()
        service = MagicMock()
        service.get_account_by_name = AsyncMock(return_value=account)
        service.create_account = AsyncMock()
        service.execute_order = AsyncMock(
            return_value={
                "success": True,
                "dry_run": False,
                "account_id": 1,
                "preview": {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": Decimal("10"),
                    "price": Decimal("70000"),
                    "gross": Decimal("700000"),
                    "fee": Decimal("105"),
                    "total_cost": Decimal("700105"),
                    "currency": "KRW",
                },
                "execution": {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": Decimal("10"),
                    "price": Decimal("70000"),
                    "gross": Decimal("700000"),
                    "fee": Decimal("105"),
                    "total_cost": Decimal("700105"),
                    "currency": "KRW",
                    "realized_pnl": None,
                    "executed_at": None,
                },
            }
        )

        fake_session_cm = AsyncMock()
        fake_session_cm.__aenter__.return_value = MagicMock()
        fake_session_cm.__aexit__.return_value = None

        with (
            patch.object(
                paper_order_handler,
                "AsyncSessionLocal",
                return_value=fake_session_cm,
            ),
            patch.object(
                paper_order_handler,
                "PaperTradingService",
                return_value=service,
            ),
        ):
            result = await paper_order_handler._place_paper_order(
                symbol="005930",
                side="buy",
                order_type="limit",
                quantity=10,
                price=70000,
                amount=None,
                dry_run=False,
                reason="demo",
                paper_account_name=None,
            )

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["account_type"] == "paper"
        assert result["paper_account"] == "default"
        assert result["preview"]["symbol"] == "005930"
        assert result["execution"]["quantity"] == Decimal("10")
        assert result["message"] == "[Paper] Order placed successfully"
        service.execute_order.assert_awaited_once()
        kwargs = service.execute_order.await_args.kwargs
        assert kwargs["account_id"] == 1
        assert kwargs["reason"] == "demo"

    @pytest.mark.asyncio
    async def test_execute_insufficient_balance_returns_prefixed_error(self):
        account = _make_account()
        service = MagicMock()
        service.get_account_by_name = AsyncMock(return_value=account)
        service.execute_order = AsyncMock(
            side_effect=ValueError("Insufficient KRW balance: have 0, need 700105")
        )

        fake_session_cm = AsyncMock()
        fake_session_cm.__aenter__.return_value = MagicMock()
        fake_session_cm.__aexit__.return_value = None

        with (
            patch.object(
                paper_order_handler,
                "AsyncSessionLocal",
                return_value=fake_session_cm,
            ),
            patch.object(
                paper_order_handler,
                "PaperTradingService",
                return_value=service,
            ),
        ):
            result = await paper_order_handler._place_paper_order(
                symbol="005930",
                side="buy",
                order_type="limit",
                quantity=10,
                price=70000,
                amount=None,
                dry_run=False,
                reason="",
                paper_account_name=None,
            )

        assert result["success"] is False
        assert result["account_type"] == "paper"
        assert result["error"].startswith("[Paper] ")
        assert "Insufficient KRW balance" in result["error"]


class TestGetPaperOrderHistory:
    @pytest.mark.asyncio
    async def test_history_returns_service_rows(self):
        account = _make_account()
        service = MagicMock()
        service.get_account_by_name = AsyncMock(return_value=account)
        service.get_trade_history = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": Decimal("10"),
                    "price": Decimal("70000"),
                    "total_amount": Decimal("700000"),
                    "fee": Decimal("105"),
                    "currency": "KRW",
                    "reason": None,
                    "realized_pnl": None,
                    "executed_at": None,
                }
            ]
        )

        fake_session_cm = AsyncMock()
        fake_session_cm.__aenter__.return_value = MagicMock()
        fake_session_cm.__aexit__.return_value = None

        with (
            patch.object(
                paper_order_handler,
                "AsyncSessionLocal",
                return_value=fake_session_cm,
            ),
            patch.object(
                paper_order_handler,
                "PaperTradingService",
                return_value=service,
            ),
        ):
            result = await paper_order_handler._get_paper_order_history(
                symbol="005930",
                status="all",
                order_id=None,
                market=None,
                side=None,
                days=None,
                limit=50,
                paper_account_name=None,
            )

        assert result["success"] is True
        assert result["account_type"] == "paper"
        assert result["paper_account"] == "default"
        assert result["total_available"] == 1
        assert result["truncated"] is False
        assert result["orders"][0]["symbol"] == "005930"
        service.get_trade_history.assert_awaited_once_with(
            account_id=1,
            symbol="005930",
            side=None,
            days=None,
            limit=50,
        )

    @pytest.mark.asyncio
    async def test_history_unknown_named_account_errors(self):
        service = MagicMock()
        service.get_account_by_name = AsyncMock(return_value=None)

        fake_session_cm = AsyncMock()
        fake_session_cm.__aenter__.return_value = MagicMock()
        fake_session_cm.__aexit__.return_value = None

        with (
            patch.object(
                paper_order_handler,
                "AsyncSessionLocal",
                return_value=fake_session_cm,
            ),
            patch.object(
                paper_order_handler,
                "PaperTradingService",
                return_value=service,
            ),
        ):
            result = await paper_order_handler._get_paper_order_history(
                symbol=None,
                status="all",
                order_id=None,
                market=None,
                side=None,
                days=None,
                limit=50,
                paper_account_name="ghost",
            )

        assert result["success"] is False
        assert result["error"].startswith("[Paper] ")
        assert "ghost" in result["error"]
