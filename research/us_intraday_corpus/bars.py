"""Bar normalisation: UTC storage, America/New_York session_date.

Direct product of ROB-1206 (brief §1). The rule is narrow and load-bearing:

* the bar's instant is stored **verbatim in UTC** -- no local-time rewriting;
* `session_date` is derived by converting that instant to America/New_York and
  taking the calendar date there.

Anchoring session_date in KST (as the operating DB's US candles do) shifts
every row by one day, because a US session that opens 09:30 ET on day D is
already day D+1 in Seoul. That single mistake would silently invalidate every
label extracted from this corpus, so the KST path is not merely avoided here --
`assert_no_kst_anchor` makes it an error.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable
from typing import Any
from zoneinfo import ZoneInfo

from . import config

UTC = ZoneInfo("UTC")
NY = ZoneInfo(config.SESSION_DATE_TZ)
KST = ZoneInfo(config.FORBIDDEN_SESSION_TZ)


def parse_rfc3339_utc(value: str) -> _dt.datetime:
    """Parse an Alpaca RFC-3339 timestamp into an aware UTC datetime."""
    text = value.replace("Z", "+00:00")
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp from source: {value!r}")
    return parsed.astimezone(UTC)


def session_date(instant: _dt.datetime) -> _dt.date:
    """Derive the US trading session date for a UTC instant."""
    if instant.tzinfo is None:
        raise ValueError("refusing to derive session_date from a naive datetime")
    return instant.astimezone(NY).date()


def assert_no_kst_anchor(instant: _dt.datetime, derived: _dt.date) -> None:
    """Fail loudly if `derived` looks like it came from a KST anchor.

    Only meaningful when the two timezones disagree for this instant, which is
    exactly the window where the bug bites (US after-hours / overnight bars).
    """
    kst_date = instant.astimezone(KST).date()
    ny_date = instant.astimezone(NY).date()
    if kst_date != ny_date and derived == kst_date:
        raise AssertionError(
            f"session_date {derived} for {instant.isoformat()} matches the KST "
            f"anchor, not the America/New_York date {ny_date}. This is the "
            "ROB-1206 one-day-shift bug."
        )


def normalize_bars(
    symbol: str, raw_bars: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert Alpaca bar dicts into corpus rows.

    No forward-fill, no gap synthesis, no silent drops (§3.5): a missing bar
    stays missing and is accounted for by the coverage report instead.
    """
    rows: list[dict[str, Any]] = []
    for bar in raw_bars:
        instant = parse_rfc3339_utc(bar["t"])
        derived = session_date(instant)
        assert_no_kst_anchor(instant, derived)
        rows.append(
            {
                "symbol": symbol,
                "ts_utc": instant,
                "session_date": derived,
                "open": float(bar["o"]),
                "high": float(bar["h"]),
                "low": float(bar["l"]),
                "close": float(bar["c"]),
                "volume": int(bar["v"]),
                "trade_count": int(bar.get("n") or 0),
                "vwap": float(bar["vw"]) if bar.get("vw") is not None else None,
            }
        )
    return rows


def ohlc_violations(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows breaking the OHLC invariant or carrying a non-positive price."""
    bad = []
    for row in rows:
        o, hi, lo, c = row["open"], row["high"], row["low"], row["close"]
        if not (hi >= max(o, c) and lo <= min(o, c) and hi >= lo):
            bad.append({**row, "violation": "ohlc_order"})
        elif min(o, hi, lo, c) <= 0:
            bad.append({**row, "violation": "nonpositive_price"})
        elif row["volume"] < 0:
            bad.append({**row, "violation": "negative_volume"})
    return bad


def is_finished_bar(
    instant: _dt.datetime, timeframe_minutes: int, now_utc: _dt.datetime
) -> bool:
    """True when the bar's interval has fully elapsed (§3.8).

    An in-progress bar must never be stored: its close is not final and would
    silently become a look-ahead value.
    """
    return instant + _dt.timedelta(minutes=timeframe_minutes) <= now_utc
