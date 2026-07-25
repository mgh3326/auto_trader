"""ROB-692 — stock-detail deterministic recommendation transport schema.

Read-only wire shape for `build_stock_detail_recommendation`
(`app/services/invest_view_model/stock_detail_recommendation_service.py`).
Mirrors the already-floored `build_recommendation_for_equity` dict
(`app/mcp_server/tooling/shared.py`) verbatim (action/confidence/rsi14/
buy_zones/sell_targets/stop_loss/reasoning/insufficient_inputs) plus an
optional risk/reward chip (`RecoTradeSetup`) that reuses the ROB-690
`risk_reward` helper (`app/services/investment_reports/risk_reward.py`).
No new judgment is introduced here — this is a transport shape only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RecommendationAction = Literal["buy", "hold", "sell"]
RecommendationConfidence = Literal["high", "medium", "low"]


class RecoZone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: float
    type: str
    reasoning: str


class RecoTradeSetup(BaseModel):
    """Mirrors `ingestion._serialise_trade_setup`'s headline shape (flattened).

    Decimal-as-string on the wire (never a raw float) — same JSON-safety
    convention as `evidence_snapshot["trade_setup"]`.
    """

    model_config = ConfigDict(extra="forbid")

    direction: Literal["long", "short"]
    entry: str
    stop: str
    target: str
    risk_pct: str
    reward_pct: str
    rr_ratio: str


class StockDetailRecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["kr", "us"]
    symbol: str
    name: str | None = None
    as_of: datetime
    current_price: float | None = None
    action: RecommendationAction
    confidence: RecommendationConfidence
    rsi14: float | None = None
    reasoning: str
    insufficient_inputs: list[str] = Field(default_factory=list)
    buy_zones: list[RecoZone] = Field(default_factory=list)
    sell_targets: list[RecoZone] = Field(default_factory=list)
    stop_loss: float | None = None
    trade_setup: RecoTradeSetup | None = None
