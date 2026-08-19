"""ROB-1297 side task: toss AMZN/GOOGL manual leftover cleanup + conflict guard.

This module is the **write** surface. It is not imported by the market-close
digest aggregator. Dry-run is the default; ``commit`` requires ``confirm``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType
from app.models.review import TossLiveOrderLedger
from app.schemas.session_context import SessionContextAppendEntry, SessionContextRefs
from app.services.session_context import SessionContextService

TOSS_LEFTOVER_TICKERS: tuple[str, ...] = ("AMZN", "GOOGL")
_FILLED_STATUSES = ("filled", "partial")


@dataclass(frozen=True)
class LeftoverManualRow:
    holding_id: int
    ticker: str
    quantity: str
    broker_account_id: int
    broker_type: str
    is_mock: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ManualBrokerConflict:
    ticker: str
    manual_holding_id: int
    broker_account_id: int
    reason: str


@dataclass
class LeftoverCleanupResult:
    matched: int
    deleted: int
    warnings_written: int
    dry_run: bool
    rows: tuple[LeftoverManualRow, ...]
    conflicts: tuple[ManualBrokerConflict, ...]


def leftover_reasons(
    *,
    ticker: str,
    is_mock: bool,
    filled_sell_symbols: set[str],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if ticker in TOSS_LEFTOVER_TICKERS:
        reasons.append("toss_us_allowlist")
    if ticker in filled_sell_symbols:
        reasons.append("filled_sell_in_toss_ledger")
    if is_mock:
        reasons.append("account_mode_mock_mismatch")
    return tuple(reasons)


def detect_manual_broker_conflicts(
    leftovers: tuple[LeftoverManualRow, ...],
) -> tuple[ManualBrokerConflict, ...]:
    """Manual toss row vs broker-ledger filled sell = double-count risk."""
    conflicts: list[ManualBrokerConflict] = []
    for row in leftovers:
        if "filled_sell_in_toss_ledger" not in row.reasons:
            continue
        conflicts.append(
            ManualBrokerConflict(
                ticker=row.ticker,
                manual_holding_id=row.holding_id,
                broker_account_id=row.broker_account_id,
                reason="manual_row_conflicts_with_broker_fill",
            )
        )
    return tuple(conflicts)


async def list_toss_leftover_manual_rows(
    session: AsyncSession,
) -> tuple[LeftoverManualRow, ...]:
    filled_stmt = select(TossLiveOrderLedger.symbol).where(
        TossLiveOrderLedger.market == "us",
        TossLiveOrderLedger.side == "sell",
        TossLiveOrderLedger.status.in_(_FILLED_STATUSES),
        TossLiveOrderLedger.symbol.in_(TOSS_LEFTOVER_TICKERS),
    )
    filled_sell_symbols = {
        str(symbol) for symbol in (await session.scalars(filled_stmt)).all()
    }

    stmt = (
        select(ManualHolding)
        .join(BrokerAccount)
        .where(
            BrokerAccount.broker_type == "toss",
            BrokerAccount.is_active.is_(True),
            ManualHolding.market_type == MarketType.US,
            ManualHolding.ticker.in_(TOSS_LEFTOVER_TICKERS),
        )
        .options(selectinload(ManualHolding.broker_account))
        .order_by(ManualHolding.ticker, ManualHolding.id)
    )
    holdings = list((await session.scalars(stmt)).all())
    rows: list[LeftoverManualRow] = []
    for holding in holdings:
        account = holding.broker_account
        reasons = leftover_reasons(
            ticker=holding.ticker,
            is_mock=bool(account.is_mock),
            filled_sell_symbols=filled_sell_symbols,
        )
        if not reasons:
            continue
        rows.append(
            LeftoverManualRow(
                holding_id=holding.id,
                ticker=holding.ticker,
                quantity=str(holding.quantity),
                broker_account_id=account.id,
                broker_type=account.broker_type,
                is_mock=bool(account.is_mock),
                reasons=reasons,
            )
        )
    return tuple(rows)


async def cleanup_toss_leftover_manual_rows(
    session: AsyncSession,
    *,
    commit: bool,
    confirm: bool,
    warn_session: bool = False,
    kst_date: date | None = None,
) -> LeftoverCleanupResult:
    rows = await list_toss_leftover_manual_rows(session)
    conflicts = detect_manual_broker_conflicts(rows)
    if not commit:
        return LeftoverCleanupResult(
            matched=len(rows),
            deleted=0,
            warnings_written=0,
            dry_run=True,
            rows=rows,
            conflicts=conflicts,
        )
    if not confirm:
        raise ValueError("commit requires confirm=True")

    deleted = 0
    if rows:
        ids = [row.holding_id for row in rows]
        holdings = list(
            (
                await session.scalars(
                    select(ManualHolding).where(ManualHolding.id.in_(ids))
                )
            ).all()
        )
        for holding in holdings:
            await session.delete(holding)
            deleted += 1

    warnings_written = 0
    if warn_session and conflicts:
        entries = [
            SessionContextAppendEntry(
                kst_date=kst_date,
                market="us",
                account_scope=None,
                entry_type="constraint",
                title=f"manual 행이 브로커 원본과 충돌: {conflict.ticker}",
                body=(
                    f"toss manual_holdings id={conflict.manual_holding_id} "
                    f"conflicts with toss live filled sell ({conflict.reason})"
                ),
                refs=SessionContextRefs(symbols=[conflict.ticker]),
                created_by="system",
                session_label="rob-1297-manual-conflict",
            )
            for conflict in conflicts
        ]
        written = await SessionContextService(session).append_entries(entries)
        warnings_written = len(written)

    await session.commit()
    return LeftoverCleanupResult(
        matched=len(rows),
        deleted=deleted,
        warnings_written=warnings_written,
        dry_run=False,
        rows=rows,
        conflicts=conflicts,
    )
