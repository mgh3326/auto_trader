from __future__ import annotations

from pydantic import BaseModel, Field


class N8nSellCondition(BaseModel):
    name: str = Field(..., description="Condition identifier")
    met: bool = Field(..., description="Whether condition is currently met")
    value: float | None = Field(None, description="Current observed value")
    threshold: float | None = Field(None, description="Threshold for trigger")
    detail: str | None = Field(None, description="Human-readable detail")


class N8nSellSignalResponse(BaseModel):
    success: bool = Field(..., description="Whether request completed successfully")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    symbol: str = Field(..., description="Stock code (e.g. 000660)")
    name: str = Field(..., description="Stock name")
    triggered: bool = Field(..., description="True if 2+ conditions met simultaneously")
    conditions_met: int = Field(..., description="Number of conditions currently met")
    conditions: list[N8nSellCondition] = Field(
        default_factory=list, description="Individual condition evaluations"
    )
    message: str = Field("", description="Summary message for notification")
    errors: list[dict[str, object]] = Field(
        default_factory=list, description="Non-fatal errors during evaluation"
    )


class N8nSellSignalBatchResponse(BaseModel):
    success: bool = Field(..., description="Whether batch request completed")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    total: int = Field(..., description="Total active symbols evaluated")
    triggered_count: int = Field(
        ..., description="Number of symbols with sell signal triggered"
    )
    results: list[N8nSellSignalResponse] = Field(
        default_factory=list, description="Per-symbol evaluation results"
    )
    errors: list[dict[str, object]] = Field(
        default_factory=list, description="Top-level errors"
    )
