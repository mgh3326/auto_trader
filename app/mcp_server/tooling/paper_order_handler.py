"""MCP shim that routes `place_order` / `get_order_history` to paper trading.

Keeps paper-only code isolated from the live order execution path.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.order_journal import (
    _append_journal_warning,
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _order_session_factory,
)
from app.mcp_server.tooling.shared import logger
from app.models.paper_trading import PaperAccount
from app.models.trade_journal import JournalStatus, TradeJournal
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


async def _activate_paper_journal(
    *,
    symbol: str,
    account_name: str,
) -> None:
    """Activate the most recent draft paper journal for a symbol.

    Paper trades fill immediately, so draft→active happens right after creation.
    Unlike live orders, paper journals do NOT set trade_id (no real trade exists).
    hold_until is recalculated from now if min_hold_days is set.
    """
    try:
        async with _order_session_factory()() as db:
            stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.symbol == symbol,
                    TradeJournal.status == JournalStatus.draft,
                    TradeJournal.account_type == "paper",
                    TradeJournal.account == account_name,
                )
                .order_by(desc(TradeJournal.created_at))
                .limit(1)
            )
            result = await db.execute(stmt)
            journal = result.scalars().first()

            if journal is None:
                return

            journal.status = JournalStatus.active
            # trade_id stays None — no real trade to link
            if journal.min_hold_days:
                journal.hold_until = now_kst() + timedelta(days=journal.min_hold_days)

            await db.commit()
            logger.info(
                "Activated paper journal id=%s for %s (account=%s)",
                journal.id,
                symbol,
                account_name,
            )
    except Exception as exc:
        logger.warning("Failed to activate paper journal: %s", exc)


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
    exit_reason: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    paper_account_name: str | None = None,
) -> dict[str, Any]:
    """Route a `place_order` call to the paper trading engine.

    Unlike live orders, thesis/strategy are optional for paper buys.
    If provided, a trade journal is auto-created; if omitted, the order
    executes without journal creation.
    """
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

            # 2. Execution
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

            p = execution["preview"]
            exec_data = execution["execution"]
            normalized_symbol = p["symbol"]
            market_type = p["instrument_type"]

            journal_id: int | None = None
            journal_status: str | None = None
            journal_warning: str | None = None

            # 3. Journal Integration (Buy) — only if thesis provided
            if side == "buy" and thesis:
                try:
                    journal_res = await _create_trade_journal_for_buy(
                        symbol=normalized_symbol,
                        market_type=market_type,
                        preview=p,
                        thesis=thesis,
                        strategy=strategy or "",
                        target_price=target_price,
                        stop_loss=stop_loss,
                        min_hold_days=min_hold_days,
                        notes=notes,
                        indicators_snapshot=indicators_snapshot,
                        account_type="paper",
                        account=account.name,
                    )
                    journal_id = journal_res["journal_id"]
                    journal_status = journal_res["journal_status"]

                    # Paper trades fill immediately — activate directly
                    await _activate_paper_journal(
                        symbol=normalized_symbol,
                        account_name=account.name,
                    )
                    journal_status = "active"
                except Exception as exc:
                    journal_warning = _append_journal_warning(
                        journal_warning, f"Journal creation failed: {exc}"
                    )

            # 4. Journal Integration (Sell)
            elif side == "sell":
                try:
                    close_res = await _close_journals_on_sell(
                        symbol=normalized_symbol,
                        sell_quantity=float(p["quantity"]),
                        sell_price=float(p["price"]),
                        exit_reason=exit_reason,
                        account_type="paper",
                        account=account.name,
                    )
                    if close_res["journals_closed"] > 0:
                        exec_data["journals_closed"] = close_res["journals_closed"]
                        exec_data["closed_journal_ids"] = close_res["closed_ids"]
                except Exception as exc:
                    journal_warning = _append_journal_warning(
                        journal_warning, f"Journal closing failed: {exc}"
                    )

            result = {
                "success": True,
                "dry_run": False,
                "account_type": "paper",
                "paper_account": account.name,
                "account_id": account.id,
                "preview": p,
                "execution": exec_data,
                "journal_id": journal_id,
                "journal_status": journal_status,
                "message": "[Paper] Order placed successfully",
            }
            if journal_warning:
                result["journal_warning"] = journal_warning

            return result
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
