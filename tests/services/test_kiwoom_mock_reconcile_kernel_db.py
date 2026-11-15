"""hk 4722 / hk 4734 (3797 T-P3, T-P4) — reconcile against the test database.

CI only (``integration``): never run locally. Rows are keyed by a per-test random
``client_order_id`` and removed in ``finally``; the shared schema is left as
bootstrapped.

T-P4 proves the full mode here: one in-effect ``execution_ledger`` row per
broker delta, partial→filled deltas summing to the order quantity, a re-run
booking nothing new, journals under ``account='kiwoom_mock'`` and one
``broker='kiwoom'`` reconcile-run row.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal, engine
from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerReconcileRun,
    execution_ledger_in_effect,
)
from app.models.review import KiwoomMockOrderLedger, Trade
from app.models.trade_journal import TradeJournal
from app.services import kiwoom_mock_reconcile as kernel
from app.services.kiwoom_execution_ledger import KIWOOM_BROKER
from app.services.kiwoom_mock_order_ledger_service import (
    KiwoomMockOrderLedgerService,
    KiwoomMockTransitionRefused,
    kr_trading_day,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

TODAY = kr_trading_day(datetime.now(UTC))


def _order_no() -> str:
    """Kiwoom order numbers are numeric-only; the normalizer rejects letters."""
    return str(uuid.uuid4().int)[:12]


def _continuation() -> dict[str, str]:
    return {"cont_yn": "N", "next_key": ""}


def _detail_row(order_no: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": order_no,
        "stk_cd": "005930",
        "ord_qty": "10",
        "cntr_qty": "0",
        "ord_remnq": "10",
        "ord_uv": "70000",
        "cntr_uv": "0",
        "mdfy_cncl": "",
        "dmst_stex_tp": "KRX",
    }
    row.update(overrides)
    return row


def _detail_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "return_code": 0,
        "continuation": _continuation(),
        "acnt_ord_cntr_prps_dtl": list(rows),
    }


def _status_payload() -> dict[str, Any]:
    return {
        "return_code": 0,
        "continuation": _continuation(),
        "acnt_ord_cntr_prst_array": [],
    }


class FakeReader:
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail

    async def read_order_status(self, *, cont_yn=None, next_key=None):
        return _status_payload()

    async def read_order_detail(self, *, order_date: str, cont_yn=None, next_key=None):
        return self.detail


async def _cleanup(client_order_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(KiwoomMockOrderLedger).where(
                KiwoomMockOrderLedger.client_order_id == client_order_id
            )
        )
        await session.commit()


async def _accepted(
    client_order_id: str,
    order_no: str,
    *,
    symbol: str = "005930",
    side: str = "buy",
    quantity: int = 10,
    correlation_id: str | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        await KiwoomMockOrderLedgerService(session).record_send(
            status="accepted",
            client_order_id=client_order_id,
            idempotency_key=client_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=70000,
            broker_order_id=order_no,
            response_code="0",
            response_message="정상",
            raw_response={"return_code": 0},
            trading_day=TODAY,
            correlation_id=correlation_id,
        )


async def _uncertain(client_order_id: str, order_no: str | None) -> None:
    async with AsyncSessionLocal() as session:
        await KiwoomMockOrderLedgerService(session).record_uncertain(
            client_order_id=client_order_id,
            idempotency_key=client_order_id,
            symbol="005930",
            side="buy",
            quantity=10,
            price=70000,
            broker_order_id=order_no,
            trading_day=TODAY,
        )


async def _fetch(client_order_id: str) -> KiwoomMockOrderLedger:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(KiwoomMockOrderLedger).where(
                    KiwoomMockOrderLedger.client_order_id == client_order_id
                )
            )
        ).scalar_one()
        return row


def _ids() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    return f"kwoomm-r-{suffix}-a", f"kwoomm-r-{suffix}-b"


async def test_mark_terminal_closes_once_and_refuses_a_repeat() -> None:
    client_order_id, _ = _ids()
    order_no = _order_no()
    try:
        await _accepted(client_order_id, order_no)
        async with AsyncSessionLocal() as session:
            service = KiwoomMockOrderLedgerService(session)
            row = (
                await session.execute(
                    select(KiwoomMockOrderLedger.id).where(
                        KiwoomMockOrderLedger.client_order_id == client_order_id
                    )
                )
            ).scalar_one()
            closed = await service.mark_terminal(
                ledger_id=row, status="expired", reason="broker_expiry_confirmed"
            )
            assert closed.status == "expired"
            assert closed.lifecycle_state == "expired"
            assert closed.reconciled_at is not None
            assert closed.last_reconcile_detail["reason"] == ("broker_expiry_confirmed")
            with pytest.raises(KiwoomMockTransitionRefused):
                await service.mark_terminal(
                    ledger_id=row, status="cancelled", reason="late"
                )
        stored = await _fetch(client_order_id)
        assert stored.status == "expired"
    finally:
        await _cleanup(client_order_id)


async def test_mark_terminal_refuses_a_fill_target() -> None:
    client_order_id, _ = _ids()
    order_no = _order_no()
    try:
        await _accepted(client_order_id, order_no)
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(KiwoomMockOrderLedger.id).where(
                        KiwoomMockOrderLedger.client_order_id == client_order_id
                    )
                )
            ).scalar_one()
            with pytest.raises(ValueError):
                await KiwoomMockOrderLedgerService(session).mark_terminal(
                    ledger_id=row, status="filled", reason="not_ours"
                )
        stored = await _fetch(client_order_id)
        assert stored.status == "accepted"
    finally:
        await _cleanup(client_order_id)


async def test_promote_uncertain_to_accepted_persists_the_broker_order_id() -> None:
    client_order_id, _ = _ids()
    order_no = _order_no()
    try:
        await _uncertain(client_order_id, order_no)
        async with AsyncSessionLocal() as session:
            service = KiwoomMockOrderLedgerService(session)
            row = (
                await session.execute(
                    select(KiwoomMockOrderLedger.id).where(
                        KiwoomMockOrderLedger.client_order_id == client_order_id
                    )
                )
            ).scalar_one()
            promoted = await service.promote_uncertain_to_accepted(
                ledger_id=row, broker_order_id=order_no
            )
            assert promoted.status == "accepted"
            with pytest.raises(KiwoomMockTransitionRefused):
                await service.promote_uncertain_to_accepted(
                    ledger_id=row, broker_order_id=order_no
                )
        stored = await _fetch(client_order_id)
        assert stored.status == "accepted"
        assert stored.broker_order_id == order_no
    finally:
        await _cleanup(client_order_id)


async def test_candidate_list_covers_open_and_uncertain_only() -> None:
    open_id, expired_id = _ids()
    try:
        await _accepted(open_id, _order_no())
        await _accepted(expired_id, _order_no())
        async with AsyncSessionLocal() as session:
            service = KiwoomMockOrderLedgerService(session)
            target = (
                await session.execute(
                    select(KiwoomMockOrderLedger.id).where(
                        KiwoomMockOrderLedger.client_order_id == expired_id
                    )
                )
            ).scalar_one()
            await service.mark_terminal(
                ledger_id=target, status="expired", reason="broker_expiry_confirmed"
            )
            rows, total = await service.list_reconcile_candidates(limit=1000)
        ids = {row.client_order_id for row in rows}
        assert open_id in ids
        assert expired_id not in ids
        assert total >= len(rows)
    finally:
        await _cleanup(open_id)
        await _cleanup(expired_id)


async def test_terminal_only_end_to_end_books_no_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(kernel.TERMINAL_GATE_ENV, "true")
    open_id, uncertain_id = _ids()
    expiring_order = _order_no()
    filled_order = _order_no()
    try:
        await _accepted(open_id, expiring_order)
        await _uncertain(uncertain_id, filled_order)
        reader = FakeReader(
            _detail_payload(
                _detail_row(expiring_order, ord_remnq="0", mdfy_cncl="만료"),
                _detail_row(
                    filled_order, cntr_qty="10", ord_remnq="0", cntr_uv="70000"
                ),
            )
        )
        result = await kernel.kiwoom_mock_reconcile_orders_impl(
            dry_run=False,
            terminal_only=True,
            session_factory=AsyncSessionLocal,
            reader=reader,
            sleep=_no_sleep,
        )
        assert result["success"] is True
        by_id = {row["client_order_id"]: row for row in result["reconciled"]}
        # The outcome carries the pre-scan ledger status; the verdict/action are
        # what the run decided.
        assert by_id[open_id]["verdict"] == "expired"
        assert by_id[open_id]["action"] == "mark_expired"
        assert by_id[uncertain_id]["action"] == "promoted_accepted"

        assert (await _fetch(open_id)).status == "expired"
        # Fills are never booked in terminal-only: no filled_qty is written.
        assert (await _fetch(uncertain_id)).filled_qty is None
    finally:
        await _cleanup(open_id)
        await _cleanup(uncertain_id)


async def _no_sleep(seconds: float) -> None:
    del seconds


# ---- hk 4734 (3797 T-P4): full mode ----------------------------------------


def _symbol() -> str:
    return str(uuid.uuid4().int % 900000 + 100000)


def _full_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in kernel.FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")


def _filled_detail(
    order_no: str, quantity: int, symbol: str, average_price: int = 70000
) -> dict[str, Any]:
    return _detail_payload(
        _detail_row(
            order_no,
            stk_cd=symbol,
            ord_qty=str(quantity),
            cntr_qty=str(quantity),
            ord_remnq="0",
            cntr_uv=str(average_price),
        )
    )


def _partial_detail(order_no: str, quantity: int, symbol: str) -> dict[str, Any]:
    return _detail_payload(
        _detail_row(
            order_no,
            stk_cd=symbol,
            cntr_qty=str(quantity),
            ord_remnq=str(10 - quantity),
            cntr_uv="70000",
        )
    )


async def _fills(order_no: str) -> list[ExecutionLedger]:
    async with AsyncSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(ExecutionLedger)
                    .where(
                        ExecutionLedger.broker == KIWOOM_BROKER,
                        ExecutionLedger.broker_order_id == order_no,
                        execution_ledger_in_effect(),
                    )
                    .order_by(ExecutionLedger.id.asc())
                )
            )
            .scalars()
            .all()
        )


async def _trade(order_no: str) -> Trade | None:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Trade).where(
                    Trade.account == kernel.JOURNAL_ACCOUNT,
                    Trade.order_id == order_no,
                )
            )
        ).scalar_one_or_none()


async def _journals(symbol: str) -> list[TradeJournal]:
    async with AsyncSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(TradeJournal)
                    .where(
                        TradeJournal.account == kernel.JOURNAL_ACCOUNT,
                        TradeJournal.symbol == symbol,
                    )
                    .order_by(TradeJournal.id.asc())
                )
            )
            .scalars()
            .all()
        )


async def _cleanup_full(
    *,
    client_order_id: str,
    order_no: str,
    symbol: str,
    run_ids: list[str],
) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(KiwoomMockOrderLedger).where(
                KiwoomMockOrderLedger.client_order_id == client_order_id
            )
        )
        await session.execute(
            delete(ExecutionLedger).where(
                ExecutionLedger.broker == KIWOOM_BROKER,
                ExecutionLedger.broker_order_id == order_no,
            )
        )
        await session.execute(
            delete(Trade).where(
                Trade.account == kernel.JOURNAL_ACCOUNT,
                Trade.order_id == order_no,
            )
        )
        await session.execute(
            delete(TradeJournal).where(
                TradeJournal.account == kernel.JOURNAL_ACCOUNT,
                TradeJournal.symbol == symbol,
            )
        )
        for run_id in run_ids:
            await session.execute(
                delete(ExecutionLedgerReconcileRun).where(
                    ExecutionLedgerReconcileRun.run_id == uuid.UUID(run_id)
                )
            )
        await session.commit()


async def _full_run(reader: FakeReader) -> dict[str, Any]:
    return await kernel.kiwoom_mock_reconcile_orders_impl(
        dry_run=False,
        terminal_only=False,
        session_factory=AsyncSessionLocal,
        reader=reader,
        sleep=_no_sleep,
    )


async def test_full_mode_books_one_in_effect_row_and_a_kiwoom_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _full_mode(monkeypatch)
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(client_order_id, order_no, symbol=symbol)
        result = await _full_run(FakeReader(_filled_detail(order_no, 10, symbol)))
        if result.get("reconcile_run"):
            run_ids.append(result["reconcile_run"]["run_id"])

        assert result["success"] is True
        assert result["scope"] == "full"
        outcome = next(
            row
            for row in result["reconciled"]
            if row["client_order_id"] == client_order_id
        )
        assert outcome["action"] == "booked_filled"

        fills = await _fills(order_no)
        assert len(fills) == 1
        fill = fills[0]
        assert fill.broker == KIWOOM_BROKER == "kiwoom"
        assert fill.account_mode == "mock"
        assert fill.venue == "krx"
        assert fill.instrument_type == "equity_kr"
        assert fill.currency == "KRW"
        assert fill.source == "reconciler"
        assert fill.filled_qty == Decimal("10")
        assert fill.filled_price == Decimal("70000")
        assert fill.filled_notional == Decimal("700000")
        assert fill.fee_basis == "not_provided"
        assert fill.fee_amount is None
        assert fill.filled_at_basis == "observed_at"

        stored = await _fetch(client_order_id)
        assert stored.status == "filled"
        assert stored.filled_qty == Decimal("10")
        assert stored.journal_id is not None
        assert stored.trade_id is not None

        async with AsyncSessionLocal() as session:
            journal = (
                await session.execute(
                    select(TradeJournal).where(TradeJournal.id == stored.journal_id)
                )
            ).scalar_one()
            assert journal.account == kernel.JOURNAL_ACCOUNT == "kiwoom_mock"
            assert journal.account_type == kernel.JOURNAL_ACCOUNT_TYPE == "mock"
            assert journal.symbol == symbol
            assert journal.quantity == Decimal("10")
            assert journal.trade_id == stored.trade_id

            run = (
                await session.execute(
                    select(ExecutionLedgerReconcileRun).where(
                        ExecutionLedgerReconcileRun.run_id == uuid.UUID(run_ids[0])
                    )
                )
            ).scalar_one()
            assert run.broker == "kiwoom"
            assert run.dry_run is False
            assert run.committed_insert >= 1
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )


async def test_full_mode_partial_then_filled_books_two_deltas_and_a_rerun_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _full_mode(monkeypatch)
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(client_order_id, order_no, symbol=symbol)

        first = await _full_run(FakeReader(_partial_detail(order_no, 4, symbol)))
        if first.get("reconcile_run"):
            run_ids.append(first["reconcile_run"]["run_id"])
        assert first["scope"] == "full"
        assert first["reconciled"][0]["action"] == "booked_partial"
        fills = await _fills(order_no)
        assert [fill.filled_qty for fill in fills] == [Decimal("4")]
        assert (await _fetch(client_order_id)).status == "partial"

        replay = await _full_run(FakeReader(_partial_detail(order_no, 4, symbol)))
        if replay.get("reconcile_run"):
            run_ids.append(replay["reconcile_run"]["run_id"])
        assert replay["reconciled"][0]["action"] == "noop_already_booked"
        assert len(await _fills(order_no)) == 1
        assert replay["reconcile_run"]["committed_insert"] == 0
        assert replay["reconcile_run"]["unchanged"] >= 1

        second = await _full_run(
            FakeReader(_filled_detail(order_no, 10, symbol, average_price=76000))
        )
        if second.get("reconcile_run"):
            run_ids.append(second["reconcile_run"]["run_id"])
        assert second["reconciled"][0]["action"] == "booked_filled"
        fills = await _fills(order_no)
        assert len(fills) == 2
        assert sum(fill.filled_qty for fill in fills) == Decimal("10")
        # 4 @ 70,000 then cumulative 10 @ 76,000 → the second row is the real
        # 6 @ 80,000 (from the cumulative notional difference), not 6 @ 76,000.
        assert [fill.filled_price for fill in fills] == [
            Decimal("70000"),
            Decimal("80000"),
        ]
        assert sum(fill.filled_notional for fill in fills) == Decimal(10) * Decimal(
            "76000"
        )
        assert (await _fetch(client_order_id)).status == "filled"

        trade = await _trade(order_no)
        assert trade is not None
        # One row per order, advanced to the cumulative figures: the second
        # partial is not dropped by the (account, order_id) unique key.
        assert trade.quantity == Decimal("10")
        assert trade.price == Decimal("76000")
        assert trade.total_amount == Decimal("760000")

        # One journal per order, written at the terminal fill: the partial
        # created nothing, and the journal holds every filled share at the
        # order's cumulative quantity and average price (hk 4734 review).
        journals = await _journals(symbol)
        assert [journal.quantity for journal in journals] == [Decimal("10")]
        assert journals[0].entry_price == Decimal("76000")
        assert journals[0].status == "active"

        # The row is terminal and fully journaled now, so another pass finds no
        # candidate at all.
        settled = await _full_run(
            FakeReader(_filled_detail(order_no, 10, symbol, average_price=76000))
        )
        assert settled["reconciled"] == []
        assert len(await _fills(order_no)) == 2
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )


async def test_full_mode_recovers_a_journal_after_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed fill whose journal failed is finished by the next run.

    The order is already ``filled`` (it left the open scan), the trade row from
    the failed attempt exists, and the journal must still be created, linked and
    activated — not left draft forever.
    """
    from app.mcp_server.tooling import order_journal

    _full_mode(monkeypatch)
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    original = order_journal._create_trade_journal_for_buy

    async def failing_journal(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("journal db down")

    try:
        await _accepted(client_order_id, order_no, symbol=symbol)
        monkeypatch.setattr(
            order_journal, "_create_trade_journal_for_buy", failing_journal
        )
        first = await _full_run(FakeReader(_filled_detail(order_no, 10, symbol)))
        if first.get("reconcile_run"):
            run_ids.append(first["reconcile_run"]["run_id"])
        outcome = first["reconciled"][0]
        assert outcome["action"] == "booked_filled"
        assert outcome["journal_error"] == "RuntimeError"
        stored = await _fetch(client_order_id)
        assert stored.status == "filled"
        assert stored.filled_qty == Decimal("10")
        assert stored.journal_id is None
        assert stored.trade_id is None
        # The journal step shares one transaction with the trade row and the
        # progress marker, so the failure rolled all of it back.
        assert await _trade(order_no) is None
        assert len(await _fills(order_no)) == 1

        # The journal store recovers; the fill is not booked again.
        monkeypatch.setattr(order_journal, "_create_trade_journal_for_buy", original)
        recovered = await _full_run(FakeReader(_filled_detail(order_no, 10, symbol)))
        if recovered.get("reconcile_run"):
            run_ids.append(recovered["reconcile_run"]["run_id"])
        outcome = recovered["reconciled"][0]
        assert outcome["action"] == "noop_already_booked"
        assert outcome["journal"]["journal_created"] is True
        assert len(await _fills(order_no)) == 1

        stored = await _fetch(client_order_id)
        assert stored.journal_id is not None
        assert stored.trade_id is not None
        trade = await _trade(order_no)
        assert trade is not None
        assert trade.id == stored.trade_id
        assert trade.quantity == Decimal("10")
        assert trade.price == Decimal("70000")
        assert trade.total_amount == Decimal("700000")
        async with AsyncSessionLocal() as session:
            journal = (
                await session.execute(
                    select(TradeJournal).where(TradeJournal.id == stored.journal_id)
                )
            ).scalar_one()
        assert journal.status == "active"
        assert journal.trade_id == stored.trade_id

        settled = await _full_run(FakeReader(_filled_detail(order_no, 10, symbol)))
        assert settled["reconciled"] == []
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )


async def test_full_mode_split_sell_consumes_one_lot_across_two_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sell filled in two smaller deltas must not leave the lot active.

    The FIFO helper used by KIS never splits a lot, so a 4-share and a 6-share
    fill against one 10-share lot would close nothing. The partial-consumption
    close decrements the lot, then closes it on the next fill.
    """
    _full_mode(monkeypatch)
    suffix = uuid.uuid4().hex[:12]
    buy_id, buy_no = f"kwoomm-f-{suffix}-b", _order_no()
    sell_a_id, sell_a_no = f"kwoomm-f-{suffix}-a", _order_no()
    sell_b_id, sell_b_no = f"kwoomm-f-{suffix}-c", _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(buy_id, buy_no, symbol=symbol)
        bought = await _full_run(FakeReader(_filled_detail(buy_no, 10, symbol)))
        if bought.get("reconcile_run"):
            run_ids.append(bought["reconcile_run"]["run_id"])
        journals = await _journals(symbol)
        assert len(journals) == 1
        assert journals[0].status == "active"
        assert journals[0].quantity == Decimal("10")

        await _accepted(sell_a_id, sell_a_no, symbol=symbol, side="sell", quantity=4)
        first = await _full_run(
            FakeReader(_filled_detail(sell_a_no, 4, symbol, average_price=72000))
        )
        if first.get("reconcile_run"):
            run_ids.append(first["reconcile_run"]["run_id"])
        # The 4 shares sold are split off as a closed row at the lot's entry
        # price, so their realized P&L is recorded instead of disappearing; the
        # leftover stays active.
        assert first["reconciled"][0]["journal"]["journals_closed"] == 1
        assert first["reconciled"][0]["journal"]["realized_pnl_pct"] > 0
        journals = await _journals(symbol)
        assert [journal.quantity for journal in journals] == [
            Decimal("6"),
            Decimal("4"),
        ]
        assert journals[0].status == "active"
        split = journals[1]
        assert split.status == "closed"
        assert split.entry_price == Decimal("70000")
        assert split.exit_price == Decimal("72000")
        assert split.pnl_pct is not None and split.pnl_pct > 0
        assert split.trade_id == journals[0].trade_id

        await _accepted(sell_b_id, sell_b_no, symbol=symbol, side="sell", quantity=6)
        second = await _full_run(
            FakeReader(_filled_detail(sell_b_no, 6, symbol, average_price=72000))
        )
        if second.get("reconcile_run"):
            run_ids.append(second["reconcile_run"]["run_id"])
        assert second["reconciled"][0]["journal"]["journals_closed"] == 1
        assert second["reconciled"][0]["journal"]["realized_pnl_pct"] > 0
        journals = await _journals(symbol)
        assert journals[0].status == "closed"
        assert journals[0].exit_price == Decimal("72000")
        assert journals[0].pnl_pct is not None
        assert journals[1].status == "closed"
    finally:
        for client_order_id, order_no in (
            (buy_id, buy_no),
            (sell_a_id, sell_a_no),
            (sell_b_id, sell_b_no),
        ):
            await _cleanup_full(
                client_order_id=client_order_id,
                order_no=order_no,
                symbol=symbol,
                run_ids=run_ids,
            )


async def test_full_mode_overlapping_runs_journal_the_order_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two full runs started together apply the journal exactly once.

    The run-level advisory lock serializes them: the run that ends up second
    finds the order filled and journaled, so it books nothing and writes no
    journal. The real lock, the order-row ``FOR UPDATE`` and the re-read before
    any journal side effect are all exercised here.
    """
    _full_mode(monkeypatch)
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(client_order_id, order_no, symbol=symbol)
        reader = FakeReader(_filled_detail(order_no, 10, symbol))
        first, second = await asyncio.gather(
            _full_run(reader),
            _full_run(reader),
        )
        for result in (first, second):
            if result.get("reconcile_run"):
                run_ids.append(result["reconcile_run"]["run_id"])

        fills = await _fills(order_no)
        assert [fill.filled_qty for fill in fills] == [Decimal("10")]
        trade = await _trade(order_no)
        assert trade is not None
        assert trade.quantity == Decimal("10")
        journals = await _journals(symbol)
        assert [journal.quantity for journal in journals] == [Decimal("10")]
        assert journals[0].entry_price == Decimal("70000")
        assert journals[0].status == "active"

        actions = [
            outcome["action"]
            for result in (first, second)
            for outcome in result["reconciled"]
        ]
        assert actions.count("booked_filled") == 1
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )


async def test_partial_sell_close_serializes_on_the_lot_pool() -> None:
    """Two overlapping partial closes for one symbol consume the lot in turn.

    The sells belong to *different* orders, so the order-row lock cannot
    serialize them; the shared lot pool is read ``FOR UPDATE``, so the second
    pass waits, re-reads the leftover and closes the lot. Without the lock both
    read ``quantity=10`` under READ COMMITTED and both write ``quantity=4``,
    leaving the lot active with 12 shares consumed against 10.
    """
    from app.mcp_server.tooling.order_journal import _close_journals_on_sell_partial

    symbol = _symbol()
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                TradeJournal(
                    symbol=symbol,
                    instrument_type="equity_kr",
                    side="buy",
                    entry_price=Decimal("70000"),
                    quantity=Decimal("10"),
                    amount=Decimal("700000"),
                    thesis="lot pool serialization",
                    status="active",
                    account=kernel.JOURNAL_ACCOUNT,
                    account_type=kernel.JOURNAL_ACCOUNT_TYPE,
                )
            )
            await session.commit()

        async def close() -> dict[str, Any]:
            async with AsyncSessionLocal() as session:
                result = await _close_journals_on_sell_partial(
                    session,
                    symbol=symbol,
                    sell_quantity=6.0,
                    sell_price=70000.0,
                    account_type=kernel.JOURNAL_ACCOUNT_TYPE,
                    account=kernel.JOURNAL_ACCOUNT,
                )
                # Hold the transaction (and its row locks) open while the peer
                # makes its own pass, so the two really do overlap.
                await asyncio.sleep(0.05)
                await session.commit()
                return result

        first, second = await asyncio.gather(close(), close())

        async with AsyncSessionLocal() as session:
            lots = list(
                (
                    await session.execute(
                        select(TradeJournal)
                        .where(TradeJournal.symbol == symbol)
                        .order_by(TradeJournal.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        # First pass: a closed split row for the 6 shares and 4 left active;
        # second pass: the leftover is closed. Both passes of the same snapshot
        # would leave the lot active at 4 with 12 shares consumed.
        leftover, split = lots[0], lots[1]
        assert [leftover.quantity, split.quantity] == [Decimal("4"), Decimal("6")]
        assert split.status == "closed"
        assert leftover.status == "closed"
        assert first["journals_closed"] + second["journals_closed"] == 2
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(TradeJournal).where(TradeJournal.symbol == symbol)
            )
            await session.commit()


async def test_full_mode_terminal_verdict_with_fill_closes_after_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled partial (4 of 10, expiry marker) is booked, journaled, closed.

    The verdict comes from the real classifier: a partial fill with no remaining
    quantity is terminal and carries its fill, so the shares reach a journal and
    the row closes. Before that, the row stayed ``partial`` forever, re-booked as
    a no-op with an empty journal.
    """
    _full_mode(monkeypatch)
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(client_order_id, order_no, symbol=symbol)
        detail = _detail_payload(
            _detail_row(
                order_no,
                stk_cd=symbol,
                ord_qty="10",
                cntr_qty="4",
                ord_remnq="0",
                cntr_uv="70000",
                mdfy_cncl="만료",
            )
        )
        result = await _full_run(FakeReader(detail))
        if result.get("reconcile_run"):
            run_ids.append(result["reconcile_run"]["run_id"])

        outcome = next(
            row
            for row in result["reconciled"]
            if row["client_order_id"] == client_order_id
        )
        assert outcome["verdict"] == "expired"
        assert outcome["fill_action"] == "booked_partial"
        assert outcome["action"] == "mark_expired"
        assert outcome["requires_manual_review"] is False

        stored = await _fetch(client_order_id)
        assert stored.status == "expired"
        assert stored.filled_qty == Decimal("4")
        assert stored.journal_id is not None
        fills = await _fills(order_no)
        assert [fill.filled_qty for fill in fills] == [Decimal("4")]
        journals = await _journals(symbol)
        assert [journal.quantity for journal in journals] == [Decimal("4")]

        # The row left the scan: the next run finds nothing to do.
        settled = await _full_run(FakeReader(detail))
        assert settled["reconciled"] == []
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )
