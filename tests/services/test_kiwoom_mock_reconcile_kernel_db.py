"""hk 4722 (3797 T-P3) — reconcile transitions against the test database.

CI only (``integration``): never run locally. Rows are keyed by a per-test random
``client_order_id`` and removed in ``finally``; the shared schema is left as
bootstrapped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal, engine
from app.models.review import KiwoomMockOrderLedger
from app.services import kiwoom_mock_reconcile as kernel
from app.services.kiwoom_mock_order_ledger_service import (
    KiwoomMockOrderLedgerService,
    KiwoomMockTransitionRefused,
    kr_trading_day,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

TODAY = kr_trading_day(datetime.now(UTC))


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


async def _accepted(client_order_id: str, order_no: str) -> None:
    async with AsyncSessionLocal() as session:
        await KiwoomMockOrderLedgerService(session).record_send(
            status="accepted",
            client_order_id=client_order_id,
            idempotency_key=client_order_id,
            symbol="005930",
            side="buy",
            quantity=10,
            price=70000,
            broker_order_id=order_no,
            response_code="0",
            response_message="정상",
            raw_response={"return_code": 0},
            trading_day=TODAY,
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
    order_no = f"T{uuid.uuid4().hex[:10]}"
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
    order_no = f"T{uuid.uuid4().hex[:10]}"
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
    order_no = f"T{uuid.uuid4().hex[:10]}"
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
        await _accepted(open_id, f"T{uuid.uuid4().hex[:10]}")
        await _accepted(expired_id, f"T{uuid.uuid4().hex[:10]}")
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
    expiring_order = f"T{uuid.uuid4().hex[:10]}"
    filled_order = f"T{uuid.uuid4().hex[:10]}"
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
        assert by_id[open_id]["status"] == "expired"
        # uncertain row's exact order is listed positively -> accepted
        assert by_id[uncertain_id]["status"] == "accepted"

        assert (await _fetch(open_id)).status == "expired"
        # Fills are never booked in terminal-only: no filled_qty is written.
        assert (await _fetch(uncertain_id)).filled_qty is None
    finally:
        await _cleanup(open_id)
        await _cleanup(uncertain_id)


async def _no_sleep(seconds: float) -> None:
    del seconds
