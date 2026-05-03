"""Pydantic read schemas for the Alpaca Paper order ledger (ROB-84/ROB-90)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class AlpacaPaperOrderLedgerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_order_id: str
    broker: str
    account_mode: str
    lifecycle_state: str

    # ROB-90 taxonomy fields
    lifecycle_correlation_id: str
    record_kind: str
    leg_role: str | None = None
    validation_attempt_no: int | None = None
    validation_outcome: str | None = None
    confirm_flag: bool | None = None
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    settlement_status: str | None = None
    settlement_at: datetime | None = None
    qty_delta: Decimal | None = None

    signal_symbol: str | None = None
    signal_venue: str | None = None
    execution_symbol: str
    execution_venue: str
    execution_asset_class: str | None = None
    instrument_type: str

    side: str
    order_type: str
    time_in_force: str | None = None
    requested_qty: Decimal | None = None
    requested_notional: Decimal | None = None
    requested_price: Decimal | None = None
    currency: str

    broker_order_id: str | None = None
    submitted_at: datetime | None = None
    order_status: str | None = None
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None

    cancel_status: str | None = None
    canceled_at: datetime | None = None

    position_snapshot: dict[str, Any] | None = None

    reconcile_status: str | None = None
    reconciled_at: datetime | None = None

    briefing_artifact_run_uuid: uuid.UUID | None = None
    briefing_artifact_status: str | None = None
    qa_evaluator_status: str | None = None
    approval_bridge_generated_at: datetime | None = None
    approval_bridge_status: str | None = None
    candidate_uuid: uuid.UUID | None = None

    workflow_stage: str | None = None
    purpose: str | None = None

    notes: str | None = None
    error_summary: str | None = None

    created_at: datetime
    updated_at: datetime


class AlpacaPaperOrderLedgerListResponse(BaseModel):
    count: int
    items: list[AlpacaPaperOrderLedgerRead]


class AlpacaPaperOrderLedgerCorrelationResponse(BaseModel):
    """Grouped response for all records sharing a lifecycle_correlation_id."""

    lifecycle_correlation_id: str
    count: int
    items: list[AlpacaPaperOrderLedgerRead]


__all__ = [
    "AlpacaPaperOrderLedgerCorrelationResponse",
    "AlpacaPaperOrderLedgerListResponse",
    "AlpacaPaperOrderLedgerRead",
]
