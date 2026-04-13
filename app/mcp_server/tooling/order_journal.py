"""Trade journal and order fill database operations."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import Trade
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType


def _order_session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


async def _save_order_fill(
    symbol: str,
    instrument_type: str,
    side: str,
    price: float,
    quantity: float,
    total_amount: float,
    fee: float,
    currency: str,
    account: str,
    order_id: str | None,
) -> int | None:
    """Save executed order to review.trades for permanent history.

    Returns the trade ID if inserted, None if conflict (already exists).
    """
    try:
        async with _order_session_factory()() as db:
            stmt = (
                pg_insert(Trade)
                .values(
                    trade_date=now_kst(),
                    symbol=symbol,
                    instrument_type=instrument_type,
                    side=side,
                    price=price,
                    quantity=quantity,
                    total_amount=total_amount,
                    fee=fee,
                    currency=currency,
                    account=account,
                    order_id=order_id,
                )
                .on_conflict_do_nothing(
                    constraint="uq_review_trades_account_order",
                )
            )
            result = await db.execute(stmt)
            await db.commit()

            if result.inserted_primary_key and result.inserted_primary_key[0]:
                return typing_cast(int, result.inserted_primary_key[0])
            return None
    except Exception as exc:
        logger.warning("Failed to save order fill: %s", exc)
        return None


async def _link_journal_to_fill(
    symbol: str,
    trade_id: int,
    account_type: str = "live",
    account: str | None = None,
) -> None:
    """Link a draft journal to a fill: draft -> active, set trade_id, recalculate hold_until."""
    try:
        async with _order_session_factory()() as db:
            stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.symbol == symbol,
                    TradeJournal.status == JournalStatus.draft,
                    TradeJournal.account_type == account_type,
                )
            )
            if account:
                stmt = stmt.where(TradeJournal.account == account)

            stmt = stmt.order_by(desc(TradeJournal.created_at)).limit(1)
            result = await db.execute(stmt)
            journal = result.scalars().first()

            if journal is None:
                return

            journal.status = JournalStatus.active
            journal.trade_id = trade_id
            if journal.min_hold_days:
                from datetime import timedelta

                journal.hold_until = now_kst() + timedelta(days=journal.min_hold_days)

            await db.commit()
            logger.info(
                "Linked journal id=%s to trade id=%s for %s (account_type=%s, account=%s)",
                journal.id,
                trade_id,
                symbol,
                account_type,
                account,
            )
    except Exception as exc:
        logger.warning("Failed to link journal to fill: %s", exc)


def _validate_buy_journal_requirements(
    *,
    side: str,
    dry_run: bool,
    thesis: str | None,
    strategy: str | None,
) -> None:
    """Validate that buy orders have required journal fields when not in dry-run mode."""
    if side != "buy" or dry_run:
        return
    if not (thesis or "").strip():
        raise ValueError("thesis is required for buy orders when dry_run=False")
    if not (strategy or "").strip():
        raise ValueError("strategy is required for buy orders when dry_run=False")


async def _create_trade_journal_for_buy(
    *,
    symbol: str,
    market_type: str,
    preview: dict[str, Any],
    thesis: str,
    strategy: str,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    account_type: str = "live",
    account: str | None = None,
) -> dict[str, Any]:
    """Create a draft trade journal entry for a buy order.

    Returns a dict with journal_created, journal_id, journal_status.
    Raises on DB errors to allow caller to handle.
    """
    hold_until = (
        now_kst() + datetime.timedelta(days=min_hold_days)
        if min_hold_days and min_hold_days > 0
        else None
    )

    if account:
        account_name = account
    else:
        account_name = "upbit" if market_type == "crypto" else "kis"

    journal = TradeJournal(
        symbol=symbol,
        instrument_type=InstrumentType(market_type),
        side="buy",
        entry_price=Decimal(str(_to_float(preview.get("price"), default=0.0))),
        quantity=Decimal(str(_to_float(preview.get("quantity"), default=0.0))),
        amount=Decimal(str(_to_float(preview.get("estimated_value"), default=0.0))),
        thesis=thesis.strip(),
        strategy=strategy.strip(),
        target_price=Decimal(str(target_price)) if target_price is not None else None,
        stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
        min_hold_days=min_hold_days,
        hold_until=hold_until,
        indicators_snapshot=indicators_snapshot,
        notes=notes,
        account=account_name,
        account_type=account_type,
        status=JournalStatus.draft,
    )

    async with _order_session_factory()() as db:
        db.add(journal)
        await db.commit()
        await db.refresh(journal)

    return {
        "journal_created": True,
        "journal_id": journal.id,
        "journal_status": "draft",
    }


async def _close_journals_on_sell(
    *,
    symbol: str,
    sell_quantity: float,
    sell_price: float,
    exit_reason: str | None = None,
    account_type: str = "live",
    account: str | None = None,
) -> dict[str, Any]:
    """Close active trade journals in FIFO order when a sell order succeeds.

    - quantity is None: close immediately (legacy/manual case)
    - quantity <= remaining_sell_qty: close and decrement remaining
    - quantity > remaining_sell_qty: stop FIFO (partial sell, leave active)

    Returns dict with journals_closed, journals_kept, closed_ids, total_pnl_pct.
    """
    sell_qty_dec = Decimal(str(sell_quantity))
    sell_price_dec = Decimal(str(sell_price))
    remaining_qty = sell_qty_dec
    resolved_reason = (exit_reason or "").strip() or "sold_via_place_order"

    async with _order_session_factory()() as db:
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.symbol == symbol,
                TradeJournal.status == JournalStatus.active,
                TradeJournal.account_type == account_type,
            )
        )
        if account:
            stmt = stmt.where(TradeJournal.account == account)

        stmt = stmt.order_by(TradeJournal.created_at.asc())
        result = await db.execute(stmt)
        journals = list(result.scalars().all())

        closed_ids: list[int] = []
        weighted_pnl_sum = Decimal("0")
        weighted_qty_sum = Decimal("0")

        for journal in journals:
            journal_qty = journal.quantity

            if journal_qty is None:
                pass  # Legacy/manual case: close without consuming quantity
            elif remaining_qty > 0 and journal_qty <= remaining_qty:
                remaining_qty -= journal_qty
            elif remaining_qty > 0 and journal_qty > remaining_qty:
                break
            else:
                break

            journal.status = JournalStatus.closed
            journal.exit_price = sell_price_dec
            journal.exit_date = now_kst()
            journal.exit_reason = resolved_reason

            if journal.entry_price and journal.entry_price > 0:
                pnl_pct = (
                    (sell_price_dec - journal.entry_price) / journal.entry_price
                ) * Decimal("100")
                journal.pnl_pct = pnl_pct
                if journal_qty and journal_qty > 0:
                    weighted_pnl_sum += pnl_pct * journal_qty
                    weighted_qty_sum += journal_qty

            closed_ids.append(journal.id)

        await db.commit()

    total_pnl_pct = (
        float(weighted_pnl_sum / weighted_qty_sum) if weighted_qty_sum > 0 else 0.0
    )
    return {
        "journals_closed": len(closed_ids),
        "journals_kept": len(journals) - len(closed_ids),
        "closed_ids": closed_ids,
        "total_pnl_pct": total_pnl_pct,
    }


def _append_journal_warning(existing: str | None, new_message: str) -> str:
    """Append a new journal warning to an existing one."""
    return new_message if not existing else f"{existing}; {new_message}"
