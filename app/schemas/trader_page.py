"""Read-only operator-page schemas for /trader (task 889, stage 1).

Transport-only shapes for the lightweight operator page served at
trader.robinco.dev/trader. All fields are read projections; nothing here maps
to a write path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.schemas.execution_ledger import DataState, ExecutionLedgerRead
from app.schemas.open_orders import (
    OpenOrderBroker,
    OpenOrderDataState,
    OpenOrderMarket,
    OpenOrderRow,
)

# AC1: orders placed inside the Toss app are not visible through the Toss Open
# API — the page must say so next to the Toss source, always.
TOSS_APP_ORDERS_NOTE = (
    "토스 앱에서 직접 낸 주문은 Toss Open API로 조회되지 않아 여기에 표시되지 않습니다."
)
# AC2: Toss fills stay absent until the dedicated poller lands.
TOSS_FILL_POLLER_NOTE = (
    "Toss 체결은 Toss fill poller(#824) 활성화 전까지 이 목록에 나타나지 않습니다."
)


class TraderOpenOrderSource(BaseModel):
    """Per (broker, market) read state for the open-orders panel."""

    model_config = ConfigDict(extra="forbid")

    broker: OpenOrderBroker
    market: OpenOrderMarket
    status: OpenOrderDataState
    count: int = Field(ge=0)
    message: str | None = None
    fetched_at: datetime | None = None
    last_ok_at: datetime | None = None


class TraderOpenOrdersCacheMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(ge=0)
    cached_at: datetime | None = None
    hit: bool = False


class TraderOpenOrdersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    data_state: OpenOrderDataState
    count: int = Field(ge=0)
    items: list[OpenOrderRow]
    sources: list[TraderOpenOrderSource]
    cache: TraderOpenOrdersCacheMeta
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    empty_reason: str | None = None


class TraderFillsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_kst: str
    window_start: datetime
    window_end: datetime
    count: int = Field(ge=0)
    items: list[ExecutionLedgerRead]
    data_state: DataState | None = None
    empty_reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class TraderWatchRow(BaseModel):
    """Active (and non-expired) watch projection for the watches panel."""

    model_config = ConfigDict(extra="forbid")

    alert_uuid: uuid.UUID
    market: str
    symbol: str
    intent: str
    metric: str
    operator: str
    threshold: Decimal
    threshold_high: Decimal | None = None
    valid_until: datetime
    action_mode: str
    status: str = "active"

    @field_serializer("threshold", "threshold_high")
    def _decimal_to_json(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f")


class TraderWatchesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    count: int = Field(ge=0)
    items: list[TraderWatchRow]
