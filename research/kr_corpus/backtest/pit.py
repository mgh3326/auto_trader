"""Point-in-time bar access — session ``t`` may only see rows with date <= t.

Lookahead is a hard error when a decision context is found to include a
future row. Walk-forward boundary tests inject a future row and assert RED.

KR sealed OHLCV columns are ``ticker`` / ``session`` / ``value`` (int64,
value nullable). This module is the **explicit mapping layer** onto harness
``Bar`` fields ``symbol`` / ``session_date`` / ``trading_value``. Null
``value`` becomes ``trading_value=None`` — never imputed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

import pyarrow as pa
from holdout_guard import assert_date_not_holdout
from windows import parse_iso_date

__all__ = [
    "Bar",
    "LookaheadViolation",
    "bars_from_table",
    "bars_available_at",
    "assert_no_lookahead",
    "parse_session_date",
]


class LookaheadViolation(RuntimeError):
    """Decision at session t consumed a row with session_date > t."""


@dataclass(frozen=True)
class Bar:
    """Harness bar after explicit KR sealed-corpus mapping.

    OHLCV remain ``int`` (sealed int64 KRW ticks). ``trading_value`` is the
    exchange-reported ``value`` column when present, else ``None``.
    """

    symbol: str
    session_date: date
    open: int
    high: int
    low: int
    close: int
    volume: int
    trading_value: int | None
    market: str
    price_mode: str
    source_product: str


def parse_session_date(value: date | datetime | str) -> date:
    """Normalize sealed session labels without inventing a timezone shift."""
    if type(value) is str:
        return parse_iso_date(value)
    if type(value) is date:
        return value
    if isinstance(value, datetime):
        return value.date()
    raise TypeError(f"session date must be date|datetime|str, got {type(value)!r}")


def bars_from_table(table: pa.Table) -> list[Bar]:
    """Map sealed KR OHLCV columns onto harness bars.

    Required real columns: session, market, ticker, open/high/low/close/volume,
    value, price_mode, source_product. Mapping is the only rename site.
    """
    required = (
        "session",
        "market",
        "ticker",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
        "price_mode",
        "source_product",
    )
    missing = [c for c in required if c not in table.column_names]
    if missing:
        raise ValueError(f"KR bars_from_table missing columns {missing}")

    data = table.to_pydict()
    out: list[Bar] = []
    for i in range(table.num_rows):
        session = parse_session_date(data["session"][i])
        assert_date_not_holdout(session)
        raw_value = data["value"][i]
        if raw_value is None:
            trading_value: int | None = None
        else:
            trading_value = int(raw_value)
        out.append(
            Bar(
                symbol=str(data["ticker"][i]),
                session_date=session,
                open=int(data["open"][i]),
                high=int(data["high"][i]),
                low=int(data["low"][i]),
                close=int(data["close"][i]),
                volume=int(data["volume"][i]),
                trading_value=trading_value,
                market=str(data["market"][i]),
                price_mode=str(data["price_mode"][i]),
                source_product=str(data["source_product"][i]),
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
