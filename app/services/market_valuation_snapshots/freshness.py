from __future__ import annotations

import datetime as dt

from app.schemas.invest_coverage import CoverageState


def valuation_state(latest_date: dt.date | None, trading_day: dt.date) -> CoverageState:
    if latest_date is None:
        return "missing"
    return "fresh" if latest_date >= trading_day else "stale"
