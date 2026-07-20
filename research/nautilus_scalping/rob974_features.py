"""ROB-974 H1: pure, complete-only 4h/VWAP feature plane.

This deliberately has no runtime, persistence, or execution imports.  All
timestamps are built-in epoch-millisecond integers and every economic input is
a finite built-in float; callers must explicitly translate external rows at
this boundary.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

MINUTE_MS = 60_000
FOUR_HOUR_MS = 240 * MINUTE_MS
SYMBOLS = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")


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
class MinuteBar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        _int(self.ts, "ts")
        if self.ts % MINUTE_MS:
            raise ValueError("ts must be minute aligned")
        for name in ("open", "high", "low", "close", "volume"):
            _float(getattr(self, name), name)
        if self.volume < 0 or min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("negative volume or non-positive OHLC")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC")


@dataclass(frozen=True)
class Bar4h:
    ts: int
    close_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_segment_start: bool

    def __post_init__(self) -> None:
        _int(self.ts, "ts"); _int(self.close_ts, "close_ts")
        if type(self.is_segment_start) is not bool:
            raise TypeError("is_segment_start must be bool")
        for name in ("open", "high", "low", "close", "volume"):
            _float(getattr(self, name), name)


def _validated_rows(rows: Sequence[MinuteBar]) -> None:
    for row in rows:
        if not isinstance(row, MinuteBar):
            raise TypeError("rows must contain MinuteBar")
    for left, right in zip(rows, rows[1:], strict=False):
        if right.ts <= left.ts:
            raise ValueError("minute rows must be strictly increasing")


def build_complete_4h(rows: Sequence[MinuteBar]) -> tuple[Bar4h, ...]:
    """Emit only exact UTC 240-minute buckets; gaps form independent segments."""
    _validated_rows(rows)
    by_ts = {row.ts: row for row in rows}
    starts = sorted({(row.ts // FOUR_HOUR_MS) * FOUR_HOUR_MS for row in rows})
    result: list[Bar4h] = []
    prior_close: int | None = None
    for start in starts:
        source = [by_ts.get(start + n * MINUTE_MS) for n in range(240)]
        if any(row is None for row in source):
            continue
        complete = tuple(row for row in source if row is not None)
        segment_start = prior_close is None or start != prior_close
        result.append(Bar4h(start, start + FOUR_HOUR_MS, complete[0].open,
                            max(row.high for row in complete), min(row.low for row in complete),
                            complete[-1].close, math.fsum(row.volume for row in complete), segment_start))
        prior_close = start + FOUR_HOUR_MS
    return tuple(result)


def vwap(rows: Sequence[MinuteBar], close_ts: int, minutes: int) -> float | None:
    """VWAP over the exact contiguous [close_ts-minutes, close_ts) minute range."""
    _int(close_ts, "close_ts"); _int(minutes, "minutes")
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    _validated_rows(rows)
    start = close_ts - minutes * MINUTE_MS
    selected = [row for row in rows if start <= row.ts < close_ts]
    if len(selected) != minutes or any(row.ts != start + i * MINUTE_MS for i, row in enumerate(selected)):
        return None
    numerator = math.fsum(((row.high + row.low + row.close) / 3.0) * row.volume for row in selected)
    denominator = math.fsum(row.volume for row in selected)
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        return None
    answer = numerator / denominator
    return answer if math.isfinite(answer) else None


def vwap12(rows: Sequence[MinuteBar], close_ts: int) -> float | None:
    """Exact 12-hour (720 completed minute) typical-price VWAP."""
    return vwap(rows, close_ts, 720)


def vwap24(rows: Sequence[MinuteBar], close_ts: int) -> float | None:
    """Exact 24-hour (1,440 completed minute) typical-price VWAP."""
    return vwap(rows, close_ts, 1440)


@dataclass(frozen=True)
class SymbolFeature:
    symbol: str
    decision_ts: int
    r: float | None
    tr: float | None
    atr20: float | None
    a: float | None
    vwap12: float | None
    vwap24: float | None
    percentile_30d: float | None
    range24: float | None


@dataclass(frozen=True)
class CommonSnapshot:
    decision_ts: int
    m: float
    M: float
    bplus: int
    bminus: int
    features: tuple[SymbolFeature, ...]


def _segments(bars: Sequence[Bar4h]) -> list[list[Bar4h]]:
    output: list[list[Bar4h]] = []
    for bar in bars:
        if not output or bar.is_segment_start or bar.ts != output[-1][-1].close_ts:
            output.append([bar])
        else:
            output[-1].append(bar)
    return output


def symbol_features(symbol: str, minutes: Sequence[MinuteBar], bars: Sequence[Bar4h] | None = None) -> tuple[SymbolFeature, ...]:
    if symbol not in SYMBOLS:
        raise ValueError("unselected symbol")
    bars = tuple(bars if bars is not None else build_complete_4h(minutes))
    result: list[SymbolFeature] = []
    for segment in _segments(bars):
        trs: list[float] = []; atr: float | None = None; avals: list[float] = []
        for i, bar in enumerate(segment):
            tr = r = None
            if i:
                previous = segment[i - 1]
                r = math.log(bar.close / previous.close)
                tr = max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
                trs.append(tr)
                if len(trs) == 20:
                    atr = math.fsum(trs) / 20.0
                elif len(trs) > 20 and atr is not None:
                    atr = (19.0 * atr + tr) / 20.0
            a = atr / bar.close if atr is not None else None
            percentile = None
            if a is not None and len(avals) >= 180:
                prior = avals[-180:]
                percentile = 100.0 * (sum(x < a for x in prior) + 0.5 * sum(x == a for x in prior)) / 180.0
            if a is not None:
                avals.append(a)
            result.append(SymbolFeature(symbol, bar.close_ts, r, tr, atr, a,
                                        vwap(minutes, bar.close_ts, 720), vwap(minutes, bar.close_ts, 1440),
                                        percentile, _range24(segment, i)))
    return tuple(result)


def _range24(segment: Sequence[Bar4h], end_index: int) -> float | None:
    current_day = segment[end_index].ts // (6 * FOUR_HOUR_MS)
    days: dict[int, list[Bar4h]] = {}
    for bar in segment[:end_index]:
        day = bar.ts // (6 * FOUR_HOUR_MS)
        if day < current_day:
            days.setdefault(day, []).append(bar)
    values = []
    for bars in days.values():
        if len(bars) == 6 and all(bars[n].ts + FOUR_HOUR_MS == bars[n + 1].ts for n in range(5)):
            values.append((max(x.high for x in bars) - min(x.low for x in bars)) / bars[-1].close)
    if len(values) < 20:
        return None
    return sorted(values[-20:])[9:11][0] if False else (sorted(values[-20:])[9] + sorted(values[-20:])[10]) / 2.0


def synchronized_features(rows: Mapping[str, Sequence[MinuteBar]]) -> tuple[CommonSnapshot, ...]:
    if tuple(sorted(rows)) != tuple(sorted(SYMBOLS)):
        raise ValueError("exact selected universe required")
    per_symbol = {symbol: symbol_features(symbol, rows[symbol]) for symbol in SYMBOLS}
    indexed = {symbol: {item.decision_ts: item for item in values} for symbol, values in per_symbol.items()}
    output: list[CommonSnapshot] = []
    for ts in sorted(set.intersection(*(set(values) for values in indexed.values()))):
        features = tuple(indexed[symbol][ts] for symbol in SYMBOLS)
        bars_at = [next((i for i, item in enumerate(per_symbol[s]) if item.decision_ts == ts), -1) for s in SYMBOLS]
        if any(i < 6 for i in bars_at):
            continue
        returns24 = []
        for symbol, index in zip(SYMBOLS, bars_at, strict=True):
            # bar close data is recoverable from the contiguous 4h build, never future rows.
            built = build_complete_4h(rows[symbol])
            if index < 6 or any(
                built[k].ts != built[k - 1].close_ts for k in range(index - 5, index + 1)
            ):
                break
            returns24.append(math.log(built[index].close / built[index - 6].close))
        else:
            rs = [x.r for x in features]
            if any(x is None for x in rs):
                continue
            output.append(CommonSnapshot(ts, sorted(rs)[1], sorted(returns24)[1], sum(x > 0 for x in returns24), sum(x < 0 for x in returns24), features))
    return tuple(output)


def compute_common_features(rows: Mapping[str, Sequence[MinuteBar]]) -> tuple[CommonSnapshot, ...]:
    """Named public boundary for the synchronized S3/S4 common plane."""
    return synchronized_features(rows)


def phase_features(rows: Mapping[str, Sequence[MinuteBar]], start_ts: int, end_ts: int) -> tuple[CommonSnapshot, ...]:
    """Stateless PIT projection: history warms features but never emits context."""
    _int(start_ts, "start_ts"); _int(end_ts, "end_ts")
    if end_ts <= start_ts:
        raise ValueError("invalid phase")
    return tuple(item for item in synchronized_features(rows) if start_ts <= item.decision_ts < end_ts)
