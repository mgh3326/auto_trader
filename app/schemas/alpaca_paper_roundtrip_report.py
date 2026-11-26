"""Read-only Alpaca Paper roundtrip audit report schemas (ROB-92)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer

from app.schemas.alpaca_paper_ledger import AlpacaPaperOrderLedgerRead


class _RoundtripBaseModel(BaseModel):
    @field_serializer("*", when_used="json")
    def _serialize_decimal(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        return value


class RoundtripLookupKey(_RoundtripBaseModel):
    kind: Literal[
        "lifecycle_correlation_id",
        "client_order_id",
        "candidate_uuid",
        "briefing_artifact_run_uuid",
    ]
    value: str


class RoundtripCompleteness(_RoundtripBaseModel):
    required_steps: list[str]
    observed_steps: list[str]
    missing_steps: list[str]
    is_complete: bool


class RoundtripCandidateBlock(_RoundtripBaseModel):
    candidate_uuid: str | None = None
    signal_symbol: str | None = None
    signal_venue: str | None = None
    execution_symbol: str | None = None
    execution_venue: str | None = None
    execution_asset_class: str | None = None
    instrument_type: str | None = None
    workflow_stage: str | None = None
    purpose: str | None = None


class RoundtripQaBlock(_RoundtripBaseModel):
    briefing_artifact_run_uuid: str | None = None
    briefing_artifact_status: str | None = None
    qa_evaluator_status: str | None = None


class RoundtripApprovalPacketBlock(_RoundtripBaseModel):
    approval_bridge_generated_at: datetime | None = None
    approval_bridge_status: str | None = None
    preview_payload: dict[str, Any] | None = None
    validation_summary: dict[str, Any] | None = None


class RoundtripOrderBlock(_RoundtripBaseModel):
    client_order_id: str | None = None
    broker_order_id: str | None = None
    order_status: str | None = None
    order_type: str | None = None
    time_in_force: str | None = None
    requested_qty: Decimal | None = None
    requested_notional: Decimal | None = None
    requested_price: Decimal | None = None
    currency: str | None = None
    submitted_at: datetime | None = None


class RoundtripFillBlock(_RoundtripBaseModel):
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    qty_delta: Decimal | None = None


class RoundtripReconcileBlock(_RoundtripBaseModel):
    reconcile_status: str | None = None
    reconciled_at: datetime | None = None
    settlement_status: str | None = None
    settlement_at: datetime | None = None
    position_snapshot: dict[str, Any] | None = None
    notes: str | None = None
    error_summary: str | None = None


class RoundtripLegBlock(_RoundtripBaseModel):
    side: Literal["buy", "sell"]
    lifecycle_states: list[str] = Field(default_factory=list)
    record_kinds: list[str] = Field(default_factory=list)
    order: RoundtripOrderBlock = Field(default_factory=RoundtripOrderBlock)
    fill: RoundtripFillBlock = Field(default_factory=RoundtripFillBlock)
    reconcile: RoundtripReconcileBlock = Field(default_factory=RoundtripReconcileBlock)
    latest_row_created_at: datetime | None = None


class RoundtripFinalPositionBlock(_RoundtripBaseModel):
    source: Literal["caller_supplied", "ledger_snapshot", "missing"] = "missing"
    symbol: str | None = None
    qty: Decimal | None = None
    snapshot: dict[str, Any] | None = None


class RoundtripOpenOrdersBlock(_RoundtripBaseModel):
    source: Literal["caller_supplied", "missing"] = "missing"
    count: int = 0
    orders: list[dict[str, Any]] = Field(default_factory=list)


class RoundtripAnomalyBlock(_RoundtripBaseModel):
    status: str
    should_block: bool
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    preflight: dict[str, Any] = Field(default_factory=dict)


class RoundtripSafetyBlock(_RoundtripBaseModel):
    read_only: bool = True
    broker_mutation_performed: bool = False
    db_write_performed: bool = False
    broker_snapshot_fetched: bool = False
    statement: str = (
        "Read-only audit report assembled from ledger rows and caller-supplied "
        "snapshots only; no broker mutation, DB repair/backfill, or schema change."
    )


class AlpacaPaperRoundtripReport(_RoundtripBaseModel):
    lookup_key: RoundtripLookupKey
    lifecycle_correlation_id: str | None = None
    generated_at: datetime
    status: Literal["complete", "incomplete", "anomaly", "not_found"]
    completeness: RoundtripCompleteness
    candidate: RoundtripCandidateBlock
    qa_result: RoundtripQaBlock
    approval_packet: RoundtripApprovalPacketBlock
    buy_leg: RoundtripLegBlock | None = None
    sell_leg: RoundtripLegBlock | None = None
    final_position: RoundtripFinalPositionBlock
    open_orders: RoundtripOpenOrdersBlock
    anomalies: RoundtripAnomalyBlock
    safety: RoundtripSafetyBlock = Field(default_factory=RoundtripSafetyBlock)
    ledger_rows: list[AlpacaPaperOrderLedgerRead] | None = None


class AlpacaPaperRoundtripReportListResponse(_RoundtripBaseModel):
    lookup_key: RoundtripLookupKey
    count: int
    items: list[AlpacaPaperRoundtripReport]


__all__ = [
    "AlpacaPaperRoundtripReport",
    "AlpacaPaperRoundtripReportListResponse",
    "RoundtripAnomalyBlock",
    "RoundtripApprovalPacketBlock",
    "RoundtripCandidateBlock",
    "RoundtripCompleteness",
    "RoundtripFillBlock",
    "RoundtripFinalPositionBlock",
    "RoundtripLegBlock",
    "RoundtripLookupKey",
    "RoundtripOpenOrdersBlock",
    "RoundtripOrderBlock",
    "RoundtripQaBlock",
    "RoundtripReconcileBlock",
    "RoundtripSafetyBlock",
]
