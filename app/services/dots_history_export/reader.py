"""Read-only source queries: an explicit column list and no writes.

Every statement names its columns, so raw broker payloads, prose fields and
account identifiers other than the denylist inputs are never fetched at all.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.paper_trading import PaperAccount, PaperPendingOrder, PaperTrade
from app.models.review import (
    AlpacaPaperOrderLedger,
    KISLiveOrderLedger,
    KISMockOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)
from app.services.dots_history_export.projection import SourceRows, Window


class ReadOnlyViolation(RuntimeError):
    """The transaction is not read-only; nothing was read."""


def kis_live_statement(window: Window) -> Select:
    m = KISLiveOrderLedger
    return (
        select(
            m.id,
            m.trade_date,
            m.symbol,
            m.instrument_type,
            m.side,
            m.order_type,
            m.quantity,
            m.price,
            m.amount,
            m.currency,
            m.order_no,
            m.status,
            m.lifecycle_state,
            m.strategy,
            m.report_item_uuid,
            m.idempotency_key,
            m.correlation_id,
            m.filled_qty,
            m.avg_fill_price,
            m.reconciled_at,
            m.created_at,
        )
        .where(m.created_at >= window.since, m.created_at < window.until)
        .order_by(m.id)
    )


def live_statement(window: Window) -> Select:
    m = LiveOrderLedger
    return (
        select(
            m.id,
            m.trade_date,
            m.broker,
            m.account_scope,
            m.market,
            m.symbol,
            m.exchange,
            m.side,
            m.order_kind,
            m.quantity,
            m.price,
            m.amount,
            m.currency,
            m.order_no,
            m.status,
            m.lifecycle_state,
            m.strategy,
            m.report_item_uuid,
            m.idempotency_key,
            m.correlation_id,
            m.filled_qty,
            m.avg_fill_price,
            m.reconciled_at,
            m.created_at,
        )
        .where(m.created_at >= window.since, m.created_at < window.until)
        .order_by(m.id)
    )


def toss_statement(window: Window) -> Select:
    m = TossLiveOrderLedger
    return (
        select(
            m.id,
            m.trade_date,
            m.operation_kind,
            m.market,
            m.symbol,
            m.side,
            m.order_type,
            m.time_in_force,
            m.quantity,
            m.price,
            m.order_amount,
            m.currency,
            m.client_order_id,
            m.broker_order_id,
            m.original_order_id,
            m.replaced_by_order_id,
            m.status,
            m.strategy,
            m.report_item_uuid,
            m.correlation_id,
            m.filled_qty,
            m.avg_fill_price,
            m.reconciled_at,
            m.expired_at,
            m.created_at,
        )
        .where(m.created_at >= window.since, m.created_at < window.until)
        .order_by(m.id)
    )


def kis_mock_statement(window: Window) -> Select:
    m = KISMockOrderLedger
    return (
        select(
            m.id,
            m.trade_date,
            m.symbol,
            m.instrument_type,
            m.side,
            m.order_type,
            m.quantity,
            m.price,
            m.amount,
            m.currency,
            m.order_no,
            m.status,
            m.lifecycle_state,
            m.strategy,
            m.report_item_uuid,
            m.correlation_id,
            m.last_reconcile_detail["attributed_fill_qty"].astext.label(
                "attributed_fill_qty"
            ),
            m.reconciled_at,
            m.created_at,
        )
        .where(m.created_at >= window.since, m.created_at < window.until)
        .order_by(m.id)
    )


def alpaca_statement(window: Window) -> Select:
    m = AlpacaPaperOrderLedger
    return (
        select(
            m.id,
            m.client_order_id,
            m.lifecycle_correlation_id,
            m.record_kind,
            m.account_mode,
            m.lifecycle_state,
            m.strategy_key,
            m.execution_symbol,
            m.execution_venue,
            m.instrument_type,
            m.side,
            m.order_type,
            m.time_in_force,
            m.requested_qty,
            m.requested_notional,
            m.requested_price,
            m.currency,
            m.broker_order_id,
            m.submitted_at,
            m.order_status,
            m.filled_qty,
            m.filled_avg_price,
            m.canceled_at,
            m.created_at,
        )
        .where(m.created_at >= window.since, m.created_at < window.until)
        .order_by(m.id)
    )


def paper_pending_statement(window: Window) -> Select:
    m = PaperPendingOrder
    return (
        select(
            m.id,
            m.account_id,
            PaperAccount.strategy_name,
            m.symbol,
            m.side,
            m.order_type,
            m.limit_price,
            m.quantity,
            m.status,
            m.correlation_id,
            m.fill_price,
            m.placed_at,
            m.filled_at,
            m.cancelled_at,
        )
        .join(PaperAccount, PaperAccount.id == m.account_id)
        .where(m.placed_at >= window.since, m.placed_at < window.until)
        .order_by(m.id)
    )


def paper_trades_statement(window: Window) -> Select:
    m = PaperTrade
    return (
        select(
            m.id,
            m.account_id,
            PaperAccount.strategy_name,
            PaperPendingOrder.id.label("pending_order_id"),
            m.symbol,
            m.instrument_type,
            m.side,
            m.order_type,
            m.quantity,
            m.price,
            m.currency,
            m.correlation_id,
            m.executed_at,
        )
        .join(PaperAccount, PaperAccount.id == m.account_id)
        .outerjoin(PaperPendingOrder, PaperPendingOrder.paper_trade_id == m.id)
        .where(m.executed_at >= window.since, m.executed_at < window.until)
        .order_by(m.id)
    )


def execution_ledger_statement(window: Window) -> Select:
    """In-effect fills from the window start with no upper bound.

    An order placed inside the window can fill after it, and its row must still
    show that fill. Rows past the window are not exported as fills.
    """
    m = ExecutionLedger
    return (
        select(
            m.id,
            m.broker,
            m.account_mode,
            m.venue,
            m.instrument_type,
            m.symbol,
            m.side,
            m.broker_order_id,
            m.filled_qty,
            m.filled_price,
            m.filled_at,
            m.currency,
            m.correlation_id,
            m.source,
            m.created_at,
        )
        .where(
            execution_ledger_in_effect(),
            m.filled_at >= window.since,
        )
        .order_by(m.id)
    )


def proposals_statement(window: Window) -> Select:
    m = OrderProposal
    return (
        select(
            m.id,
            m.proposal_id,
            m.root_proposal_id,
            m.revision,
            m.symbol,
            m.market,
            m.account_mode,
            m.side,
            m.order_type,
            m.strategy,
            m.broker_account_id,
            m.action,
            m.target_broker_order_id,
            m.exit_intent,
            m.void_reason,
            m.lifecycle_state,
            m.correlation_id,
            m.valid_until,
            m.validated_at,
            m.approved_at,
            m.created_at,
        )
        .where(m.created_at >= window.since, m.created_at < window.until)
        .order_by(m.id)
    )


def rungs_statement(window: Window) -> Select:
    m = OrderProposalRung
    return (
        select(
            m.id,
            OrderProposal.proposal_id,
            OrderProposal.account_mode,
            OrderProposal.market,
            OrderProposal.symbol,
            m.rung_index,
            m.side,
            m.quantity,
            m.limit_price,
            m.notional,
            m.state,
            m.idempotency_key,
            m.broker_order_id,
            m.correlation_id,
            m.validated_at,
            m.filled_qty,
            m.void_reason,
            m.void_reason_group,
            m.created_at,
            m.updated_at,
        )
        .join(OrderProposal, OrderProposal.id == m.proposal_pk)
        .where(
            OrderProposal.created_at >= window.since,
            OrderProposal.created_at < window.until,
        )
        .order_by(m.id)
    )


async def _begin_read_only(session: AsyncSession) -> None:
    # One repeatable-read snapshot, so refs and row counts agree across tables.
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    )
    result = await session.execute(text("SHOW transaction_read_only"))
    if result.scalar_one() != "on":
        raise ReadOnlyViolation("transaction_read_only is not on")


async def read_sources(session: AsyncSession, window: Window) -> SourceRows:
    await _begin_read_only(session)

    async def rows(statement: Select) -> list[dict]:
        result = await session.execute(statement)
        return [dict(row) for row in result.mappings().all()]

    return SourceRows(
        kis_live_order_ledger=await rows(kis_live_statement(window)),
        live_order_ledger=await rows(live_statement(window)),
        toss_live_order_ledger=await rows(toss_statement(window)),
        kis_mock_order_ledger=await rows(kis_mock_statement(window)),
        alpaca_paper_order_ledger=await rows(alpaca_statement(window)),
        paper_pending_orders=await rows(paper_pending_statement(window)),
        paper_trades=await rows(paper_trades_statement(window)),
        execution_ledger=await rows(execution_ledger_statement(window)),
        order_proposals=await rows(proposals_statement(window)),
        order_proposal_rungs=await rows(rungs_statement(window)),
    )


def default_since(until: datetime) -> datetime:
    """Six calendar months before ``until``, clamped to the month's last day."""
    month_index = until.year * 12 + (until.month - 1) - 6
    year, month = divmod(month_index, 12)
    month += 1
    return until.replace(
        year=year, month=month, day=min(until.day, monthrange(year, month)[1])
    )
