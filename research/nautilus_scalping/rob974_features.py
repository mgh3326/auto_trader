"""ROB-974 H1: pure, complete-only 4h/VWAP feature plane.

This deliberately has no runtime, persistence, or execution imports.  All
timestamps are built-in epoch-millisecond integers and every economic input is
a finite built-in float; callers must explicitly translate external rows at
this boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
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
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
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
        _int(self.ts, "ts")
        _int(self.close_ts, "close_ts")
        if self.ts % FOUR_HOUR_MS:
            raise ValueError("ts must be UTC 4h aligned")
        if self.close_ts != self.ts + FOUR_HOUR_MS:
            raise ValueError("close_ts must be exactly one 4h bucket later")
        if type(self.is_segment_start) is not bool:
            raise TypeError("is_segment_start must be bool")
        for name in ("open", "high", "low", "close", "volume"):
            _float(getattr(self, name), name)
        if self.volume < 0 or min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("negative volume or non-positive OHLC")
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("invalid OHLC")


def _validated_rows(rows: Sequence[MinuteBar]) -> None:
    for row in rows:
        if type(row) is not MinuteBar:
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
        result.append(
            Bar4h(
                start,
                start + FOUR_HOUR_MS,
                complete[0].open,
                max(row.high for row in complete),
                min(row.low for row in complete),
                complete[-1].close,
                math.fsum(row.volume for row in complete),
                segment_start,
            )
        )
        prior_close = start + FOUR_HOUR_MS
    return tuple(result)


def vwap(rows: Sequence[MinuteBar], close_ts: int, minutes: int) -> float | None:
    """VWAP over the exact contiguous [close_ts-minutes, close_ts) minute range."""
    _int(close_ts, "close_ts")
    _int(minutes, "minutes")
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    _validated_rows(rows)
    start = close_ts - minutes * MINUTE_MS
    selected = [row for row in rows if start <= row.ts < close_ts]
    if len(selected) != minutes or any(
        row.ts != start + i * MINUTE_MS for i, row in enumerate(selected)
    ):
        return None
    numerator = math.fsum(
        ((row.high + row.low + row.close) / 3.0) * row.volume for row in selected
    )
    denominator = math.fsum(row.volume for row in selected)
    if (
        not math.isfinite(numerator)
        or not math.isfinite(denominator)
        or denominator <= 0
    ):
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

    def __post_init__(self) -> None:
        if self.symbol not in SYMBOLS:
            raise ValueError("unselected symbol")
        _int(self.decision_ts, "decision_ts")
        for name in (
            "r",
            "tr",
            "atr20",
            "a",
            "vwap12",
            "vwap24",
            "percentile_30d",
            "range24",
        ):
            value = getattr(self, name)
            if value is not None:
                _float(value, name)


@dataclass(frozen=True)
class CommonSnapshot:
    decision_ts: int
    m: float
    M: float
    bplus: int
    bminus: int
    features: tuple[SymbolFeature, ...]

    def __post_init__(self) -> None:
        _int(self.decision_ts, "decision_ts")
        _float(self.m, "m")
        _float(self.M, "M")
        _int(self.bplus, "bplus")
        _int(self.bminus, "bminus")
        if (
            not isinstance(self.features, tuple)
            or tuple(x.symbol for x in self.features) != SYMBOLS
        ):
            raise ValueError("features must use fixed selected symbol order")
        if any(
            type(x) is not SymbolFeature or x.decision_ts != self.decision_ts
            for x in self.features
        ):
            raise ValueError("features must be exact synchronized SymbolFeature values")


def _segments(bars: Sequence[Bar4h]) -> list[list[Bar4h]]:
    output: list[list[Bar4h]] = []
    for bar in bars:
        if not output or bar.is_segment_start or bar.ts != output[-1][-1].close_ts:
            output.append([bar])
        else:
            output[-1].append(bar)
    return output


def symbol_features(
    symbol: str, minutes: Sequence[MinuteBar], bars: Sequence[Bar4h] | None = None
) -> tuple[SymbolFeature, ...]:
    if symbol not in SYMBOLS:
        raise ValueError("unselected symbol")
    _validated_rows(minutes)
    bars = tuple(bars if bars is not None else build_complete_4h(minutes))
    if any(type(bar) is not Bar4h for bar in bars):
        raise TypeError("bars must contain exact Bar4h values")
    minute_index = {row.ts: index for index, row in enumerate(minutes)}
    result: list[SymbolFeature] = []
    for segment in _segments(bars):
        trs: list[float] = []
        atr: float | None = None
        avals: list[float] = []
        ranges = _range24_values(segment)
        for i, bar in enumerate(segment):
            tr = r = None
            if i:
                previous = segment[i - 1]
                r = math.log(bar.close / previous.close)
                tr = max(
                    bar.high - bar.low,
                    abs(bar.high - previous.close),
                    abs(bar.low - previous.close),
                )
                trs.append(tr)
                if len(trs) == 20:
                    atr = math.fsum(trs) / 20.0
                elif len(trs) > 20 and atr is not None:
                    atr = (19.0 * atr + tr) / 20.0
            a = atr / bar.close if atr is not None else None
            percentile = None
            if a is not None and len(avals) >= 180:
                prior = avals[-180:]
                percentile = (
                    100.0
                    * (sum(x < a for x in prior) + 0.5 * sum(x == a for x in prior))
                    / 180.0
                )
            if a is not None:
                avals.append(a)
            result.append(
                SymbolFeature(
                    symbol,
                    bar.close_ts,
                    r,
                    tr,
                    atr,
                    a,
                    _indexed_vwap(minutes, minute_index, bar.close_ts, 720),
                    _indexed_vwap(minutes, minute_index, bar.close_ts, 1440),
                    percentile,
                    ranges[i],
                )
            )
    return tuple(result)


def _indexed_vwap(
    rows: Sequence[MinuteBar], index: Mapping[int, int], close_ts: int, minutes: int
) -> float | None:
    start = close_ts - minutes * MINUTE_MS
    first = index.get(start)
    if first is None or first + minutes > len(rows):
        return None
    selected = rows[first : first + minutes]
    if any(row.ts != start + offset * MINUTE_MS for offset, row in enumerate(selected)):
        return None
    numerator = math.fsum(
        ((row.high + row.low + row.close) / 3.0) * row.volume for row in selected
    )
    denominator = math.fsum(row.volume for row in selected)
    if (
        denominator <= 0
        or not math.isfinite(numerator)
        or not math.isfinite(denominator)
    ):
        return None
    answer = numerator / denominator
    return answer if math.isfinite(answer) else None


def _range24_values(segment: Sequence[Bar4h]) -> list[float | None]:
    """One-pass prior-complete-UTC-day Range24 values for one contiguous segment."""
    result: list[float | None] = []
    completed: list[float] = []
    current_day: int | None = None
    day_bars: list[Bar4h] = []
    for bar in segment:
        day = bar.ts // (6 * FOUR_HOUR_MS)
        if current_day is None:
            current_day = day
        if day != current_day:
            if len(day_bars) == 6 and all(
                day_bars[index].close_ts == day_bars[index + 1].ts for index in range(5)
            ):
                completed.append(
                    (max(x.high for x in day_bars) - min(x.low for x in day_bars))
                    / day_bars[-1].close
                )
            current_day = day
            day_bars = []
        if len(completed) < 20:
            result.append(None)
        else:
            window = sorted(completed[-20:])
            result.append((window[9] + window[10]) / 2.0)
        day_bars.append(bar)
    return result


def synchronized_features(
    rows: Mapping[str, Sequence[MinuteBar]],
) -> tuple[CommonSnapshot, ...]:
    if tuple(sorted(rows)) != tuple(sorted(SYMBOLS)):
        raise ValueError("exact selected universe required")
    built = {symbol: build_complete_4h(rows[symbol]) for symbol in SYMBOLS}
    per_symbol = {
        symbol: symbol_features(symbol, rows[symbol], built[symbol])
        for symbol in SYMBOLS
    }
    indexed = {
        symbol: {item.decision_ts: item for item in values}
        for symbol, values in per_symbol.items()
    }
    positions = {
        symbol: {
            item.decision_ts: index for index, item in enumerate(per_symbol[symbol])
        }
        for symbol in SYMBOLS
    }
    output: list[CommonSnapshot] = []
    for ts in sorted(set.intersection(*(set(values) for values in indexed.values()))):
        features = tuple(indexed[symbol][ts] for symbol in SYMBOLS)
        bars_at = [positions[symbol].get(ts, -1) for symbol in SYMBOLS]
        if any(i < 6 for i in bars_at):
            continue
        returns24 = []
        for symbol, index in zip(SYMBOLS, bars_at, strict=True):
            if index < 6 or any(
                built[symbol][k].ts != built[symbol][k - 1].close_ts
                for k in range(index - 5, index + 1)
            ):
                break
            returns24.append(
                math.log(built[symbol][index].close / built[symbol][index - 6].close)
            )
        else:
            rs = [x.r for x in features]
            if any(x is None for x in rs):
                continue
            output.append(
                CommonSnapshot(
                    ts,
                    sorted(rs)[1],
                    sorted(returns24)[1],
                    sum(x > 0 for x in returns24),
                    sum(x < 0 for x in returns24),
                    features,
                )
            )
    return tuple(output)


def compute_common_features(
    rows: Mapping[str, Sequence[MinuteBar]],
) -> tuple[CommonSnapshot, ...]:
    """Named public boundary for the synchronized S3/S4 common plane."""
    return synchronized_features(rows)


def phase_features(
    rows: Mapping[str, Sequence[MinuteBar]], start_ts: int, end_ts: int
) -> tuple[CommonSnapshot, ...]:
    """Stateless PIT projection: history warms features but never emits context."""
    _int(start_ts, "start_ts")
    _int(end_ts, "end_ts")
    if end_ts <= start_ts:
        raise ValueError("invalid phase")
    return tuple(
        item
        for item in synchronized_features(rows)
        if start_ts <= item.decision_ts < end_ts
    )
