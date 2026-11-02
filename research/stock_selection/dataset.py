"""Causal features and session-aligned outcomes, kept in separate columns."""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from research.data_plane.calendars import bar_available_at, partition_days

HORIZONS = (1, 3, 5, 10)
FEATURE_CONTRACT = "stock-selection-v2"
MARKETS = ("kr", "us", "crypto")
FEATURES = (
    "momentum_5",
    "momentum_20",
    "momentum_60",
    "reversal_1",
    "reversal_5",
    "volume_shock",
    "value_shock",
    "volatility_20",
    "gap",
    "high_distance_52w",
)
MODEL_FEATURES = FEATURES + tuple(f"relative_{f}" for f in FEATURES)
LIQUID_N = {"kr": 500, "us": 1000, "crypto": 100}
COST_BPS = {"kr": 25.0, "us": 10.0, "crypto": 15.0}  # assumed one-way, not a tariff


class LeakageError(ValueError):
    """An input or derived value is unavailable at its claimed decision time."""


@lru_cache(maxsize=10000)
def decision_at(market: str, day: date) -> pd.Timestamp:
    # Crypto bar D covers [D 00:00, D+1 00:00); availability is on D+1.
    partition = day + timedelta(days=1) if market == "crypto" else day
    return pd.Timestamp(bar_available_at(market, partition))


def sessions(market: str, start: str, end: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        partition_days(market, date.fromisoformat(start), date.fromisoformat(end))
    )


def validate_bars(bars: pd.DataFrame, market: str) -> pd.DataFrame:
    if market not in MARKETS or bars.empty:
        raise ValueError("market must be kr/us/crypto and bars must be nonempty")
    required = {
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "traded_value",
        "available_at",
        "action",
    }
    if required - set(bars):
        raise ValueError(f"missing bar columns: {sorted(required - set(bars))}")
    bars = bars.copy()
    bars["date"] = pd.to_datetime(bars.date).dt.normalize()
    bars["symbol"] = bars.symbol.astype(str)
    if bars.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate symbol/session")
    bars["available_at"] = pd.to_datetime(bars.available_at, utc=True)
    deadlines = bars.date.map(lambda d: decision_at(market, d.date()))
    if bars.available_at.isna().any() or (bars.available_at > deadlines).any():
        raise LeakageError("bar available_at exceeds its session decision time")
    prices = bars[["open", "high", "low", "close"]].to_numpy(float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("OHLC must be finite and positive")
    if (
        (bars.high < bars[["open", "close", "low"]].max(axis=1))
        | (bars.low > bars[["open", "close", "high"]].min(axis=1))
    ).any():
        raise ValueError("inconsistent OHLC")
    for col in ("volume", "traded_value"):
        bars[col] = pd.to_numeric(bars[col])
        if not np.isfinite(bars[col]).all() or (bars[col] < 0).any():
            raise ValueError(f"{col} must be finite and nonnegative")
    if bars.action.isna().any() or not bars.action.isin([True, False, 0, 1]).all():
        raise ValueError("action must explicitly indicate corporate-action crossings")
    return bars.sort_values(["symbol", "date"]).reset_index(drop=True)


def _features(frame: pd.DataFrame, *, high_window: int) -> pd.DataFrame:
    close = frame.close
    ret = close.pct_change(fill_method=None)
    f = pd.DataFrame(index=frame.index)
    for h in (5, 20, 60):
        f[f"momentum_{h}"] = close.pct_change(h, fill_method=None)
    f["reversal_1"] = -ret
    f["reversal_5"] = -f.momentum_5
    for col, name in (("volume", "volume_shock"), ("traded_value", "value_shock")):
        f[name] = (
            frame[col] / frame[col].shift(1).rolling(20).mean().replace(0, np.nan) - 1
        )
    f["volatility_20"] = ret.rolling(20).std(ddof=1)
    f["gap"] = frame.open / close.shift(1) - 1
    f["high_distance_52w"] = close / frame.high.rolling(high_window).max() - 1
    f["liquidity_20"] = frame.traded_value.rolling(20).mean()
    return f.replace([np.inf, -np.inf], np.nan)


def build_dataset(
    bars: pd.DataFrame, market: str, *, liquid_n: int | None = None
) -> pd.DataFrame:
    """Rows never depend on later membership or label availability.

    Missing sessions are explicit NaNs. Horizons use the exchange calendar,
    not the next h *observed* bars. Action-crossing outcomes stay unknown.
    ``__benchmark__`` must be supplied, never estimated from future survivors.
    """
    bars = validate_bars(bars, market)
    liquid_n = LIQUID_N[market] if liquid_n is None else liquid_n
    if liquid_n < 2:
        raise ValueError("liquid_n must be >= 2")
    days = sessions(
        market, bars.date.min().date().isoformat(), bars.date.max().date().isoformat()
    )
    if not bars.date.isin(days).all():
        raise ValueError("bar on a non-session")
    bench = bars[bars.symbol == "__benchmark__"].set_index("date").reindex(days)
    if bench.close.isna().any():
        raise ValueError("benchmark must cover every session; gaps cannot be filled")
    high_window = 364 if market == "crypto" else 252
    bf = _features(bench, high_window=high_window)
    regime_return = bench.close.pct_change(20, fill_method=None)
    regime = pd.Series(
        np.where(
            regime_return > 0.02, "up", np.where(regime_return < -0.02, "down", "flat")
        ),
        index=days,
    )
    regime[regime_return.isna()] = "unknown"
    frames = []
    for symbol, raw in bars[bars.symbol != "__benchmark__"].groupby(
        "symbol", sort=True
    ):
        frame = raw.set_index("date").reindex(days)
        f = _features(frame, high_window=high_window)
        for col in FEATURES:
            f[f"relative_{col}"] = f[col] - bf[col]
        f["symbol"], f["market"], f["regime"] = symbol, market, regime
        f["available_at"] = frame.available_at
        # A zero-volume current bar is observable but not tradable.
        f["tradable"] = frame.close.notna() & (frame.volume > 0)
        for h in HORIZONS:
            valid = frame.close.notna()
            crossed = pd.Series(False, index=days)
            for step in range(1, h + 1):
                valid &= (
                    frame.close.shift(-step).notna() & bench.close.shift(-step).notna()
                )
                crossed |= frame.action.shift(-step).fillna(True).astype(
                    bool
                ) | bench.action.shift(-step).fillna(True).astype(bool)
            f[f"return_{h}"] = (frame.close.shift(-h) / frame.close - 1).where(
                valid & ~crossed
            )
            br = bench.close.shift(-h) / bench.close - 1
            f[f"relative_return_{h}"] = f[f"return_{h}"] - br
            f[f"label_end_{h}"] = pd.Series(days, index=days).shift(-h)
        # Keep only observed rows, including outcomes that remain unknown.
        frames.append(f.loc[frame.close.notna()].rename_axis("date").reset_index())
    if not frames:
        raise ValueError("no asset bars")
    result = pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"])
    # Eligibility uses only today's feature history, never future outcome quality.
    ready = result[list(MODEL_FEATURES)].notna().all(axis=1) & result.tradable
    ranks = (
        result.liquidity_20.where(ready)
        .groupby(result.date)
        .rank(ascending=False, method="first")
    )
    result["eligible"] = ready & (ranks <= liquid_n)
    return result.reset_index(drop=True)


def audit_dataset(
    bars: pd.DataFrame,
    dataset: pd.DataFrame,
    market: str,
    *,
    liquid_n: int | None = None,
) -> None:
    """Recompute feature/label lineage; refuse tampered or future-derived values."""
    expected = build_dataset(bars, market, liquid_n=liquid_n).set_index(
        ["date", "symbol"]
    )
    actual = dataset.set_index(["date", "symbol"])
    cols = list(MODEL_FEATURES) + [
        f"{p}_{h}" for h in HORIZONS for p in ("return", "relative_return", "label_end")
    ]
    try:
        pd.testing.assert_frame_equal(actual[cols], expected.loc[actual.index, cols])
    except (AssertionError, KeyError) as exc:
        raise LeakageError("feature/label lineage does not match closed bars") from exc
