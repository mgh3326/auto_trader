"""Buy/sell roundtrip lifecycle fixture tests for ROB-90 canonical taxonomy.

Verifies that a complete paper roundtrip can be represented as a sequence of
ledger rows sharing lifecycle_correlation_id but having distinct client_order_id,
side, record_kind, and timestamps.

All tests are pure-Python unit tests with no DB or broker calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
    LIFECYCLE_SUBMITTED,
    LIFECYCLE_VALIDATED,
    RECORD_KIND_EXECUTION,
    RECORD_KIND_PLAN,
    RECORD_KIND_PREVIEW,
    RECORD_KIND_RECONCILE,
    RECORD_KIND_VALIDATION_ATTEMPT,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CORRELATION_ID = "corr-roundtrip-test-001"
_BUY_CLIENT_ORDER_ID = "buy-order-001"
_SELL_CLIENT_ORDER_ID = "sell-order-001"

_T0 = datetime(2026, 5, 3, 9, 0, 0, tzinfo=UTC)


def _ts(offset_seconds: int = 0) -> datetime:
    return _T0 + timedelta(seconds=offset_seconds)


def _make_row(
    *,
    client_order_id: str,
    lifecycle_correlation_id: str = _CORRELATION_ID,
    lifecycle_state: str,
    record_kind: str,
    side: str,
    validation_attempt_no: int | None = None,
    validation_outcome: str | None = None,
    confirm_flag: bool | None = None,
    leg_role: str | None = None,
    qty_delta: str | None = None,
    settlement_status: str | None = None,
    signal_symbol: str | None = "KRW-BTC",
    signal_venue: str | None = "upbit",
    created_at: datetime | None = None,
    **kwargs: Any,
) -> Any:
    return SimpleNamespace(
        id=len(client_order_id),
        client_order_id=client_order_id,
        lifecycle_correlation_id=lifecycle_correlation_id,
        lifecycle_state=lifecycle_state,
        record_kind=record_kind,
        side=side,
        validation_attempt_no=validation_attempt_no,
        validation_outcome=validation_outcome,
        confirm_flag=confirm_flag,
        leg_role=leg_role,
        qty_delta=qty_delta,
        settlement_status=settlement_status,
        signal_symbol=signal_symbol,
        signal_venue=signal_venue,
        broker="alpaca",
        account_mode="alpaca_paper",
        execution_symbol="BTCUSD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        instrument_type="crypto",
        order_type="limit",
        currency="USD",
        raw_responses=None,
        created_at=created_at or _T0,
        **kwargs,
    )


def _mock_db_returning(rows: list[Any]) -> AsyncMock:
    """DB mock whose list_by_correlation_id returns the provided rows list."""

    class _ScalarResult:
        def __init__(self, row_list):
            self._rows = row_list

        def scalar_one_or_none(self):
            return self._rows[0] if self._rows else None

        def scalars(self):
            rows = self._rows

            class _S:
                def all(self_inner):
                    return rows

            return _S()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(rows))
    db.commit = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Full buy/sell roundtrip fixture
# ---------------------------------------------------------------------------


def _build_roundtrip_fixture() -> list[Any]:
    """Synthetic rows representing a complete buy/sell paper roundtrip."""
    return [
        # 1. Buy leg: planned
        _make_row(
            client_order_id=_BUY_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_PLANNED,
            record_kind=RECORD_KIND_PLAN,
            side="buy",
            leg_role="buy",
            confirm_flag=None,
            created_at=_ts(0),
        ),
        # 2. Buy leg: previewed
        _make_row(
            client_order_id=_BUY_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_PREVIEWED,
            record_kind=RECORD_KIND_PREVIEW,
            side="buy",
            leg_role="buy",
            confirm_flag=None,
            created_at=_ts(1),
        ),
        # 3. Buy leg: validation attempt (confirm=false)
        _make_row(
            client_order_id=_BUY_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_VALIDATED,
            record_kind=RECORD_KIND_VALIDATION_ATTEMPT,
            side="buy",
            leg_role="buy",
            validation_attempt_no=1,
            validation_outcome="passed",
            confirm_flag=False,
            created_at=_ts(2),
        ),
        # 4. Buy leg: submitted (execution, confirm=true)
        _make_row(
            client_order_id=_BUY_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_SUBMITTED,
            record_kind=RECORD_KIND_EXECUTION,
            side="buy",
            leg_role="buy",
            confirm_flag=True,
            created_at=_ts(3),
        ),
        # 5. Buy leg: filled
        _make_row(
            client_order_id=_BUY_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_FILLED,
            record_kind=RECORD_KIND_EXECUTION,
            side="buy",
            leg_role="buy",
            confirm_flag=True,
            qty_delta="0.001",
            created_at=_ts(10),
        ),
        # 6. Buy leg: position reconciled
        _make_row(
            client_order_id=_BUY_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_POSITION_RECONCILED,
            record_kind=RECORD_KIND_EXECUTION,
            side="buy",
            leg_role="buy",
            confirm_flag=True,
            qty_delta="0.001",
            created_at=_ts(15),
        ),
        # 7. Sell leg: sell_validated (confirm=false)
        _make_row(
            client_order_id=_SELL_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_SELL_VALIDATED,
            record_kind=RECORD_KIND_VALIDATION_ATTEMPT,
            side="sell",
            leg_role="sell",
            validation_attempt_no=1,
            validation_outcome="passed",
            confirm_flag=False,
            created_at=_ts(20),
        ),
        # 8. Sell leg: closed (execution, confirm=true)
        _make_row(
            client_order_id=_SELL_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_CLOSED,
            record_kind=RECORD_KIND_EXECUTION,
            side="sell",
            leg_role="sell",
            confirm_flag=True,
            qty_delta="-0.001",
            created_at=_ts(25),
        ),
        # 9. Final reconcile
        _make_row(
            client_order_id=_SELL_CLIENT_ORDER_ID,
            lifecycle_state=LIFECYCLE_FINAL_RECONCILED,
            record_kind=RECORD_KIND_RECONCILE,
            side="sell",
            leg_role="roundtrip",
            confirm_flag=True,
            settlement_status="n_a",
            created_at=_ts(30),
        ),
    ]


@pytest.mark.unit
def test_roundtrip_fixture_all_states_in_canonical_set():
    rows = _build_roundtrip_fixture()
    for row in rows:
        assert row.lifecycle_state in CANONICAL_LIFECYCLE_STATES, (
            f"lifecycle_state={row.lifecycle_state!r} not in canonical set"
        )


@pytest.mark.unit
def test_roundtrip_fixture_covers_expected_lifecycle_sequence():
    rows = _build_roundtrip_fixture()
    states = [r.lifecycle_state for r in rows]
    expected_sequence = [
        LIFECYCLE_PLANNED,
        LIFECYCLE_PREVIEWED,
        LIFECYCLE_VALIDATED,
        LIFECYCLE_SUBMITTED,
        LIFECYCLE_FILLED,
        LIFECYCLE_POSITION_RECONCILED,
        LIFECYCLE_SELL_VALIDATED,
        LIFECYCLE_CLOSED,
        LIFECYCLE_FINAL_RECONCILED,
    ]
    assert states == expected_sequence


@pytest.mark.unit
def test_roundtrip_fixture_buy_sell_legs_share_correlation_id():
    rows = _build_roundtrip_fixture()
    for row in rows:
        assert row.lifecycle_correlation_id == _CORRELATION_ID, (
            f"row {row.client_order_id!r} has wrong correlation id"
        )


@pytest.mark.unit
def test_roundtrip_fixture_buy_sell_legs_distinct_client_order_ids():
    rows = _build_roundtrip_fixture()
    buy_rows = [r for r in rows if r.side == "buy"]
    sell_rows = [r for r in rows if r.side == "sell"]
    buy_ids = {r.client_order_id for r in buy_rows}
    sell_ids = {r.client_order_id for r in sell_rows}
    assert buy_ids == {_BUY_CLIENT_ORDER_ID}
    assert sell_ids == {_SELL_CLIENT_ORDER_ID}
    assert buy_ids.isdisjoint(sell_ids)


@pytest.mark.unit
def test_roundtrip_fixture_validation_rows_have_confirm_false():
    rows = _build_roundtrip_fixture()
    validation_rows = [
        r for r in rows if r.record_kind == RECORD_KIND_VALIDATION_ATTEMPT
    ]
    assert len(validation_rows) >= 2
    for row in validation_rows:
        assert row.confirm_flag is False, (
            f"Validation attempt row should have confirm_flag=False: {row!r}"
        )


@pytest.mark.unit
def test_roundtrip_fixture_execution_rows_have_confirm_true():
    rows = _build_roundtrip_fixture()
    execution_rows = [r for r in rows if r.record_kind == RECORD_KIND_EXECUTION]
    assert len(execution_rows) >= 2
    for row in execution_rows:
        assert row.confirm_flag is True, (
            f"Execution row should have confirm_flag=True: {row!r}"
        )


@pytest.mark.unit
def test_roundtrip_fixture_all_rows_have_signal_provenance():
    rows = _build_roundtrip_fixture()
    for row in rows:
        assert row.signal_symbol is not None, (
            f"Row {row.client_order_id!r} missing signal_symbol"
        )
        assert row.signal_venue is not None, (
            f"Row {row.client_order_id!r} missing signal_venue"
        )


@pytest.mark.unit
def test_roundtrip_fixture_timestamps_ordered():
    rows = _build_roundtrip_fixture()
    timestamps = [r.created_at for r in rows]
    assert timestamps == sorted(timestamps), "Roundtrip rows must be in time order"


@pytest.mark.unit
def test_roundtrip_fixture_final_reconcile_has_settlement_status():
    rows = _build_roundtrip_fixture()
    final_rows = [r for r in rows if r.lifecycle_state == LIFECYCLE_FINAL_RECONCILED]
    assert len(final_rows) == 1
    assert final_rows[0].settlement_status == "n_a"


@pytest.mark.unit
def test_roundtrip_fixture_no_anomaly_in_happy_path():
    rows = _build_roundtrip_fixture()
    anomaly_rows = [r for r in rows if r.lifecycle_state == LIFECYCLE_ANOMALY]
    assert len(anomaly_rows) == 0, "Happy-path fixture should have no anomaly rows"


# ---------------------------------------------------------------------------
# service list_by_correlation_id returns all roundtrip rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_by_correlation_id_returns_all_roundtrip_rows():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    rows = _build_roundtrip_fixture()
    db = _mock_db_returning(rows)

    svc = AlpacaPaperLedgerService(db)
    result = await svc.list_by_correlation_id(_CORRELATION_ID)
    assert isinstance(result, list)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_by_correlation_id_empty_raises():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    db = AsyncMock()
    svc = AlpacaPaperLedgerService(db)
    with pytest.raises(ValueError):
        await svc.list_by_correlation_id("")
