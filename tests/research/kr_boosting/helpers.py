"""Deterministic panel fixtures for the U7-KR tests. No file, DB or network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd


def weekdays(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def bar_time(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 7, 0, tzinfo=UTC)


def row(
    symbol: str,
    session: date,
    o: float,
    h: float,
    lo: float,
    c: float,
    volume: float = 100_000.0,
    *,
    state: str = "valid",
    market: str = "KOSPI",
    terminal: bool = False,
    payout: str | None = None,
    terminal_at: datetime | None = None,
) -> dict[str, object]:
    empty = state != "valid"
    return {
        "symbol": symbol,
        "listing_id": f"{symbol}#0",
        "market": market,
        "session": session,
        "session_state": state,
        "open": np.nan if empty else o,
        "high": np.nan if empty else h,
        "low": np.nan if empty else lo,
        "close": np.nan if state == "invalid_ohlc" else c,
        "volume": np.nan if empty else volume,
        "available_at": bar_time(session),
        "is_terminal_session": terminal,
        "terminal_outcome": "statutory_cause" if terminal else None,
        "payout_semantics": payout,
        "terminal_available_at": terminal_at,
    }


def flat_rows(
    symbol: str, sessions: list[date], price: float = 10_000.0, *, market: str = "KOSPI"
) -> list[dict[str, object]]:
    return [
        row(symbol, d, price, price * 1.01, price * 0.99, price, market=market)
        for d in sessions
    ]


def random_walk_rows(
    symbol: str, sessions: list[date], seed: int, *, drift: float = 0.0
) -> list[dict[str, object]]:
    rng = np.random.default_rng(seed)
    out: list[dict[str, object]] = []
    close = 10_000.0
    for d in sessions:
        step = rng.normal(drift, 0.022)
        o = round(close * (1 + rng.normal(0, 0.004)))
        c = round(close * np.exp(step))
        h = round(max(o, c) * (1 + abs(rng.normal(0, 0.006))))
        lo = round(min(o, c) * (1 - abs(rng.normal(0, 0.006))))
        out.append(row(symbol, d, o, h, lo, c, float(rng.integers(50_000, 500_000))))
        close = c
    return out


def frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)
