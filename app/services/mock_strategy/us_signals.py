"""Frozen US baselines, causal daily lists and exchange-session exit dates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import exchange_calendars as xcals

from app.core.symbol import to_db_symbol

# Report §5: highest baseline walk-forward IC, selected once by the ROB-1474
# operator brief. Bounded survivor diagnostics, not a readiness verdict.
BASELINES = {
    1: "high_distance_52w",
    3: "relative_volatility_20",
    5: "relative_volatility_20",
    10: "relative_volatility_20",
}


@dataclass(frozen=True)
class Candidate:
    symbol: str
    price: Decimal


@dataclass(frozen=True)
class UsSelection:
    asof: date
    signal_date: date
    horizon: int
    up: tuple[Candidate, ...]
    down: tuple[Candidate, ...]
    input_sha256: str
    universe_observed_at: datetime | None = None

    @property
    def strategy_id(self) -> str:
        return f"rob1474.us.{BASELINES[self.horizon]}.h{self.horizon}.v1"


def session_times(day: date, horizon: int = 5) -> tuple[datetime, datetime, datetime]:
    if horizon not in BASELINES:
        raise ValueError("horizon must be one of 1,3,5,10")
    calendar = xcals.get_calendar("XNYS")
    session = calendar.date_to_session(day.isoformat(), direction="none")
    due = calendar.session_offset(session, horizon)
    return (
        calendar.session_open(session).to_pydatetime(),
        calendar.session_close(session).to_pydatetime(),
        calendar.session_open(due).to_pydatetime(),
    )


def client_order_id(
    strategy: str, asof: date, symbol: str, side: str, horizon: int
) -> str:
    baseline = "h52" if horizon == 1 else "rv20"
    digest = hashlib.sha256(f"{strategy}|{asof}|{symbol}|{side}".encode()).hexdigest()[
        :24
    ]
    return f"r1474us{baseline}h{horizon}v1-{digest}"


def build_selection(
    root: Path, asof: date, *, horizon: int = 5, top_n: int = 10
) -> UsSelection:
    # Research stays order/DB-free. Only this explicit paper consumer imports it.
    import pandas as pd

    from research.stock_selection.forward import make_lists
    from research.stock_selection.io import read_bars

    session_times(asof, horizon)
    calendar = xcals.get_calendar("XNYS")
    signal_date = calendar.previous_session(asof.isoformat()).date()
    bars, meta = read_bars(root, "us")
    if (
        meta.get("producer") == "mock-data-refresh/v1"
        and date.fromisoformat(meta["observation_date"]) > asof
    ):
        raise ValueError("future universe observation")
    if (
        meta.get("producer") == "mock-data-refresh/v1"
        and date.fromisoformat(meta["data_cutoff"]) != signal_date
    ):
        raise ValueError("universe cutoff must match previous signal session")
    payload = make_lists(
        bars,
        market="us",
        asof=signal_date.isoformat(),
        model=BASELINES[horizon],
        top_n=top_n,
    )
    latest = bars[pd.to_datetime(bars.date) == pd.Timestamp(signal_date)].set_index(
        "symbol"
    )

    def candidates(side: str) -> tuple[Candidate, ...]:
        return tuple(
            Candidate(
                to_db_symbol(row["symbol"]),
                Decimal(str(latest.loc[row["symbol"], "close"])),
            )
            for row in payload["horizons"][str(horizon)][side]
        )

    return UsSelection(
        asof,
        signal_date,
        horizon,
        candidates("up"),
        candidates("down"),
        payload["input_prefix_sha256"],
        pd.Timestamp(meta["fetched_at"]).to_pydatetime()
        if meta.get("producer") == "mock-data-refresh/v1"
        else None,
    )
