"""Tests for the read-only Alpaca Paper roundtrip report assembler (ROB-92)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.alpaca_paper_ledger_service import (
    LIFECYCLE_CLOSED,
    LIFECYCLE_FILLED,
    LIFECYCLE_FINAL_RECONCILED,
    LIFECYCLE_PLANNED,
    LIFECYCLE_POSITION_RECONCILED,
    LIFECYCLE_PREVIEWED,
    LIFECYCLE_SELL_VALIDATED,
    LIFECYCLE_SUBMITTED,
    LIFECYCLE_VALIDATED,
    RECORD_KIND_EXECUTION,
    RECORD_KIND_PLAN,
    RECORD_KIND_PREVIEW,
    RECORD_KIND_RECONCILE,
    RECORD_KIND_VALIDATION_ATTEMPT,
)
from app.services.alpaca_paper_roundtrip_report_service import (
    AlpacaPaperRoundtripReportService,
)

_T0 = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
_CANDIDATE_UUID = uuid.uuid4()
_BRIEFING_UUID = uuid.uuid4()


def _ts(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


def _row(
    *,
    lifecycle_state: str,
    side: str,
    record_kind: str,
    client_order_id: str,
    lifecycle_correlation_id: str = "corr-rob92",
    created_at: datetime | None = None,
    **overrides,
) -> SimpleNamespace:
    defaults = {
        "id": len(client_order_id) + int((created_at or _T0).timestamp()) % 100,
        "client_order_id": client_order_id,
        "lifecycle_correlation_id": lifecycle_correlation_id,
        "record_kind": record_kind,
        "broker": "alpaca",
        "account_mode": "alpaca_paper",
        "lifecycle_state": lifecycle_state,
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_symbol": "BTCUSD",
        "execution_venue": "alpaca_paper",
        "execution_asset_class": "crypto",
        "instrument_type": "crypto",
        "side": side,
        "order_type": "limit",
        "time_in_force": "gtc",
        "requested_qty": "0.001",
        "requested_notional": None,
        "requested_price": "50000",
        "currency": "USD",
        "broker_order_id": None,
        "submitted_at": None,
        "order_status": None,
        "filled_qty": None,
        "filled_avg_price": None,
        "fee_amount": None,
        "fee_currency": None,
        "qty_delta": None,
        "position_snapshot": None,
        "reconcile_status": None,
        "reconciled_at": None,
        "settlement_status": None,
        "settlement_at": None,
        "notes": None,
        "error_summary": None,
        "preview_payload": None,
        "validation_summary": None,
        "briefing_artifact_run_uuid": _BRIEFING_UUID,
        "briefing_artifact_status": "ready",
        "qa_evaluator_status": "passed",
        "approval_bridge_generated_at": _ts(-60),
        "approval_bridge_status": "available",
        "candidate_uuid": _CANDIDATE_UUID,
        "workflow_stage": "crypto_weekend",
        "purpose": "paper_roundtrip_audit",
        "created_at": created_at or _T0,
        "updated_at": created_at or _T0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _complete_roundtrip_rows(
    correlation_id: str = "corr-rob92",
) -> list[SimpleNamespace]:
    return [
        _row(
            lifecycle_state=LIFECYCLE_PLANNED,
            record_kind=RECORD_KIND_PLAN,
            side="buy",
            client_order_id="buy-rob92",
            lifecycle_correlation_id=correlation_id,
            created_at=_ts(0),
        ),
        _row(
            lifecycle_state=LIFECYCLE_PREVIEWED,
            record_kind=RECORD_KIND_PREVIEW,
            side="buy",
            client_order_id="buy-rob92",
            lifecycle_correlation_id=correlation_id,
            preview_payload={"symbol": "BTCUSD", "side": "buy"},
            created_at=_ts(1),
        ),
        _row(
            lifecycle_state=LIFECYCLE_VALIDATED,
            record_kind=RECORD_KIND_VALIDATION_ATTEMPT,
            side="buy",
            client_order_id="buy-rob92",
            lifecycle_correlation_id=correlation_id,
            validation_summary={"ok": True},
            created_at=_ts(2),
        ),
        _row(
            lifecycle_state=LIFECYCLE_SUBMITTED,
            record_kind=RECORD_KIND_EXECUTION,
            side="buy",
            client_order_id="buy-rob92",
            lifecycle_correlation_id=correlation_id,
            broker_order_id="broker-buy",
            submitted_at=_ts(3),
            order_status="accepted",
            created_at=_ts(3),
        ),
        _row(
            lifecycle_state=LIFECYCLE_FILLED,
            record_kind=RECORD_KIND_EXECUTION,
            side="buy",
            client_order_id="buy-rob92",
            lifecycle_correlation_id=correlation_id,
            filled_qty="0.001",
            filled_avg_price="50000",
            qty_delta="0.001",
            order_status="filled",
            created_at=_ts(4),
        ),
        _row(
            lifecycle_state=LIFECYCLE_POSITION_RECONCILED,
            record_kind=RECORD_KIND_EXECUTION,
            side="buy",
            client_order_id="buy-rob92",
            lifecycle_correlation_id=correlation_id,
            reconcile_status="matched",
            position_snapshot={"symbol": "BTCUSD", "qty": "0.001"},
            created_at=_ts(5),
        ),
        _row(
            lifecycle_state=LIFECYCLE_SELL_VALIDATED,
            record_kind=RECORD_KIND_VALIDATION_ATTEMPT,
            side="sell",
            client_order_id="sell-rob92",
            lifecycle_correlation_id=correlation_id,
            validation_summary={"ok": True, "side": "sell"},
            created_at=_ts(6),
        ),
        _row(
            lifecycle_state=LIFECYCLE_CLOSED,
            record_kind=RECORD_KIND_EXECUTION,
            side="sell",
            client_order_id="sell-rob92",
            lifecycle_correlation_id=correlation_id,
            broker_order_id="broker-sell",
            filled_qty="0.001",
            filled_avg_price="51000",
            qty_delta="-0.001",
            order_status="filled",
            created_at=_ts(7),
        ),
        _row(
            lifecycle_state=LIFECYCLE_FINAL_RECONCILED,
            record_kind=RECORD_KIND_RECONCILE,
            side="sell",
            client_order_id="sell-rob92",
            lifecycle_correlation_id=correlation_id,
            reconcile_status="flat",
            settlement_status="n_a",
            position_snapshot={"symbol": "BTCUSD", "qty": "0"},
            created_at=_ts(8),
        ),
    ]


def _service_with_rows(
    rows: list[SimpleNamespace],
) -> tuple[AlpacaPaperRoundtripReportService, AsyncMock]:
    db = AsyncMock()
    svc = AlpacaPaperRoundtripReportService(db)
    ledger = SimpleNamespace(
        list_by_correlation_id=AsyncMock(return_value=rows),
        get_by_client_order_id=AsyncMock(return_value=rows[0] if rows else None),
        list_by_candidate_uuid=AsyncMock(return_value=rows),
        list_by_briefing_artifact_run_uuid=AsyncMock(return_value=rows),
    )
    svc._ledger = ledger  # noqa: SLF001 - isolate the read assembler from SQL in unit tests
    return svc, db


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_report_by_correlation_returns_complete_read_only_report():
    rows = _complete_roundtrip_rows()
    svc, db = _service_with_rows(rows)

    report = await svc.build_report(
        lifecycle_correlation_id="corr-rob92",
        include_ledger_rows=False,
        now=_ts(30),
    )

    assert report.status == "complete"
    assert report.lifecycle_correlation_id == "corr-rob92"
    assert report.completeness.is_complete is True
    assert report.candidate.candidate_uuid == str(_CANDIDATE_UUID)
    assert report.qa_result.briefing_artifact_run_uuid == str(_BRIEFING_UUID)
    assert report.approval_packet.preview_payload == {"symbol": "BTCUSD", "side": "buy"}
    assert report.buy_leg is not None
    assert report.buy_leg.fill.filled_qty == Decimal("0.001")
    assert report.sell_leg is not None
    assert report.sell_leg.fill.filled_avg_price == Decimal("51000")
    assert report.final_position.source == "ledger_snapshot"
    assert report.final_position.qty == Decimal("0")
    assert report.open_orders.source == "missing"
    assert report.anomalies.should_block is False
    assert report.safety.read_only is True
    assert report.safety.broker_mutation_performed is False
    assert report.ledger_rows is None
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_report_missing_required_steps_marks_incomplete_read_only():
    omitted_steps = {LIFECYCLE_CLOSED, LIFECYCLE_FINAL_RECONCILED}
    rows = [
        row
        for row in _complete_roundtrip_rows()
        if row.lifecycle_state not in omitted_steps
    ]
    svc, db = _service_with_rows(rows)

    report = await svc.build_report(
        lifecycle_correlation_id="corr-rob92",
        include_ledger_rows=False,
        now=_ts(30),
    )

    assert report.status == "incomplete"
    assert report.completeness.is_complete is False
    assert omitted_steps.issubset(set(report.completeness.missing_steps))
    assert report.ledger_rows is None
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_report_by_client_order_id_expands_to_correlation_rows():
    rows = _complete_roundtrip_rows()
    svc, _db = _service_with_rows(rows)

    report = await svc.build_report(
        client_order_id="buy-rob92", include_ledger_rows=False
    )

    assert report.status == "complete"
    svc._ledger.get_by_client_order_id.assert_awaited_once_with("buy-rob92")  # noqa: SLF001
    svc._ledger.list_by_correlation_id.assert_awaited_once_with("corr-rob92")  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_report_not_found_has_safe_empty_shape():
    svc, db = _service_with_rows([])

    report = await svc.build_report(
        lifecycle_correlation_id="missing-corr",
        include_ledger_rows=False,
        now=_ts(30),
    )

    assert report.status == "not_found"
    assert report.completeness.is_complete is False
    assert report.buy_leg is None
    assert report.sell_leg is None
    assert report.safety.read_only is True
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_candidate_lookup_returns_grouped_list_response():
    rows = _complete_roundtrip_rows("corr-a") + _complete_roundtrip_rows("corr-b")
    svc, _db = _service_with_rows(rows)

    response = await svc.build_reports_for_candidate_uuid(
        _CANDIDATE_UUID,
        include_ledger_rows=False,
    )

    assert response.lookup_key.kind == "candidate_uuid"
    assert response.count == 2
    assert [item.lifecycle_correlation_id for item in response.items] == [
        "corr-a",
        "corr-b",
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_open_order_snapshot_is_caller_supplied_and_can_block_report():
    rows = _complete_roundtrip_rows()
    svc, _db = _service_with_rows(rows)

    report = await svc.build_report(
        lifecycle_correlation_id="corr-rob92",
        open_orders=[{"id": "open-1", "status": "new", "symbol": "BTCUSD"}],
        include_ledger_rows=False,
        now=_ts(30),
    )

    assert report.status == "anomaly"
    assert report.open_orders.source == "caller_supplied"
    assert report.anomalies.should_block is True
    assert report.anomalies.anomalies[0]["check_id"] == "unexpected_open_orders"
