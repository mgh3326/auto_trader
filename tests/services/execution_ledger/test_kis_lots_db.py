"""Task #963 — DB-backed tests for the KIS live KR ledger lots loader (test DB only).

Timestamps sit in 2099 so this file's reconcile runs are always the "latest kis
run" regardless of other rows in the shared test database, and every row uses a
unique symbol/order number that the test deletes afterwards.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete

from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun
from app.models.review import KISLiveOrderLedger
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.kis_lots import (
    PositionRef,
    load_kis_live_kr_lot_blocks,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# 2099-06-10 14:30 KST
NOW = datetime(2099, 6, 10, 5, 30, tzinfo=UTC)
SYM = "T96301"
OTHER_SYM = "T96302"
ORDER_PREFIX = "T963"


def _fill(**overrides: Any) -> ExecutionLedger:
    data: dict[str, Any] = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": SYM,
        "raw_symbol": SYM,
        "side": "buy",
        "broker_order_id": f"{ORDER_PREFIX}-{uuid.uuid4().hex[:8]}",
        "fill_seq": 0,
        "filled_qty": Decimal("5"),
        "filled_price": Decimal("1000"),
        "filled_at": NOW - timedelta(days=5),
        "currency": "KRW",
        "source": "reconciler",
    }
    data.update(overrides)
    return ExecutionLedger(**ExecutionLedgerUpsert(**data).model_dump())


def _order(order_no: str, **overrides: Any) -> KISLiveOrderLedger:
    data: dict[str, Any] = {
        "trade_date": NOW - timedelta(hours=4),
        "symbol": SYM,
        "instrument_type": "equity_kr",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("5"),
        "price": Decimal("1000"),
        "order_no": order_no,
        "account_mode": "kis_live",
        "broker": "kis",
        "status": "accepted",
        "lifecycle_state": "accepted",
    }
    data.update(overrides)
    return KISLiveOrderLedger(**data)


def _run(*, finished_minutes_ago: int | None, dry_run: bool = False):
    started = NOW - timedelta(minutes=(finished_minutes_ago or 0) + 1)
    return ExecutionLedgerReconcileRun(
        run_id=uuid.uuid4(),
        broker="kis",
        window_start=started - timedelta(hours=24),
        window_end=started,
        started_at=started,
        finished_at=(
            None
            if finished_minutes_ago is None
            else NOW - timedelta(minutes=finished_minutes_ago)
        ),
        dry_run=dry_run,
    )


async def _cleanup(db_session, run_ids: list[uuid.UUID], order_nos: list[str]) -> None:
    await db_session.rollback()
    await db_session.execute(
        delete(ExecutionLedger).where(ExecutionLedger.symbol.in_([SYM, OTHER_SYM]))
    )
    await db_session.execute(
        delete(ExecutionLedger).where(
            ExecutionLedger.broker_order_id.like(f"{ORDER_PREFIX}-%")
        )
    )
    await db_session.execute(
        delete(KISLiveOrderLedger).where(KISLiveOrderLedger.order_no.in_(order_nos))
    )
    await db_session.execute(
        delete(ExecutionLedgerReconcileRun).where(
            ExecutionLedgerReconcileRun.run_id.in_(run_ids)
        )
    )
    await db_session.commit()


async def test_loader_projects_only_kis_live_kr_rows_for_requested_symbols(
    db_session,
) -> None:
    run = _run(finished_minutes_ago=10)
    run_ids = [run.run_id]
    order_nos = [f"{ORDER_PREFIX}{i}" for i in range(1, 7)]
    db_session.add_all(
        [
            run,
            # counted: seed lot + reconciled buy
            _fill(
                side="buy",
                filled_qty=Decimal("10"),
                filled_price=Decimal("2000"),
                source="manual_import",
                broker_order_id=f"{ORDER_PREFIX}-SEED",
                filled_at=NOW - timedelta(days=20),
            ),
            _fill(filled_qty=Decimal("5"), filled_price=Decimal("1800")),
            # decoys that must NOT leak into the projection
            _fill(broker="toss", venue="toss_krx", filled_qty=Decimal("99")),
            _fill(account_mode="mock", filled_qty=Decimal("98")),
            _fill(
                instrument_type="equity_us",
                venue="NASD",
                currency="USD",
                filled_qty=Decimal("97"),
            ),
            _fill(symbol=OTHER_SYM, raw_symbol=OTHER_SYM, filled_qty=Decimal("96")),
            # S2: same-day non-terminal buy for SYM, plus decoys
            _order(order_nos[0], status="accepted"),
            _order(order_nos[1], status="accepted", trade_date=NOW - timedelta(days=1)),
            _order(order_nos[2], status="accepted", side="sell"),
            _order(order_nos[3], status="accepted", account_mode="kis_mock"),
            _order(order_nos[4], status="accepted", symbol=OTHER_SYM),
            _order(order_nos[5], status="filled"),
        ]
    )
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            db_session,
            [PositionRef(SYM, Decimal("15"), Decimal("1700"))],
            now=NOW,
        )
    finally:
        await _cleanup(db_session, run_ids, order_nos)

    assert set(blocks) == {SYM}
    block = blocks[SYM]
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert [(lot["origin"], lot["quantity"]) for lot in block["lots"]] == [
        ("opening_seed", "10"),
        ("fill", "5"),
    ]
    assert block["freshness"]["state"] == "fresh"
    assert block["freshness"]["lag_minutes"] == 10.0
    evidence = block["open_buy_evidence"]
    assert evidence["state"] == "known"
    assert evidence["blocking"] is True
    assert [o["order_no"] for o in evidence["kis_live_order_ledger_open_buys"]] == [
        order_nos[0]
    ]
    assert [o["order_no"] for o in evidence["presumed_dead_prior_day_buys"]] == [
        order_nos[1]
    ]
    assert evidence["external_orders_verifiable"] is False


async def test_loader_dry_run_and_unfinished_runs_never_make_the_ledger_fresh(
    db_session,
) -> None:
    dry = _run(finished_minutes_ago=5, dry_run=True)
    unfinished = _run(finished_minutes_ago=None)
    run_ids = [dry.run_id, unfinished.run_id]
    db_session.add_all([dry, unfinished, _fill(filled_qty=Decimal("5"))])
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            db_session, [PositionRef(SYM, Decimal("5"))], now=NOW
        )
    finally:
        await _cleanup(db_session, run_ids, [])
    block = blocks[SYM]
    # The far-future dry run must not count; with no committed run in 2099 the
    # loader falls back to whatever real kis run exists — never "fresh" here.
    assert block["freshness"]["state"] != "fresh"
    assert block["ledger_state"] == "unknown"
    assert block["lots"] is None


async def test_loader_stale_run_is_unknown(db_session) -> None:
    run = _run(finished_minutes_ago=91)
    run_ids = [run.run_id]
    db_session.add_all([run, _fill(filled_qty=Decimal("5"))])
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            db_session, [PositionRef(SYM, Decimal("5"))], now=NOW
        )
    finally:
        await _cleanup(db_session, run_ids, [])
    assert blocks[SYM]["ledger_state"] == "unknown"
    assert blocks[SYM]["unknown_reasons"] == ["ledger_stale"]
    assert blocks[SYM]["lots"] is None


async def test_loader_symbol_with_no_rows_is_unknown_not_empty(db_session) -> None:
    run = _run(finished_minutes_ago=10)
    run_ids = [run.run_id]
    db_session.add(run)
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            db_session, [PositionRef(SYM, Decimal("5"))], now=NOW
        )
    finally:
        await _cleanup(db_session, run_ids, [])
    assert blocks[SYM]["ledger_state"] == "unknown"
    assert "no_ledger_rows" in blocks[SYM]["unknown_reasons"]
    assert blocks[SYM]["lots"] is None


async def test_order_ledger_read_failure_degrades_only_the_evidence(
    db_session,
) -> None:
    run = _run(finished_minutes_ago=10)
    run_ids = [run.run_id]
    db_session.add_all([run, _fill(filled_qty=Decimal("5"))])
    await db_session.commit()

    class OrderReadFails:
        """Delegates to the real session but fails the kis_live_order_ledger query."""

        def __init__(self, inner):
            self._inner = inner

        async def execute(self, statement, *args, **kwargs):
            if "kis_live_order_ledger" in str(statement):
                raise RuntimeError("boom")
            return await self._inner.execute(statement, *args, **kwargs)

        async def rollback(self):
            await self._inner.rollback()

    try:
        blocks = await load_kis_live_kr_lot_blocks(
            OrderReadFails(db_session),  # type: ignore[arg-type]
            [PositionRef(SYM, Decimal("5"))],
            now=NOW,
        )
    finally:
        await _cleanup(db_session, run_ids, [])
    block = blocks[SYM]
    assert block["ledger_state"] == "known"
    assert block["open_buy_evidence"]["state"] == "unknown"
    assert block["open_buy_evidence"]["blocking"] is True
    assert "order_ledger_read_failed" in block["open_buy_evidence"]["unknown_reasons"]


async def test_fills_read_failure_propagates_to_the_caller(db_session) -> None:
    class FillReadFails:
        async def execute(self, statement, *args, **kwargs):
            raise RuntimeError("boom")

        async def rollback(self):
            return None

    with pytest.raises(RuntimeError):
        await load_kis_live_kr_lot_blocks(
            FillReadFails(),  # type: ignore[arg-type]
            [PositionRef(SYM, Decimal("5"))],
            now=NOW,
        )


async def test_no_refs_reads_nothing() -> None:
    class Boom:
        async def execute(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("no query expected")

    assert await load_kis_live_kr_lot_blocks(Boom(), [], now=NOW) == {}  # type: ignore[arg-type]


async def test_935_part_b_duplicate_and_websocket_only_fills_in_the_real_ledger(
    db_session,
) -> None:
    """The same fill as a reconciler row and a websocket row with different fill_seq.

    Both rows insert (the unique key includes fill_seq), exactly like the
    06-10..09-14 production rows. Expected: no double count for the duplicate
    symbol; the websocket-only symbol is unknown via quantity_mismatch_with_reference.
    """
    dup, ws_only, ws_no_seed = "T96303", "T96304", "T96305"
    run = _run(finished_minutes_ago=10)
    run_ids = [run.run_id]

    def seed(symbol: str) -> ExecutionLedger:
        return _fill(
            symbol=symbol,
            raw_symbol=symbol,
            filled_qty=Decimal("10"),
            filled_price=Decimal("2000"),
            source="manual_import",
            broker_order_id=f"{ORDER_PREFIX}-SEED-{symbol}",
            filled_at=NOW - timedelta(days=20),
        )

    rows = [
        run,
        seed(dup),
        seed(ws_only),
        # duplicate symbol: reconciler and websocket twin, different fill_seq
        _fill(
            symbol=dup,
            raw_symbol=dup,
            broker_order_id="0000963555",
            fill_seq=0,
            filled_qty=Decimal("2"),
            filled_price=Decimal("1900"),
            source="reconciler",
            filled_at=NOW - timedelta(days=3),
        ),
        _fill(
            symbol=dup,
            raw_symbol=dup,
            broker_order_id="963555",
            fill_seq=41,
            filled_qty=Decimal("2"),
            filled_price=Decimal("1900"),
            source="websocket",
            filled_at=NOW - timedelta(days=3),
        ),
        # websocket-only fill on a seeded symbol
        _fill(
            symbol=ws_only,
            raw_symbol=ws_only,
            broker_order_id="963900",
            fill_seq=7,
            filled_qty=Decimal("2"),
            filled_price=Decimal("1900"),
            source="websocket",
            filled_at=NOW - timedelta(days=3),
        ),
        # websocket-only symbol with no seed at all
        _fill(
            symbol=ws_no_seed,
            raw_symbol=ws_no_seed,
            broker_order_id="963901",
            fill_seq=9,
            filled_qty=Decimal("12"),
            filled_price=Decimal("1900"),
            source="websocket",
            filled_at=NOW - timedelta(days=3),
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            db_session,
            [
                PositionRef(dup, Decimal("12")),
                PositionRef(ws_only, Decimal("12")),
                PositionRef(ws_no_seed, Decimal("12")),
            ],
            now=NOW,
        )
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(ExecutionLedger).where(
                ExecutionLedger.symbol.in_([dup, ws_only, ws_no_seed])
            )
        )
        await _cleanup(db_session, run_ids, [])

    dup_block = blocks[dup]
    assert dup_block["ledger_state"] == "known", dup_block["unknown_reasons"]
    assert sum(Decimal(lot["quantity"]) for lot in dup_block["lots"]) == Decimal("12")
    assert dup_block["diagnostics"]["superseded_websocket_duplicates"] == 1
    assert dup_block["provisional_rows_excluded"] == []

    only_block = blocks[ws_only]
    assert only_block["ledger_state"] == "unknown"
    assert "quantity_mismatch_with_reference" in only_block["unknown_reasons"]
    assert only_block["lots"] is None
    assert only_block["diagnostics"]["ledger_net_quantity"] == "10"

    no_seed_block = blocks[ws_no_seed]
    assert no_seed_block["ledger_state"] == "unknown"
    assert "quantity_mismatch_with_reference" in no_seed_block["unknown_reasons"]
    assert "only_provisional_rows" in no_seed_block["unknown_reasons"]
    assert no_seed_block["lots"] is None
