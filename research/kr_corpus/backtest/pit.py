"""Point-in-time bar access — session ``t`` may only see rows with date <= t.

Lookahead is a hard error when a decision context is found to include a
future row. Walk-forward boundary tests inject a future row and assert RED.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import pyarrow as pa
from holdout_guard import assert_date_not_holdout
from windows import parse_iso_date

__all__ = [
    "Bar",
    "LookaheadViolation",
    "bars_from_table",
    "bars_available_at",
    "assert_no_lookahead",
]


class LookaheadViolation(RuntimeError):
    """Decision at session t consumed a row with session_date > t."""


@dataclass(frozen=True)
class Bar:
    symbol: str
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    trading_value: float
    market: str


def bars_from_table(table: pa.Table) -> list[Bar]:
    data = table.to_pydict()
    out: list[Bar] = []
    for i in range(table.num_rows):
        session = parse_iso_date(data["session_date"][i])
        assert_date_not_holdout(session)
        out.append(
            Bar(
                symbol=str(data["symbol"][i]),
                session_date=session,
                open=float(data["open"][i]),
                high=float(data["high"][i]),
                low=float(data["low"][i]),
                close=float(data["close"][i]),
                volume=float(data["volume"][i]),
                trading_value=float(data["trading_value"][i]),
                market=str(data["market"][i]),
            )
        )
    return out


def bars_available_at(
    bars: Iterable[Bar],
    session: date | str,
    *,
    symbol: str | None = None,
) -> list[Bar]:
    """Return bars with ``session_date <= session`` (and optional symbol filter)."""
    t = assert_date_not_holdout(session)
    out: list[Bar] = []
    for bar in bars:
        if bar.session_date > t:
            continue
        if symbol is not None and bar.symbol != symbol:
            continue
        out.append(bar)
    return out


def assert_no_lookahead(
    used_bars: Iterable[Bar],
    decision_session: date | str,
) -> None:
    """Raise ``LookaheadViolation`` if any used bar is after the decision session."""
    t = assert_date_not_holdout(decision_session)
    for bar in used_bars:
        if bar.session_date > t:
            raise LookaheadViolation(
                f"lookahead: decision at {t.isoformat()} used bar "
                f"{bar.symbol}@{bar.session_date.isoformat()}"
            )
