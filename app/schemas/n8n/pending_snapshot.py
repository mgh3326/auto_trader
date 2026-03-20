# app/schemas/n8n/pending_snapshot.py
"""Schemas for the n8n pending snapshots endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "N8nPendingSnapshotItem",
    "N8nPendingSnapshotsRequest",
    "N8nPendingSnapshotsResponse",
    "N8nPendingResolutionItem",
    "N8nPendingResolveRequest",
    "N8nPendingResolveResponse",
]


class N8nPendingSnapshotItem(BaseModel):
    symbol: str = Field(...)
    instrument_type: str = Field(..., description="crypto, equity_kr, equity_us")
    side: str = Field(...)
    order_price: float = Field(...)
    quantity: float = Field(...)
    current_price: float | None = Field(None)
    gap_pct: float | None = Field(None)
    days_pending: int | None = Field(None)
    account: str = Field(...)
    order_id: str | None = Field(None)


class N8nPendingSnapshotsRequest(BaseModel):
    snapshots: list[N8nPendingSnapshotItem] = Field(
        ..., min_length=1, description="Pending order snapshots to save"
    )


class N8nPendingSnapshotsResponse(BaseModel):
    success: bool = Field(...)
    saved_count: int = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)


class N8nPendingResolutionItem(BaseModel):
    order_id: str = Field(...)
    account: str = Field(...)
    resolved_as: str = Field(..., description="filled, cancelled, or expired")


class N8nPendingResolveRequest(BaseModel):
    resolutions: list[N8nPendingResolutionItem] = Field(
        ..., min_length=1, description="Resolutions to apply"
    )


class N8nPendingResolveResponse(BaseModel):
    success: bool = Field(...)
    resolved_count: int = Field(...)
    not_found_count: int = Field(0)
    errors: list[dict[str, object]] = Field(default_factory=list)
