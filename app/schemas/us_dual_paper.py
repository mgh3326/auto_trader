from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DualPaperBrokerStatus(StrEnum):
    PREVIEWED = "previewed"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class AccountStateSummary(BaseModel):
    """Read-only account context. Counts/numbers only — never secrets or raw payloads."""

    model_config = {"extra": "forbid"}

    cash_usd: float | None = None
    buying_power_usd: float | None = None
    position_count: int | None = None
    open_order_count: int | None = None


class BrokerPreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    symbol: str
    quantity: float
    limit_price_usd: float
    notional_cap_usd: float
    reference_price_usd: float | None = (
        None  # operator/report-supplied; quote fallback later
    )


class BrokerPreviewResult(BaseModel):
    model_config = {"extra": "forbid"}

    account_scope: str  # "kis_mock" | "alpaca_paper"
    status: DualPaperBrokerStatus
    reason: str | None = None
    blocked_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    quantity: float | None = None
    limit_price_usd: float | None = None
    notional_usd: float | None = None
    account_state: AccountStateSummary | None = None
    check_details: dict = Field(default_factory=dict)  # never secrets


class DualBrokerPreviewPacket(BaseModel):
    model_config = {"extra": "forbid"}

    symbol: str
    market: str = "us"
    side: str = "buy"  # long/buy only this issue
    order_type: str = "limit"  # limit only this issue
    limit_price_source: str  # "quote" | "operator_input" | "report_item"
    notional_cap_usd: float
    generated_at: datetime = Field(default_factory=lambda: datetime.now())
    submit_enabled: bool = False  # always False on premarket path
    brokers: dict[str, BrokerPreviewResult] = Field(default_factory=dict)
