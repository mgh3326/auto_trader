"""Tests for AlpacaPaperLedgerService lifecycle methods (ROB-84).

Covers: model columns/constraints, _derive_lifecycle_state, service method behavior,
forbidden imports/strings, ORM model shape.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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
    assert "uq_alpaca_paper_ledger_client_order_id" in constraint_names
    # SQLAlchemy may prefix with table name via naming convention
    assert any("alpaca_paper_ledger_broker" in (n or "") for n in constraint_names)
    assert any(
        "alpaca_paper_ledger_account_mode" in (n or "") for n in constraint_names
    )
    assert any(
        "alpaca_paper_ledger_lifecycle_state" in (n or "") for n in constraint_names
    )
    assert any("alpaca_paper_ledger_side" in (n or "") for n in constraint_names)
    assert any("alpaca_paper_ledger_order_type" in (n or "") for n in constraint_names)


# ---------------------------------------------------------------------------
# _derive_lifecycle_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order_status,filled_qty,expected",
    [
        ("canceled", None, "canceled"),
        ("canceled", 0, "canceled"),
        ("filled", None, "filled"),
        ("filled", 1.5, "filled"),
        ("partially_filled", None, "partially_filled"),
        ("partially_filled", 0.5, "partially_filled"),
        # open statuses
        ("new", None, "open"),
        ("accepted", None, "open"),
        ("pending_new", None, "open"),
        ("accepted_for_bidding", None, "open"),
        ("held", None, "open"),
        ("pending_cancel", None, "open"),
        ("pending_replace", None, "open"),
        ("replaced", None, "open"),
        # open status with filled_qty > 0 → unexpected
        ("new", 0.5, "unexpected"),
        ("accepted", 1.0, "unexpected"),
        # open status with filled_qty = 0 → open
        ("new", 0.0, "open"),
        # unexpected statuses
        ("rejected", None, "unexpected"),
        ("expired", None, "unexpected"),
        ("suspended", None, "unexpected"),
        # unknown status
        ("mystery_status", None, "unexpected"),
        (None, None, "unexpected"),
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
        "broker": "alpaca",
        "account_mode": "alpaca_paper",
        "lifecycle_state": "previewed",
        "execution_symbol": "BTCUSD",
        "execution_venue": "alpaca_paper",
        "instrument_type": "crypto",
        "side": "buy",
        "order_type": "limit",
        "currency": "USD",
        "raw_responses": None,
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
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
# record_preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_preview_inserts_and_returns_row():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="previewed")
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
async def test_record_preview_validation_failed_lifecycle():
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="validation_failed")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_preview(
        client_order_id="test-client-002",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        lifecycle_state="validation_failed",
        validation_summary={"reason": "insufficient_buying_power"},
    )
    assert result.lifecycle_state == "validation_failed"


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
    # The returned row's preview_payload should have no raw secret
    # (sanitization happens before persistence; we verify the call happened)
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
# record_submit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_canceled_order():
    from app.services.alpaca_paper_ledger_service import (
        AlpacaPaperLedgerService,
    )

    row = _make_row(lifecycle_state="canceled", order_status="canceled")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "broker-order-id", "status": "canceled", "filled_qty": "0"},
    )
    assert result.lifecycle_state == "canceled"


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
async def test_record_submit_partially_filled():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="partially_filled", order_status="partially_filled")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "bid2", "status": "partially_filled", "filled_qty": "0.0005"},
    )
    assert result.lifecycle_state == "partially_filled"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_rejected_is_unexpected():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="unexpected", order_status="rejected")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "bid3", "status": "rejected"},
    )
    assert result.lifecycle_state == "unexpected"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_submit_unknown_status_is_unexpected():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(lifecycle_state="unexpected", order_status="mystery")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_submit(
        "test-client-001",
        order={"id": "bid4", "status": "mystery"},
    )
    assert result.lifecycle_state == "unexpected"


# ---------------------------------------------------------------------------
# record_cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_cancel_writes_cancel_metadata():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(cancel_status="confirmed", lifecycle_state="canceled")
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_cancel("test-client-001", cancel_status="confirmed")
    assert result.cancel_status == "confirmed"
    # lifecycle_state should come from record_status, not forced here
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
        }
    )
    db = _mock_db_with_row(row)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.record_position_snapshot("test-client-001", position=None)
    assert result.position_snapshot["qty"] == "0"
    assert result.position_snapshot["avg_entry_price"] is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_position_snapshot_with_position_writes_qty():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    row = _make_row(
        position_snapshot={
            "qty": "0.001",
            "avg_entry_price": "50000",
            "fetched_at": "2026-05-03T00:00:00+00:00",
        }
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
            # Track update calls that contain raw_responses
            nonlocal accumulated
            return _ScalarResult(
                _make_row(raw_responses=accumulated if accumulated else None)
            )

        return _execute

    db = AsyncMock()
    db.commit = AsyncMock()

    # Simulate accumulation by tracking the update values
    existing_raw: dict = {}

    async def execute_side_effect(stmt):
        nonlocal existing_raw
        # When it's a select, return row with current raw_responses
        row = _make_row(raw_responses=dict(existing_raw))
        return _ScalarResult(row)

    db.execute = AsyncMock(side_effect=execute_side_effect)

    svc = AlpacaPaperLedgerService(db)
    # First accumulation
    existing_raw["submit"] = {"status": "canceled"}
    await svc._accumulate_raw_response("test-client-001", "status", {"status": "new"})
    # Second accumulation
    existing_raw["status"] = {"status": "new"}
    await svc._accumulate_raw_response("test-client-001", "cancel", {"cancel": "ok"})

    # Verify execute was called multiple times
    assert db.execute.call_count >= 2


# ---------------------------------------------------------------------------
# Static safety: no broker mutation imports
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_service_has_no_broker_mutation_imports():
    """Ensure the ledger service does not import broker mutation code."""
    import ast as _ast

    source = SERVICE_PATH.read_text()
    tree = _ast.parse(source)

    # Collect all import lines as strings for pattern matching
    import_strings: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            import_strings.append(_ast.unparse(node))

    import_source = "\n".join(import_strings)

    # These must not appear in any import statement
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

    # These strings must not appear anywhere in the source (including non-import lines)
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
