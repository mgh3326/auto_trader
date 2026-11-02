"""ROB-1461: the evidence reader and journal reads against the test DB (runs in CI)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, text

from app.core.db import AsyncSessionLocal, engine
from app.models.execution_ledger import ExecutionLedger
from app.models.review import KISMockOrderLedger
from app.services.execution_intent_gateway.decisions import ChildSnapshot, ChildState
from app.services.execution_intent_gateway.kis_mock_forwarder import (
    order_correlation_id,
)
from app.services.execution_intent_gateway.query import (
    LedgerOrder,
    SqlLedgerEvidenceReader,
)
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.execution_intent_gateway.support import intent_snapshot

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration


def _submit(key: str, order_id: str | None) -> ChildSnapshot:
    return ChildSnapshot(
        child_id=1,
        idempotency_key=key,
        request_digest="digest",
        action="submit",
        target_idempotency_key=None,
        quantity=10,
        limit_price_krw=70_000,
        state=ChildState.ACCEPTED if order_id else ChildState.UNCERTAIN,
        refusal_reason=None,
        broker_order_id=order_id,
        result=None,
    )


def _ledger_row(symbol: str, order_no: str, correlation: str) -> KISMockOrderLedger:
    return KISMockOrderLedger(
        trade_date=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        symbol=symbol,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("10"),
        price=Decimal("70000"),
        amount=Decimal("700000"),
        currency="KRW",
        order_no=order_no,
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="fill",
        last_reconcile_detail={"attributed_fill_qty": "3"},
        correlation_id=correlation,
    )


def _execution_row(
    symbol: str, order_no: str, *, fill_seq: int, account_mode: str = "mock"
) -> ExecutionLedger:
    return ExecutionLedger(
        broker="kis",
        account_mode=account_mode,
        venue="krx",
        instrument_type="equity_kr",
        symbol=symbol,
        raw_symbol=symbol,
        side="buy",
        broker_order_id=order_no,
        fill_seq=fill_seq,
        filled_qty=Decimal("2"),
        filled_price=Decimal("70000"),
        filled_notional=Decimal("140000"),
        filled_at=datetime(2026, 11, 2, 0, 1, tzinfo=UTC),
        currency="KRW",
        source="websocket",
    )


@pytest_asyncio.fixture
async def seeded(db_session: Any) -> AsyncIterator[dict[str, str]]:
    tag = uuid4().hex[:5].upper()
    # The ledger symbol is compared as stored, so any unique text works here.
    values = {"symbol": f"9{tag}", "order_no": f"M{tag}", "root": f"key-{tag}"}
    db_session.add(
        _ledger_row(
            values["symbol"], values["order_no"], order_correlation_id(values["root"])
        )
    )
    for fill_seq in (1, 2):
        db_session.add(
            _execution_row(values["symbol"], values["order_no"], fill_seq=fill_seq)
        )
    await db_session.commit()
    yield values
    await db_session.rollback()
    await db_session.execute(
        delete(KISMockOrderLedger).where(KISMockOrderLedger.symbol == values["symbol"])
    )
    await db_session.execute(
        delete(ExecutionLedger).where(ExecutionLedger.symbol == values["symbol"])
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_reader_returns_ledger_evidence_for_the_lineage(
    seeded: dict[str, str],
) -> None:
    intent = intent_snapshot(symbol=seeded["symbol"])
    evidence = await SqlLedgerEvidenceReader(AsyncSessionLocal).read(
        intent, [_submit(seeded["root"], seeded["order_no"])]
    )
    assert evidence.orders_by_root[seeded["root"]] == (
        LedgerOrder(
            order_no=seeded["order_no"],
            status="accepted",
            lifecycle_state="fill",
            quantity=10,
            filled_qty=3,
            broker_cancel_confirmed=None,
        ),
    )
    assert evidence.filled_by_order_id == {seeded["order_no"]: 4}


@pytest.mark.asyncio
async def test_reader_finds_the_order_id_of_an_unknown_submit_from_the_ledger(
    seeded: dict[str, str],
) -> None:
    intent = intent_snapshot(symbol=seeded["symbol"])
    evidence = await SqlLedgerEvidenceReader(AsyncSessionLocal).read(
        intent, [_submit(seeded["root"], None)]
    )
    assert evidence.filled_by_order_id == {seeded["order_no"]: 4}


@pytest.mark.asyncio
async def test_reader_ignores_live_rows_with_the_same_order_number(
    db_session: Any, seeded: dict[str, str]
) -> None:
    db_session.add(
        _execution_row(
            seeded["symbol"], seeded["order_no"], fill_seq=3, account_mode="live"
        )
    )
    await db_session.commit()
    intent = intent_snapshot(symbol=seeded["symbol"])
    evidence = await SqlLedgerEvidenceReader(AsyncSessionLocal).read(
        intent, [_submit(seeded["root"], seeded["order_no"])]
    )
    assert evidence.filled_by_order_id == {seeded["order_no"]: 4}


@pytest.mark.asyncio
async def test_reader_runs_read_only_and_issues_no_write(
    seeded: dict[str, str],
) -> None:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        await SqlLedgerEvidenceReader(AsyncSessionLocal).read(
            intent_snapshot(symbol=seeded["symbol"]),
            [_submit(seeded["root"], seeded["order_no"])],
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    own = [s for s in statements if not s.lstrip().upper().startswith("SELECT PG_")]
    assert own[0] == "SET TRANSACTION READ ONLY"
    assert all(s.lstrip().upper().startswith("SELECT") for s in own[1:])


@pytest.mark.asyncio
async def test_a_write_in_a_read_only_transaction_is_refused(db_session: Any) -> None:
    from sqlalchemy.exc import DBAPIError

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE review.kis_mock_order_ledger SET notes = 'x'")
            )
        await session.rollback()
