from __future__ import annotations

import datetime as dt
from typing import Literal

FundamentalsDataState = Literal["fresh", "stale", "partial", "unavailable"]


def row_data_state(*, filing_date: dt.date | None) -> FundamentalsDataState:
    """Row-level data_state stored at build time.

    `partial` if the filing date could not be resolved (read-path must not PIT-cite
    a figure whose public-availability date is unknown). Otherwise `fresh`.
    `stale`/`unavailable` are read-path/coverage states (PR2), not set here.
    """
    if filing_date is None:
        return "partial"
    return "fresh"
