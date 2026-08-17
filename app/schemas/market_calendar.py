"""Read schema for the Toss-backed market calendar endpoint (ROB-1280)."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel


class MarketCalendarSessionWindow(BaseModel):
    start: dt.datetime
    end: dt.datetime


class MarketCalendarSessionWindows(BaseModel):
    pre_market: MarketCalendarSessionWindow | None = None
    regular_market: MarketCalendarSessionWindow | None = None
    after_market: MarketCalendarSessionWindow | None = None
    day_market: MarketCalendarSessionWindow | None = None


class MarketCalendarResponse(BaseModel):
    date: dt.date
    market: Literal["kr", "us"]
    is_open: bool | None
    session_windows: MarketCalendarSessionWindows | None = None
    source: Literal["toss_calendar"] = "toss_calendar"
    unavailable_reason: str | None = None
    as_of: dt.datetime
