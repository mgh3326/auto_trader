"""ROB-121 — Research retrospective aggregation DTOs (read-only)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Market = Literal["KR", "US", "CRYPTO"]
DecisionVerdict = Literal["buy", "hold", "sell"]
StageType = Literal["market", "news", "fundamentals", "social"]


class StageCoverageStat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_type: StageType
    coverage_pct: float = Field(ge=0.0, le=100.0)
    stale_pct: float = Field(ge=0.0, le=100.0)
    unavailable_pct: float = Field(ge=0.0, le=100.0)


class DecisionDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_buy: int = 0
    ai_hold: int = 0
    ai_sell: int = 0
    user_accept: int = 0
    user_reject: int = 0
    user_modify: int = 0
    user_defer: int = 0
    user_pending: int = 0


class PnlSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    realized_pnl_pct_avg: float | None = None
    unrealized_pnl_pct_avg: float | None = None
    sample_size: int


class RetrospectiveOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: str
    period_end: str
    market: Market | None
    strategy: str | None
    sessions_total: int
    summaries_total: int
    decision_distribution: DecisionDistribution
    stage_coverage: list[StageCoverageStat]
    pnl: PnlSummary
    warnings: list[str] = Field(default_factory=list)


class StagePerformanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_combo: str
    sample_size: int
    win_rate_pct: float | None = None
    avg_realized_pnl_pct: float | None = None


class RetrospectiveDecisionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_session_id: int
    symbol: str
    market: Market
    decided_at: str
    ai_decision: DecisionVerdict | None = None
    user_response: str | None = None
    realized_pnl_pct: float | None = None
    proposal_id: int | None = None


class RetrospectiveDecisionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    rows: list[RetrospectiveDecisionRow]
