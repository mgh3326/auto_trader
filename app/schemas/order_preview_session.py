"""ROB-118 — Order preview session schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Market = Literal["equity_kr", "equity_us", "crypto"]
Side = Literal["buy", "sell"]
Status = Literal[
    "created",
    "preview_passed",
    "preview_failed",
    "submitted",
    "submit_failed",
    "canceled",
]


class PreviewLegInput(BaseModel):
    leg_index: int = Field(ge=0)
    quantity: Decimal = Field(gt=Decimal(0))
    price: Decimal | None = None
    order_type: Literal["limit", "market"] = "limit"

    @field_validator("price")
    @classmethod
    def _price_for_limit(cls, v: Decimal | None, info) -> Decimal | None:
        ot = info.data.get("order_type", "limit")
        if ot == "limit" and v is None:
            raise ValueError("limit order requires price")
        return v


class CreatePreviewRequest(BaseModel):
    source_kind: Literal["portfolio_action", "candidate", "research_run"]
    source_ref: str | None = None
    research_session_id: str | None = None
    symbol: str = Field(min_length=1)
    market: Market
    venue: str = Field(min_length=1)
    side: Side
    legs: list[PreviewLegInput] = Field(min_length=1)


class PreviewLegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    leg_index: int
    quantity: Decimal
    price: Decimal | None
    order_type: str
    estimated_value: Decimal | None
    estimated_fee: Decimal | None
    expected_pnl: Decimal | None
    dry_run_status: str | None
    dry_run_error: dict | None


class ExecutionRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    leg_index: int
    broker_order_id: str | None
    status: str
    error_payload: dict | None
    submitted_at: datetime


class PreviewSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    preview_uuid: str
    source_kind: str
    source_ref: str | None
    research_session_id: str | None
    symbol: str
    market: Market
    venue: str
    side: Side
    status: Status
    legs: list[PreviewLegOut]
    executions: list[ExecutionRequestOut] = []
    dry_run_error: dict | None = None
    approved_at: datetime | None
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SubmitPreviewRequest(BaseModel):
    approval_token: str = Field(min_length=8)
