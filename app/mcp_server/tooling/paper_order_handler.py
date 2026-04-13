"""MCP shim that routes `place_order` / `get_order_history` to paper trading.

Keeps paper-only code isolated from the live order execution path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import logger
from app.models.paper_trading import PaperAccount
from app.services.paper_trading_service import PaperTradingService

DEFAULT_PAPER_ACCOUNT_NAME = "default"
DEFAULT_PAPER_INITIAL_CAPITAL_KRW = Decimal("100000000")  # 1억 KRW


def _paper_error(message: str, *, symbol: str | None = None) -> dict[str, Any]:
    """Build a paper-trading error response with the `[Paper]` prefix."""
    result: dict[str, Any] = {
        "success": False,
        "account_type": "paper",
        "error": f"[Paper] {message}",
        "source": "paper",
    }
    if symbol is not None:
        result["symbol"] = symbol
    return result


async def _resolve_paper_account(
    service: PaperTradingService,
    name: str | None,
) -> PaperAccount:
    """Return the named paper account, auto-creating the default one if missing.

    Only the default account is auto-created; an explicit name that does not
    exist raises ValueError so users don't create typo'd ghost accounts.
    """
    account_name = name or DEFAULT_PAPER_ACCOUNT_NAME
    account = await service.get_account_by_name(account_name)
    if account is not None:
        return account

    if name is not None and name != DEFAULT_PAPER_ACCOUNT_NAME:
        raise ValueError(f"Paper account '{name}' not found")

    return await service.create_account(
        name=DEFAULT_PAPER_ACCOUNT_NAME,
        initial_capital_krw=DEFAULT_PAPER_INITIAL_CAPITAL_KRW,
        description="Auto-created default paper account",
    )


async def _place_paper_order(
    *,
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    amount: float | None,
    dry_run: bool,
    reason: str,
    paper_account_name: str | None,
) -> dict[str, Any]:
    """Route a `place_order` call to the paper trading engine."""
    try:
        async with AsyncSessionLocal() as db:
            service = PaperTradingService(db)
            try:
                account = await _resolve_paper_account(service, paper_account_name)
            except ValueError as exc:
                return _paper_error(str(exc), symbol=symbol)

            if dry_run:
                preview = await service.preview_order(
                    account_id=account.id,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=quantity,
                    price=price,
                    amount=amount,
                )
                return {
                    "success": True,
                    "dry_run": True,
                    "account_type": "paper",
                    "paper_account": account.name,
                    "account_id": account.id,
                    "preview": preview["preview"],
                    "message": "[Paper] Order preview (dry_run=True)",
                }

            execution = await service.execute_order(
                account_id=account.id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                amount=amount,
                reason=reason or "",
            )
            return {
                "success": True,
                "dry_run": False,
                "account_type": "paper",
                "paper_account": account.name,
                "account_id": account.id,
                "preview": execution["preview"],
                "execution": execution["execution"],
                "message": "[Paper] Order placed successfully",
            }
    except ValueError as exc:
        return _paper_error(str(exc), symbol=symbol)
    except Exception as exc:  # pragma: no cover — unexpected failure
        logger.exception("Paper order failed: %s", exc)
        return _paper_error(f"unexpected error: {exc}", symbol=symbol)


async def _get_paper_order_history(
    *,
    symbol: str | None,
    status: str,
    order_id: str | None,
    market: str | None,
    side: str | None,
    days: int | None,
    limit: int | None,
    paper_account_name: str | None,
) -> dict[str, Any]:
    """Return paper trade history in a shape compatible with the live tool.

    `status`, `order_id`, and `market` are accepted for signature parity with
    the live tool but are not meaningful for paper trades (all paper trades
    are immediate fills). They are echoed back in the response for tracing.
    """
    del order_id, market  # signature parity only
    limit_val = limit if limit is not None else 50

    try:
        async with AsyncSessionLocal() as db:
            service = PaperTradingService(db)
            try:
                account = await _resolve_paper_account(service, paper_account_name)
            except ValueError as exc:
                return _paper_error(str(exc), symbol=symbol)

            rows = await service.get_trade_history(
                account_id=account.id,
                symbol=symbol,
                side=side,
                days=days,
                limit=limit_val,
            )

            return {
                "success": True,
                "account_type": "paper",
                "paper_account": account.name,
                "account_id": account.id,
                "orders": rows,
                "total_available": len(rows),
                "truncated": False,
                "status": status,
                "errors": [],
            }
    except Exception as exc:  # pragma: no cover — unexpected failure
        logger.exception("Paper history failed: %s", exc)
        return _paper_error(f"unexpected error: {exc}", symbol=symbol)


__all__ = [
    "DEFAULT_PAPER_ACCOUNT_NAME",
    "DEFAULT_PAPER_INITIAL_CAPITAL_KRW",
    "_place_paper_order",
    "_get_paper_order_history",
]
