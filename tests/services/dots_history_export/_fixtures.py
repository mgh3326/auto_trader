"""Fixture ledger rows shaped exactly like ``reader.py``'s selected columns."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.services.dots_history_export.projection import (
    RefHasher,
    SourceRows,
    Window,
    project,
)

SALT = b"fixture-salt-that-is-long-enough-for-the-minimum-0123"
WINDOW = Window(
    since=datetime(2026, 6, 1, tzinfo=UTC), until=datetime(2026, 7, 1, tzinfo=UTC)
)
ACCOUNT_NUMBER = "87654321-01"
KIS_ORDER_NO = "0012345678"
KIS_ORDER_NO_IN_FILL = "12345678"
UPBIT_ORDER_ID = "0f3c7e8a-1b2d-4c5e-8f90-a1b2c3d4e5f6"
TOSS_ORDER_ID = "TOSSORDER7788"
TOSS_ORIGINAL_ORDER_ID = "TOSSORDER7700"
TOSS_CLIENT_ORDER_ID = "tossp6-0123456789abcdef"
ALPACA_ORDER_ID = "7c1d2e3f-4a5b-4c6d-9e8f-0a1b2c3d4e5f"
ALPACA_CLIENT_ORDER_ID = "alpaca-client-991"
CORRELATION_ID = "live-correlation-abc123"
PROPOSAL_ID = "11111111-2222-4333-8444-555555555555"
REPORT_ITEM_ID = "99999999-8888-4777-8666-555555555555"
IDEMPOTENCY_KEY = "idem-key-55123"

CREATED = datetime(2026, 6, 10, 1, 0, tzinfo=UTC)  # 10:00 KST
FILLED = datetime(2026, 6, 10, 1, 30, tzinfo=UTC)
RECONCILED = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

RAW_IDENTIFIERS = (
    ACCOUNT_NUMBER,
    KIS_ORDER_NO,
    KIS_ORDER_NO_IN_FILL,
    UPBIT_ORDER_ID,
    TOSS_ORDER_ID,
    TOSS_ORIGINAL_ORDER_ID,
    TOSS_CLIENT_ORDER_ID,
    ALPACA_ORDER_ID,
    ALPACA_CLIENT_ORDER_ID,
    CORRELATION_ID,
    PROPOSAL_ID,
    REPORT_ITEM_ID,
    IDEMPOTENCY_KEY,
)


def kis_live_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "trade_date": CREATED,
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("10.00000000"),
        "price": Decimal("70000.0000"),
        "amount": Decimal("700000.0000"),
        "currency": "KRW",
        "order_no": KIS_ORDER_NO,
        "status": "filled",
        "lifecycle_state": "filled",
        "strategy": "h3-momentum",
        "report_item_uuid": REPORT_ITEM_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "correlation_id": CORRELATION_ID,
        "filled_qty": Decimal("10.00000000"),
        "avg_fill_price": Decimal("69900.0000"),
        "reconciled_at": RECONCILED,
        "created_at": CREATED,
    }
    return {**row, **overrides}


def live_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 2,
        "trade_date": CREATED,
        "broker": "upbit",
        "account_scope": "upbit_live",
        "market": "crypto",
        "symbol": "KRW-BTC",
        "exchange": None,
        "side": "buy",
        "order_kind": "limit",
        "quantity": Decimal("0.10000000"),
        "price": Decimal("150000000.00000000"),
        "amount": None,
        "currency": "KRW",
        "order_no": UPBIT_ORDER_ID,
        "status": "cancelled",
        "lifecycle_state": "cancelled",
        "strategy": None,
        "report_item_uuid": None,
        "idempotency_key": None,
        "correlation_id": None,
        "filled_qty": Decimal("0.05000000"),
        "avg_fill_price": Decimal("149000000.00000000"),
        "reconciled_at": RECONCILED,
        "created_at": CREATED,
    }
    return {**row, **overrides}


def toss_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 3,
        "trade_date": CREATED,
        "operation_kind": "place",
        "market": "us",
        "symbol": "BRK/B",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "DAY",
        "quantity": Decimal("3"),
        "price": Decimal("400.5"),
        "order_amount": None,
        "currency": "USD",
        "client_order_id": TOSS_CLIENT_ORDER_ID,
        "broker_order_id": TOSS_ORDER_ID,
        "original_order_id": TOSS_ORIGINAL_ORDER_ID,
        "replaced_by_order_id": None,
        "status": "expired",
        "strategy": "us-open",
        "report_item_uuid": None,
        "correlation_id": None,
        "filled_qty": None,
        "avg_fill_price": None,
        "reconciled_at": RECONCILED,
        "expired_at": RECONCILED,
        "created_at": CREATED,
    }
    return {**row, **overrides}


def kis_mock_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 4,
        "trade_date": CREATED,
        "symbol": "000660",
        "instrument_type": "equity_kr",
        "side": "sell",
        "order_type": "limit",
        "quantity": Decimal("2"),
        "price": Decimal("100000"),
        "amount": Decimal("200000"),
        "currency": "KRW",
        "order_no": "0000555666",
        "status": "accepted",
        "lifecycle_state": "expired",
        "strategy": "mock_counterfactual_mirror",
        "report_item_uuid": None,
        "correlation_id": None,
        "attributed_fill_qty": None,
        "reconciled_at": RECONCILED,
        "created_at": CREATED,
    }
    return {**row, **overrides}


def alpaca_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 5,
        "client_order_id": ALPACA_CLIENT_ORDER_ID,
        "lifecycle_correlation_id": ALPACA_CLIENT_ORDER_ID,
        "record_kind": "execution",
        "account_mode": "alpaca_paper",
        "lifecycle_state": "submitted",
        "strategy_key": "h3-us",
        "execution_symbol": "AAPL",
        "execution_venue": "alpaca_paper",
        "instrument_type": "equity_us",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "day",
        "requested_qty": Decimal("1"),
        "requested_notional": None,
        "requested_price": Decimal("200.25"),
        "currency": "USD",
        "broker_order_id": ALPACA_ORDER_ID,
        "submitted_at": CREATED,
        "order_status": "new",
        "filled_qty": None,
        "filled_avg_price": None,
        "canceled_at": None,
        "created_at": CREATED,
    }
    return {**row, **overrides}


def paper_pending_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 6,
        "account_id": 11,
        "strategy_name": "h3-crypto",
        "symbol": "KRW-ETH",
        "side": "buy",
        "order_type": "limit",
        "limit_price": Decimal("4000000"),
        "quantity": Decimal("0.5"),
        "status": "filled",
        "correlation_id": None,
        "fill_price": Decimal("3999000"),
        "placed_at": CREATED,
        "filled_at": FILLED,
        "cancelled_at": None,
    }
    return {**row, **overrides}


def paper_trade_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 7,
        "account_id": 11,
        "strategy_name": "h3-crypto",
        "pending_order_id": 6,
        "symbol": "KRW-ETH",
        "instrument_type": "crypto",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("0.5"),
        "price": Decimal("3999000"),
        "currency": "KRW",
        "correlation_id": None,
        "executed_at": FILLED,
    }
    return {**row, **overrides}


def fill_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 8,
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "side": "buy",
        "broker_order_id": KIS_ORDER_NO_IN_FILL,
        "filled_qty": Decimal("10.00000000"),
        "filled_price": Decimal("69900.00000000"),
        "filled_at": FILLED,
        "currency": "KRW",
        "correlation_id": CORRELATION_ID,
        "source": "reconciler",
        "created_at": RECONCILED,
    }
    return {**row, **overrides}


def proposal_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 9,
        "proposal_id": PROPOSAL_ID,
        "root_proposal_id": PROPOSAL_ID,
        "revision": 1,
        "symbol": "005930",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "side": "buy",
        "order_type": "limit",
        "strategy": "h3-momentum",
        "broker_account_id": ACCOUNT_NUMBER,
        "action": "place",
        "target_broker_order_id": None,
        "exit_intent": None,
        "void_reason": None,
        "lifecycle_state": "fully_filled",
        "correlation_id": CORRELATION_ID,
        "valid_until": RECONCILED,
        "validated_at": CREATED,
        "approved_at": CREATED,
        "created_at": CREATED,
    }
    return {**row, **overrides}


def rung_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 10,
        "proposal_id": PROPOSAL_ID,
        "account_mode": "kis_live",
        "market": "equity_kr",
        "symbol": "005930",
        "rung_index": 0,
        "side": "buy",
        "quantity": Decimal("10.000000000000"),
        "limit_price": Decimal("70000.000000000000"),
        "notional": None,
        "state": "filled",
        "idempotency_key": IDEMPOTENCY_KEY,
        "broker_order_id": KIS_ORDER_NO,
        "correlation_id": CORRELATION_ID,
        "validated_at": CREATED,
        "filled_qty": Decimal("10.000000000000"),
        "void_reason": None,
        "void_reason_group": None,
        "created_at": CREATED,
        "updated_at": FILLED,
    }
    return {**row, **overrides}


def sources(**tables: list[dict[str, Any]]) -> SourceRows:
    defaults: dict[str, list[dict[str, Any]]] = {
        "kis_live_order_ledger": [kis_live_row()],
        "live_order_ledger": [live_row()],
        "toss_live_order_ledger": [toss_row()],
        "kis_mock_order_ledger": [kis_mock_row()],
        "alpaca_paper_order_ledger": [alpaca_row()],
        "paper_pending_orders": [paper_pending_row()],
        "paper_trades": [paper_trade_row()],
        "execution_ledger": [fill_row()],
        "order_proposals": [proposal_row()],
        "order_proposal_rungs": [rung_row()],
    }
    return SourceRows(**{**defaults, **tables})


def projected(salt: bytes = SALT, **tables: list[dict[str, Any]]):
    return project(sources(**tables), hasher=RefHasher(salt), window=WINDOW)
