"""ROB-1059 H1 (spec §14.3/§11.2) — pure UTC calendar-day validity + aggregation
over a canonical Binance public-spot 1-minute corpus.

A valid UTC day requires ALL of: raw observed minute_count >= 1433/1440, max
consecutive raw-missing run <= 2 minutes, and zero raw-missing minutes in the
last 60 minutes before the decision (the 60 minutes immediately preceding the
day's close — freshness right before a decision is never patched). Gaps of
<= 2 minutes are imputed with the rolling previous close (O=H=L=C, volume=0.0,
``imputed=true``); gaps of 3+ minutes are NEVER imputed — that run alone makes
the day invalid. Daily OHLCV = first open / max high / min low / last close /
``math.fsum(volume)`` in ascending timestamp order over [00:00, 24:00) UTC.

This module has no runtime, persistence, network, or execution imports — every
timestamp is a built-in epoch-millisecond ``int`` and every economic value is a
finite built-in ``float`` (rejecting ``bool``/non-finite at construction), the
same discipline as ``rob974_features.MinuteBar``/``Bar4h`` (composition, not a
fork: this is a new, ROB-1059-specific contract with a stricter duplicate rule
and UTC-day rather than 4h buckets).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MINUTE_MS = 60_000
DAY_MS = 1440 * MINUTE_MS
MIN_MINUTE_COUNT = 1433
MAX_GAP_MINUTES = 2
LAST_WINDOW_MINUTES = 60

__all__ = [
    "DAY_MS",
    "MINUTE_MS",
    "DailyBar",
    "DuplicateOpenTimeError",
    "ReversedRowError",
    "SpotMinute",
    "build_daily_series",
    "build_utc_day",
    "ingest_minute_sequence",
]


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class SpotMinute:
    """One raw observed 1-minute row (already checksum-verified upstream)."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        _int(self.open_time_ms, "open_time_ms")
        if self.open_time_ms % MINUTE_MS:
            raise ValueError("open_time_ms must be minute-aligned")
        for name in ("open", "high", "low", "close", "volume"):
            _float(getattr(self, name), name)
        if self.volume < 0:
            raise ValueError("negative volume")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("non-positive OHLC")
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("invalid OHLC invariant")


class DuplicateOpenTimeError(ValueError):
    """A row shares ``open_time_ms`` with a previously ingested row — terminal.

    Unlike ``rob941_kline_schema.parse_kline_csv`` (which dedupes byte-identical
    duplicates), the ROB-1059 §14/AC2 contract is strictly terminal: ANY
    duplicate open_time is invalid input, content-identical or not.
    """


class ReversedRowError(ValueError):
    """A row's ``open_time_ms`` is earlier than the previous row's — terminal,
    never silently resorted."""


def ingest_minute_sequence(rows: Sequence[SpotMinute]) -> tuple[SpotMinute, ...]:
    """Validate a purportedly-ascending sequence of raw minute rows.

    Fails closed: a duplicate ``open_time_ms`` raises ``DuplicateOpenTimeError``;
    an out-of-order (reversed) row raises ``ReversedRowError``. Missing minutes
    are NOT an error here — only recorded as gaps by ``build_utc_day``.
    """
    for row in rows:
        if type(row) is not SpotMinute:
            raise TypeError("rows must contain SpotMinute")
    previous: SpotMinute | None = None
    for row in rows:
        if previous is not None:
            if row.open_time_ms == previous.open_time_ms:
                raise DuplicateOpenTimeError(
                    f"duplicate open_time_ms={row.open_time_ms}"
                )
            if row.open_time_ms < previous.open_time_ms:
                raise ReversedRowError(
                    f"reversed row: open_time_ms={row.open_time_ms} < previous "
                    f"{previous.open_time_ms}"
                )
        previous = row
    return tuple(rows)


@dataclass(frozen=True)
class DailyBar:
    day_start_ms: int  # UTC midnight, inclusive
    day_end_ms: int  # UTC midnight next day, exclusive
    open: float
    high: float
    low: float
    close: float
    volume: float
    minute_count_observed: int
    imputed_minutes: int
    max_gap_minutes: int
    gap_in_last_60min: bool
    is_valid: bool
    is_segment_start: bool

    def __post_init__(self) -> None:
        _int(self.day_start_ms, "day_start_ms")
        _int(self.day_end_ms, "day_end_ms")
        if self.day_start_ms % DAY_MS:
            raise ValueError("day_start_ms must be UTC-midnight aligned")
        if self.day_end_ms != self.day_start_ms + DAY_MS:
            raise ValueError("day_end_ms must be exactly one day later")
        _int(self.minute_count_observed, "minute_count_observed")
        _int(self.imputed_minutes, "imputed_minutes")
        _int(self.max_gap_minutes, "max_gap_minutes")
        for name in ("gap_in_last_60min", "is_valid", "is_segment_start"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        for name in ("open", "high", "low", "close", "volume"):
            _float(getattr(self, name), name)


def build_utc_day(
    day_start_ms: int,
    rows: Sequence[SpotMinute],
    *,
    prior_close: float | None,
    is_segment_start: bool,
) -> DailyBar:
    """Build one UTC ``[day_start_ms, day_start_ms+DAY_MS)`` daily bar.

    ``rows`` need not cover a full day; missing minutes are gaps. ``prior_close``
    (the previous UTC day's close, ``None`` if unavailable) is used ONLY to
    impute a <=2-minute gap's O=H=L=C; a run of >=3 missing minutes is NEVER
    imputed, and a leading gap with no ``prior_close`` to impute from fails the
    day closed (``is_valid=False``) rather than raising or fabricating a price.
    """
    _int(day_start_ms, "day_start_ms")
    if day_start_ms % DAY_MS:
        raise ValueError("day_start_ms must be UTC-midnight aligned")
    day_end_ms = day_start_ms + DAY_MS
    for row in rows:
        if type(row) is not SpotMinute:
            raise TypeError("rows must contain SpotMinute")
        if not (day_start_ms <= row.open_time_ms < day_end_ms):
            raise ValueError("row outside declared UTC day window")
    ingest_minute_sequence(rows)  # duplicate/reversed -> terminal, even within a day

    by_ts = {row.open_time_ms: row for row in rows}
    minute_count_observed = len(by_ts)

    max_gap_minutes = 0
    run = 0
    gap_last_60 = False
    last_60_start = day_end_ms - LAST_WINDOW_MINUTES * MINUTE_MS
    t = day_start_ms
    while t < day_end_ms:
        if t in by_ts:
            run = 0
        else:
            run += 1
            max_gap_minutes = max(max_gap_minutes, run)
            if t >= last_60_start:
                gap_last_60 = True
        t += MINUTE_MS

    leading_gap_no_baseline = day_start_ms not in by_ts and prior_close is None

    is_valid = (
        minute_count_observed >= MIN_MINUTE_COUNT
        and max_gap_minutes <= MAX_GAP_MINUTES
        and not gap_last_60
        and not leading_gap_no_baseline
    )

    if not is_valid:
        ohlc_source = sorted(by_ts)
        if not ohlc_source:
            if prior_close is None:
                o = h = low = c = (
                    1.0  # placeholder only; is_valid=False is authoritative
                )
            else:
                o = h = low = c = prior_close
            v = 0.0
        else:
            o = by_ts[ohlc_source[0]].open
            h = max(by_ts[k].high for k in ohlc_source)
            low = min(by_ts[k].low for k in ohlc_source)
            c = by_ts[ohlc_source[-1]].close
            v = math.fsum(by_ts[k].volume for k in ohlc_source)
        return DailyBar(
            day_start_ms,
            day_end_ms,
            o,
            h,
            low,
            c,
            v,
            minute_count_observed,
            0,
            max_gap_minutes,
            gap_last_60,
            False,
            is_segment_start,
        )

    imputed_minutes = 0
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []
    rolling_close = prior_close
    first_open: float | None = None
    t = day_start_ms
    while t < day_end_ms:
        row = by_ts.get(t)
        if row is not None:
            opens.append(row.open)
            highs.append(row.high)
            lows.append(row.low)
            closes.append(row.close)
            volumes.append(row.volume)
            rolling_close = row.close
        else:
            assert (
                rolling_close is not None
            )  # guaranteed by leading_gap_no_baseline check above
            imputed_minutes += 1
            opens.append(rolling_close)
            highs.append(rolling_close)
            lows.append(rolling_close)
            closes.append(rolling_close)
            volumes.append(0.0)
        if first_open is None:
            first_open = opens[-1]
        t += MINUTE_MS

    return DailyBar(
        day_start_ms,
        day_end_ms,
        first_open,
        max(highs),
        min(lows),
        closes[-1],
        math.fsum(volumes),
        minute_count_observed,
        imputed_minutes,
        max_gap_minutes,
        False,
        True,
        is_segment_start,
    )


def build_daily_series(
    rows: Sequence[SpotMinute],
    *,
    window_start_ms: int,
    window_end_ms: int,
    prior_close_seed: float | None = None,
) -> tuple[DailyBar, ...]:
    """Build one ``DailyBar`` per full UTC day in ``[window_start_ms,
    window_end_ms)``. A partial (in-progress) trailing day is NEVER emitted —
    only whole ``DAY_MS`` buckets inside the window are attempted. A day with
    zero rows at all is skipped entirely (not an all-gap invalid bar) and
    breaks the segment for the next present day, matching
    ``rob974_features.build_complete_4h``'s segment-on-gap discipline.
    """
    _int(window_start_ms, "window_start_ms")
    _int(window_end_ms, "window_end_ms")
    if window_start_ms % DAY_MS:
        raise ValueError("window_start_ms must be UTC-midnight aligned")
    if window_end_ms <= window_start_ms:
        raise ValueError("window_end_ms must be after window_start_ms")

    by_day: dict[int, list[SpotMinute]] = {}
    for row in rows:
        if type(row) is not SpotMinute:
            raise TypeError("rows must contain SpotMinute")
        day = (row.open_time_ms // DAY_MS) * DAY_MS
        by_day.setdefault(day, []).append(row)
    for day_rows in by_day.values():
        day_rows.sort(key=lambda r: r.open_time_ms)

    result: list[DailyBar] = []
    prior_close = prior_close_seed
    prior_day_present = False
    day = window_start_ms
    while day + DAY_MS <= window_end_ms:
        day_rows = by_day.get(day)
        if day_rows is None:
            prior_day_present = False
            day += DAY_MS
            continue
        bar = build_utc_day(
            day,
            day_rows,
            prior_close=prior_close,
            is_segment_start=not prior_day_present,
        )
        result.append(bar)
        prior_close = bar.close
        prior_day_present = bar.is_valid
        day += DAY_MS
    return tuple(result)
