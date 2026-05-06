# app/schemas/trade_journal.py
"""ROB-120 — Position thesis journal DTOs (operator-facing read + write)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JournalStatus = Literal["draft", "active", "closed", "stopped", "expired"]
WritableJournalStatus = Literal["draft", "active"]
JournalCoverageStatus = Literal["present", "missing", "stale"]
SummaryDecision = Literal["buy", "hold", "sell"]
Market = Literal["KR", "US", "CRYPTO"]


class JournalCoverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str | None = None
    market: Market
    instrument_type: str | None = None
    quantity: float | None = None
    position_weight_pct: float | None = None

    journal_status: JournalCoverageStatus = "missing"
    journal_id: int | None = None
    thesis: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = None
    hold_until: str | None = None

    latest_research_session_id: int | None = None
    latest_research_summary_id: int | None = None
    latest_summary_decision: SummaryDecision | None = None
    thesis_conflict_with_summary: bool = False


class JournalCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    total: int
    rows: list[JournalCoverageRow]
    warnings: list[str] = Field(default_factory=list)


class JournalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    instrument_type: str
    side: Literal["buy", "sell"] = "buy"
    thesis: str
    strategy: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = Field(default=None, ge=0, le=3650)
    status: WritableJournalStatus = "draft"
    account: str | None = None
    notes: str | None = None
    research_session_id: int | None = None
    research_summary_id: int | None = None

    @field_validator("thesis")
    @classmethod
    def thesis_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("thesis must not be blank")
        return v

    @field_validator("symbol")
    @classmethod
    def symbol_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("symbol must not be blank")
        return v


class JournalUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis: str | None = None
    strategy: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = Field(default=None, ge=0, le=3650)
    status: WritableJournalStatus | None = None
    notes: str | None = None
    research_session_id: int | None = None
    research_summary_id: int | None = None

    @field_validator("thesis")
    @classmethod
    def thesis_not_blank_when_present(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("thesis must not be blank when provided")
        return v


class JournalReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    symbol: str
    instrument_type: str
    side: Literal["buy", "sell"]
    thesis: str
    strategy: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = None
    hold_until: str | None = None
    status: JournalStatus
    account: str | None = None
    account_type: Literal["live", "paper"]
    notes: str | None = None
    research_session_id: int | None = None
    research_summary_id: int | None = None
    created_at: str
    updated_at: str
