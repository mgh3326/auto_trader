"""Point-in-time features: 60 completed sessions, joined only when available.

``feature_join`` is the single way rows reach a model or a policy. It refuses a
row whose ``available_at`` is after its decision time (rules §2.1) and excludes
and counts a row with no ``available_at``. Terminal columns are never an input:
``_base_arrays`` reads only prices, volume and state.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.kr_boosting.panel import NAT, STATE_VALID, Panel, kst_us
from research.kr_delist_repair.model import AvailabilityViolation

LOOKBACK = 60
DECISION_HOUR, DECISION_MINUTE = 8, 30  # pre-open of the session after t

LAG_FEATURES = tuple(f"ret_lag_{k}" for k in range(1, LOOKBACK + 1)) + tuple(
    f"logtv_lag_{k}" for k in range(1, LOOKBACK + 1)
)
SUMMARY_FEATURES = (
    "ret_5",
    "ret_20",
    "ret_60",
    "rv_5",
    "rv_20",
    "rv_60",
    "range_20",
    "drawdown_60",
    "tv_mean_5",
    "tv_mean_20",
    "tv_mean_60",
    "tv_ratio_5_60",
    "valid_share_60",
    "is_kosdaq",
)
MARKET_FEATURES = (
    "mkt_ret_1",
    "mkt_ret_5",
    "mkt_ret_20",
    "mkt_ret_60",
    "mkt_rv_20",
    "mkt_breadth_1",
    "mkt_dispersion_1",
)
FEATURE_NAMES: tuple[str, ...] = LAG_FEATURES + SUMMARY_FEATURES + MARKET_FEATURES


def decision_times(panel: Panel) -> np.ndarray:
    """Decision time for each session ``t``: 08:30 KST of the next session.

    The last calendar session has no next session and so no decision (``NAT``).
    """

    out = np.full(len(panel.calendar), NAT, dtype=np.int64)
    for i, session in enumerate(panel.calendar[1:]):
        out[i] = kst_us(session, DECISION_HOUR, DECISION_MINUTE)
    return out


def _roll(frame: np.ndarray, window: int, how: str, min_periods: int) -> np.ndarray:
    """Rolling statistic over the last ``window`` sessions, ending at each session."""

    rolled = pd.DataFrame(frame.T).rolling(window, min_periods=min_periods)
    return getattr(rolled, how)().to_numpy().T


@dataclass(slots=True)
class FeatureStore:
    arrays: dict[str, np.ndarray]  # (L, S)
    market: dict[str, np.ndarray]  # (2, S): row 0 KOSPI, row 1 KOSDAQ
    ret: np.ndarray
    logtv: np.ndarray
    is_kosdaq: np.ndarray


def _base_arrays(panel: Panel) -> tuple[np.ndarray, np.ndarray]:
    valid = panel.state == STATE_VALID
    close = np.where(valid, panel.close, np.nan)
    prev = np.concatenate([np.full((close.shape[0], 1), np.nan), close[:, :-1]], axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.log(close / prev)
        traded = close * np.where(valid, panel.volume, np.nan)
        logtv = np.log1p(traded)
    return ret, logtv


def compute_features(panel: Panel) -> FeatureStore:
    ret, logtv = _base_arrays(panel)
    valid = panel.state == STATE_VALID
    close = np.where(valid, panel.close, np.nan)
    high = np.where(valid, panel.high, np.nan)
    low = np.where(valid, panel.low, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        range_pct = (high - low) / close

    def back(k: int) -> np.ndarray:
        return np.concatenate(
            [np.full((close.shape[0], k), np.nan), close[:, :-k]], axis=1
        )

    with np.errstate(divide="ignore", invalid="ignore"):
        arrays: dict[str, np.ndarray] = {
            "ret_5": np.log(close / back(5)),
            "ret_20": np.log(close / back(20)),
            "ret_60": np.log(close / back(60)),
            "drawdown_60": close / _roll(close, 60, "max", 20) - 1.0,
        }
    arrays["rv_5"] = _roll(ret, 5, "std", 3)
    arrays["rv_20"] = _roll(ret, 20, "std", 10)
    arrays["rv_60"] = _roll(ret, 60, "std", 30)
    arrays["range_20"] = _roll(range_pct, 20, "mean", 10)
    arrays["tv_mean_5"] = _roll(logtv, 5, "mean", 3)
    arrays["tv_mean_20"] = _roll(logtv, 20, "mean", 10)
    arrays["tv_mean_60"] = _roll(logtv, 60, "mean", 30)
    arrays["tv_ratio_5_60"] = arrays["tv_mean_5"] - arrays["tv_mean_60"]
    arrays["valid_share_60"] = _roll(valid.astype(float), 60, "mean", 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        traded = close * np.where(valid, panel.volume, np.nan)
    arrays["median_traded_20"] = _roll(traded, 20, "median", 10)

    market: dict[str, np.ndarray] = {
        name: np.full((2, ret.shape[1]), np.nan) for name in MARKET_FEATURES
    }
    for row, mask in enumerate((~panel.is_kosdaq, panel.is_kosdaq)):
        if not mask.any():
            continue
        sub = ret[mask]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN sessions
            mean = np.nanmean(sub, axis=0)
            market["mkt_ret_1"][row] = mean
            market["mkt_breadth_1"][row] = np.nanmean(
                np.where(np.isnan(sub), np.nan, sub > 0), axis=0
            )
            market["mkt_dispersion_1"][row] = np.nanstd(sub, axis=0)
        series = pd.Series(mean)
        market["mkt_ret_5"][row] = series.rolling(5, min_periods=3).sum().to_numpy()
        market["mkt_ret_20"][row] = series.rolling(20, min_periods=10).sum().to_numpy()
        market["mkt_ret_60"][row] = series.rolling(60, min_periods=30).sum().to_numpy()
        market["mkt_rv_20"][row] = series.rolling(20, min_periods=10).std().to_numpy()
    return FeatureStore(arrays, market, ret, logtv, panel.is_kosdaq)


def _matrix(
    store: FeatureStore, li: np.ndarray, t: np.ndarray, names: tuple[str, ...]
) -> np.ndarray:
    out = np.full((len(li), len(names)), np.nan, dtype=np.float32)
    market_row = store.is_kosdaq[li].astype(int)
    for j, name in enumerate(names):
        if name.startswith(("ret_lag_", "logtv_lag_")):
            base, k = name.rsplit("_lag_", 1)
            source = store.ret if base == "ret" else store.logtv
            at = t - int(k) + 1
            ok = at >= 0
            out[ok, j] = source[li[ok], at[ok]]
        elif name == "is_kosdaq":
            out[:, j] = store.is_kosdaq[li].astype(np.float32)
        elif name in store.market:
            out[:, j] = store.market[name][market_row, t]
        else:
            out[:, j] = store.arrays[name][li, t]
    return out


@dataclass(frozen=True, slots=True)
class JoinResult:
    frame: pd.DataFrame
    excluded_no_available_at: int


def feature_join(
    panel: Panel,
    store: FeatureStore,
    li: np.ndarray,
    t: np.ndarray,
    *,
    decision_time: np.ndarray | None = None,
    names: tuple[str, ...] = FEATURE_NAMES,
) -> JoinResult:
    """Features at rows ``(li, t)``. Refuses ``available_at > decision time``.

    A row with no ``available_at`` is excluded and counted, never assumed
    available. ``decision_time`` defaults to 08:30 KST of the session after
    ``t``; a row on the last session has no decision time and is refused.
    """

    li = np.asarray(li, dtype=np.int64)
    t = np.asarray(t, dtype=np.int64)
    decide = decision_times(panel)[t] if decision_time is None else decision_time
    available = panel.available_at[li, t]
    known = available != NAT
    if (decide[known] == NAT).any():
        raise AvailabilityViolation("a row on the last session has no decision time")
    late = known & (available > decide)
    if late.any():
        i = int(np.flatnonzero(late)[0])
        raise AvailabilityViolation(
            f"{panel.listing_ids[int(li[i])]} session {panel.calendar[int(t[i])]}: "
            f"available_at {int(available[i])} is after decision time {int(decide[i])}"
        )
    # Lag windows and market features read every earlier row and every peer on t.
    latest_read = np.maximum.accumulate(panel.available_at.max(axis=0))[t]
    used_late = known & (latest_read > decide)
    if used_late.any():
        i = int(np.flatnonzero(used_late)[0])
        raise AvailabilityViolation(
            f"session {panel.calendar[int(t[i])]}: a row the features read has "
            f"available_at {int(latest_read[i])} after decision time {int(decide[i])}"
        )
    keep_l, keep_t = li[known], t[known]
    matrix = _matrix(store, keep_l, keep_t, names)
    frame = pd.DataFrame(matrix, columns=list(names))
    frame.insert(0, "t", keep_t)
    frame.insert(0, "li", keep_l)
    frame["available_at"] = available[known]
    frame["decision_time"] = decide[known]
    return JoinResult(frame, int((~known).sum()))
