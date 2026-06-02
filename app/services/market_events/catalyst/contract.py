# app/services/market_events/catalyst/contract.py
"""catalyst read-model + 가드 계약 dataclass (ROB-408 Slice 1)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalystEvent:
    symbol: str | None
    category: str
    title: str | None
    event_date: dt.date
    days_until: int
    polarity: str              # positive | negative | neutral
    source: str | None


@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "unavailable"
    stale_reason: str | None


@dataclass(frozen=True)
class UpcomingCatalysts:
    market: str
    within_days: int
    rows: tuple[CatalystEvent, ...]
    freshness: Freshness


@dataclass(frozen=True)
class CatalystGuard:
    flag: str | None           # "upcoming_positive_catalyst" | "upcoming_negative_catalyst" | None
    nearest_days: int | None
    positive: tuple[CatalystEvent, ...]
    negative: tuple[CatalystEvent, ...]
    reason: str | None
