"""ROB-974 H3 S3 point-in-time context and exact formula core.

The input boundary stores actual merged-H1 frozen DTOs.  Calculations are
stateless, consume only complete history at or before ``decision_ts``, and use
a separate half-open emission window.  Missing required history is represented
as no formula result; malformed input is terminal.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rob974_features import FOUR_HOUR_MS, Bar4h, CommonSnapshot, SymbolFeature
from rob974_h3_manifest import (
    SYMBOLS,
    S3Config,
    assert_registered_config,
)


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    return value


def _float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _validate_bar(bar: Bar4h) -> None:
    if type(bar) is not Bar4h:
        raise TypeError("FeatureContext bars must be exact merged-H1 Bar4h values")
    _int(bar.ts, "bar.ts")
    _int(bar.close_ts, "bar.close_ts")
    if bar.close_ts != bar.ts + FOUR_HOUR_MS or bar.ts % FOUR_HOUR_MS:
        raise ValueError("malformed H1 Bar4h timestamp")
    for name in ("open", "high", "low", "close", "volume"):
        _float(getattr(bar, name), f"bar.{name}")
    if bar.close <= 0.0 or bar.low <= 0.0 or bar.volume < 0.0:
        raise ValueError("malformed H1 Bar4h economic value")


def _validate_feature(feature: SymbolFeature, decision_ts: int) -> None:
    if type(feature) is not SymbolFeature:
        raise TypeError("snapshot features must be exact merged-H1 SymbolFeature")
    if feature.decision_ts != decision_ts:
        raise ValueError("H1 feature timestamp differs from snapshot")
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
        value = getattr(feature, name)
        if value is not None:
            _float(value, f"feature.{name}")
    for name in ("atr20", "a", "range24"):
        value = getattr(feature, name)
        if value is not None and value < 0.0:
            raise ValueError(f"feature.{name} must not be negative")
    if feature.vwap12 is not None and feature.vwap12 <= 0.0:
        raise ValueError("feature.vwap12 must be positive")
    if feature.vwap24 is not None and feature.vwap24 <= 0.0:
        raise ValueError("feature.vwap24 must be positive")
    if feature.percentile_30d is not None and not (
        0.0 <= feature.percentile_30d <= 100.0
    ):
        raise ValueError("feature.percentile_30d must be in [0,100]")


def _validate_snapshot(snapshot: CommonSnapshot) -> None:
    if type(snapshot) is not CommonSnapshot:
        raise TypeError("snapshots must be exact merged-H1 CommonSnapshot values")
    _int(snapshot.decision_ts, "snapshot.decision_ts")
    if snapshot.decision_ts % FOUR_HOUR_MS:
        raise ValueError("snapshot decision_ts must be a UTC 4h close")
    _float(snapshot.m, "snapshot.m")
    _float(snapshot.M, "snapshot.M")
    _int(snapshot.bplus, "snapshot.bplus")
    _int(snapshot.bminus, "snapshot.bminus")
    if not (0 <= snapshot.bplus <= 3 and 0 <= snapshot.bminus <= 3):
        raise ValueError("snapshot breadth must be in [0,3]")
    if (
        type(snapshot.features) is not tuple
        or tuple(feature.symbol for feature in snapshot.features) != SYMBOLS
    ):
        raise ValueError("snapshot feature order must match merged H1")
    for feature in snapshot.features:
        _validate_feature(feature, snapshot.decision_ts)


@dataclass(frozen=True, slots=True)
class FeatureContext:
    """Immutable normalized storage of actual H1 bars and synchronized DTOs."""

    bars_by_symbol: tuple[tuple[str, tuple[Bar4h, ...]], ...]
    snapshots: tuple[CommonSnapshot, ...]

    def __post_init__(self) -> None:
        if (
            type(self.bars_by_symbol) is not tuple
            or tuple(symbol for symbol, _ in self.bars_by_symbol) != SYMBOLS
        ):
            raise ValueError("bars_by_symbol must use the fixed H1 symbol order")
        for symbol, bars in self.bars_by_symbol:
            if type(symbol) is not str or type(bars) is not tuple:
                raise TypeError("FeatureContext bar storage must be built-in tuples")
            prior: Bar4h | None = None
            seen: set[int] = set()
            for bar in bars:
                _validate_bar(bar)
                if bar.close_ts in seen or (
                    prior is not None and bar.close_ts <= prior.close_ts
                ):
                    raise ValueError("H1 Bar4h history must be strictly ordered")
                if (
                    prior is not None
                    and bar.ts != prior.close_ts
                    and not bar.is_segment_start
                ):
                    raise ValueError("an H1 time gap must start a new segment")
                seen.add(bar.close_ts)
                prior = bar
        if type(self.snapshots) is not tuple:
            raise TypeError("snapshots must be built-in tuple")
        prior_ts: int | None = None
        available = {
            symbol: {bar.close_ts for bar in bars}
            for symbol, bars in self.bars_by_symbol
        }
        for snapshot in self.snapshots:
            _validate_snapshot(snapshot)
            if prior_ts is not None and snapshot.decision_ts <= prior_ts:
                raise ValueError("H1 snapshots must be strictly ordered and unique")
            if any(snapshot.decision_ts not in available[symbol] for symbol in SYMBOLS):
                raise ValueError("H1 snapshot lacks its exact synchronized Bar4h close")
            prior_ts = snapshot.decision_ts

    @classmethod
    def from_h1(
        cls,
        bars: Mapping[str, Sequence[Bar4h]],
        snapshots: Sequence[CommonSnapshot],
    ) -> FeatureContext:
        if not isinstance(bars, Mapping) or set(bars) != set(SYMBOLS):
            raise ValueError("bars must cover the exact H1 selected universe")
        normalized: list[tuple[str, tuple[Bar4h, ...]]] = []
        for symbol in SYMBOLS:
            values = bars[symbol]
            if not isinstance(values, Sequence):
                raise TypeError("each H1 bar history must be a sequence")
            exact = values if type(values) is tuple else tuple(values)
            normalized.append((symbol, exact))
        if not isinstance(snapshots, Sequence):
            raise TypeError("H1 snapshots must be a sequence")
        exact_snapshots = snapshots if type(snapshots) is tuple else tuple(snapshots)
        return cls(tuple(normalized), exact_snapshots)

    def bars_for(self, symbol: str) -> tuple[Bar4h, ...]:
        _str(symbol, "symbol")
        if symbol not in SYMBOLS:
            raise ValueError("symbol outside the frozen H1 universe")
        return self.bars_by_symbol[SYMBOLS.index(symbol)][1]

    def snapshot_at(self, decision_ts: int) -> CommonSnapshot | None:
        _int(decision_ts, "decision_ts")
        return next(
            (
                snapshot
                for snapshot in self.snapshots
                if snapshot.decision_ts == decision_ts
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class EmitWindow:
    start: int
    end: int

    def __post_init__(self) -> None:
        _int(self.start, "emit_window.start")
        _int(self.end, "emit_window.end")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("emit_window must be a non-empty half-open range")


def expected_decision_closes(emit_window: EmitWindow) -> tuple[int, ...]:
    if type(emit_window) is not EmitWindow:
        raise TypeError("emit_window must be exact EmitWindow")
    first = ((emit_window.start + FOUR_HOUR_MS - 1) // FOUR_HOUR_MS) * FOUR_HOUR_MS
    return tuple(range(first, emit_window.end, FOUR_HOUR_MS))


@dataclass(frozen=True, slots=True)
class S3Metrics:
    config_id: str
    decision_ts: int
    symbol: str
    R: float
    ER: float
    S: float
    Qplus: float
    Qminus: float
    close: float
    previous_close: float
    prior_l_high: float
    prior_l_low: float
    atr20: float
    A: float
    vwap12: float
    vwap24: float
    percentile_30d: float
    range24: float
    market_return_24h: float
    current_market_return_4h: float
    bplus: int
    bminus: int

    def __post_init__(self) -> None:
        _str(self.config_id, "config_id")
        _int(self.decision_ts, "decision_ts")
        if _str(self.symbol, "symbol") not in SYMBOLS:
            raise ValueError("unregistered symbol")
        for name in (
            "R",
            "ER",
            "S",
            "Qplus",
            "Qminus",
            "close",
            "previous_close",
            "prior_l_high",
            "prior_l_low",
            "atr20",
            "A",
            "vwap12",
            "vwap24",
            "percentile_30d",
            "range24",
            "market_return_24h",
            "current_market_return_4h",
        ):
            _float(getattr(self, name), name)
        _int(self.bplus, "bplus")
        _int(self.bminus, "bminus")


@dataclass(frozen=True, slots=True)
class S3FormulaUnit:
    decision_ts: int
    symbol: str
    metrics: S3Metrics | None

    def __post_init__(self) -> None:
        _int(self.decision_ts, "decision_ts")
        if _str(self.symbol, "symbol") not in SYMBOLS:
            raise ValueError("unregistered symbol")
        if self.metrics is not None and type(self.metrics) is not S3Metrics:
            raise TypeError("metrics must be exact S3Metrics or None")
        if self.metrics is not None and (
            self.metrics.decision_ts != self.decision_ts
            or self.metrics.symbol != self.symbol
        ):
            raise ValueError("formula-unit identity mismatch")


def _feature_at(snapshot: CommonSnapshot, symbol: str) -> SymbolFeature:
    return snapshot.features[SYMBOLS.index(symbol)]


def _required_current_values(feature: SymbolFeature) -> tuple[float, ...] | None:
    values = (
        feature.atr20,
        feature.a,
        feature.vwap12,
        feature.vwap24,
        feature.percentile_30d,
        feature.range24,
    )
    if any(value is None for value in values):
        return None
    return tuple(value for value in values if value is not None)


def compute_s3_metrics(
    feature_context: FeatureContext,
    config: S3Config,
    decision_ts: int,
    symbol: str,
) -> S3Metrics | None:
    """Compute exact registered S3 formula values, or None for unavailable PIT data."""
    if type(feature_context) is not FeatureContext:
        raise TypeError("feature_context must be exact FeatureContext")
    if type(config) is not S3Config:
        raise TypeError("config must be exact registered S3Config")
    assert_registered_config(config)
    _int(decision_ts, "decision_ts")
    if decision_ts % FOUR_HOUR_MS:
        raise ValueError("decision_ts must be an exact UTC 4h close")
    if _str(symbol, "symbol") not in SYMBOLS:
        raise ValueError("symbol outside the frozen universe")

    snapshot = feature_context.snapshot_at(decision_ts)
    if snapshot is None:
        return None
    current_feature = _feature_at(snapshot, symbol)
    required = _required_current_values(current_feature)
    if required is None:
        return None
    atr20, a_value, vwap12, vwap24, percentile, range24 = required

    bars = feature_context.bars_for(symbol)
    current_index = next(
        (index for index, bar in enumerate(bars) if bar.close_ts == decision_ts),
        None,
    )
    if current_index is None or current_index < config.L:
        return None
    history = bars[current_index - config.L : current_index + 1]
    if len(history) != config.L + 1:
        return None
    for left, right in zip(history, history[1:], strict=False):
        if right.ts != left.close_ts or right.is_segment_start:
            return None

    prior_snapshots = (
        feature_context.snapshot_at(decision_ts - 2 * FOUR_HOUR_MS),
        feature_context.snapshot_at(decision_ts - FOUR_HOUR_MS),
    )
    if any(item is None for item in prior_snapshots):
        return None
    normalized_pullbacks: list[float] = []
    for bar, prior_snapshot in zip(history[-3:-1], prior_snapshots, strict=True):
        if prior_snapshot is None:  # narrowed above; keeps runtime branch explicit
            return None
        feature = _feature_at(prior_snapshot, symbol)
        if feature.atr20 is None or feature.vwap12 is None or feature.atr20 <= 0.0:
            return None
        normalized_pullbacks.append((bar.close - feature.vwap12) / feature.atr20)

    closes = tuple(bar.close for bar in history)
    er_denominator = math.fsum(
        abs(current - previous)
        for previous, current in zip(closes, closes[1:], strict=False)
    )
    if er_denominator <= 0.0:
        return None
    r_value = math.log(closes[-1] / closes[0])
    er_value = abs(closes[-1] - closes[0]) / er_denominator
    s_denominator = max(a_value * math.sqrt(config.L), 1e-6)
    s_value = r_value / s_denominator
    q_plus = -min(normalized_pullbacks)
    q_minus = max(normalized_pullbacks)
    calculated = (r_value, er_value, s_value, q_plus, q_minus)
    if not all(math.isfinite(value) for value in calculated):
        raise ValueError("nonfinite S3 formula result")

    prior_l = history[:-1]
    return S3Metrics(
        config.config_id,
        decision_ts,
        symbol,
        r_value,
        er_value,
        s_value,
        q_plus,
        q_minus,
        history[-1].close,
        history[-2].close,
        max(bar.high for bar in prior_l),
        min(bar.low for bar in prior_l),
        atr20,
        a_value,
        vwap12,
        vwap24,
        percentile,
        range24,
        snapshot.M,
        snapshot.m,
        snapshot.bplus,
        snapshot.bminus,
    )


def s3_formula_grid(
    feature_context: FeatureContext,
    emit_window: EmitWindow,
    config: S3Config,
) -> tuple[S3FormulaUnit, ...]:
    """Enumerate all three symbol units at every expected close in [start,end)."""
    if type(feature_context) is not FeatureContext:
        raise TypeError("feature_context must be exact FeatureContext")
    if type(config) is not S3Config:
        raise TypeError("config must be exact registered S3Config")
    assert_registered_config(config)
    return tuple(
        S3FormulaUnit(
            decision_ts,
            symbol,
            compute_s3_metrics(feature_context, config, decision_ts, symbol),
        )
        for decision_ts in expected_decision_closes(emit_window)
        for symbol in SYMBOLS
    )


__all__ = [
    "EmitWindow",
    "FeatureContext",
    "S3FormulaUnit",
    "S3Metrics",
    "compute_s3_metrics",
    "expected_decision_closes",
    "s3_formula_grid",
]
