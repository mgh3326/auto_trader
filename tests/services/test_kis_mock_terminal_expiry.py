"""Q-46 row-local KIS mock expiry and service write guards."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.models.review import KISMockOrderLedger
from app.services import kis_mock_lifecycle_service as lifecycle_module
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from app.services.kis_mock_terminal_expiry import (
    RULE_VERSION,
    classify_row,
    validate_request,
)
from app.services.market_events.session_calendar import trading_session_status

pytestmark = pytest.mark.unit
TODAY = date(2026, 9, 18)
REF = "hk #706 Q-46"
STRATEGY = "manual_kr"


def _row(**changes) -> KISMockOrderLedger:
    order_no = str(uuid4().int % 10**12).zfill(12)
    values = {
        "trade_date": datetime(2026, 9, 14, 9, tzinfo=UTC),
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("2"),
        "price": Decimal("70000"),
        "amount": Decimal("140000"),
        "currency": "KRW",
        "order_no": order_no,
        "order_time": "091500",
        "account_mode": "kis_mock",
        "broker": "kis",
        "status": "accepted",
        "lifecycle_state": "pending",
        "strategy": STRATEGY,
        "response_code": "0",
        "raw_response": {
            "rt_cd": "0",
            "odno": order_no,
            "ord_tmd": "091500",
            "msg": "accepted",
            "msg_cd": "0",
        },
        "last_reconcile_detail": {
            "reason_code": "pending_unconfirmed",
            "attributed_fill_qty": "0",
        },
        "correlation_id": f"q46-{uuid4().hex}",
        "scalping_role": None,
    }
    values.update(changes)
    return KISMockOrderLedger(**values)


def _calendar(_market: str, day: date) -> str:
    return "closed" if day.weekday() >= 5 else "open"


@pytest.fixture(autouse=True)
def _fixed_service_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_module, "_today_kst", lambda: TODAY)
    monkeypatch.setattr(lifecycle_module, "trading_session_status", _calendar)


def test_request_requires_exact_unique_bounded_ids_and_reference():
    assert validate_request([1, 2], REF, STRATEGY) is None
    for ids in ([], [True], [0], [-1], [1.0], ["1"], [1, 1], list(range(1, 52))):
        assert validate_request(ids, REF, STRATEGY) is not None
    assert validate_request([1], " ", STRATEGY) == "operator_decision_ref_invalid"
    assert validate_request([1], "x" * 121, STRATEGY) == "operator_decision_ref_invalid"
    assert validate_request([1], REF, "unknown") == "expected_strategy_invalid"


def test_expiry_writer_does_not_accept_caller_supplied_clock_or_calendar():
    parameters = inspect.signature(
        KISMockLifecycleService.expire_legacy_day_orders
    ).parameters
    assert "today" not in parameters
    assert "calendar" not in parameters


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"account_mode": "kis_live"}, "foreign_account_or_broker"),
        ({"broker": "toss"}, "foreign_account_or_broker"),
        ({"instrument_type": "equity_us"}, "not_kr_cash_equity"),
        ({"strategy": "other"}, "strategy_mismatch"),
        ({"scalping_role": "entry"}, "native_source_unproven"),
        ({"correlation_id": None}, "native_source_unproven"),
        ({"mirror_cohort": "mock_counterfactual"}, "native_source_unproven"),
        ({"mirror_source_bucket": "place_original"}, "native_source_unproven"),
        ({"report_item_uuid": uuid4()}, "native_source_unproven"),
        ({"response_code": None}, "native_source_unproven"),
        ({"order_type": "ioc"}, "day_terms_unproven"),
        ({"order_type": "market"}, "day_terms_unproven"),
        ({"price": Decimal("0")}, "day_terms_unproven"),
        ({"lifecycle_state": "fill"}, "state_not_unfilled"),
        ({"lifecycle_state": "stale"}, "already_terminal"),
        ({"lifecycle_state": "expired"}, "already_terminal"),
        ({"status": "unknown"}, "native_source_unproven"),
        ({"last_reconcile_detail": None}, "fill_unknown"),
        (
            {
                "last_reconcile_detail": {
                    "reason_code": "partial_fill_detected",
                    "attributed_fill_qty": "1",
                }
            },
            "fill_unknown",
        ),
        (
            {
                "last_reconcile_detail": {
                    "reason_code": "pending_unconfirmed",
                    "attributed_fill_qty": "1",
                }
            },
            "fill_recorded",
        ),
    ],
)
def test_every_guard_refuses(change, reason):
    row = _row(**change)
    result, evidence = classify_row(
        row, expected_strategy=STRATEGY, today=TODAY, min_sessions=2, calendar=_calendar
    )
    assert result == reason
    assert evidence["scope"] == "row_local_and_xkrx_no_broker_read"


def test_native_ack_exact_identity_and_market_mapping():
    row = _row(order_type="market", price=Decimal("0"))
    result, evidence = classify_row(
        row, expected_strategy=STRATEGY, today=TODAY, min_sessions=2, calendar=_calendar
    )
    assert result == "eligible"
    assert evidence["ord_dvsn"] == "01"
    row.raw_response = {
        "rt_cd": "0",
        "odno": "999999",
        "ord_tmd": "091500",
        "msg": "accepted",
        "msg_cd": "0",
    }
    assert (
        classify_row(
            row,
            expected_strategy=STRATEGY,
            today=TODAY,
            min_sessions=2,
            calendar=_calendar,
        )[0]
        == "native_source_unproven"
    )


def test_calendar_weekend_holiday_and_unknown_fail_closed():
    row = _row(trade_date=datetime(2026, 9, 14, 9, tzinfo=UTC))

    def with_holiday(market: str, day: date) -> str:
        if day == date(2026, 9, 16):
            return "closed"
        return _calendar(market, day)

    reason, evidence = classify_row(
        row,
        expected_strategy=STRATEGY,
        today=TODAY,
        min_sessions=2,
        calendar=with_holiday,
    )
    assert reason == "eligible"
    assert evidence["age_sessions"] == 2
    assert (
        classify_row(
            row,
            expected_strategy=STRATEGY,
            today=TODAY,
            min_sessions=3,
            calendar=with_holiday,
        )[0]
        == "too_recent"
    )

    def broken(market: str, day: date) -> str:
        return "unknown" if day == date(2026, 9, 16) else _calendar(market, day)

    assert (
        classify_row(
            row,
            expected_strategy=STRATEGY,
            today=TODAY,
            min_sessions=2,
            calendar=broken,
        )[0]
        == "calendar_unknown"
    )

    def raised(_market: str, _day: date) -> str:
        raise RuntimeError("calendar unavailable")

    assert (
        classify_row(
            row,
            expected_strategy=STRATEGY,
            today=TODAY,
            min_sessions=2,
            calendar=raised,
        )[0]
        == "calendar_unknown"
    )
    assert (
        classify_row(
            _row(trade_date=datetime(2026, 9, 18, 9, tzinfo=UTC)),
            expected_strategy=STRATEGY,
            today=TODAY,
            min_sessions=2,
            calendar=_calendar,
        )[0]
        == "trade_date_not_past"
    )


def test_real_xkrx_chuseok_boundary_is_too_recent():
    row = _row(trade_date=datetime(2026, 9, 23, 3, tzinfo=UTC))
    assert trading_session_status("kr", date(2026, 9, 23)) == "open"
    assert trading_session_status("kr", date(2026, 9, 24)) == "closed"
    assert trading_session_status("kr", date(2026, 9, 25)) == "closed"
    reason, evidence = classify_row(
        row,
        expected_strategy=STRATEGY,
        today=date(2026, 9, 28),
        min_sessions=2,
    )
    assert reason == "too_recent"
    assert evidence["age_sessions"] == 0


def test_real_xkrx_exact_two_session_cutoff():
    today = date(2026, 9, 28)
    for order_day, expected_reason, expected_age in (
        (date(2026, 9, 21), "eligible", 2),
        (date(2026, 9, 22), "too_recent", 1),
    ):
        row = _row(
            trade_date=datetime(
                order_day.year, order_day.month, order_day.day, 3, tzinfo=UTC
            )
        )
        reason, evidence = classify_row(
            row, expected_strategy=STRATEGY, today=today, min_sessions=2
        )
        assert reason == expected_reason
        assert evidence["age_sessions"] == expected_age


@pytest.mark.asyncio
async def test_service_dry_run_confirm_expiry_and_repeat_are_audit_idempotent(
    db_session: AsyncSession,
):
    row = _row()
    db_session.add(row)
    await db_session.commit()
    service = KISMockLifecycleService(db_session)
    kwargs = {
        "ledger_ids": [row.id],
        "operator_decision_ref": REF,
        "expected_strategy": STRATEGY,
        "min_sessions": 2,
    }
    dry = await service.expire_legacy_day_orders(**kwargs)
    assert dry[0]["decision"] == "would_expire"
    await db_session.refresh(row)
    assert row.lifecycle_state == "pending"
    assert row.reconcile_attempts == 0
    with pytest.raises(ValueError, match="confirm_required"):
        await service.expire_legacy_day_orders(**kwargs, dry_run=False)
    written = await service.expire_legacy_day_orders(
        **kwargs, dry_run=False, confirm=True
    )
    assert written[0]["decision"] == "expired"
    assert written[0]["after_status"] == "expired"
    await db_session.refresh(row)
    assert row.lifecycle_state == "expired"
    assert row.reconcile_attempts == 1
    assert row.last_reconcile_detail["rule_version"] == RULE_VERSION
    assert row.last_reconcile_detail["operator_decision_ref"] == REF
    detail = dict(row.last_reconcile_detail)
    row_id = row.id
    repeat = await service.expire_legacy_day_orders(
        **kwargs, dry_run=False, confirm=True
    )
    assert repeat[0]["reason_code"] == "already_terminal"
    with pytest.raises(ValueError, match="expired_terminal_immutable"):
        await service.apply_lifecycle_transition(
            ledger_id=row_id,
            next_state="pending",
            reason_code="bypass_attempt",
            detail={},
            dry_run=False,
        )
    await db_session.refresh(row)
    assert row.reconcile_attempts == 1
    assert row.last_reconcile_detail == detail


@pytest.mark.asyncio
async def test_generic_lifecycle_transition_cannot_bypass_expiry_guards(
    db_session: AsyncSession,
) -> None:
    row = _row(order_type="ioc", last_reconcile_detail=None)
    db_session.add(row)
    await db_session.commit()
    service = KISMockLifecycleService(db_session)
    for dry_run in (True, False):
        with pytest.raises(ValueError, match="expired_requires_day_classification"):
            await service.apply_lifecycle_transition(
                ledger_id=row.id,
                next_state="expired",
                reason_code="bypass_attempt",
                detail={"untrusted": "data"},
                dry_run=dry_run,
            )
    await db_session.refresh(row)
    assert row.lifecycle_state == "pending"
    assert row.reconcile_attempts == 0
    assert row.reconciled_at is None
    assert row.last_reconcile_detail is None


@pytest.mark.asyncio
async def test_service_calendar_failure_refuses_without_write(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    row = _row()
    db_session.add(row)
    await db_session.commit()
    monkeypatch.setattr(
        lifecycle_module, "trading_session_status", lambda _market, _day: "unknown"
    )
    result = await KISMockLifecycleService(db_session).expire_legacy_day_orders(
        ledger_ids=[row.id],
        operator_decision_ref=REF,
        expected_strategy=STRATEGY,
        min_sessions=2,
        dry_run=False,
        confirm=True,
    )
    assert result[0]["reason_code"] == "calendar_unknown"
    await db_session.refresh(row)
    assert row.lifecycle_state == "pending"
    assert row.reconcile_attempts == 0


@pytest.mark.asyncio
async def test_service_real_xkrx_cutoff_writes_only_exact_eligible_row(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    cutoff = _row(trade_date=datetime(2026, 9, 21, 3, tzinfo=UTC))
    newer = _row(trade_date=datetime(2026, 9, 22, 3, tzinfo=UTC))
    db_session.add_all([cutoff, newer])
    await db_session.commit()
    monkeypatch.setattr(lifecycle_module, "_today_kst", lambda: date(2026, 9, 28))
    monkeypatch.setattr(
        lifecycle_module, "trading_session_status", trading_session_status
    )
    result = await KISMockLifecycleService(db_session).expire_legacy_day_orders(
        ledger_ids=[cutoff.id, newer.id],
        operator_decision_ref=REF,
        expected_strategy=STRATEGY,
        min_sessions=2,
        dry_run=False,
        confirm=True,
    )
    assert [item["reason_code"] for item in result] == ["eligible", "too_recent"]
    assert [item["evidence"]["age_sessions"] for item in result] == [2, 1]
    await db_session.refresh(cutoff)
    await db_session.refresh(newer)
    assert cutoff.lifecycle_state == "expired"
    assert newer.lifecycle_state == "pending"


@pytest.mark.asyncio
async def test_service_mixed_ids_and_local_fill_sibling_refuse(
    db_session: AsyncSession,
):
    eligible = _row()
    filled_sibling = _row(
        order_no=str(uuid4().int % 10**12).zfill(12),
        correlation_id=eligible.correlation_id,
        lifecycle_state="fill",
    )
    refused = _row(trade_date=datetime(2026, 9, 17, 9, tzinfo=UTC))
    db_session.add_all([eligible, filled_sibling, refused])
    await db_session.commit()
    results = await KISMockLifecycleService(db_session).expire_legacy_day_orders(
        ledger_ids=[eligible.id, refused.id, 2**63 - 1],
        operator_decision_ref=REF,
        expected_strategy=STRATEGY,
        min_sessions=2,
        dry_run=False,
        confirm=True,
    )
    assert [item["reason_code"] for item in results] == [
        "fill_row_present",
        "too_recent",
        "row_missing",
    ]
    await db_session.refresh(eligible)
    assert eligible.lifecycle_state == "pending"
    assert eligible.reconcile_attempts == 0


@pytest.mark.asyncio
async def test_service_refusal_rows_remain_unmodified_in_mixed_write_call(
    db_session: AsyncSession,
) -> None:
    rows = [
        _row(),
        _row(strategy="other_strategy"),
        _row(order_type="ioc"),
        _row(response_code=None),
        _row(last_reconcile_detail=None),
        _row(
            last_reconcile_detail={
                "reason_code": "pending_unconfirmed",
                "attributed_fill_qty": "1",
            }
        ),
        _row(lifecycle_state="cancelled"),
        _row(lifecycle_state="fill"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    results = await KISMockLifecycleService(db_session).expire_legacy_day_orders(
        ledger_ids=[row.id for row in rows],
        operator_decision_ref=REF,
        expected_strategy=STRATEGY,
        min_sessions=2,
        dry_run=False,
        confirm=True,
    )
    assert [result["reason_code"] for result in results] == [
        "eligible",
        "strategy_mismatch",
        "day_terms_unproven",
        "native_source_unproven",
        "fill_unknown",
        "fill_recorded",
        "already_terminal",
        "state_not_unfilled",
    ]
    for row in rows:
        await db_session.refresh(row)
    assert [row.lifecycle_state for row in rows] == [
        "expired",
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
        "cancelled",
        "fill",
    ]
    assert [row.reconcile_attempts for row in rows] == [1, 0, 0, 0, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_execution_fill_row_refuses_expiry(db_session: AsyncSession) -> None:
    row = _row()
    db_session.add(row)
    db_session.add(
        ExecutionLedger(
            broker="kis",
            account_mode="mock",
            venue="KRX",
            instrument_type="equity_kr",
            symbol=row.symbol,
            raw_symbol=row.symbol,
            side=row.side,
            broker_order_id=row.order_no,
            fill_seq=1,
            filled_qty=Decimal("1"),
            filled_price=Decimal("70000"),
            filled_notional=Decimal("70000"),
            filled_at=datetime(2026, 9, 14, 10, tzinfo=UTC),
            currency="KRW",
            source="reconciler",
        )
    )
    await db_session.commit()
    result = await KISMockLifecycleService(db_session).expire_legacy_day_orders(
        ledger_ids=[row.id],
        operator_decision_ref=REF,
        expected_strategy=STRATEGY,
        min_sessions=2,
        dry_run=False,
        confirm=True,
    )
    assert result[0]["reason_code"] == "fill_row_present"
    await db_session.refresh(row)
    assert row.lifecycle_state == "pending"
    assert row.reconcile_attempts == 0
