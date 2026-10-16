"""Pure judgement functions shared by Q1-Q4 (unit-tested, no I/O).

Conventions (each one is pinned by a test):

* A *bar* is one row of a ``prices`` frame. ``close_ts`` is when it completes.
* "Known at time t" bars are those with ``close_ts <= t``. The decision bar is
  the last such bar; its close is the decision-time reference price.
* "After t" bars are those with ``close_ts > t`` -- the first one is the
  session in progress at ``t`` (or the next one). A DAY order placed at ``t``
  lives in exactly that first bar, so ``N = 1`` means ``window[0]`` only.
* A resting buy limit at ``anchor`` is *touched* on the first bar whose
  ``low <= anchor`` (inclusive: a print at the limit is a touch).
* Post-touch return at horizon ``h`` uses the close ``h`` bars after the touch
  bar (touch bar = day 0) against the anchor as the entry price; if that bar
  does not exist yet the observation is censored (``None``), never 0.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

import numpy as np
import pandas as pd

from app.mcp_server.tick_size import adjust_tick_size_kr, get_tick_size_kr

# ---------------------------------------------------------------- bar windows


def _ns(series: pd.Series) -> np.ndarray:
    """UTC epoch nanoseconds of a (tz-aware) timestamp column."""

    return series.to_numpy(dtype="datetime64[ns]").view("i8")


def _ts_ns(at: datetime | pd.Timestamp) -> int:
    return pd.Timestamp(at).value


def decision_index(bars: pd.DataFrame, at: datetime) -> int | None:
    """Position of the last bar complete at ``at`` (``close_ts <= at``)."""

    close = _ns(bars["close_ts"])
    n = int(np.searchsorted(close, _ts_ns(at), side="right"))
    return n - 1 if n > 0 else None


def forward_positions(bars: pd.DataFrame, at: datetime, n: int) -> list[int]:
    """Positions of the first ``n`` bars not yet complete at ``at``."""

    if n < 1:
        raise ValueError("n must be >= 1")
    close = _ns(bars["close_ts"])
    first = int(np.searchsorted(close, _ts_ns(at), side="right"))
    return list(range(first, min(first + n, len(close))))


def first_touch(lows: Sequence[float | None], anchor: float) -> int | None:
    """Offset of the first ``low <= anchor`` in ``lows`` (inclusive).

    ``None``/NaN entries (a session with no tradable time left) never touch.
    """

    for offset, low in enumerate(lows):
        if low is not None and not math.isnan(low) and low <= anchor:
            return offset
    return None


def forward_return(
    closes: Sequence[float], day0: int, horizon: int, entry: float
) -> float | None:
    """``closes[day0 + horizon] / entry - 1`` in percent; ``None`` if censored."""

    if entry <= 0:
        raise ValueError("entry must be positive")
    pos = day0 + horizon
    if pos >= len(closes):
        return None
    return (float(closes[pos]) / entry - 1.0) * 100.0


@dataclass(frozen=True, slots=True)
class PointInTime:
    """What a live session could see at ``at``.

    ``history`` = completed daily bars, plus -- when ``at`` falls inside a
    session -- one *partial* bar rebuilt from the hourly bars of that session
    that had completed by ``at`` (open = first hour open, high/low = running
    extremes, close = last completed hour close). This mirrors the live tools,
    whose daily frame ends with today's in-progress candle.

    ``price_basis``: ``intraday_partial`` | ``prior_close`` (no session in
    progress) | ``prior_close_no_hourly`` (session in progress but no completed
    hourly bar was available).
    """

    history: pd.DataFrame
    price_basis: str
    session_pos: int | None  # daily position of the in-progress session


def point_in_time(
    daily: pd.DataFrame, hourly: pd.DataFrame | None, at: datetime
) -> PointInTime | None:
    t = _ts_ns(at)
    pos = decision_index(daily, at)
    if pos is None:
        return None
    cols = ["open", "high", "low", "close", "volume"]
    history = daily.iloc[: pos + 1][cols].reset_index(drop=True)
    nxt = pos + 1
    if nxt >= len(daily):
        return PointInTime(history, "prior_close", None)
    open_ns = _ns(daily["open_ts"].iloc[nxt : nxt + 1])[0]
    close_ns = _ns(daily["close_ts"].iloc[nxt : nxt + 1])[0]
    if not open_ns <= t < close_ns:
        return PointInTime(history, "prior_close", None)
    session_date = daily["session_date"].iloc[nxt]
    idx = np.array([], dtype=int)
    if hourly is not None:
        idx = np.flatnonzero(
            (hourly["session_date"].to_numpy() == session_date)
            & (_ns(hourly["end_ts"]) <= t)
        )
    if len(idx) == 0:
        return PointInTime(history, "prior_close_no_hourly", nxt)
    o = hourly["open"].to_numpy(float)[idx]
    h = hourly["high"].to_numpy(float)[idx]
    lo = hourly["low"].to_numpy(float)[idx]
    c = hourly["close"].to_numpy(float)[idx]
    v = hourly["volume"].to_numpy(float)[idx]
    partial = pd.DataFrame(
        [
            {
                "open": o[0],
                "high": h.max(),
                "low": lo.min(),
                "close": c[-1],
                "volume": v.sum(),
            }
        ]
    )
    return PointInTime(
        pd.concat([history, partial], ignore_index=True), "intraday_partial", nxt
    )


def low_after(
    daily: pd.DataFrame,
    hourly: pd.DataFrame | None,
    pos: int,
    start: datetime,
    end: datetime | None = None,
    *,
    inclusive: bool = False,
) -> tuple[float | None, str]:
    """Lowest price of daily bar ``pos`` traded in ``[start, end]``.

    Uses hourly bars that *start* at/after ``start`` and *end* at/before
    ``end`` (conservative: an hour straddling either boundary is excluded
    because its low may fall outside the interval). ``inclusive=True`` keeps
    every hour overlapping the interval (optimistic bound). When ``start`` is at/before the
    session open and ``end`` at/after its close, the daily low is exact.
    Returns ``(low, basis)`` with basis ``daily`` | ``hourly`` |
    ``hourly_empty`` | ``daily_fallback`` (session partly covered but no
    hourly data).
    """

    bar_open = _ns(daily["open_ts"].iloc[pos : pos + 1])[0]
    bar_close = _ns(daily["close_ts"].iloc[pos : pos + 1])[0]
    daily_low = float(daily["low"].iloc[pos])
    s = _ts_ns(start)
    e = _ts_ns(end) if end is not None else bar_close
    if s <= bar_open and e >= bar_close:
        return daily_low, "daily"
    if hourly is None:
        return daily_low, "daily_fallback"
    day = hourly["session_date"].to_numpy() == daily["session_date"].iloc[pos]
    if not day.any():
        return daily_low, "daily_fallback"
    start_ns = _ns(hourly["start_ts"])
    end_ns = _ns(hourly["end_ts"])
    if inclusive:
        # Every hour overlapping [start, end] (optimistic at both ends).
        keep = (end_ns > s) & (start_ns < e)
    else:
        # Only hours wholly inside [start, end] (conservative at both ends).
        keep = (start_ns >= s) & (end_ns <= e)
    idx = np.flatnonzero(day & keep)
    if len(idx) == 0:
        return None, "hourly_empty"
    return float(hourly["low"].to_numpy(float)[idx].min()), "hourly"


# ------------------------------------------------------------- order legs

FILL_RULES = ("touch", "trade_through")


def leg_filled(market: str, low: float | None, limit: float, rule: str) -> bool:
    """Would a resting buy limit fill given the period's ``low``?

    * ``touch`` (optimistic): ``low <= limit`` -- any print at the limit fills.
    * ``trade_through`` (conservative): ``low <= limit - 1 tick`` -- price must
      trade at least one tick below the limit, so the queue at the limit is
      certainly exhausted.
    """

    if rule not in FILL_RULES:
        raise ValueError(f"unknown fill rule {rule!r}")
    if low is None or math.isnan(low):
        return False
    if rule == "touch":
        return low <= limit
    return low <= limit - tick_size(market, limit) + 1e-12 * limit


@dataclass(frozen=True, slots=True)
class LegFill:
    filled: tuple[bool, ...]
    filled_weight: float  # share of the budget notional that filled
    any_fill: bool


def fill_legs(
    market: str, low: float | None, legs: Sequence[tuple[float, float]], rule: str
) -> LegFill:
    """``legs`` = ``[(limit, budget_weight), ...]``; weights sum to 1."""

    if not legs or abs(sum(w for _, w in legs) - 1.0) > 1e-9:
        raise ValueError("leg weights must sum to 1")
    filled = tuple(leg_filled(market, low, p, rule) for p, _ in legs)
    weight = sum(w for (_, w), f in zip(legs, filled, strict=True) if f)
    return LegFill(filled, weight, any(filled))


def legs_return(
    close: float, legs: Sequence[tuple[float, float]], filled: Sequence[bool]
) -> float | None:
    """Notional-weighted return (%) of the filled legs at ``close``."""

    pairs = [(p, w) for (p, w), f in zip(legs, filled, strict=True) if f]
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    return sum(w * (close / p - 1.0) for p, w in pairs) / total * 100.0


def expected_bench_return_pct(log_returns: Sequence[float], sessions: int) -> float:
    """Ex-ante benchmark expectation over ``sessions`` from past daily log returns.

    Lognormal mean: ``exp((mu + sigma^2 / 2) * t) - 1`` with ``mu`` / ``sigma^2``
    the sample mean / variance (ddof=1) of the supplied *past* returns.
    """

    r = [float(x) for x in log_returns if x is not None and not math.isnan(x)]
    if len(r) < 2:
        raise ValueError("need at least two past returns")
    mu = sum(r) / len(r)
    var = sum((x - mu) ** 2 for x in r) / (len(r) - 1)
    return (math.exp((mu + var / 2.0) * sessions) - 1.0) * 100.0


# ------------------------------------------------------------ excess hit (Q4)


def required_move_pct(base: float, target: float) -> float:
    if base <= 0:
        raise ValueError("base must be positive")
    return (target / base - 1.0) * 100.0


def excess_hit(
    *,
    direction: str,
    base: float,
    target: float,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    bench_base: float,
    bench_closes: Sequence[float],
    mode: str,
) -> bool:
    """Benchmark-relative version of the window-touch forecast rule.

    ``r = target/base - 1``. Per window day ``t`` the stock path is ``high_t``
    (``at_or_above``) or ``low_t`` (``at_or_below``) relative to ``base``; the
    benchmark path is its close relative to ``bench_base``.

    * ``mode="touch"``: hit if on any day ``stock_t - bench_t`` crosses ``r``
      (>= for at_or_above, <= for at_or_below). With a flat benchmark this
      collapses exactly to the original ``high >= target`` / ``low <= target``.
    * ``mode="end"``: hit if the last close's excess return crosses ``r``.
    """

    if direction not in ("at_or_above", "at_or_below"):
        raise ValueError(f"unsupported direction {direction!r}")
    n = len(closes)
    if not (len(highs) == len(lows) == n == len(bench_closes)) or n == 0:
        raise ValueError("window series must be non-empty and aligned")
    r = required_move_pct(base, target)
    bench = [(float(c) / bench_base - 1.0) * 100.0 for c in bench_closes]
    if mode == "end":
        excess = (float(closes[-1]) / base - 1.0) * 100.0 - bench[-1]
        return excess >= r if direction == "at_or_above" else excess <= r
    if mode != "touch":
        raise ValueError(f"unknown mode {mode!r}")
    path = highs if direction == "at_or_above" else lows
    for p, b in zip(path, bench, strict=True):
        excess = (float(p) / base - 1.0) * 100.0 - b
        if direction == "at_or_above" and excess >= r:
            return True
        if direction == "at_or_below" and excess <= r:
            return True
    return False


# ---------------------------------------------------------------- tick floor


def _upbit_unit(price: float) -> float:
    # Upbit KRW price-unit table, identical to the boundaries documented in
    # app.services.brokers.upbit.orders.adjust_price_to_upbit_unit (that helper
    # rounds to nearest; a buy anchor needs a floor, so only the unit is read).
    for floor, unit in (
        (2_000_000, 1000),
        (1_000_000, 500),
        (500_000, 100),
        (100_000, 50),
        (10_000, 10),
        (1_000, 5),
        (100, 1),
        (10, 0.1),
        (1, 0.01),
        (0.1, 0.001),
        (0.01, 0.0001),
    ):
        if price >= floor:
            return unit
    return 0.00001


def tick_floor(market: str, price: float) -> float:
    """Buy-side tick floor: KRX via the app helper, US 0.01, Upbit KRW unit."""

    if price <= 0:
        raise ValueError("price must be positive")
    if market == "kr":
        return float(adjust_tick_size_kr(price, "buy"))
    if market == "us":
        return math.floor(round(price * 100, 6)) / 100
    if market == "crypto":
        unit = _upbit_unit(price)
        return round(math.floor(round(price / unit, 6)) * unit, 8)
    raise ValueError(f"unknown market {market!r}")


# ------------------------------------------------------- support gate (Q1)

STRENGTH_RANK = {"weak": 0, "moderate": 1, "strong": 2}


@dataclass(frozen=True, slots=True)
class GateVariant:
    name: str
    strength_min: str = "moderate"  # screen.support_strength_min
    families_min: int = 2  # screen.independent_support_source_count_min
    within_pct_max: float = 8.0  # screen.support_within_pct
    deep_min_pct: float = 3.0  # buy.deep_limit_pct_range upper edge (-3)
    deep_max_pct: float = 12.0  # buy.deep_limit_pct_range lower edge (-12)
    deep_min_inclusive: bool = True


VARIANT_NAMES = (
    "baseline",
    "V1_families_1",
    "V2_strength_weak",
    "V3_within_12",
    "V4_deep_upper_0",
    "V1+V2+V3",
    "V1+V2+V3+V4",
)


def build_variant(name: str) -> GateVariant:
    """Policy 2026-09-30.1 baseline with the named relaxations applied.

    V1 families 2->1 · V2 strength moderate->weak · V3 support_within 8->12 ·
    V4 buy.deep_limit_pct_range upper edge -3 -> 0 (exclusive, near supports).
    A combo name applies every token it contains.
    """

    if name not in VARIANT_NAMES:
        raise ValueError(f"unknown variant {name!r}")
    v = GateVariant(name)
    if "V1" in name:
        v = replace(v, families_min=1)
    if "V2" in name:
        v = replace(v, strength_min="weak")
    if "V3" in name:
        v = replace(v, within_pct_max=12.0)
    if "V4" in name:
        v = replace(v, deep_min_pct=0.0, deep_min_inclusive=False)
    return v


@dataclass(frozen=True, slots=True)
class SupportLevel:
    price: float
    strength: str
    families: int
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Reconstruction:
    """Point-in-time output of the live S/R + RSI code for one rejection."""

    current_price: float
    rsi: float | None
    supports: tuple[SupportLevel, ...]
    price_basis: str


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    level: SupportLevel | None
    anchor: float | None
    anchor_distance_pct: float | None


def evaluate_support_gate(
    market: str,
    supports: Sequence[SupportLevel],
    current_price: float,
    variant: GateVariant,
) -> GateResult:
    """Regular-discovery support gate + deep-limit anchor.

    A level qualifies when, below the current price: strength >= min, families
    >= min, support distance <= ``within_pct_max``, and the tick-floored anchor
    distance lies inside the deep-limit band. Among qualifying levels the
    strongest wins, ties -> nearest (same rule as the ROB-1301 backtest).
    """

    if current_price <= 0:
        raise ValueError("current_price must be positive")
    qualifying: list[tuple[SupportLevel, float, float]] = []
    for level in supports:
        if level.price <= 0 or level.price >= current_price:
            continue
        if STRENGTH_RANK.get(level.strength, -1) < STRENGTH_RANK[variant.strength_min]:
            continue
        if level.families < variant.families_min:
            continue
        support_dist = (current_price - level.price) / current_price * 100.0
        if support_dist > variant.within_pct_max:
            continue
        anchor = tick_floor(market, level.price)
        anchor_dist = (current_price - anchor) / current_price * 100.0
        lower_ok = (
            anchor_dist >= variant.deep_min_pct
            if variant.deep_min_inclusive
            else anchor_dist > variant.deep_min_pct
        )
        if not lower_ok or anchor_dist > variant.deep_max_pct:
            continue
        qualifying.append((level, anchor, anchor_dist))
    if not qualifying:
        return GateResult(False, None, None, None)
    level, anchor, dist = min(
        qualifying, key=lambda q: (-STRENGTH_RANK[q[0].strength], q[2])
    )
    return GateResult(True, level, anchor, dist)


SUPPORT_GATE_CODES = (
    "support_strength",
    "independent_sources",
    "support_within_pct",
    "deep_limit_band_shallow",  # anchor above -3% (too close to the price)
    "deep_limit_band_deep",  # anchor below -12%
    "no_support_below",
)


def level_failures(
    market: str, level: SupportLevel, current_price: float, variant: GateVariant
) -> frozenset[str]:
    """Which support sub-gates one level fails under ``variant``."""

    fails: set[str] = set()
    if STRENGTH_RANK.get(level.strength, -1) < STRENGTH_RANK[variant.strength_min]:
        fails.add("support_strength")
    if level.families < variant.families_min:
        fails.add("independent_sources")
    if (current_price - level.price) / current_price * 100.0 > variant.within_pct_max:
        fails.add("support_within_pct")
    anchor = tick_floor(market, level.price)
    dist = (current_price - anchor) / current_price * 100.0
    shallow = (
        dist < variant.deep_min_pct
        if variant.deep_min_inclusive
        else dist <= variant.deep_min_pct
    )
    if shallow:
        fails.add("deep_limit_band_shallow")
    if dist > variant.deep_max_pct:
        fails.add("deep_limit_band_deep")
    return frozenset(fails)


def recomputed_support_failures(
    market: str,
    supports: Sequence[SupportLevel],
    current_price: float,
    variant: GateVariant,
) -> tuple[frozenset[str], SupportLevel | None]:
    """Failed support sub-gates of the *closest-to-passing* level.

    The level with the fewest failed sub-gates is chosen (ties: strongest, then
    nearest). An empty set means the support gate passes. No level below the
    price at all -> ``{"no_support_below"}``.
    """

    below = [lv for lv in supports if 0 < lv.price < current_price]
    if not below:
        return frozenset({"no_support_below"}), None
    scored = [(level_failures(market, lv, current_price, variant), lv) for lv in below]
    fails, level = min(
        scored,
        key=lambda x: (
            len(x[0]),
            -STRENGTH_RANK.get(x[1].strength, -1),
            current_price - x[1].price,
        ),
    )
    return fails, level


def tick_size(market: str, price: float) -> float:
    """One price increment at ``price`` (KRX via the app table, US 0.01, Upbit unit)."""

    if market == "kr":
        return float(get_tick_size_kr(price))
    if market == "us":
        return 0.01
    if market == "crypto":
        return _upbit_unit(price)
    raise ValueError(f"unknown market {market!r}")


# ------------------------------------------------- reason-text classifiers

_DEEP_TOKEN = re.compile(
    r"deep[_ ]limit|deep band|\[\s*[−-]\s*12\s*,\s*[−-]\s*3\s*\]|"
    r"buy\.deep_limit_pct_range",
    re.IGNORECASE,
)
_OUT_TOKEN = re.compile(
    r"밖|outside|EXCLUDE|closer than|불성립|앵커 불|anchor 불|no anchor|"
    r"너무 얕|out of (the )?band|미달",
    re.IGNORECASE,
)
_INSIDE_ONLY = re.compile(r"inside the deep band|밴드 안", re.IGNORECASE)
_RSI = re.compile(r"RSI(?:14|-14| ?14)?\s*[:=]?\s*(\d{1,2}(?:\.\d+)?)")


def classify_deep_limit_out(reason: str | None) -> bool:
    """True when the reason says the anchor sat outside buy.deep_limit_pct_range.

    Needs a deep-limit token *and* an out/exclude token within the same clause
    window (±120 chars). A bare "inside the deep band" never counts.
    """

    if not reason:
        return False
    for m in _DEEP_TOKEN.finditer(reason):
        lo = max(0, m.start() - 120)
        window = reason[lo : m.end() + 120]
        if _OUT_TOKEN.search(window):
            if _INSIDE_ONLY.search(window) and not re.search(
                r"밖|outside|EXCLUDE|closer than", window, re.IGNORECASE
            ):
                continue
            return True
    return False


def parse_reason_rsi(reason: str | None) -> float | None:
    if not reason:
        return None
    m = _RSI.search(reason)
    if not m:
        return None
    value = float(m.group(1))
    return value if 0 < value < 100 else None
