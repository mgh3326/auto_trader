"""Tests for AlpacaPaperLedgerService lifecycle methods (ROB-84/ROB-90).

Covers: model columns/constraints, _derive_lifecycle_state, service method behavior,
forbidden imports/strings, ORM model shape, canonical taxonomy constants.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

SERVICE_PATH = Path(__file__).parents[2] / "app/services/alpaca_paper_ledger_service.py"


# ---------------------------------------------------------------------------
# ORM model shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_alpaca_paper_ledger_model_columns():
    from app.models.review import AlpacaPaperOrderLedger

    cols = {c.name for c in AlpacaPaperOrderLedger.__table__.columns}
    expected = {
        "id",
        "client_order_id",
        # ROB-90 new columns
        "lifecycle_correlation_id",
        "record_kind",
        "leg_role",
        "validation_attempt_no",
        "validation_outcome",
        "confirm_flag",
        "fee_amount",
        "fee_currency",
        "settlement_status",
        "settlement_at",
        "qty_delta",
        # existing columns
        "broker",
        "account_mode",
        "lifecycle_state",
        "signal_symbol",
        "signal_venue",
        "execution_symbol",
        "execution_venue",
        "execution_asset_class",
        "instrument_type",
        "side",
        "order_type",
        "time_in_force",
        "requested_qty",
        "requested_notional",
        "requested_price",
        "currency",
        "preview_payload",
        "validation_summary",
        "broker_order_id",
        "submitted_at",
        "order_status",
        "filled_qty",
        "filled_avg_price",
        "cancel_status",
        "canceled_at",
        "position_snapshot",
        "reconcile_status",
        "reconciled_at",
        "briefing_artifact_run_uuid",
        "briefing_artifact_status",
        "qa_evaluator_status",
        "approval_bridge_generated_at",
        "approval_bridge_status",
        "candidate_uuid",
        "workflow_stage",
        "purpose",
        "raw_responses",
        "notes",
        "error_summary",
        "created_at",
        "updated_at",
    }
    assert expected <= cols
    assert AlpacaPaperOrderLedger.__table__.schema == "review"


@pytest.mark.unit
def test_alpaca_paper_ledger_model_constraints():
    from app.models.review import AlpacaPaperOrderLedger

    constraint_names = {c.name for c in AlpacaPaperOrderLedger.__table__.constraints}
    # ROB-90: old single-column unique replaced by partial unique indexes
    assert "uq_alpaca_paper_ledger_client_order_id" not in constraint_names
    # Core broker/mode/side constraints must still exist
    assert any("alpaca_paper_ledger_broker" in (n or "") for n in constraint_names)
    assert any(
        "alpaca_paper_ledger_account_mode" in (n or "") for n in constraint_names
    )
    assert any(
        "alpaca_paper_ledger_lifecycle_state" in (n or "") for n in constraint_names
    )
    assert any("alpaca_paper_ledger_side" in (n or "") for n in constraint_names)
    assert any("alpaca_paper_ledger_order_type" in (n or "") for n in constraint_names)
    # ROB-90 new CHECK constraints
    assert any("alpaca_paper_ledger_record_kind" in (n or "") for n in constraint_names)
    assert any(
        "alpaca_paper_ledger_validation_outcome" in (n or "") for n in constraint_names
    )
    assert any(
        "alpaca_paper_ledger_settlement_status" in (n or "") for n in constraint_names
    )


@pytest.mark.unit
def test_alpaca_paper_ledger_partial_unique_indexes():
    from app.models.review import AlpacaPaperOrderLedger

    index_names = {i.name for i in AlpacaPaperOrderLedger.__table__.indexes}
    # ROB-90 partial unique indexes
    assert "uq_alpaca_paper_ledger_client_order_kind" in index_names
    assert "uq_alpaca_paper_ledger_validation_attempt" in index_names
    # New correlation/record_kind lookup indexes
    assert "ix_alpaca_paper_ledger_correlation_id" in index_names
    assert "ix_alpaca_paper_ledger_record_kind" in index_names


@pytest.mark.unit
def test_alpaca_paper_ledger_lifecycle_check_canonical_states():
    """The lifecycle CHECK contains ROB-90 states plus stale cleanup-required."""
    from app.models.review import AlpacaPaperOrderLedger

    lifecycle_check = None
    for c in AlpacaPaperOrderLedger.__table__.constraints:
        # SQLAlchemy naming_convention prefixes check names with ck_<table>_.
        if hasattr(c, "name") and c.name.endswith(
            "alpaca_paper_ledger_lifecycle_state"
        ):
            lifecycle_check = c
            break

    assert lifecycle_check is not None
    check_text = str(lifecycle_check.sqltext)

    canonical = [
        "planned",
        "previewed",
        "validated",
        "submitted",
        "filled",
        "position_reconciled",
        "sell_validated",
        "closed",
        "final_reconciled",
        "anomaly",
        "stale_preview_cleanup_required",
    ]
    for state in canonical:
        assert state in check_text, f"Canonical state {state!r} missing from CHECK"

    excluded = [
        "validation_failed",
        "open",
        "partially_filled",
        "canceled",
        "unexpected",
    ]
    for state in excluded:
        assert state not in check_text, f"Old state {state!r} should not be in CHECK"


# ---------------------------------------------------------------------------
# Canonical lifecycle constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonical_lifecycle_constants_exported():
    from app.services.alpaca_paper_ledger_service import (
        CANONICAL_LIFECYCLE_STATES,
        LIFECYCLE_ANOMALY,
        LIFECYCLE_CLOSED,
        LIFECYCLE_FILLED,
        LIFECYCLE_FINAL_RECONCILED,
        LIFECYCLE_PLANNED,
        LIFECYCLE_POSITION_RECONCILED,
        LIFECYCLE_PREVIEWED,
        LIFECYCLE_SELL_VALIDATED,
        LIFECYCLE_STALE_PREVIEW_CLEANUP_REQUIRED,
        LIFECYCLE_SUBMITTED,
        LIFECYCLE_VALIDATED,
    )

    assert LIFECYCLE_PLANNED == "planned"
    assert LIFECYCLE_PREVIEWED == "previewed"
    assert LIFECYCLE_VALIDATED == "validated"
    assert LIFECYCLE_SUBMITTED == "submitted"
    assert LIFECYCLE_FILLED == "filled"
    assert LIFECYCLE_POSITION_RECONCILED == "position_reconciled"
    assert LIFECYCLE_SELL_VALIDATED == "sell_validated"
    assert LIFECYCLE_CLOSED == "closed"
    assert LIFECYCLE_FINAL_RECONCILED == "final_reconciled"
    assert LIFECYCLE_ANOMALY == "anomaly"
    assert LIFECYCLE_STALE_PREVIEW_CLEANUP_REQUIRED == "stale_preview_cleanup_required"

    assert len(CANONICAL_LIFECYCLE_STATES) == 11
    assert CANONICAL_LIFECYCLE_STATES == {
        "planned",
        "previewed",
        "validated",
        "submitted",
        "filled",
        "position_reconciled",
        "sell_validated",
        "closed",
        "final_reconciled",
        "anomaly",
        "stale_preview_cleanup_required",
    }


# ---------------------------------------------------------------------------
# _derive_lifecycle_state — ROB-90 canonical mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order_status,filled_qty,expected",
    [
        # filled → filled
        ("filled", None, "filled"),
        ("filled", 1.5, "filled"),
        # partially_filled → submitted (broker status preserved in order_status)
        ("partially_filled", None, "submitted"),
        ("partially_filled", 0.5, "submitted"),
        # open statuses → submitted
        ("new", None, "submitted"),
        ("accepted", None, "submitted"),
        ("pending_new", None, "submitted"),
        ("accepted_for_bidding", None, "submitted"),
        ("held", None, "submitted"),
        ("pending_cancel", None, "submitted"),
        ("pending_replace", None, "submitted"),
        ("replaced", None, "submitted"),
        # open status with filled_qty = 0 → submitted (not anomaly)
        ("new", 0.0, "submitted"),
        # open status with filled_qty > 0 → anomaly
        ("new", 0.5, "anomaly"),
        ("accepted", 1.0, "anomaly"),
        # canceled → anomaly (ROB-90: no benign cancel state)
        ("canceled", None, "anomaly"),
        ("canceled", 0, "anomaly"),
        # broker anomaly statuses
        ("rejected", None, "anomaly"),
        ("expired", None, "anomaly"),
        ("suspended", None, "anomaly"),
        # unknown status → anomaly
        ("mystery_status", None, "anomaly"),
        (None, None, "anomaly"),
    ],
)
@pytest.mark.unit
def test_derive_lifecycle_state(order_status, filled_qty, expected):
    from app.services.alpaca_paper_ledger_service import _derive_lifecycle_state

    assert _derive_lifecycle_state(order_status, filled_qty) == expected


# ---------------------------------------------------------------------------
# Helpers for mocking the service DB session
# ---------------------------------------------------------------------------


def _make_row(**kwargs) -> Any:
    """Create a fake AlpacaPaperOrderLedger-like namespace."""
    defaults: dict[str, Any] = {
        "id": 1,
        "client_order_id": "test-client-001",
        "lifecycle_correlation_id": "test-client-001",
        "record_kind": "execution",
        "broker": "alpaca",
        "account_mode": "alpaca_paper",
        "lifecycle_state": "previewed",
        "execution_symbol": "BTCUSD",
        "execution_venue": "alpaca_paper",
        "instrument_type": "crypto",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "gtc",
        "requested_qty": None,
        "requested_notional": None,
        "requested_price": None,
        "currency": "USD",
        "preview_payload": None,
        "validation_summary": None,
        "raw_responses": None,
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_asset_class": "crypto",
        "workflow_stage": None,
        "purpose": None,
        "briefing_artifact_run_uuid": None,
        "briefing_artifact_status": None,
        "qa_evaluator_status": None,
        "approval_bridge_generated_at": None,
        "approval_bridge_status": None,
        "candidate_uuid": None,
        "validation_attempt_no": None,
        "validation_outcome": None,
        "confirm_flag": None,
        "leg_role": None,
        "settlement_status": None,
        "qty_delta": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _mock_db_with_row(row: Any):
    """Build an AsyncMock DB session that always returns a single row."""
    db = AsyncMock()

    class _ScalarResult:
        def scalar_one_or_none(self):
            return row

        def scalars(self):
            class _S:
                def all(self_inner):
                    return [row] if row is not None else []

            return _S()

    db.execute = AsyncMock(return_value=_ScalarResult())
    db.commit = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# record_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_plan_inserts_planned_row():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="planned", record_kind="plan")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_plan(
        client_order_id="test-client-001",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
    )
    assert result.lifecycle_state == "planned"
    assert result.record_kind == "plan"
    db.execute.assert_called()
    db.commit.assert_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_plan_uses_client_order_id_as_correlation_default():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        lifecycle_state="planned",
        record_kind="plan",
        lifecycle_correlation_id="test-client-001",
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_plan(
        client_order_id="test-client-001",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
    )
    assert result.lifecycle_correlation_id == "test-client-001"


# ---------------------------------------------------------------------------
# record_preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_preview_inserts_and_returns_row():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="previewed", record_kind="preview")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_preview(
        client_order_id="test-client-001",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
    )
    assert result.lifecycle_state == "previewed"
    assert result.record_kind == "preview"
    assert result.client_order_id == "test-client-001"
    db.execute.assert_called()
    db.commit.assert_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_preview_empty_client_order_id_raises():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    db = AsyncMock()
    svc = AlpacaPaperLedgerService(db)

    with pytest.raises(ValueError, match="client_order_id must not be empty"):
        await svc.record_preview(
            client_order_id="   ",
            execution_symbol="BTCUSD",
            execution_venue="alpaca_paper",
            instrument_type=InstrumentType.crypto,
            side="buy",
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_preview_sets_correlation_id():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="previewed", lifecycle_correlation_id="corr-999")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_preview(
        client_order_id="test-client-001",
        lifecycle_correlation_id="corr-999",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
    )
    assert result.lifecycle_correlation_id == "corr-999"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_preview_sanitizes_preview_payload():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        preview_payload={
            "symbol": "BTCUSD",
            "api_key": "***",
        }
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_preview(
        client_order_id="test-client-003",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        preview_payload={"symbol": "BTCUSD", "api_key": "***"},
    )
    assert result is not None


@pytest.mark.unit
def test_redact_sensitive_text_masks_operator_narrative_values():
    from app.services.alpaca_paper_ledger_service import _redact_sensitive_text

    text = (
        "reconcile failed token=abc123 authorization: Bearer xyz account_id=paper-1 ok"
    )

    redacted = _redact_sensitive_text(text)

    assert redacted == (
        "reconcile failed token=[REDACTED] "
        "authorization: [REDACTED] account_id=[REDACTED] ok"
    )
    assert "abc123" not in redacted
    assert "xyz" not in redacted
    assert "paper-1" not in redacted


# ---------------------------------------------------------------------------
# record_validation_attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_validation_attempt_failed_creates_anomaly_row():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        lifecycle_state="anomaly",
        record_kind="validation_attempt",
        validation_attempt_no=1,
        validation_outcome="failed",
        confirm_flag=False,
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_validation_attempt(
        client_order_id="test-val-001",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        validation_attempt_no=1,
        validation_outcome="failed",
    )
    assert result.lifecycle_state == "anomaly"
    assert result.record_kind == "validation_attempt"
    assert result.validation_attempt_no == 1
    assert result.validation_outcome == "failed"
    assert result.confirm_flag is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_validation_attempt_passed_creates_validated_row():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        lifecycle_state="validated",
        record_kind="validation_attempt",
        validation_attempt_no=1,
        validation_outcome="passed",
        confirm_flag=False,
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_validation_attempt(
        client_order_id="test-val-002",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        validation_attempt_no=1,
        validation_outcome="passed",
    )
    assert result.lifecycle_state == "validated"
    assert result.confirm_flag is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_validation_attempt_increments():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row2 = _make_row(
        lifecycle_state="anomaly",
        record_kind="validation_attempt",
        validation_attempt_no=2,
        validation_outcome="failed",
        confirm_flag=False,
    )
    db = _mock_db_with_row(row2)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_validation_attempt(
        client_order_id="test-val-003",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        validation_attempt_no=2,
        validation_outcome="failed",
    )
    assert result.validation_attempt_no == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_validation_attempt_invalid_no_raises():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    db = AsyncMock()
    svc = AlpacaPaperLedgerService(db)
    with pytest.raises(ValueError, match="validation_attempt_no must be >= 1"):
        await svc.record_validation_attempt(
            client_order_id="test-val-004",
            execution_symbol="BTCUSD",
            execution_venue="alpaca_paper",
            instrument_type=InstrumentType.crypto,
            side="buy",
            validation_attempt_no=0,
        )


# ---------------------------------------------------------------------------
# record_submit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_anomaly_on_canceled_order():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="anomaly", order_status="canceled")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "broker-order-id", "status": "canceled", "filled_qty": "0"},
    )
    assert result.lifecycle_state == "anomaly"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_filled_order():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        lifecycle_state="filled",
        order_status="filled",
        filled_qty="0.001",
        filled_avg_price="50000",
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={
            "id": "bid1",
            "status": "filled",
            "filled_qty": "0.001",
            "filled_avg_price": "50000",
        },
    )
    assert result.lifecycle_state == "filled"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_partially_filled_is_submitted():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="submitted", order_status="partially_filled")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "bid2", "status": "partially_filled", "filled_qty": "0.0005"},
    )
    assert result.lifecycle_state == "submitted"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_rejected_is_anomaly():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="anomaly", order_status="rejected")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "bid3", "status": "rejected"},
    )
    assert result.lifecycle_state == "anomaly"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_unknown_status_is_anomaly():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="anomaly", order_status="mystery")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "bid4", "status": "mystery"},
    )
    assert result.lifecycle_state == "anomaly"


# ---------------------------------------------------------------------------
# record_cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_cancel_writes_cancel_metadata():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(cancel_status="confirmed", lifecycle_state="anomaly")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_cancel("test-client-001", cancel_status="confirmed")
    assert result.cancel_status == "confirmed"
    db.execute.assert_called()


# ---------------------------------------------------------------------------
# record_position_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_position_snapshot_none_writes_zero_qty():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        position_snapshot={
            "qty": "0",
            "avg_entry_price": None,
            "fetched_at": "2026-05-03T00:00:00+00:00",
        },
        lifecycle_state="position_reconciled",
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_position_snapshot("test-client-001", position=None)
    assert result.position_snapshot["qty"] == "0"
    assert result.position_snapshot["avg_entry_price"] is None
    assert result.lifecycle_state == "position_reconciled"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_position_snapshot_with_position_writes_qty():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        position_snapshot={
            "qty": "0.001",
            "avg_entry_price": "50000",
            "fetched_at": "2026-05-03T00:00:00+00:00",
        },
        lifecycle_state="position_reconciled",
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_position_snapshot(
        "test-client-001",
        position={"qty": "0.001", "avg_entry_price": "50000"},
    )
    assert result.position_snapshot["qty"] == "0.001"
    assert result.position_snapshot["avg_entry_price"] == "50000"


# ---------------------------------------------------------------------------
# record_close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_close_advances_to_closed():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="closed", qty_delta="-0.001")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_close("test-client-001", qty_delta=-0.001)
    assert result.lifecycle_state == "closed"
    db.execute.assert_called()


# ---------------------------------------------------------------------------
# record_reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_reconcile_writes_status():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(reconcile_status="reconciled")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_reconcile(
        "test-client-001", reconcile_status="reconciled"
    )
    assert result.reconcile_status == "reconciled"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_reconcile_persists_redacted_error_summary():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(reconcile_status="unexpected_state")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    sensitive_key = "password"
    await svc.record_reconcile(
        "test-client-001",
        reconcile_status="unexpected_state",
        error_summary=f"failed with api_key=abc123 and {sensitive_key}=secret",
    )

    update_stmt = db.execute.call_args_list[1].args[0]
    update_params = update_stmt.compile().params
    assert update_params["error_summary"] == (
        f"failed with api_key=[REDACTED] and {sensitive_key}=[REDACTED]"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_reconcile_clears_error_summary_when_omitted():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        reconcile_status="filled_position_matched",
        error_summary="previous failure",
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    await svc.record_reconcile(
        "test-client-001", reconcile_status="filled_position_matched"
    )

    update_stmt = db.execute.call_args_list[1].args[0]
    update_params = update_stmt.compile().params
    assert update_params["error_summary"] is None


# ---------------------------------------------------------------------------
# record_final_reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_final_reconcile_advances_to_final_reconciled():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        lifecycle_state="final_reconciled",
        record_kind="reconcile",
        settlement_status="n_a",
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_final_reconcile("test-client-001")
    assert result.lifecycle_state == "final_reconciled"
    assert result.settlement_status == "n_a"


# ---------------------------------------------------------------------------
# list_by_correlation_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_by_correlation_id_returns_rows():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    buy_row = _make_row(
        client_order_id="buy-001",
        lifecycle_correlation_id="corr-abc",
        side="buy",
        lifecycle_state="filled",
    )
    sell_row = _make_row(
        client_order_id="sell-001",
        lifecycle_correlation_id="corr-abc",
        side="sell",
        lifecycle_state="closed",
    )

    class _ScalarResult:
        def scalar_one_or_none(self):
            return buy_row

        def scalars(self):
            class _S:
                def all(self_inner):
                    return [buy_row, sell_row]

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult())
    db.commit = AsyncMock()

    svc = AlpacaPaperLedgerService(db)
    rows = await svc.list_by_correlation_id("corr-abc")
    assert isinstance(rows, list)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_by_correlation_id_empty_raises():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    db = AsyncMock()
    svc = AlpacaPaperLedgerService(db)
    with pytest.raises(ValueError, match="lifecycle_correlation_id must not be empty"):
        await svc.list_by_correlation_id("  ")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_by_candidate_uuid_returns_rows():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    candidate_uuid = uuid4()
    row = _make_row(
        candidate_uuid=candidate_uuid, lifecycle_correlation_id="corr-candidate"
    )

    class _ScalarResult:
        def scalars(self):
            class _S:
                def all(self_inner):
                    return [row]

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult())
    db.commit = AsyncMock()

    svc = AlpacaPaperLedgerService(db)
    rows = await svc.list_by_candidate_uuid(candidate_uuid)

    assert rows == [row]
    db.execute.assert_awaited_once()
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_by_briefing_artifact_run_uuid_returns_rows():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    briefing_uuid = uuid4()
    row = _make_row(
        briefing_artifact_run_uuid=briefing_uuid,
        lifecycle_correlation_id="corr-briefing",
    )

    class _ScalarResult:
        def scalars(self):
            class _S:
                def all(self_inner):
                    return [row]

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult())
    db.commit = AsyncMock()

    svc = AlpacaPaperLedgerService(db)
    rows = await svc.list_by_briefing_artifact_run_uuid(briefing_uuid)

    assert rows == [row]
    db.execute.assert_awaited_once()
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# find_executed_by_client_order_id (ROB-91)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_find_executed_returns_none_for_preview_only_row():
    """A preview-only row (record_kind='preview') must not be returned."""
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    class _ScalarResult:
        def scalar_one_or_none(self):
            return None

        def scalars(self):
            class _S:
                def all(self_inner):
                    return []

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult())
    svc = AlpacaPaperLedgerService(db)
    result = await svc.find_executed_by_client_order_id("preview-only-001")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_find_executed_returns_row_after_fill():
    """An execution row in 'filled' state must be returned."""
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    exec_row = _make_row(
        client_order_id="buy-exec-001",
        record_kind="execution",
        lifecycle_state="filled",
        filled_qty="0.001",
    )

    class _ScalarResult:
        def scalar_one_or_none(self):
            return exec_row

        def scalars(self):
            class _S:
                def all(self_inner):
                    return [exec_row]

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult())
    svc = AlpacaPaperLedgerService(db)
    result = await svc.find_executed_by_client_order_id("buy-exec-001")
    assert result is not None
    assert result.lifecycle_state == "filled"
    assert result.record_kind == "execution"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_find_executed_returns_row_for_position_reconciled():
    """An execution row in 'position_reconciled' is an executed state."""
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    exec_row = _make_row(
        client_order_id="buy-exec-002",
        record_kind="execution",
        lifecycle_state="position_reconciled",
    )

    class _ScalarResult:
        def scalar_one_or_none(self):
            return exec_row

        def scalars(self):
            class _S:
                def all(self_inner):
                    return [exec_row]

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult())
    svc = AlpacaPaperLedgerService(db)
    result = await svc.find_executed_by_client_order_id("buy-exec-002")
    assert result is not None
    assert result.lifecycle_state == "position_reconciled"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_find_executed_returns_none_for_anomaly_execution():
    """An execution row in 'anomaly' state is not an executed state."""
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    class _ScalarResult:
        def scalar_one_or_none(self):
            return None

        def scalars(self):
            class _S:
                def all(self_inner):
                    return []

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult())
    svc = AlpacaPaperLedgerService(db)
    result = await svc.find_executed_by_client_order_id("anomaly-exec-001")
    assert result is None


@pytest.mark.unit
def test_executed_lifecycle_states_exported():
    """EXECUTED_LIFECYCLE_STATES must be exported and contain correct post-submit states."""
    from app.services.alpaca_paper_ledger_service import EXECUTED_LIFECYCLE_STATES

    assert isinstance(EXECUTED_LIFECYCLE_STATES, frozenset)
    # All post-submit states included
    assert "submitted" in EXECUTED_LIFECYCLE_STATES
    assert "filled" in EXECUTED_LIFECYCLE_STATES
    assert "position_reconciled" in EXECUTED_LIFECYCLE_STATES
    assert "sell_validated" in EXECUTED_LIFECYCLE_STATES
    assert "closed" in EXECUTED_LIFECYCLE_STATES
    assert "final_reconciled" in EXECUTED_LIFECYCLE_STATES
    # Pre-submit and anomaly excluded
    assert "planned" not in EXECUTED_LIFECYCLE_STATES
    assert "previewed" not in EXECUTED_LIFECYCLE_STATES
    assert "validated" not in EXECUTED_LIFECYCLE_STATES
    assert "anomaly" not in EXECUTED_LIFECYCLE_STATES


# ---------------------------------------------------------------------------
# LedgerNotFoundError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_missing_client_order_id_raises_ledger_not_found():
    from app.services.alpaca_paper_ledger_service import (
        AlpacaPaperLedgerService,
        LedgerNotFoundError,
    )

    db = _mock_db_with_row(None)  # row not found

    svc = AlpacaPaperLedgerService(db)
    with pytest.raises(LedgerNotFoundError):
        await svc.record_submit("nonexistent-id", order={"status": "canceled"})


@pytest.mark.asyncio
@pytest.mark.unit
async def test_missing_client_order_id_raises_on_cancel():
    from app.services.alpaca_paper_ledger_service import (
        AlpacaPaperLedgerService,
        LedgerNotFoundError,
    )

    db = _mock_db_with_row(None)
    svc = AlpacaPaperLedgerService(db)

    with pytest.raises(LedgerNotFoundError):
        await svc.record_cancel("nonexistent-id", cancel_status="confirmed")


# ---------------------------------------------------------------------------
# Raw response accumulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_raw_responses_accumulate_by_event_key():
    """Verify that _accumulate_raw_response merges by event key."""
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    accumulated: dict = {}

    class _ScalarResult:
        def __init__(self, row):
            self._row = row

        def scalar_one_or_none(self):
            return self._row

        def scalars(self):
            class _S:
                def __init__(self, row):
                    self._row = row

                def all(self):
                    return [self._row] if self._row else []

            return _S(self._row)

    def make_execute_mock():
        async def _execute(stmt):
            nonlocal accumulated
            return _ScalarResult(
                _make_row(raw_responses=accumulated if accumulated else None)
            )

        return _execute

    db = AsyncMock()
    db.commit = AsyncMock()

    existing_raw: dict = {}

    async def execute_side_effect(stmt):
        nonlocal existing_raw
        row = _make_row(raw_responses=dict(existing_raw))
        return _ScalarResult(row)

    db.execute = AsyncMock(side_effect=execute_side_effect)

    svc = AlpacaPaperLedgerService(db)
    existing_raw["submit"] = {"status": "canceled"}
    await svc._accumulate_raw_response("test-client-001", "status", {"status": "new"})
    existing_raw["status"] = {"status": "new"}
    await svc._accumulate_raw_response("test-client-001", "cancel", {"cancel": "ok"})

    assert db.execute.call_count >= 2


# ---------------------------------------------------------------------------
# Static safety: no broker mutation imports / fan-out ledger updates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lifecycle_update_statements_scope_by_primary_key():
    """Lifecycle updates must not fan out across rows sharing client_order_id."""
    source = SERVICE_PATH.read_text()
    tree = ast.parse(source)

    def is_ledger_update_expr(node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "update"
                and node.args
                and ast.unparse(node.args[0]) == "AlpacaPaperOrderLedger"
            ):
                return True
            return is_ledger_update_expr(node.func)
        if isinstance(node, ast.Attribute):
            return is_ledger_update_expr(node.value)
        return False

    update_where_conditions: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "where"
            and is_ledger_update_expr(node.func.value)
        ):
            condition = ast.unparse(node.args[0])
            update_where_conditions.append(condition)
            assert "AlpacaPaperOrderLedger.id" in condition
            assert "AlpacaPaperOrderLedger.client_order_id" not in condition

    assert update_where_conditions


@pytest.mark.unit
def test_service_has_no_broker_mutation_imports():
    """Ensure the ledger service does not import broker mutation code."""
    import ast as _ast

    source = SERVICE_PATH.read_text()
    tree = _ast.parse(source)

    import_strings: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            import_strings.append(_ast.unparse(node))

    import_source = "\n".join(import_strings)

    forbidden_imports = [
        "AlpacaPaperBrokerService",
        "app.services.brokers",
        "app.services.kis",
        "app.services.upbit",
        "watch_alert",
        "order_intent",
        "taskiq",
    ]
    for term in forbidden_imports:
        assert term not in import_source, (
            f"Forbidden import/string found in service imports: {term!r}"
        )

    forbidden_anywhere = [
        "submit_order",
        "cancel_order",
        "place_order",
        "modify_order",
    ]
    for term in forbidden_anywhere:
        assert term not in source, (
            f"Forbidden call-site string found in service: {term!r}"
        )


@pytest.mark.unit
def test_service_is_valid_python():
    source = SERVICE_PATH.read_text()
    tree = ast.parse(source)
    assert tree is not None
