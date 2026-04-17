# app/mcp_server/tooling/trade_journal_tools.py
"""Trade journal MCP tool implementations."""

from __future__ import annotations

import logging
from datetime import timedelta, timezone
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _serialize_journal(j: TradeJournal) -> dict[str, Any]:
    """Convert a TradeJournal row to a JSON-safe dict."""
    return {
        "id": j.id,
        "symbol": j.symbol,
        "instrument_type": j.instrument_type.value
        if hasattr(j.instrument_type, "value")
        else str(j.instrument_type),
        "side": j.side,
        "entry_price": float(j.entry_price) if j.entry_price is not None else None,
        "quantity": float(j.quantity) if j.quantity is not None else None,
        "amount": float(j.amount) if j.amount is not None else None,
        "thesis": j.thesis,
        "strategy": j.strategy,
        "target_price": float(j.target_price) if j.target_price is not None else None,
        "stop_loss": float(j.stop_loss) if j.stop_loss is not None else None,
        "min_hold_days": j.min_hold_days,
        "hold_until": j.hold_until.isoformat() if j.hold_until else None,
        "indicators_snapshot": j.indicators_snapshot,
        "metadata": j.extra_metadata,
        "status": j.status,
        "trade_id": j.trade_id,
        "exit_price": float(j.exit_price) if j.exit_price is not None else None,
        "exit_date": j.exit_date.isoformat() if j.exit_date else None,
        "exit_reason": j.exit_reason,
        "pnl_pct": float(j.pnl_pct) if j.pnl_pct is not None else None,
        "account": j.account,
        "account_type": j.account_type,
        "paper_trade_id": j.paper_trade_id,
        "paperclip_issue_id": j.paperclip_issue_id,
        "notes": j.notes,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "updated_at": j.updated_at.isoformat() if j.updated_at else None,
    }


async def save_trade_journal(
    symbol: str,
    thesis: str,
    side: str = "buy",
    entry_price: float | None = None,
    quantity: float | None = None,
    amount: float | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    indicators_snapshot: dict | None = None,
    account: str | None = None,
    notes: str | None = None,
    status: str = "draft",
    account_type: str = "live",
    paper_trade_id: int | None = None,
    paperclip_issue_id: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Save a trade journal entry with investment thesis and strategy metadata.

    symbol is auto-detected for instrument_type (KRW-BTC -> crypto, AAPL -> equity_us, 005930 -> equity_kr).
    min_hold_days auto-calculates hold_until from now.
    Warns if an active journal already exists for the same symbol.
    account_type='paper' for paper trading journals (requires account name).
    paper_trade_id links to the paper trade record.
    paperclip_issue_id links to the Paperclip issue tracking this trade.
    metadata is an optional JSON dict for extensible fields.
    """
    symbol = (symbol or "").strip()
    thesis = (thesis or "").strip()

    if not symbol:
        return {"success": False, "error": "symbol is required"}
    if not thesis:
        return {"success": False, "error": "thesis is required"}
    if side not in ("buy", "sell"):
        return {"success": False, "error": "side must be 'buy' or 'sell'"}
    if status not in {s.value for s in JournalStatus}:
        return {"success": False, "error": f"Invalid status: {status}"}

    # account_type 검증
    if account_type not in ("live", "paper"):
        return {"success": False, "error": f"Invalid account_type: {account_type}"}
    if account_type == "live" and paper_trade_id is not None:
        return {
            "success": False,
            "error": "paper_trade_id cannot be set for live account_type",
        }
    if account_type == "paper" and not account:
        return {
            "success": False,
            "error": "account is required for paper account_type",
        }

    try:
        market_type, normalized_symbol = _resolve_market_type(symbol, None)
    except ValueError as exc:
        return {"success": False, "error": f"Cannot detect market type: {exc}"}

    instrument = InstrumentType(market_type)

    hold_until = None
    if min_hold_days is not None and min_hold_days > 0:
        hold_until = now_kst() + timedelta(days=min_hold_days)

    try:
        async with _session_factory()() as db:
            # Check for existing active journal
            warning = None
            existing_stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.symbol == normalized_symbol,
                    TradeJournal.status == JournalStatus.active,
                    TradeJournal.account_type == account_type,
                )
                .order_by(desc(TradeJournal.created_at))
                .limit(1)
            )
            existing_result = await db.execute(existing_stmt)
            existing = existing_result.scalars().first()
            if existing:
                warning = (
                    f"Active journal already exists for {normalized_symbol} "
                    f"(id={existing.id}, thesis='{existing.thesis[:50]}...', "
                    f"account_type={account_type}). "
                    "Creating new journal anyway."
                )

            journal = TradeJournal(
                symbol=normalized_symbol,
                instrument_type=instrument,
                side=side,
                entry_price=Decimal(str(entry_price))
                if entry_price is not None
                else None,
                quantity=Decimal(str(quantity)) if quantity is not None else None,
                amount=Decimal(str(amount)) if amount is not None else None,
                thesis=thesis,
                strategy=strategy,
                target_price=Decimal(str(target_price))
                if target_price is not None
                else None,
                stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
                min_hold_days=min_hold_days,
                hold_until=hold_until,
                indicators_snapshot=indicators_snapshot,
                status=status,
                account=account,
                account_type=account_type,
                paper_trade_id=paper_trade_id,
                paperclip_issue_id=paperclip_issue_id,
                notes=notes,
                extra_metadata=metadata,
            )
            db.add(journal)
            await db.commit()
            await db.refresh(journal)

            result: dict[str, Any] = {
                "success": True,
                "action": "created",
                "data": _serialize_journal(journal),
            }
            if warning:
                result["warning"] = warning
            return result

    except Exception as exc:
        logger.exception("save_trade_journal failed")
        return {"success": False, "error": f"save_trade_journal failed: {exc}"}


async def get_trade_journal(
    symbol: str | None = None,
    status: str | None = None,
    market: str | None = None,
    strategy: str | None = None,
    days: int | None = None,
    include_closed: bool = False,
    limit: int = 50,
    account_type: str | None = "live",
    account: str | None = None,
    paperclip_issue_id: str | None = None,
) -> dict[str, Any]:
    """Query trade journals. Call before any sell decision to check thesis and hold periods.

    Returns active journals by default. Set include_closed=True for closed/stopped.
    Each entry includes hold_remaining_days, hold_expired for hold period checks.
    account_type defaults to 'live'; set to 'paper' for paper journals, or None to query both.
    account (optional) filters to a specific account name.
    paperclip_issue_id (optional) filters by Paperclip issue ID for reverse lookup.
    """
    try:
        async with _session_factory()() as db:
            filters = []

            if symbol:
                symbol = symbol.strip()
                try:
                    _, normalized = _resolve_market_type(symbol, None)
                    filters.append(TradeJournal.symbol == normalized)
                except ValueError:
                    filters.append(TradeJournal.symbol == symbol)

            if status:
                if status not in {s.value for s in JournalStatus}:
                    return {"success": False, "error": f"Invalid status: {status}"}
                filters.append(TradeJournal.status == status)
            elif not include_closed:
                filters.append(
                    TradeJournal.status.in_(
                        [
                            JournalStatus.draft,
                            JournalStatus.active,
                        ]
                    )
                )

            if account_type is not None:
                filters.append(TradeJournal.account_type == account_type)

            if account is not None:
                filters.append(TradeJournal.account == account)

            if paperclip_issue_id is not None:
                filters.append(TradeJournal.paperclip_issue_id == paperclip_issue_id)

            if market:
                market_map = {
                    "crypto": InstrumentType.crypto,
                    "kr": InstrumentType.equity_kr,
                    "us": InstrumentType.equity_us,
                }
                itype = market_map.get(market)
                if itype:
                    filters.append(TradeJournal.instrument_type == itype)

            if strategy:
                filters.append(TradeJournal.strategy == strategy)

            if days is not None and days > 0:
                cutoff = now_kst() - timedelta(days=days)
                filters.append(TradeJournal.created_at >= cutoff)

            stmt = (
                select(TradeJournal)
                .where(*filters)
                .order_by(desc(TradeJournal.created_at))
                .limit(limit)
            )
            result = await db.execute(stmt)
            journals = result.scalars().all()

            now = now_kst()
            entries = []
            total_active = 0
            hold_locked = 0
            near_target = 0
            near_stop = 0

            for j in journals:
                entry = _serialize_journal(j)

                # Hold period calculations
                if j.hold_until:
                    remaining = (j.hold_until - now).days
                    entry["hold_remaining_days"] = remaining
                    entry["hold_expired"] = remaining < 0
                    if remaining >= 0 and j.status == JournalStatus.active:
                        hold_locked += 1
                else:
                    entry["hold_remaining_days"] = None
                    entry["hold_expired"] = None

                # Current price not fetched here (too slow for bulk queries)
                # Caller can use get_quote separately
                entry["current_price"] = None
                entry["pnl_pct_live"] = None
                entry["target_reached"] = None
                entry["stop_reached"] = None

                if j.status == JournalStatus.active:
                    total_active += 1

                entries.append(entry)

            return {
                "success": True,
                "entries": entries,
                "summary": {
                    "total_active": total_active,
                    "hold_locked": hold_locked,
                    "near_target": near_target,
                    "near_stop": near_stop,
                    "total_returned": len(entries),
                },
            }

    except Exception as exc:
        logger.exception("get_trade_journal failed")
        return {"success": False, "error": f"get_trade_journal failed: {exc}"}


async def update_trade_journal(
    journal_id: int | None = None,
    symbol: str | None = None,
    status: str | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    trade_id: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Update a trade journal entry.

    Find by journal_id, or by symbol (most recent active).
    On close/stop: auto-calculates pnl_pct from entry_price and exit_price.
    On activate: recalculates hold_until from now if min_hold_days is set.
    """
    if journal_id is None and not symbol:
        return {"success": False, "error": "Either journal_id or symbol is required"}

    try:
        async with _session_factory()() as db:
            journal: TradeJournal | None = None

            if journal_id is not None:
                journal = await db.get(TradeJournal, journal_id)

            if journal is None and symbol:
                symbol = symbol.strip()
                try:
                    _, normalized = _resolve_market_type(symbol, None)
                except ValueError:
                    normalized = symbol

                stmt = (
                    select(TradeJournal)
                    .where(
                        TradeJournal.symbol == normalized,
                        TradeJournal.status.in_(
                            [
                                JournalStatus.draft,
                                JournalStatus.active,
                            ]
                        ),
                    )
                    .order_by(desc(TradeJournal.created_at))
                    .limit(1)
                )
                result = await db.execute(stmt)
                journal = result.scalars().first()

            if journal is None:
                target = f"id={journal_id}" if journal_id else f"symbol={symbol}"
                return {"success": False, "error": f"Journal not found: {target}"}

            # Apply updates
            if status is not None:
                if status not in {s.value for s in JournalStatus}:
                    return {"success": False, "error": f"Invalid status: {status}"}
                journal.status = status

                # On activation: recalculate hold_until from now
                if status == JournalStatus.active and journal.min_hold_days:
                    journal.hold_until = now_kst() + timedelta(
                        days=journal.min_hold_days
                    )

            if trade_id is not None:
                journal.trade_id = trade_id

            if target_price is not None:
                journal.target_price = Decimal(str(target_price))

            if stop_loss is not None:
                journal.stop_loss = Decimal(str(stop_loss))

            if min_hold_days is not None:
                journal.min_hold_days = min_hold_days
                journal.hold_until = now_kst() + timedelta(days=min_hold_days)

            if notes is not None:
                journal.notes = notes

            if exit_price is not None:
                journal.exit_price = Decimal(str(exit_price))
                journal.exit_date = now_kst()

                # Auto-calculate pnl_pct
                if journal.entry_price and journal.entry_price > 0:
                    pnl = (Decimal(str(exit_price)) / journal.entry_price - 1) * 100
                    journal.pnl_pct = round(pnl, 4)

            if exit_reason is not None:
                journal.exit_reason = exit_reason

            await db.commit()
            await db.refresh(journal)

            return {
                "success": True,
                "action": "updated",
                "data": _serialize_journal(journal),
            }

    except Exception as exc:
        logger.exception("update_trade_journal failed")
        return {"success": False, "error": f"update_trade_journal failed: {exc}"}


def _extract_daily_allocation_krw(journal: TradeJournal) -> float:
    """Extract per-day KRW allocation from metadata or model columns."""
    metadata = (
        journal.extra_metadata if isinstance(journal.extra_metadata, dict) else {}
    )

    amount_krw = metadata.get("amount_krw")
    hold_days = metadata.get("hold_days")

    if amount_krw is None:
        amount_krw = journal.amount
    if hold_days is None:
        hold_days = journal.min_hold_days

    if amount_krw is None or hold_days is None:
        return 0.0

    try:
        total_amount = float(amount_krw)
        total_hold_days = int(hold_days)
    except (TypeError, ValueError):
        return 0.0

    if total_amount <= 0 or total_hold_days <= 0:
        return 0.0

    return total_amount / total_hold_days


async def compute_active_dca_daily_burn() -> dict[str, Any]:
    """Compute current daily burn from active DCA journal records."""
    try:
        async with _session_factory()() as db:
            stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.status == JournalStatus.active,
                    TradeJournal.strategy.in_(("dca_oversold", "coinmoogi")),
                )
                .order_by(desc(TradeJournal.created_at))
            )
            result = await db.execute(stmt)
            journals = result.scalars().all()
    except Exception as exc:
        logger.exception("compute_active_dca_daily_burn failed")
        return {
            "daily_burn_krw": 0.0,
            "active_count": 0,
            "per_record": [],
            "days_to_next_obligation": None,
            "cash_needed_until_obligation": 0.0,
            "error": f"compute_active_dca_daily_burn failed: {exc}",
        }

    today = now_kst().date()
    per_record: list[dict[str, Any]] = []
    daily_burn = 0.0
    obligation_days: list[int] = []

    for journal in journals:
        allocation = _extract_daily_allocation_krw(journal)
        per_record.append(
            {
                "symbol": journal.symbol,
                "allocation_krw": allocation,
                "hold_until": journal.hold_until,
            }
        )
        daily_burn += allocation

        if journal.hold_until is not None:
            hold_until = journal.hold_until
            hold_until_with_tz = (
                hold_until
                if hold_until.tzinfo is not None
                else hold_until.replace(tzinfo=timezone.utc)
            )
            hold_until_kst_date = hold_until_with_tz.astimezone(KST).date()
            days_to_obligation = (hold_until_kst_date - today).days
            if days_to_obligation > 0:
                obligation_days.append(days_to_obligation)

    days_to_next_obligation = min(obligation_days) if obligation_days else None
    cash_needed_until_obligation = (
        daily_burn * days_to_next_obligation
        if days_to_next_obligation is not None
        else 0.0
    )

    return {
        "daily_burn_krw": round(daily_burn, 6),
        "active_count": len(journals),
        "per_record": per_record,
        "days_to_next_obligation": days_to_next_obligation,
        "cash_needed_until_obligation": round(cash_needed_until_obligation, 6),
    }
