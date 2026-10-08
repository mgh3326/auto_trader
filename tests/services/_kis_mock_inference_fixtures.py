"""#1250 — real-shaped kis_mock rows 80/66/64/63 for the inference-expiry tests.

Shapes follow the 10-01 desk read (hk doc ops/2026-10-01/706-phase-b-desk-run):
all four are ``accepted``/``accepted`` limit buys (ORD_DVSN 00) with a native
accepted KIS response, no recorded zero fill, and these strategies — 80
``b0xk``, 66 ``deep_limit_support_pullback``, 64 a long free-text rationale,
63 ``buy_review mirror``. Send times are inside the XKRX regular session.

The ids are fixed by the decision, so every test inserts them explicitly and
removes them afterwards. The audit table is append-only; teardown removes its
rows with a transaction-local ``session_replication_role = replica`` (test
databases are superuser-owned; the application role is not), the same way the
#1175 tests purge quarantined ledger rows.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.models.execution_ledger import ExecutionLedger
from app.models.review import (
    KISLiveOrderLedger,
    KISMockInferenceExpiryEvent,
    KISMockOrderLedger,
)
from app.models.trading import InstrumentType

KST = datetime.timezone(datetime.timedelta(hours=9))
IDS = (80, 66, 64, 63)
#: 2026-10-05 06:30 KST — the decision morning.
NOW = datetime.datetime(2026, 10, 5, 6, 30, tzinfo=KST)

LONG_RATIONALE = (
    "지지선 근접 + RSI 과매도 반등 기대. 거래량 감소 구간에서 분할 매수 1차, "
    "손절은 직전 저점 이탈 시. "
) * 6

#: Row 63's real symbol is 005930. Other test files leave kis_mock rows on
#: 005930 in the shared worker DB, which the same-symbol holding check then
#: (correctly) treats as fills that may postdate the accept, so the fixtures
#: use a symbol no other test writes.
T1250_SYMBOL_63 = "990063"

ROW_SPECS: dict[int, dict[str, Any]] = {
    80: {
        "symbol": "000100",
        "quantity": Decimal("3"),
        "price": Decimal("83000"),
        "sent": datetime.datetime(2026, 8, 12, 10, 2, 11, tzinfo=KST),
        "order_no": "0000031180",
        "strategy": "b0xk",
    },
    66: {
        "symbol": "009830",
        "quantity": Decimal("1"),
        "price": Decimal("26300"),
        "sent": datetime.datetime(2026, 7, 22, 9, 41, 5, tzinfo=KST),
        "order_no": "0000027766",
        "strategy": "deep_limit_support_pullback",
    },
    64: {
        "symbol": "248070",
        "quantity": Decimal("1"),
        "price": Decimal("13700"),
        "sent": datetime.datetime(2026, 7, 21, 13, 12, 40, tzinfo=KST),
        "order_no": "0000026064",
        "strategy": LONG_RATIONALE,
    },
    63: {
        "symbol": T1250_SYMBOL_63,
        "quantity": Decimal("2"),
        "price": Decimal("234500"),
        "sent": datetime.datetime(2026, 7, 21, 13, 10, 2, tzinfo=KST),
        "order_no": "0000026063",
        "strategy": "buy_review mirror",
    },
}


def mock_row(ledger_id: int, **changes: Any) -> KISMockOrderLedger:
    spec = ROW_SPECS[ledger_id]
    sent: datetime.datetime = spec["sent"]
    order_time = sent.strftime("%H%M%S")
    values: dict[str, Any] = {
        "id": ledger_id,
        "trade_date": sent,
        "symbol": spec["symbol"],
        "instrument_type": InstrumentType.equity_kr,
        "side": "buy",
        "order_type": "limit",
        "quantity": spec["quantity"],
        "price": spec["price"],
        "amount": spec["quantity"] * spec["price"],
        "currency": "KRW",
        "order_no": spec["order_no"],
        "order_time": order_time,
        "account_mode": "kis_mock",
        "broker": "kis",
        "status": "accepted",
        "lifecycle_state": "accepted",
        "response_code": "0",
        "response_message": "모의투자 매수주문이 완료 되었습니다.",
        "raw_response": {
            "rt_cd": "0",
            "msg_cd": "40600000",
            "msg": "모의투자 매수주문이 완료 되었습니다.",
            "odno": spec["order_no"],
            "ord_tmd": order_time,
        },
        "strategy": spec["strategy"],
        "holdings_baseline_qty": Decimal("0"),
        "reconcile_attempts": 0,
        "last_reconcile_detail": None,
        "correlation_id": f"kis-mock-{ledger_id}-t1250",
    }
    values.update(changes)
    return KISMockOrderLedger(**values)


def exec_row(**changes: Any) -> ExecutionLedger:
    values: dict[str, Any] = {
        "broker": "kis",
        "account_mode": "mock",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": T1250_SYMBOL_63,
        "raw_symbol": T1250_SYMBOL_63,
        "side": "buy",
        "broker_order_id": "0000026063",
        "fill_seq": 1,
        "filled_qty": Decimal("2"),
        "filled_price": Decimal("234500"),
        "filled_notional": Decimal("469000"),
        "filled_at": datetime.datetime(2026, 7, 21, 13, 20, tzinfo=KST),
        "currency": "KRW",
        "source": "websocket",
    }
    values.update(changes)
    return ExecutionLedger(**values)


def live_row(ledger_id: int) -> KISLiveOrderLedger:
    spec = ROW_SPECS[ledger_id]
    return KISLiveOrderLedger(
        id=ledger_id,
        trade_date=spec["sent"],
        symbol=spec["symbol"],
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=spec["quantity"],
        price=spec["price"],
        currency="KRW",
        order_no=spec["order_no"],
        order_time=spec["sent"].strftime("%H%M%S"),
        account_mode="kis_live",
        broker="kis",
        status="accepted",
        lifecycle_state="accepted",
        response_code="0",
    )


async def purge_audit_only(db) -> None:
    """Test-only: drop the audit rows (replica role) and keep the closed rows."""
    await db.rollback()
    await db.execute(sa.text("SET LOCAL session_replication_role = replica"))
    await db.execute(
        sa.delete(KISMockInferenceExpiryEvent).where(
            KISMockInferenceExpiryEvent.ledger_id.in_(IDS)
        )
    )
    await db.commit()


async def purge(db, *, mock_ids=(), exec_ids=(), live_ids=()) -> None:
    """Test-only teardown, including append-only audit rows (replica role)."""
    await db.rollback()
    await db.execute(sa.text("SET LOCAL session_replication_role = replica"))
    await db.execute(
        sa.delete(KISMockInferenceExpiryEvent).where(
            KISMockInferenceExpiryEvent.ledger_id.in_(IDS)
        )
    )
    if mock_ids:
        await db.execute(
            sa.delete(KISMockOrderLedger).where(KISMockOrderLedger.id.in_(mock_ids))
        )
    if exec_ids:
        await db.execute(
            sa.delete(ExecutionLedger).where(ExecutionLedger.id.in_(exec_ids))
        )
    if live_ids:
        await db.execute(
            sa.delete(KISLiveOrderLedger).where(KISLiveOrderLedger.id.in_(live_ids))
        )
    await db.commit()
