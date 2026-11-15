"""Trade journal and order fill database operations."""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.fx_pnl import (
    FX_PNL_ACCURACY_UNAVAILABLE,
    FX_RATE_SOURCE_UNAVAILABLE,
    compute_us_equity_fx_pnl,
    fx_label_error,
)
from app.mcp_server.tooling.order_validation import DefensiveTrimContext
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import Trade
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType


def _order_session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


@asynccontextmanager
async def _own_or_caller_session(
    db: AsyncSession | None,
) -> AsyncIterator[AsyncSession]:
    """Yield the caller's transaction, or a fresh session when none was given.

    A caller transaction is never committed here; a self-opened session is left
    for the caller of this context manager to commit only when it owned it (the
    legacy helpers do, the reconcile path commits its own outer transaction).
    """
    if db is not None:
        yield db
        return
    async with _order_session_factory()() as session:
        yield session


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


async def _record_trade_for_fill(
    db: AsyncSession,
    *,
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
) -> int:
    """Insert or update the order's single trade row to its cumulative figures.

    ``review.trades`` is unique on ``(account, order_id)``, so a later partial
    fill of the same order must advance the existing row instead of being
    dropped by the unique conflict. The caller owns the transaction (this
    function never commits), which is what lets a reconcile commit the trade,
    the journal and its progress marker atomically.
    """
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
        .on_conflict_do_update(
            constraint="uq_review_trades_account_order",
            set_={
                "price": price,
                "quantity": quantity,
                "total_amount": total_amount,
            },
        )
        .returning(Trade.id)
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def _link_journal_to_fill_in(
    db: AsyncSession,
    symbol: str,
    trade_id: int,
    account_type: str = "live",
    account: str | None = None,
) -> int | None:
    """Link the newest matching draft journal; returns its id (no commit)."""
    stmt = select(TradeJournal).where(
        TradeJournal.symbol == symbol,
        TradeJournal.status == JournalStatus.draft,
        TradeJournal.account_type == account_type,
    )
    if account:
        stmt = stmt.where(TradeJournal.account == account)

    stmt = stmt.order_by(desc(TradeJournal.created_at)).limit(1)
    result = await db.execute(stmt)
    journal = result.scalars().first()

    if journal is None:
        return None

    journal.status = JournalStatus.active
    journal.trade_id = trade_id
    if journal.min_hold_days:
        from datetime import timedelta

        journal.hold_until = now_kst() + timedelta(days=journal.min_hold_days)
    await db.flush()
    logger.info(
        "Linked journal id=%s to trade id=%s for %s (account_type=%s, account=%s)",
        journal.id,
        trade_id,
        symbol,
        account_type,
        account,
    )
    return int(journal.id)


async def _link_journal_to_fill(
    symbol: str,
    trade_id: int,
    account_type: str = "live",
    account: str | None = None,
    *,
    db: AsyncSession | None = None,
) -> None:
    """Link a draft journal to a fill: draft -> active, set trade_id, recalculate hold_until.

    ``db`` is the caller's transaction (no commit, errors propagate). Without
    it the historical own-session-and-commit behaviour is kept.
    """
    if db is not None:
        await _link_journal_to_fill_in(
            db, symbol, trade_id, account_type=account_type, account=account
        )
        return
    try:
        async with _order_session_factory()() as session:
            linked = await _link_journal_to_fill_in(
                session, symbol, trade_id, account_type=account_type, account=account
            )
            if linked is not None:
                await session.commit()
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
    buy_fx_rate: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
    correlation_id: str | None = None,
    db: AsyncSession | None = None,
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
        buy_fx_rate=Decimal(str(buy_fx_rate)) if buy_fx_rate is not None else None,
        fx_rate_source=fx_rate_source,
        fx_pnl_accuracy=fx_pnl_accuracy,
        correlation_id=correlation_id,
    )

    if db is not None:
        db.add(journal)
        await db.flush()
    else:
        async with _order_session_factory()() as session:
            session.add(journal)
            await session.commit()
            await session.refresh(journal)

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
    defensive_trim_ctx: DefensiveTrimContext | None = None,
    sell_fx_rate: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Close active trade journals in FIFO order when a sell order succeeds.

    FIFO lot attribution: active journals are consumed oldest-first
    (``order_by(created_at.asc())``); the oldest active journal is closed
    against the sell before any newer averaging-down ("물타기") lot. So the
    realized PnL is attributed against the per-lot ``entry_price`` of whichever
    lot(s) the FIFO walk consumes, NOT the position's account-average cost.

    - quantity is None: close immediately (legacy/manual case)
    - quantity <= remaining_sell_qty: close and decrement remaining
    - quantity > remaining_sell_qty: stop FIFO (partial sell, leave active)

    Returns dict with journals_closed, journals_kept, closed_ids, total_pnl_pct,
    and realized_pnl_basis. ``total_pnl_pct`` is quantity-weighted across the
    FIFO-closed journals' per-lot ``entry_price`` (i.e. journal/lot basis), and
    ``realized_pnl_basis`` labels it as ``"journal_entry"`` so callers do not
    confuse it with the account-average basis surfaced by place_order preview /
    get_holdings / get_available_capital (ROB-544).
    """
    sell_qty_dec = Decimal(str(sell_quantity))
    sell_price_dec = Decimal(str(sell_price))
    sell_fx_rate_dec = Decimal(str(sell_fx_rate)) if sell_fx_rate is not None else None
    remaining_qty = sell_qty_dec
    resolved_reason = (exit_reason or "").strip() or "sold_via_place_order"

    async with _own_or_caller_session(db) as session:
        stmt = select(TradeJournal).where(
            TradeJournal.symbol == symbol,
            TradeJournal.status == JournalStatus.active,
            TradeJournal.account_type == account_type,
        )
        if account:
            stmt = stmt.where(TradeJournal.account == account)

        stmt = stmt.order_by(TradeJournal.created_at.asc())
        result = await session.execute(stmt)
        journals = list(result.scalars().all())

        closed_ids: list[int] = []
        weighted_pnl_sum = Decimal("0")
        weighted_qty_sum = Decimal("0")

        fx_pnl_sum = Decimal("0")
        security_pnl_usd_sum = Decimal("0")
        security_pnl_krw_sum = Decimal("0")
        total_pnl_krw_sum = Decimal("0")
        fx_buy_notional_sum = Decimal("0")
        fx_buy_weighted_sum = Decimal("0")
        fx_computed_count = 0
        fx_unavailable_journal_ids: list[int] = []

        for journal in journals:
            journal_qty = journal.quantity

            if journal_qty is None:
                pass  # Legacy/manual case: close without consuming quantity
            elif remaining_qty > 0 and journal_qty <= remaining_qty:
                remaining_qty -= journal_qty
            elif remaining_qty > 0 and journal_qty > remaining_qty:
                # Partial sell smaller than the oldest active lot: intentionally stop
                # here without consuming any lot (no lot-splitting). This is the
                # designed behavior behind journals_closed=0 / security_pnl_usd=null
                # on such sells (ROB-955 F3) — not a bug. See
                # docs/runbooks/live-order-reconcile.md "journals_closed=0 진단".
                break
            else:
                break

            journal.status = JournalStatus.closed
            journal.exit_price = sell_price_dec
            journal.exit_date = now_kst()
            journal.exit_reason = resolved_reason
            if defensive_trim_ctx is not None:
                journal.notes = _append_defensive_trim_note(
                    journal.notes,
                    approval_issue_id=defensive_trim_ctx.approval_issue_id,
                    requester_agent_id=defensive_trim_ctx.requester_agent_id,
                )

            if journal.entry_price and journal.entry_price > 0:
                pnl_pct = (
                    (sell_price_dec - journal.entry_price) / journal.entry_price
                ) * Decimal("100")
                journal.pnl_pct = pnl_pct
                if journal_qty and journal_qty > 0:
                    weighted_pnl_sum += pnl_pct * journal_qty
                    weighted_qty_sum += journal_qty

            # ROB-568 — US FX PnL split
            if (
                journal.instrument_type == InstrumentType.equity_us
                and journal_qty is not None
                and journal.entry_price is not None
            ):
                journal.sell_fx_rate = sell_fx_rate_dec
                fx_values = compute_us_equity_fx_pnl(
                    buy_price=Decimal(str(journal.entry_price)),
                    sell_price=sell_price_dec,
                    quantity=Decimal(str(journal_qty)),
                    buy_fx_rate=Decimal(str(journal.buy_fx_rate))
                    if journal.buy_fx_rate is not None
                    else None,
                    sell_fx_rate=sell_fx_rate_dec,
                )
                if fx_values is None:
                    journal.fx_rate_source = FX_RATE_SOURCE_UNAVAILABLE
                    journal.fx_pnl_accuracy = FX_PNL_ACCURACY_UNAVAILABLE
                    fx_unavailable_journal_ids.append(journal.id)
                else:
                    journal.security_pnl_usd = fx_values["security_pnl_usd"]
                    journal.security_pnl_krw = fx_values["security_pnl_krw"]
                    journal.fx_pnl_krw = fx_values["fx_pnl_krw"]
                    journal.total_pnl_krw = fx_values["total_pnl_krw"]
                    journal.fx_rate_source = fx_rate_source
                    journal.fx_pnl_accuracy = fx_pnl_accuracy

                    fx_pnl_sum += fx_values["fx_pnl_krw"]
                    security_pnl_usd_sum += fx_values["security_pnl_usd"]
                    security_pnl_krw_sum += fx_values["security_pnl_krw"]
                    total_pnl_krw_sum += fx_values["total_pnl_krw"]
                    fx_buy_notional_sum += fx_values["buy_notional_usd"]
                    fx_buy_weighted_sum += fx_values["buy_notional_usd"] * Decimal(
                        str(journal.buy_fx_rate)
                    )
                    fx_computed_count += 1

            closed_ids.append(journal.id)

        if db is None:
            await session.commit()
        else:
            # The caller's transaction commits the journal close together with
            # its progress marker, so a retry can never re-close the same lot.
            await session.flush()

    # total_pnl_pct is quantity-weighted across the FIFO-closed journals'
    # per-lot entry_price (lot basis), NOT the position account-average. The
    # realized_pnl_basis label (ROB-544) makes this explicit so downstream
    # reconcile/report consumers don't conflate it with place_order preview's
    # account-average pchs_avg_pric basis.
    total_pnl_pct = (
        float(weighted_pnl_sum / weighted_qty_sum) if weighted_qty_sum > 0 else 0.0
    )
    return {
        "journals_closed": len(closed_ids),
        "journals_kept": len(journals) - len(closed_ids),
        "closed_ids": closed_ids,
        "total_pnl_pct": total_pnl_pct,
        "realized_pnl_basis": "journal_entry",
        "buy_fx_rate": float(fx_buy_weighted_sum / fx_buy_notional_sum)
        if fx_buy_notional_sum > 0
        else None,
        "sell_fx_rate": float(sell_fx_rate_dec)
        if sell_fx_rate_dec is not None
        else None,
        "fx_pnl_krw": float(fx_pnl_sum) if fx_computed_count else None,
        "security_pnl_usd": float(security_pnl_usd_sum) if fx_computed_count else None,
        "security_pnl_krw": float(security_pnl_krw_sum) if fx_computed_count else None,
        "total_pnl_krw": float(total_pnl_krw_sum) if fx_computed_count else None,
        "fx_rate_source": fx_rate_source
        if fx_computed_count
        else FX_RATE_SOURCE_UNAVAILABLE,
        "fx_pnl_accuracy": fx_pnl_accuracy
        if fx_computed_count
        else FX_PNL_ACCURACY_UNAVAILABLE,
        "fx_unavailable_journal_ids": fx_unavailable_journal_ids,
    }


async def _close_journals_on_sell_partial(
    db: AsyncSession,
    *,
    symbol: str,
    sell_quantity: float,
    sell_price: float,
    exit_reason: str | None = None,
    account_type: str = "live",
    account: str | None = None,
) -> dict[str, Any]:
    """FIFO close that decrements a partially consumed lot instead of stopping.

    ``_close_journals_on_sell`` deliberately never splits a lot, so a position
    sold in several smaller fills would stay active forever and its realized P&L
    would never be recorded. This variant decrements the boundary lot's quantity
    (and amount) and closes only the lots the sell fully consumes. The caller
    owns the transaction (no commit).

    The active lots are read ``FOR UPDATE``: the pass is a read-modify-write of
    a pool shared by every sell of that symbol/account, so two transactions
    journaling *different* orders of the same symbol (they lock different order
    rows) would otherwise read the same snapshot and both decrement it — 6 + 6
    shares sold out of one 10-share lot would leave 4 while 12 were consumed.
    The lock order is deterministic (``created_at``, ``id``) so two passes
    cannot deadlock on the same pair of lots.
    """
    remaining = Decimal(str(sell_quantity))
    price = Decimal(str(sell_price))
    reason = (exit_reason or "").strip() or "sold_via_place_order"
    stmt = select(TradeJournal).where(
        TradeJournal.symbol == symbol,
        TradeJournal.status == JournalStatus.active,
        TradeJournal.account_type == account_type,
    )
    if account:
        stmt = stmt.where(TradeJournal.account == account)
    stmt = stmt.order_by(
        TradeJournal.created_at.asc(), TradeJournal.id.asc()
    ).with_for_update()
    journals = list((await db.execute(stmt)).scalars().all())

    closed_ids: list[int] = []
    weighted_pnl_sum = Decimal("0")
    weighted_qty_sum = Decimal("0")
    for journal in journals:
        if remaining <= 0:
            break
        journal_qty = journal.quantity
        if journal_qty is None:
            pass  # Legacy/manual case: close without consuming quantity
        elif journal_qty <= remaining:
            remaining -= journal_qty
        else:
            # Partially consume this lot. The shares sold now are a closed
            # position of their own, so split them into a closed row at the
            # lot's entry price: their realized P&L is recorded instead of
            # vanishing with the consumed quantity, and the leftover stays
            # active at the reduced size (it keeps this lot's ``created_at``,
            # so the FIFO order of later sells is unchanged).
            consumed = remaining
            split = TradeJournal(
                symbol=journal.symbol,
                instrument_type=journal.instrument_type,
                side=journal.side,
                entry_price=journal.entry_price,
                quantity=consumed,
                amount=(
                    consumed * journal.entry_price
                    if journal.entry_price is not None
                    else None
                ),
                thesis=journal.thesis,
                strategy=journal.strategy,
                target_price=journal.target_price,
                stop_loss=journal.stop_loss,
                min_hold_days=journal.min_hold_days,
                account=journal.account,
                account_type=journal.account_type,
                trade_id=journal.trade_id,
                correlation_id=journal.correlation_id,
                status=JournalStatus.closed,
                exit_price=price,
                exit_date=now_kst(),
                exit_reason=reason,
            )
            if journal.entry_price and journal.entry_price > 0:
                pnl_pct = (
                    (price - journal.entry_price) / journal.entry_price
                ) * Decimal("100")
                split.pnl_pct = pnl_pct
                weighted_pnl_sum += pnl_pct * consumed
                weighted_qty_sum += consumed
            leftover = journal_qty - consumed
            journal.quantity = leftover
            journal.amount = (
                leftover * journal.entry_price
                if journal.entry_price is not None
                else None
            )
            db.add(split)
            await db.flush()
            closed_ids.append(split.id)
            remaining = Decimal("0")
            break

        journal.status = JournalStatus.closed
        journal.exit_price = price
        journal.exit_date = now_kst()
        journal.exit_reason = reason
        if journal.entry_price and journal.entry_price > 0:
            pnl_pct = ((price - journal.entry_price) / journal.entry_price) * Decimal(
                "100"
            )
            journal.pnl_pct = pnl_pct
            if journal_qty and journal_qty > 0:
                weighted_pnl_sum += pnl_pct * journal_qty
                weighted_qty_sum += journal_qty
        closed_ids.append(journal.id)

    await db.flush()
    total_pnl_pct = (
        float(weighted_pnl_sum / weighted_qty_sum) if weighted_qty_sum > 0 else 0.0
    )
    return {
        "journals_closed": len(closed_ids),
        "journals_kept": len(journals) - len(closed_ids),
        "closed_ids": closed_ids,
        "total_pnl_pct": total_pnl_pct,
        "realized_pnl_basis": "journal_entry",
    }


def _append_journal_warning(existing: str | None, new_message: str) -> str:
    """Append a new journal warning to an existing one."""
    return new_message if not existing else f"{existing}; {new_message}"


def _append_defensive_trim_note(
    existing: str | None,
    *,
    approval_issue_id: str,
    requester_agent_id: str,
) -> str:
    line = (
        "_defensive_trim: "
        f"approval={approval_issue_id}, "
        f"caller={requester_agent_id}, "
        "bypassed_floor=avg*1.01_"
    )
    if existing and existing.strip():
        return f"{existing.rstrip()}\n\n{line}"
    return line


async def list_active_journals(
    *,
    symbol: str | None = None,
    account_type: str = "live",
    account: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List active trade journals for audit/planning."""
    async with _order_session_factory()() as db:
        stmt = select(TradeJournal).where(
            TradeJournal.status == JournalStatus.active,
            TradeJournal.account_type == account_type,
        )
        if symbol:
            stmt = stmt.where(TradeJournal.symbol == symbol)
        if account:
            stmt = stmt.where(TradeJournal.account == account)

        stmt = stmt.order_by(TradeJournal.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        journals = result.scalars().all()

        return [
            {
                "journal_id": j.id,
                "symbol": j.symbol,
                "instrument_type": j.instrument_type.value,
                "side": j.side,
                "entry_price": float(j.entry_price) if j.entry_price else None,
                "quantity": float(j.quantity) if j.quantity else None,
                "amount": float(j.amount) if j.amount else None,
                "status": j.status.value,
                "thesis": j.thesis,
                "strategy": j.strategy,
                "created_at": j.created_at.isoformat(),
                "buy_fx_rate": float(j.buy_fx_rate) if j.buy_fx_rate else None,
                "fx_rate_source": j.fx_rate_source,
                "fx_pnl_accuracy": j.fx_pnl_accuracy,
            }
            for j in journals
        ]


async def get_journal_entry(journal_id: int) -> dict[str, Any] | None:
    """Retrieve a single journal entry by ID."""
    async with _order_session_factory()() as db:
        journal = await db.get(TradeJournal, journal_id)
        if journal is None:
            return None

        return {
            "journal_id": journal.id,
            "symbol": journal.symbol,
            "instrument_type": journal.instrument_type.value,
            "side": journal.side,
            "entry_price": float(journal.entry_price) if journal.entry_price else None,
            "quantity": float(journal.quantity) if journal.quantity else None,
            "amount": float(journal.amount) if journal.amount else None,
            "status": journal.status.value,
            "thesis": journal.thesis,
            "strategy": journal.strategy,
            "notes": journal.notes,
            "created_at": journal.created_at.isoformat(),
            "buy_fx_rate": float(journal.buy_fx_rate) if journal.buy_fx_rate else None,
            "sell_fx_rate": float(journal.sell_fx_rate)
            if journal.sell_fx_rate
            else None,
            "fx_pnl_krw": float(journal.fx_pnl_krw) if journal.fx_pnl_krw else None,
            "security_pnl_usd": float(journal.security_pnl_usd)
            if journal.security_pnl_usd
            else None,
            "security_pnl_krw": float(journal.security_pnl_krw)
            if journal.security_pnl_krw
            else None,
            "total_pnl_krw": float(journal.total_pnl_krw)
            if journal.total_pnl_krw
            else None,
            "fx_rate_source": journal.fx_rate_source,
            "fx_pnl_accuracy": journal.fx_pnl_accuracy,
        }


async def modify_journal_entry(
    journal_id: int,
    *,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    notes: str | None = None,
    buy_fx_rate: float | None = None,
    sell_fx_rate: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
) -> dict[str, Any]:
    """Update fields in an existing journal entry.

    ROB-568 — supports US FX overrides.
    """
    label_error = fx_label_error(fx_rate_source, fx_pnl_accuracy)
    if label_error is not None:
        return {"success": False, "error": label_error}

    async with _order_session_factory()() as db:
        journal = await db.get(TradeJournal, journal_id)
        if journal is None:
            return {"success": False, "error": f"Journal {journal_id} not found"}

        if thesis is not None:
            journal.thesis = thesis.strip()
        if strategy is not None:
            journal.strategy = strategy.strip()
        if target_price is not None:
            journal.target_price = Decimal(str(target_price))
        if stop_loss is not None:
            journal.stop_loss = Decimal(str(stop_loss))
        if notes is not None:
            journal.notes = notes

        # FX Overrides
        if buy_fx_rate is not None:
            journal.buy_fx_rate = Decimal(str(buy_fx_rate))
        if sell_fx_rate is not None:
            journal.sell_fx_rate = Decimal(str(sell_fx_rate))
        if fx_rate_source is not None:
            journal.fx_rate_source = fx_rate_source
        if fx_pnl_accuracy is not None:
            journal.fx_pnl_accuracy = fx_pnl_accuracy

        # If price/fx changed and it's closed US, recompute PnL
        if (
            journal.status == JournalStatus.closed
            and journal.instrument_type == InstrumentType.equity_us
        ):
            fx_values = compute_us_equity_fx_pnl(
                buy_price=journal.entry_price or Decimal("0"),
                sell_price=journal.exit_price or Decimal("0"),
                quantity=journal.quantity or Decimal("0"),
                buy_fx_rate=journal.buy_fx_rate,
                sell_fx_rate=journal.sell_fx_rate,
            )
            if fx_values:
                journal.security_pnl_usd = fx_values["security_pnl_usd"]
                journal.security_pnl_krw = fx_values["security_pnl_krw"]
                journal.fx_pnl_krw = fx_values["fx_pnl_krw"]
                journal.total_pnl_krw = fx_values["total_pnl_krw"]

        await db.commit()
        return {"success": True, "journal_id": journal.id}
