"""Schemas for the broker execution ledger (ROB-211)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Broker = Literal["kis", "upbit"]
AccountMode = Literal["live", "mock"]
Side = Literal["buy", "sell"]
Currency = Literal["KRW", "USD"]
ExecutionSource = Literal["reconciler", "websocket", "manual_import"]
InstrumentTypeValue = Literal["equity_kr", "equity_us", "crypto"]
DataState = Literal["fresh", "stale", "missing"]


class ExecutionLedgerCommitDisabledError(RuntimeError):
    """Raised when commit mode is requested while the activation flag is off."""


class ExecutionLedgerUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: Broker
    account_mode: AccountMode = "live"
    venue: str = Field(min_length=1)
    instrument_type: InstrumentTypeValue
    symbol: str = Field(min_length=1)
    raw_symbol: str = Field(min_length=1)
    side: Side
    broker_order_id: str = Field(min_length=1)
    fill_seq: int = Field(default=0, ge=0)
    filled_qty: Decimal = Field(gt=0)
    filled_price: Decimal = Field(gt=0)
    filled_notional: Decimal | None = Field(default=None, ge=0)
    fee_amount: Decimal | None = Field(default=None, ge=0)
    fee_currency: str | None = None
    filled_at: datetime
    currency: Currency
    correlation_id: str | None = None
    source: ExecutionSource = "reconciler"
    source_run_id: uuid.UUID | None = None
    raw_payload_json: dict | None = None

    @field_validator("venue", "symbol", "raw_symbol", "broker_order_id", mode="before")
    @classmethod
    def _strip_non_empty(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @model_validator(mode="after")
    def _compute_notional(self) -> ExecutionLedgerUpsert:
        if self.filled_notional is None:
            self.filled_notional = self.filled_qty * self.filled_price
        return self


class ExecutionLedgerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    broker: str
    account_mode: str
    venue: str
    instrument_type: str
    symbol: str
    raw_symbol: str
    side: str
    broker_order_id: str
    fill_seq: int
    filled_qty: Decimal
    filled_price: Decimal
    filled_notional: Decimal
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    filled_at: datetime
    currency: str
    correlation_id: str | None = None
    source: str
    source_run_id: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    symbol_name: str | None = None
    cost_basis_notional: Decimal | None = None
    realized_profit: Decimal | None = None
    realized_profit_rate: Decimal | None = None


class SourceBreakdown(BaseModel):
    reconciler: int = 0
    websocket: int = 0
    manual_import: int = 0


class ExecutionLedgerListResponse(BaseModel):
    count: int
    items: list[ExecutionLedgerRead]
    data_state: DataState | None = None
    source_breakdown: SourceBreakdown | None = None
    empty_reason: str | None = None


class ExecutionLedgerFreshnessEntry(BaseModel):
    broker: str
    last_run_at: datetime | None = None
    lag_minutes: float | None = None
    dataState: DataState
    last_run_id: uuid.UUID | None = None
    notes: str | None = None


class ExecutionLedgerFreshnessReport(BaseModel):
    items: list[ExecutionLedgerFreshnessEntry]


class ReconcileDiff(BaseModel):
    would_insert: int = 0
    would_update: int = 0
    unchanged: int = 0
    committed_insert: int = 0
    committed_update: int = 0
    sample_inserts: list[ExecutionLedgerRead] = Field(default_factory=list)
    sample_updates: list[ExecutionLedgerRead] = Field(default_factory=list)
    source_run_id: uuid.UUID | None = None

    def add_insert_sample(self, item: ExecutionLedgerRead) -> None:
        if len(self.sample_inserts) < 10:
            self.sample_inserts.append(item)

    def add_update_sample(self, item: ExecutionLedgerRead) -> None:
        if len(self.sample_updates) < 10:
            self.sample_updates.append(item)


class ReconcileRunRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    broker: Broker
    window_start: datetime
    window_end: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    dry_run: bool
    would_insert: int = 0
    would_update: int = 0
    unchanged: int = 0
    committed_insert: int = 0
    committed_update: int = 0
    error_summary: str | None = None
    notes: str | None = None
