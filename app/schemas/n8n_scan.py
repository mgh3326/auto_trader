"""Pydantic response schemas for n8n scan API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class N8nStrategyScanDetails(BaseModel):
    buy_signals: list[str] = Field(default_factory=list)
    sell_signals: list[str] = Field(default_factory=list)
    sentiment_signals: list[str] = Field(default_factory=list)
    btc_context: str = ""


class N8nStrategyScanResponse(BaseModel):
    success: bool
    as_of: str
    scan_type: Literal["strategy"] = "strategy"
    alerts_sent: int = 0
    message: str = ""
    details: N8nStrategyScanDetails = Field(default_factory=N8nStrategyScanDetails)
    errors: list[dict] = Field(default_factory=list)


class N8nCrashScanDetails(BaseModel):
    crash_signals: list[str] = Field(default_factory=list)


class N8nCrashScanResponse(BaseModel):
    success: bool
    as_of: str
    scan_type: Literal["crash_detection"] = "crash_detection"
    alerts_sent: int = 0
    message: str = ""
    details: N8nCrashScanDetails = Field(default_factory=N8nCrashScanDetails)
    errors: list[dict] = Field(default_factory=list)
