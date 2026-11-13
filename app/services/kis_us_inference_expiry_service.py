"""Transactional operator-only inference; never contacts a broker."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerReconcileRun,
    execution_ledger_in_effect,
)
from app.models.review import KISUSInferenceExpiryEvent, LiveOrderLedger
from app.services.execution_ledger.kis_lots import us_trading_day_window
from app.services.execution_ledger.supersede import normalize_order_id, order_identity
from app.services.kis_us_inference_expiry import (
    DECISION_REF,
    MARKER,
    RULE_VERSION,
    Facts,
    classify,
    deadline_for,
)

KST = timezone(timedelta(hours=9))
COMMIT_TIMEOUT_SECONDS = 10


def _accept_at(row: LiveOrderLedger) -> datetime | None:
    if (
        row.trade_date is None
        or row.trade_date.tzinfo is None
        or not isinstance(row.order_time, str)
    ):
        return None
    raw = row.order_time.strip()
    if re.fullmatch(r"[0-9]{4}(?:[0-9]{2})?", raw) is None:
        return None
    try:
        digits = (raw + "00")[:6]
        local = row.trade_date.astimezone(KST)
        result = local.replace(
            hour=int(digits[:2]),
            minute=int(digits[2:4]),
            second=int(digits[4:6]),
            microsecond=0,
        )
    except ValueError:
        return None
    # Do not infer a day from a broker time that conflicts with the recorded send.
    if result > row.trade_date:
        return None
    return result


async def _decide(session: AsyncSession, row: LiveOrderLedger, now: datetime) -> dict:
    accepted = _accept_at(row)
    deadline = deadline_for(accepted)
    identity_valid = bool(
        row.order_no
        and normalize_order_id(row.order_no)
        and row.symbol
        and row.side in ("buy", "sell")
        and row.currency == "USD"
    )
    order_count = symbol_count = None
    covered = None
    run = None
    if accepted is not None and deadline is not None:
        start, end = us_trading_day_window(accepted)
        fills = (
            await session.execute(
                select(
                    ExecutionLedger.broker,
                    ExecutionLedger.account_mode,
                    ExecutionLedger.instrument_type,
                    ExecutionLedger.symbol,
                    ExecutionLedger.side,
                    ExecutionLedger.currency,
                    ExecutionLedger.broker_order_id,
                    ExecutionLedger.filled_at,
                ).where(
                    ExecutionLedger.broker == "kis",
                    ExecutionLedger.account_mode == "live",
                    ExecutionLedger.instrument_type == "equity_us",
                    ExecutionLedger.filled_at >= start,
                    ExecutionLedger.filled_at <= end,
                    execution_ledger_in_effect(),
                )
            )
        ).all()
        if identity_valid:
            target = SimpleNamespace(
                broker="kis",
                account_mode="live",
                instrument_type="equity_us",
                symbol=row.symbol,
                side=row.side,
                currency=row.currency,
                broker_order_id=row.order_no,
                filled_at=accepted,
            )
            order_count = sum(
                fill.filled_at < end and order_identity(fill) == order_identity(target)
                for fill in fills
            )
        from app.core.symbol import to_db_symbol

        symbol_count = (
            sum(
                to_db_symbol(fill.symbol).upper() == to_db_symbol(row.symbol).upper()
                and accepted <= fill.filled_at <= deadline
                for fill in fills
            )
            if row.symbol
            else None
        )
        run = await session.scalar(
            select(ExecutionLedgerReconcileRun.run_id)
            .where(
                ExecutionLedgerReconcileRun.broker == "kis",
                ExecutionLedgerReconcileRun.dry_run.is_(False),
                ExecutionLedgerReconcileRun.error_summary.is_(None),
                ExecutionLedgerReconcileRun.window_start <= accepted,
                ExecutionLedgerReconcileRun.window_end >= deadline,
                ExecutionLedgerReconcileRun.finished_at > deadline,
            )
            .limit(1)
        )
        covered = run is not None
    facts = Facts(
        ledger_id=row.id,
        broker=row.broker,
        account_scope=row.account_scope,
        market=row.market,
        status=row.status,
        lifecycle_state=row.lifecycle_state,
        order_kind=row.order_kind,
        accepted_at=accepted,
        filled_qty=row.filled_qty,
        identity_valid=identity_valid,
        order_fills=order_count,
        symbol_fills=symbol_count,
        covering_run=covered,
    )
    result = classify(facts, now)
    result.update(
        {
            "reconcile_run_id": str(run) if covered else None,
            "symbol": row.symbol,
            "order_no": row.order_no,
            "side": row.side,
            "currency": row.currency,
            "order_kind": row.order_kind,
            "status": row.status,
            "lifecycle_state": row.lifecycle_state,
            "filled_qty": str(row.filled_qty) if row.filled_qty is not None else None,
        }
    )
    return result


async def _candidates(
    session: AsyncSession, *, lock: bool = False
) -> list[LiveOrderLedger]:
    stmt = (
        select(LiveOrderLedger)
        .where(
            LiveOrderLedger.broker == "kis",
            LiveOrderLedger.account_scope == "kis_live",
            LiveOrderLedger.market == "us",
            LiveOrderLedger.status.in_(("accepted", "pending")),
        )
        .order_by(LiveOrderLedger.id)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update()
    return list((await session.execute(stmt)).scalars().all())


async def preview(session: AsyncSession, *, now: datetime | None = None) -> list[dict]:
    current = now or datetime.now(UTC)
    return [await _decide(session, row, current) for row in await _candidates(session)]


async def commit(
    session: AsyncSession, *, ids: list[int], decision_ref: str, actor: str, reason: str
) -> dict:
    if (
        decision_ref != DECISION_REF
        or not actor.strip()
        or not reason.strip()
        or not ids
        or len(ids) > 45
        or len(set(ids)) != len(ids)
        or any(type(i) is not int or i <= 0 for i in ids)
    ):
        raise ValueError("invalid commit arguments")
    async with asyncio.timeout(COMMIT_TIMEOUT_SECONDS), session.begin():
        await session.execute(text("SET LOCAL lock_timeout = '2s'"))
        await session.execute(text("SET LOCAL statement_timeout = '5s'"))
        # Blocks every fill insert/update until after the facts and transitions commit.
        await session.execute(text("LOCK TABLE review.execution_ledger IN SHARE MODE"))
        rows = await _candidates(session, lock=True)
        now = datetime.now(UTC)
        decisions = [await _decide(session, row, now) for row in rows]
        eligible = {d["ledger_id"] for d in decisions if d["eligible"]}
        events = (
            (
                await session.execute(
                    select(KISUSInferenceExpiryEvent).where(
                        KISUSInferenceExpiryEvent.ledger_id.in_(ids),
                        KISUSInferenceExpiryEvent.decision_ref == decision_ref,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(events) == len(ids) and len({event.batch_id for event in events}) == 1:
            batch_id = events[0].batch_id
            batch_ids = set(
                (
                    await session.execute(
                        select(KISUSInferenceExpiryEvent.ledger_id).where(
                            KISUSInferenceExpiryEvent.batch_id == batch_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            if batch_ids == set(ids):
                return {"changed": 0, "rows": decisions}
        if eligible != set(ids):
            raise ValueError("ids must exactly match the eligible preview set")
        batch_id = uuid.uuid4()
        for row, decision in zip(rows, decisions, strict=True):
            if row.id not in eligible:
                continue
            detail = {
                "reason_code": MARKER,
                "expiry_basis": "inference",
                "expiry_caveat": "no_broker_original",
                "decision_ref": decision_ref,
                "rule_version": RULE_VERSION,
                "facts": decision,
            }
            row.status = row.lifecycle_state = "expired"
            row.reconciled_at = now
            row.last_reconcile_detail = detail
            session.add(
                KISUSInferenceExpiryEvent(
                    ledger_id=row.id,
                    batch_id=batch_id,
                    decision_ref=decision_ref,
                    rule_version=RULE_VERSION,
                    actor=actor,
                    reason=reason,
                    facts=decision,
                )
            )
        await session.flush()
        return {"changed": len(eligible), "rows": decisions}
