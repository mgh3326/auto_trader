"""hk 4722 / hk 4734 / hk 4802 — reconcile against the test database.

CI only (``integration``): never run locally. Rows are keyed by a per-test random
``client_order_id`` and removed in ``finally``; the shared schema is left as
bootstrapped.

T-P4 proves the full mode here: one in-effect ``execution_ledger`` row per
broker delta, partial→filled deltas summing to the order quantity, a re-run
booking nothing new, one ``broker='kiwoom'`` reconcile-run row — and, since
hk 4802, the per-delta journal projection that commits with it: a ``review.trades``
row, a buy ``TradeJournal`` lot grown at the implied delta prices, sell FIFO
closes, and ``journaled_qty``/``journaled_avg`` progress on the ledger row. It
also proves the two service guards the round-8 review found missing: a changed
cumulative average without a fill is refused (and the next delta keeps the
booked basis), and the terminal close requires the verdict's whole fill to be
booked.
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

#: The account label the journal projection (hk 4802) writes its trade rows and
#: journal lots under.
KIWOOM_MOCK_ACCOUNT = "kiwoom_mock"


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
                    Trade.account == KIWOOM_MOCK_ACCOUNT,
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
                        TradeJournal.account == KIWOOM_MOCK_ACCOUNT,
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
                Trade.account == KIWOOM_MOCK_ACCOUNT,
                Trade.order_id == order_no,
            )
        )
        await session.execute(
            delete(TradeJournal).where(
                TradeJournal.account == KIWOOM_MOCK_ACCOUNT,
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


async def test_full_mode_books_one_in_effect_row_and_journals_it(
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
        # hk 4802: the booked delta journaled in the same transaction — one
        # cumulative trade row, one active buy lot, journaled progress.
        trade = await _trade(order_no)
        assert trade is not None
        assert trade.quantity == 10
        assert trade.price == 70000
        journals = await _journals(symbol)
        assert len(journals) == 1
        assert journals[0].status == "active"
        assert journals[0].quantity == Decimal("10")
        assert journals[0].entry_price == Decimal("70000")
        assert stored.journal_id == journals[0].id
        assert stored.trade_id == trade.id
        detail = stored.last_reconcile_detail or {}
        assert Decimal(detail["journaled_qty"]) == Decimal("10")
        assert Decimal(detail["journaled_avg"]) == Decimal("70000")

        assert run_ids, "the full run must record its reconcile-run row"
        async with AsyncSessionLocal() as session:
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
        stored = await _fetch(client_order_id)
        assert stored.status == "filled"
        assert stored.avg_fill_price == Decimal("76000")
        # hk 4802: one lot grown to the quantity-weighted 76,000 (the 6-share
        # delta journaled at its implied 80,000), one cumulative trade row.
        trade = await _trade(order_no)
        assert trade is not None
        assert trade.quantity == 10
        assert trade.price == 76000
        journals = await _journals(symbol)
        assert len(journals) == 1
        assert journals[0].quantity == Decimal("10")
        assert journals[0].entry_price == Decimal("76000")
        assert stored.journal_id == journals[0].id
        assert stored.trade_id == trade.id
        detail = stored.last_reconcile_detail or {}
        assert Decimal(detail["journaled_qty"]) == Decimal("10")
        assert Decimal(detail["journaled_avg"]) == Decimal("76000")

        # The row is terminal now, so another pass finds no candidate at all.
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


async def test_same_quantity_average_change_is_refused_and_keeps_the_booked_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed average with no new share must not replace the booked basis.

    Review r4577667962: after booking 4 @ 70,000, a replay claiming
    ``(4, 71,000)`` used to overwrite ``avg_fill_price``. The next cumulative
    fill then priced its delta from an average no ledger row ever carried. The
    refusal keeps the basis, so the 6-share delta books at 80,000 and the ledger
    notional still equals the broker's 760,000.
    """
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(client_order_id, order_no, symbol=symbol)
        ledger_id = (await _fetch(client_order_id)).id

        async with AsyncSessionLocal() as session:
            booking = await KiwoomMockOrderLedgerService(session).record_fill(
                ledger_id=ledger_id, filled_quantity=4, average_price=70000
            )
        assert booking.action == "booked_partial"

        with pytest.raises(KiwoomMockTransitionRefused) as excinfo:
            async with AsyncSessionLocal() as session:
                await KiwoomMockOrderLedgerService(session).record_fill(
                    ledger_id=ledger_id, filled_quantity=4, average_price=71000
                )
        assert excinfo.value.reason == "average_price_changed_without_fill"

        unchanged = await _fetch(client_order_id)
        assert unchanged.filled_qty == Decimal("4")
        assert unchanged.avg_fill_price == Decimal("70000")
        assert len(await _fills(order_no)) == 1

        async with AsyncSessionLocal() as session:
            booking = await KiwoomMockOrderLedgerService(session).record_fill(
                ledger_id=ledger_id, filled_quantity=10, average_price=76000
            )
        assert booking.action == "booked_filled"
        assert booking.delta_qty == Decimal("6")
        assert booking.avg_price == Decimal("80000")

        fills = await _fills(order_no)
        assert [fill.filled_price for fill in fills] == [
            Decimal("70000"),
            Decimal("80000"),
        ]
        assert sum(fill.filled_notional for fill in fills) == Decimal("760000")
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )


async def test_close_filled_as_terminal_requires_the_whole_booked_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The close refuses a verdict quantity the row has not booked."""
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(client_order_id, order_no, symbol=symbol)
        ledger_id = (await _fetch(client_order_id)).id
        async with AsyncSessionLocal() as session:
            await KiwoomMockOrderLedgerService(session).record_fill(
                ledger_id=ledger_id, filled_quantity=4, average_price=70000
            )

        with pytest.raises(KiwoomMockTransitionRefused) as excinfo:
            async with AsyncSessionLocal() as session:
                await KiwoomMockOrderLedgerService(session).close_filled_as_terminal(
                    ledger_id=ledger_id,
                    status="expired",
                    reason="broker_expiry_confirmed",
                    filled_quantity=10,
                )
        assert excinfo.value.reason == "fill_not_booked"
        still_open = await _fetch(client_order_id)
        assert still_open.status == "partial"
        assert still_open.filled_qty == Decimal("4")

        async with AsyncSessionLocal() as session:
            closed = await KiwoomMockOrderLedgerService(
                session
            ).close_filled_as_terminal(
                ledger_id=ledger_id,
                status="expired",
                reason="broker_expiry_confirmed",
                filled_quantity=4,
            )
        assert closed.status == "expired"
        assert closed.last_reconcile_detail["booked_filled_qty"] == "4"
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )


async def test_journal_recovery_scan_skips_unmarked_history_and_orders_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mergewake r4578899898: recovery candidates need a journal marker and
    must sit behind open rows so they cannot starve the open scan.

    A row booked before hk 4802 has ``filled_qty`` but no journaled/manual
    marker — it must NOT be retroactively journaled. A row whose sell
    remainder is unmatched (marker present) IS a recovery candidate but
    orders after every open row.
    """
    legacy_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    marked_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    open_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(legacy_id, order_no, symbol=symbol)
        await _accepted(marked_id, _order_no(), symbol=symbol)
        await _accepted(open_id, _order_no(), symbol=symbol)
        legacy_ledger = (await _fetch(legacy_id)).id
        marked_ledger = (await _fetch(marked_id)).id
        async with AsyncSessionLocal() as session:
            service = KiwoomMockOrderLedgerService(session)
            # A pre-4802 booked fill: filled_qty, no journaled markers.
            await service.record_fill(
                ledger_id=legacy_ledger, filled_quantity=10, average_price=70000
            )
            # A post-4802 unmatched sell remainder on a 'filled' row: marker
            # present, so recovery-only (not an open-status candidate).
            await service.record_fill(
                ledger_id=marked_ledger, filled_quantity=10, average_price=70000
            )
            await service.record_journal_progress(
                ledger_id=marked_ledger,
                journaled_qty=Decimal("0"),
                journaled_avg=Decimal("0"),
                manual_review_reason="unmatched_sell_quantity:4",
            )
            rows, _ = await service.list_reconcile_candidates(
                limit=1000, include_journal_recovery=True
            )
        ids = [row.client_order_id for row in rows]
        assert legacy_id not in ids  # unmarked history is never backfilled
        assert marked_id in ids
        assert open_id in ids
        # Recovery candidates order behind every open row.
        assert ids.index(marked_id) > ids.index(open_id)
    finally:
        await _cleanup_full(
            client_order_id=legacy_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )
        await _cleanup(marked_id)
        await _cleanup(open_id)


async def test_journal_failure_rolls_back_the_whole_booking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ledger delta, journal and progress commit atomically: a journal error
    must leave neither an execution-ledger row nor a booked quantity, so the
    next run can retry the whole delta.
    """
    _full_mode(monkeypatch)
    client_order_id = f"kwoomm-f-{uuid.uuid4().hex[:12]}"
    order_no = _order_no()
    symbol = _symbol()
    run_ids: list[str] = []
    try:
        await _accepted(client_order_id, order_no, symbol=symbol)

        async def explode(db: Any, service: Any, booking: Any) -> dict[str, Any]:
            raise RuntimeError("journal write failed")

        monkeypatch.setattr(kernel, "_journal_unjournaled", explode)
        result = await _full_run(FakeReader(_filled_detail(order_no, 10, symbol)))
        if result.get("reconcile_run"):
            run_ids.append(result["reconcile_run"]["run_id"])

        outcome = next(
            row
            for row in result["reconciled"]
            if row["client_order_id"] == client_order_id
        )
        assert outcome["action"] == "fill_journal_failed"
        assert outcome["requires_manual_review"] is True
        assert result["unresolved_count"] >= 1

        stored = await _fetch(client_order_id)
        assert stored.filled_qty is None
        assert stored.status == "accepted"
        assert await _fills(order_no) == []
        assert await _trade(order_no) is None
        assert await _journals(symbol) == []

        # The retry books and journals the whole delta in one transaction.
        monkeypatch.undo()
        _full_mode(monkeypatch)
        second = await _full_run(FakeReader(_filled_detail(order_no, 10, symbol)))
        if second.get("reconcile_run"):
            run_ids.append(second["reconcile_run"]["run_id"])
        assert second["reconciled"][0]["action"] == "booked_filled"
        assert len(await _fills(order_no)) == 1
        assert len(await _journals(symbol)) == 1
    finally:
        await _cleanup_full(
            client_order_id=client_order_id,
            order_no=order_no,
            symbol=symbol,
            run_ids=run_ids,
        )


async def test_full_mode_overlapping_runs_book_the_delta_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two full runs started together book and journal each delta once."""
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
        assert (await _trade(order_no)).quantity == 10
        journals = await _journals(symbol)
        assert len(journals) == 1
        assert journals[0].quantity == Decimal("10")

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


async def test_full_mode_terminal_verdict_with_fill_books_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled partial (4 of 10, expiry marker) is booked, then closed.

    The verdict comes from the real classifier: a partial fill with no remaining
    quantity is terminal and carries its fill. Full mode books that delta and
    closes the row once its booked quantity equals the verdict's; before that
    the row stayed ``partial`` forever, re-booked as a no-op.
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
        assert not outcome.get("requires_manual_review")

        stored = await _fetch(client_order_id)
        assert stored.status == "expired"
        assert stored.filled_qty == Decimal("4")
        # The settled partial journaled in the same transaction as its booking.
        assert stored.journal_id is not None
        assert stored.trade_id is not None
        detail = stored.last_reconcile_detail or {}
        assert Decimal(detail["journaled_qty"]) == Decimal("4")
        fills = await _fills(order_no)
        assert [fill.filled_qty for fill in fills] == [Decimal("4")]
        assert (await _trade(order_no)).quantity == 4
        journals = await _journals(symbol)
        assert len(journals) == 1
        assert journals[0].quantity == Decimal("4")
        assert journals[0].status == "active"

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
