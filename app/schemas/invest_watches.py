"""Read-only schemas for /invest watch alerts (ROB-591)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

WatchAlertStatus = Literal["active", "triggered", "expired", "canceled"]
WatchProximityBand = Literal["hit", "within_0_5_pct", "within_1_pct", "outside"]
WatchDataState = Literal["ok", "degraded", "unavailable"]


class WatchEventSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_uuid: UUID
    outcome: (
        str  # notified/review_required/preview_attached/executed/expired/ignored/failed
    )
    current_value: Decimal | None
    created_at: datetime

    @field_serializer("current_value")
    def _decimal_to_json(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f")


class WatchAlertRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_uuid: UUID
    source_report_uuid: UUID
    market: Literal["kr", "us", "crypto"]
    symbol: str = Field(min_length=1)
    symbol_name: str | None = None
    target_kind: str
    metric: str
    operator: Literal["above", "below", "between"]
    threshold: Decimal
    threshold_high: Decimal | None = None
    status: WatchAlertStatus
    valid_until: datetime
    intent: str
    action_mode: str
    rationale: str
    trigger_checklist: list[dict] = Field(default_factory=list)
    max_action: dict = Field(default_factory=dict)
    # 근접도 (price metric만, 외 metric은 null)
    current_price: Decimal | None = None
    proximity_band: WatchProximityBand | None = None
    # 최근 트리거 (triggered/expired 행에 의미)
    last_event: WatchEventSummary | None = None
    # valid_until 임박 플래그 (active 한정, <= 2일)
    near_expiry: bool = False

    @field_serializer("threshold", "threshold_high", "current_price")
    def _decimal_to_json(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f")


class WatchesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["all", "kr", "us", "crypto"]
    status: Literal["all", "active", "triggered", "expired", "canceled"] = "all"
    count: int = Field(ge=0)
    data_state: WatchDataState
    as_of: datetime
    items: list[WatchAlertRow]
    warnings: list[str] = Field(default_factory=list)
    empty_reason: str | None = None
