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


ALL_SYMBOLS = [
    SYM,
    OTHER_SYM,
    "T96303",
    "T96304",
    "T96305",
    "T96306",
    "T97301",
    "T97302",
    "T97303",
]


async def _cleanup(db_session, run_ids: list[uuid.UUID], order_nos: list[str]) -> None:
    """Delete every row this file can create. One rollback first, then deletes, then commit."""
    await db_session.rollback()
    await db_session.execute(
        delete(ExecutionLedger).where(ExecutionLedger.symbol.in_(ALL_SYMBOLS))
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


async def test_same_day_sell_fill_and_future_dated_order_row_block(db_session) -> None:
    sell_sym, skew_sym = "T96306", "T96303"
    run = _run(finished_minutes_ago=10)
    run_ids = [run.run_id]
    order_nos = [f"{ORDER_PREFIX}9"]
    db_session.add_all(
        [
            run,
            _fill(
                symbol=sell_sym,
                raw_symbol=sell_sym,
                filled_qty=Decimal("12"),
                filled_price=Decimal("2000"),
                source="manual_import",
                broker_order_id=f"{ORDER_PREFIX}-SEED-{sell_sym}",
                filled_at=NOW - timedelta(days=20),
            ),
            _fill(
                symbol=sell_sym,
                raw_symbol=sell_sym,
                side="sell",
                filled_qty=Decimal("2"),
                filled_price=Decimal("2100"),
                broker_order_id="963777",
                filled_at=NOW - timedelta(hours=3),
            ),
            _fill(
                symbol=skew_sym,
                raw_symbol=skew_sym,
                filled_qty=Decimal("5"),
                source="manual_import",
                broker_order_id=f"{ORDER_PREFIX}-SEED-{skew_sym}",
                filled_at=NOW - timedelta(days=20),
            ),
            # writer clock skew: the order row is stamped on the NEXT KST day
            _order(
                order_nos[0],
                symbol=skew_sym,
                status="accepted",
                trade_date=NOW + timedelta(hours=12),
            ),
        ]
    )
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            db_session,
            [PositionRef(sell_sym, Decimal("10")), PositionRef(skew_sym, Decimal("5"))],
            now=NOW,
        )
    finally:
        await _cleanup(db_session, run_ids, order_nos)

    sold = blocks[sell_sym]
    assert sold["ledger_state"] == "known", sold["unknown_reasons"]
    assert sold["net_quantity"] == "10"  # the sell is folded into the FIFO lots
    sell_evidence = sold["same_day_sell_evidence"]
    assert sell_evidence["state"] == "known"
    assert sell_evidence["blocking"] is True
    assert sell_evidence["blocking_reasons"] == ["same_day_sell_fill_in_ledger"]
    assert [f["broker_order_id"] for f in sell_evidence["fills"]] == ["963777"]
    assert sold["open_buy_evidence"]["blocking"] is False  # buy side is clean

    skewed = blocks[skew_sym]["open_buy_evidence"]
    assert skewed["blocking"] is True  # future-dated non-terminal row fails closed
    assert skewed["presumed_dead_prior_day_buys"] == []


async def test_973_pre_seed_reconciler_rows_are_superseded(db_session) -> None:
    """#973 desk scenario end-to-end: pre-seed reconciler history is inside the
    seed; only rows at-or-after the seed's cutover instant count toward lots.

    The re-seeded symbol carries TWO manual_import generations (the seed CLI's
    order id embeds the cutover date, so a re-seed inserts a new row rather
    than updating); the latest seed governs and the older seed is superseded
    together with all pre-cutover history. The seedless symbol is untouched.
    """
    seeded, reseeded, seedless = "T97301", "T97302", "T97303"
    cutover = datetime(2099, 5, 10, tzinfo=UTC)  # seed filled_at == cutover
    recutover = datetime(2099, 6, 1, tzinfo=UTC)
    run = _run(finished_minutes_ago=10)
    run_ids = [run.run_id]

    def seed(symbol: str, qty: str, when: datetime) -> ExecutionLedger:
        return _fill(
            symbol=symbol,
            raw_symbol=symbol,
            filled_qty=Decimal(qty),
            filled_price=Decimal("2000"),
            source="manual_import",
            broker_order_id=f"SEED-{when:%Y%m%d}-kis-krx-{symbol}",
            filled_at=when,
        )

    def rec(symbol: str, side: str, qty: str, when: datetime, tag: str):
        return _fill(
            symbol=symbol,
            raw_symbol=symbol,
            side=side,
            filled_qty=Decimal(qty),
            broker_order_id=f"{ORDER_PREFIX}-{symbol}-{tag}",
            filled_at=when,
        )

    db_session.add_all(
        [
            run,
            # seeded: seed 12 + pre-seed history (net 12, inside the seed) +
            # post-cutover net -1 -> broker 11
            seed(seeded, "12", cutover),
            rec(seeded, "buy", "12", cutover - timedelta(days=60), "p1"),
            rec(seeded, "sell", "2", cutover + timedelta(days=2), "q1"),
            rec(seeded, "buy", "1", cutover + timedelta(days=20), "q2"),
            # re-seeded: first generation seed 4 @05-10 plus its history, then
            # a second generation seed 6 @06-01 -> only the latest seed counts
            seed(reseeded, "4", cutover),
            rec(reseeded, "buy", "5", cutover - timedelta(days=70), "p1"),
            rec(reseeded, "buy", "1", cutover + timedelta(days=8), "q1"),
            seed(reseeded, "6", recutover),
            # seedless: no seed, nothing may be filtered
            rec(seedless, "buy", "3", cutover - timedelta(days=50), "p1"),
            rec(seedless, "buy", "2", cutover - timedelta(days=10), "p2"),
        ]
    )
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            db_session,
            [
                PositionRef(seeded, Decimal("11"), Decimal("1900")),
                PositionRef(reseeded, Decimal("6"), Decimal("1900")),
                PositionRef(seedless, Decimal("5"), Decimal("1900")),
            ],
            now=NOW,
        )
    finally:
        await _cleanup(db_session, run_ids, [])

    seeded_block = blocks[seeded]
    assert seeded_block["ledger_state"] == "known", seeded_block["unknown_reasons"]
    assert seeded_block["net_quantity"] == "11"
    diag = seeded_block["diagnostics"]
    assert diag["seed_cutover"] == cutover.isoformat()
    assert diag["authoritative_row_count"] == 4
    assert diag["counted_row_count"] == 3
    assert [r["broker_order_id"] for r in diag["pre_seed_rows_superseded"]] == [
        f"{ORDER_PREFIX}-{seeded}-p1"
    ]

    reseeded_block = blocks[reseeded]
    assert reseeded_block["ledger_state"] == "known", reseeded_block["unknown_reasons"]
    assert reseeded_block["net_quantity"] == "6"
    rdiag = reseeded_block["diagnostics"]
    assert rdiag["seed_cutover"] == recutover.isoformat()
    # the older seed row is superseded along with all pre-cutover history
    superseded_ids = {r["broker_order_id"] for r in rdiag["pre_seed_rows_superseded"]}
    assert superseded_ids == {
        f"SEED-{cutover:%Y%m%d}-kis-krx-{reseeded}",
        f"{ORDER_PREFIX}-{reseeded}-p1",
        f"{ORDER_PREFIX}-{reseeded}-q1",
    }
    assert rdiag["counted_row_count"] == 1

    seedless_block = blocks[seedless]
    assert seedless_block["ledger_state"] == "known", seedless_block["unknown_reasons"]
    assert seedless_block["net_quantity"] == "5"
    assert seedless_block["diagnostics"]["seed_cutover"] is None
    assert seedless_block["diagnostics"]["pre_seed_rows_superseded"] == []


async def test_zz_this_file_leaves_no_rows_behind(db_session) -> None:
    """Regression for the round-1 finding: shared test DB rows must not leak."""
    from sqlalchemy import func, select

    await db_session.rollback()
    leaked = (
        await db_session.execute(
            select(func.count())
            .select_from(ExecutionLedger)
            .where(ExecutionLedger.symbol.in_(ALL_SYMBOLS))
        )
    ).scalar_one()
    assert leaked == 0
    orders = (
        await db_session.execute(
            select(func.count())
            .select_from(KISLiveOrderLedger)
            .where(KISLiveOrderLedger.order_no.like(f"{ORDER_PREFIX}%"))
        )
    ).scalar_one()
    assert orders == 0
