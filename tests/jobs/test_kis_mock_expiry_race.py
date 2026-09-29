"""Concurrent operator expiry must win over stale mock holdings reconciliation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.models.review import KISMockOrderLedger
from app.services import kis_mock_lifecycle_service as lifecycle_module
from app.services.kis_mock_holdings_reconciler import ReconcilerThresholds
from app.services.kis_mock_lifecycle_service import (
    ExpiredLifecycleConflict,
    KISMockLifecycleService,
)
from app.services.kis_mock_terminal_expiry import RULE_VERSION
from app.services.market_events.session_calendar import trading_session_status

pytestmark = pytest.mark.unit
TODAY = date(2026, 9, 28)
REF = "hk #706 Q-46 race"
STRATEGY = "manual_kr"


def _row(symbol: str = "005930") -> KISMockOrderLedger:
    order_no = str(uuid4().int % 10**10).zfill(10)
    return KISMockOrderLedger(
        trade_date=datetime(2026, 9, 21, 3, tzinfo=UTC),
        symbol=symbol,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("3"),
        price=Decimal("61000"),
        amount=Decimal("183000"),
        currency="KRW",
        order_no=order_no,
        order_time="100000",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="pending",
        strategy=STRATEGY,
        response_code="0",
        raw_response={
            "rt_cd": "0",
            "odno": order_no,
            "ord_tmd": "100000",
            "msg": "accepted",
            "msg_cd": "APBK0013",
        },
        last_reconcile_detail={
            "reason_code": "pending_unconfirmed",
            "attributed_fill_qty": "0",
        },
        correlation_id=f"q46-race-{uuid4().hex}",
        holdings_baseline_qty=Decimal("0"),
    )


@pytest.fixture(autouse=True)
def _pin_operator_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_module, "_today_kst", lambda: TODAY)
    monkeypatch.setattr(
        lifecycle_module, "trading_session_status", trading_session_status
    )


async def _expire(row_id: int) -> None:
    async with AsyncSessionLocal() as operator:
        result = await KISMockLifecycleService(operator).expire_legacy_day_orders(
            ledger_ids=[row_id],
            operator_decision_ref=REF,
            expected_strategy=STRATEGY,
            min_sessions=2,
            dry_run=False,
            confirm=True,
        )
    assert result[0]["decision"] == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("next_state", ["pending", "stale", "fill"])
async def test_generic_write_reloads_expired_state_from_stale_identity_map(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, next_state: str
) -> None:
    row = _row()
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    async with AsyncSessionLocal() as reconciler:
        stale = await reconciler.get(KISMockOrderLedger, row_id)
        assert stale is not None and stale.lifecycle_state == "pending"
        await _expire(row_id)
        async with AsyncSessionLocal() as baseline_reader:
            expired = await baseline_reader.get(KISMockOrderLedger, row_id)
            assert expired is not None
            expected_audit = (
                expired.reconcile_attempts,
                expired.reconciled_at,
                expired.last_reconcile_detail,
            )
        observed_queries: list[tuple[bool, bool]] = []
        original_execute = reconciler.execute

        async def capture_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            observed_queries.append(
                (
                    "FOR UPDATE" in str(statement).upper(),
                    statement.get_execution_options().get("populate_existing") is True,
                )
            )
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(reconciler, "execute", capture_execute)
        with pytest.raises(
            ExpiredLifecycleConflict, match="expired_terminal_immutable"
        ):
            await KISMockLifecycleService(reconciler).apply_lifecycle_transition(
                ledger_id=row_id,
                next_state=cast(Any, next_state),
                reason_code="reconcile_after_expiry",
                detail={"attributed_fill_qty": "1"},
                dry_run=False,
            )
        assert observed_queries == [(True, True)]
        await reconciler.rollback()
    async with AsyncSessionLocal() as reader:
        fresh = await reader.get(KISMockOrderLedger, row_id)
        assert fresh is not None
        assert fresh.lifecycle_state == "expired"
        assert (
            fresh.reconcile_attempts,
            fresh.reconciled_at,
            fresh.last_reconcile_detail,
        ) == expected_audit
        assert fresh.last_reconcile_detail["operator_decision_ref"] == REF
        assert fresh.last_reconcile_detail["rule_version"] == RULE_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held_qty", "stale_threshold", "proposed_state"),
    [
        ("0", 10**9, "pending"),
        ("0", -(10**9), "stale"),
        ("3", 10**9, "fill"),
        ("1", 10**9, "fill"),
    ],
)
async def test_reconcile_job_skips_expired_interleaving_and_continues_other_row(
    db_session: AsyncSession,
    held_qty: str,
    stale_threshold: int,
    proposed_state: str,
) -> None:
    expiring, other = _row(), _row(symbol="000660")
    db_session.add_all([expiring, other])
    await db_session.commit()
    expiring_id, other_id = expiring.id, other.id

    class FakeMockClient:
        async def fetch_my_stocks(self, *, is_mock: bool, is_overseas: bool):
            assert is_mock is True
            if is_overseas:
                return []
            await _expire(expiring_id)
            if held_qty == "0":
                return []
            return [{"pdno": "005930", "hldg_qty": held_qty}]

    async with AsyncSessionLocal() as reconciler:
        result = await run_kis_mock_reconciliation(
            reconciler,
            dry_run=False,
            ledger_ids=[expiring_id, other_id],
            thresholds=ReconcilerThresholds(stale_threshold_sec=stale_threshold),
            kis_client=cast(Any, FakeMockClient()),
        )
    assert result["success"] is True
    assert result["orders_processed"] == 2
    assert result["transitions_applied"] == 1
    skips = [
        item
        for item in result["transitions"]
        if item.get("reason_code") == "expired_during_reconciliation"
    ]
    assert len(skips) == 1
    assert skips[0]["ledger_id"] == expiring_id
    assert skips[0]["proposed_state"] == proposed_state
    assert skips[0]["applied"] is False
    assert skips[0]["skipped"] is True
    assert len(result["events"]) == 1
    assert result["events"][0]["detail"]["ledger_id"] == other_id

    async with AsyncSessionLocal() as reader:
        fresh = await reader.get(KISMockOrderLedger, expiring_id)
        assert fresh is not None
        assert fresh.lifecycle_state == "expired"
        assert fresh.reconcile_attempts == 1
        assert fresh.last_reconcile_detail["operator_decision_ref"] == REF
        assert fresh.last_reconcile_detail["rule_version"] == RULE_VERSION
        open_ids = {
            item.id
            for item in await KISMockLifecycleService(reader).list_open_orders(
                ledger_ids=[expiring_id]
            )
        }
        assert expiring_id not in open_ids
