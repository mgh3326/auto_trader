"""Resolve unknown Upbit intent orders from the broker (hk 5441, design B4, B30, S5).

Preview by default: it reads and prints, and writes nothing. ``--commit`` writes.

For every live order of a go-path sleeve whose end is not proven, this reads the
order from Upbit -- by its uuid when the journal has one, otherwise by its
``identifier``, which is the child key -- and records what the broker returned
as evidence. It then moves each exit handoff as far as that evidence allows.

What it never does:

* It sends, cancels and amends nothing. It only reads orders.
* It never changes a journal row. ``sending`` and ``uncertain`` stay as they are;
  the evidence row sits beside them.
* It never treats "not found" or a failed read as proof that no order exists.
  Such a child stays unresolved and is listed, for the operator.

An order found only by its identifier (the outcome was never recorded, B3) also
gets its ``review.live_order_ledger`` row through the existing ledger service if
it has none, so the ordinary reconcile can book its fills.

It uses the application's own database and Upbit settings. Run it in the same
slot as the strategy check job, before it (design B4).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.models.execution_mandates import ExecutionExitHandoff
from app.models.review import LiveOrderLedger
from app.models.strategy_sleeves import StrategySleeve
from app.services.execution_intent_gateway.order_evidence import (
    OrderReading,
    parse_order_reading,
    record_child_order_evidence,
)
from app.services.execution_intent_gateway.upbit_decisions import (
    CURRENCY,
    LEDGER_BROKER,
    ORDER_LEDGER_ACCOUNT_SCOPE,
)
from app.services.strategy_sleeves.exit_handoffs import (
    BrokerOrderReading,
    advance_exit_handoff,
    plan_card_settle,
    settle_exit_handoff,
)
from app.services.strategy_sleeves.intent_orders import (
    IntentChildOrder,
    live_orders,
    load_intent_child_orders,
)

RECORDED_BY = "script:execution_intent_upbit_resolve"
EXIT_OK = 0
EXIT_ERROR = 1

#: ``(order uuid or None, identifier or None) -> Upbit order body or None``.
OrderReader = Callable[[str | None, str | None], Awaitable[Any]]


async def read_upbit_order(order_uuid: str | None, identifier: str | None) -> Any:
    """One Upbit order by uuid, else by identifier. Raises on a failed read."""
    from app.services.brokers.upbit.orders import (
        fetch_order_by_identifier,
        fetch_order_detail,
    )

    if order_uuid:
        return await fetch_order_detail(order_uuid)
    if identifier:
        return await fetch_order_by_identifier(identifier)
    return None


async def _reading(
    reader: OrderReader, order_uuid: str | None, identifier: str | None
) -> tuple[OrderReading | None, str | None]:
    try:
        detail = await reader(order_uuid, identifier)
    except Exception as exc:  # noqa: BLE001 - a failed read proves nothing
        return None, f"read_failed:{type(exc).__name__}"
    reading = parse_order_reading(detail)
    if reading is None:
        return None, "not_found_or_unreadable"
    if order_uuid and reading.order_uuid != order_uuid:
        return None, "uuid_mismatch"
    return reading, None


async def _ensure_ledger_row(order: IntentChildOrder, reading: OrderReading) -> str:
    """B3: a send whose outcome was never recorded may have no ledger row."""
    from app.core.db import AsyncSessionLocal
    from app.mcp_server.tooling.live_order_ledger import _save_live_order_ledger

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(LiveOrderLedger.id).where(
                LiveOrderLedger.broker == LEDGER_BROKER,
                LiveOrderLedger.account_scope == ORDER_LEDGER_ACCOUNT_SCOPE,
                LiveOrderLedger.order_no == reading.order_uuid,
            )
        )
        await session.rollback()
    if existing is not None:
        return "present"
    await _save_live_order_ledger(
        broker=LEDGER_BROKER,
        account_scope=ORDER_LEDGER_ACCOUNT_SCOPE,
        market="crypto",
        symbol=order.symbol,
        exchange=None,
        market_symbol=order.symbol,
        side=order.side,
        order_kind="limit",
        quantity=float(order.quantity),
        price=float(order.limit_price),
        amount=float(order.quantity * order.limit_price),
        currency=CURRENCY,
        order_no=reading.order_uuid,
        order_time=None,
        status="accepted",
        response_code="0",
        response_message="recovered by identifier (hk 5441 B4)",
        raw_response=None,
        reason=f"intent_gateway:{order.intent_id}",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        idempotency_key=order.idempotency_key,
    )
    return "recorded"


#: Writes the missing ``live_order_ledger`` row of a recovered order.
LedgerWriter = Callable[[IntentChildOrder, OrderReading], Awaitable[str]]


async def _resolve_orders(
    session_factory: Any,
    *,
    reader: OrderReader,
    commit: bool,
    now: datetime,
    ledger_writer: LedgerWriter,
) -> list[dict[str, Any]]:
    async with session_factory() as session:
        sleeves = (
            await session.scalars(
                select(StrategySleeve).where(StrategySleeve.execution_path == "go")
            )
        ).all()
        pending: list[IntentChildOrder] = []
        for sleeve in sleeves:
            orders = await load_intent_child_orders(session, sleeve.id)
            pending.extend(order for order in live_orders(orders) if not order.resolved)
        await session.rollback()

    report: list[dict[str, Any]] = []
    for order in pending:
        entry: dict[str, Any] = {
            "child_id": order.child_id,
            "idempotency_key": order.idempotency_key,
            "symbol": order.symbol,
            "journal_state": order.state,
        }
        reading, problem = await _reading(
            reader, order.order_uuid, order.idempotency_key
        )
        if reading is None:
            # Not evidence of anything: the child stays unresolved (§1.5).
            entry["result"] = "unresolved"
            entry["detail"] = problem
            report.append(entry)
            continue
        entry.update(
            order_uuid=reading.order_uuid,
            broker_state=reading.state,
            executed_volume=format(reading.executed_volume, "f"),
            final=reading.final,
        )
        if not commit:
            entry["result"] = "would_record"
            report.append(entry)
            continue
        if order.broker_order_id is None:
            # B3: the ledger row first. Final evidence makes the child resolved,
            # and a resolved child is never looked at again -- so if the row is
            # missing and cannot be written now, no evidence is recorded and the
            # next run finds this child still pending and tries both again.
            try:
                entry["live_order_ledger"] = await ledger_writer(order, reading)
            except Exception as exc:  # noqa: BLE001 - retried by the next run
                entry["live_order_ledger"] = f"failed:{type(exc).__name__}"
                entry["result"] = "unresolved"
                entry["detail"] = "live_order_ledger_not_written"
                report.append(entry)
                continue
        async with session_factory() as session:
            entry["result"] = await record_child_order_evidence(
                session,
                child_id=order.child_id,
                reading=reading,
                observed_at=now,
                recorded_by=RECORDED_BY,
            )
            await session.commit()
        report.append(entry)
    return report


async def _move_handoffs(
    session_factory: Any, *, reader: OrderReader, commit: bool, now: datetime
) -> list[dict[str, Any]]:
    async with session_factory() as session:
        handoffs = (
            await session.scalars(
                select(ExecutionExitHandoff)
                .where(ExecutionExitHandoff.state.in_(("open", "card_claimed")))
                .order_by(ExecutionExitHandoff.id)
            )
        ).all()
        targets = [(row.id, row.state, row.symbol) for row in handoffs]
        await session.rollback()

    report: list[dict[str, Any]] = []
    for handoff_id, state, symbol in targets:
        entry: dict[str, Any] = {
            "handoff_id": handoff_id,
            "symbol": symbol,
            "state": state,
        }
        async with session_factory() as session:
            if state == "open":
                entry["after"] = await advance_exit_handoff(
                    session, handoff_id, now=now
                )
            else:
                plan = await plan_card_settle(session, handoff_id)
                if plan is None:
                    await session.rollback()
                    continue
                broker: BrokerOrderReading | None = None
                if plan.may_have_sent:
                    await session.rollback()
                    reading, problem = await _reading(
                        reader, plan.order_uuid, plan.client_identifier
                    )
                    if reading is None:
                        entry["broker_reading"] = problem
                    else:
                        broker = BrokerOrderReading(
                            reading.order_uuid, reading.state, reading.executed_volume
                        )
                result = await settle_exit_handoff(
                    session,
                    handoff_id,
                    expected_claim_count=plan.claim_count,
                    reading=broker,
                    now=now,
                )
                entry["outcome"] = result.outcome.value
                entry["after"] = result.state
                entry["detail"] = result.detail
            await (session.commit() if commit else session.rollback())
        report.append(entry)
    return report


async def run(
    *,
    commit: bool,
    session_factory: Any | None = None,
    reader: OrderReader = read_upbit_order,
    now: datetime | None = None,
    ledger_writer: LedgerWriter | None = None,
) -> dict[str, Any]:
    if session_factory is None:
        from app.core.db import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    moment = now or datetime.now(UTC)
    orders = await _resolve_orders(
        session_factory,
        reader=reader,
        commit=commit,
        now=moment,
        ledger_writer=ledger_writer or _ensure_ledger_row,
    )
    handoffs = await _move_handoffs(
        session_factory, reader=reader, commit=commit, now=moment
    )
    return {
        "mode": "commit" if commit else "preview",
        "orders": orders,
        "handoffs": handoffs,
        "unresolved_orders": sum(
            1 for entry in orders if entry.get("result") == "unresolved"
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="hk 5441: resolve unknown Upbit intent orders (read-only by default)"
    )
    parser.add_argument(
        "--commit", action="store_true", help="Record the evidence and move handoffs"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = asyncio.run(run(commit=args.commit))
    except Exception as exc:  # noqa: BLE001 - report the class, never a DSN or a key
        print(
            json.dumps({"error": f"failed:{exc.__class__.__name__}"}), file=sys.stderr
        )
        return EXIT_ERROR
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
