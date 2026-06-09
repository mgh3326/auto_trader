"""ROB-382 — faithful signal port of ichiV1 (Ichimoku cloud + EMA fan-magnitude).

Source: https://raw.githubusercontent.com/PeetCrypto/freqtrade-stuff/main/IchisV1.py
(``class ichiV1``, @author 25-Jul-21). Only the SIGNAL logic is ported here — no
freqtrade / talib / qtpylib / pandas import, no ROI/stoploss/trailing config machinery
beyond the published numbers needed for ``EXIT_MODEL``.

Faithful nuance (matches the source ``populate_indicators`` exactly):
  * The strategy overwrites OHLC open/high/low with the Heikin-Ashi values but KEEPS the
    REAL close (``dataframe['close'] = heikinashi['close']`` is commented out). So:
      - ``trend_close_*`` are EMAs of the REAL close,
      - ``trend_open_*``  are EMAs of the HA open,
      - ichimoku is computed on the HA high / HA low.
  * fan_magnitude = trend_close_1h / trend_close_8h = EMA(close,12) / EMA(close,96).
  * fan_magnitude_gain[i] = fan_magnitude[i] / fan_magnitude[i-1].

Causality: every condition at bar ``i`` uses only bars[0..i]. The ``.shift(x+1)`` in the
source is a PAST shift (``fan.shift(k)[i] == fan[i-k]``). The ichimoku senkou spans are
forward-displaced (span at i derives from i-displacement), so ``close > senkou_a`` is
causal (no lookahead). ``crossed_below`` uses only i and i-1.
"""
from __future__ import annotations

import rob382_backtest as bt
import rob382_indicators as I

# --- faithful interface contract ---
NATIVE_INTERVAL: str = "5m"  # ichiV1.timeframe = '5m' (its own timeframe — not changed)
NEEDS_INFORMATIVE_1H: bool = False

# ichiV1.stoploss = -0.275; minimal_roi has a 114-min time-exit "0" rung that we do NOT
# model (no time-based ROI in the harness) — bounded instead by the generous max-hold cap.
EXIT_MODEL = bt.ExitModel(type="signal", hard_sl_pct=0.275, max_hold_bars=288)

HOLD_SEMANTICS: str = (
    "signal exit (trend_close_5m crosses below trend_close_2h = close crosses below "
    "EMA(close,24)) preserved; hard stop -27.5% (published stoploss); 24h max-hold cap "
    "(minimal_roi 114-min time-exit not modeled). timeframe 5m native, horizon NOT changed."
)

# --- ichiV1 default hyperspace params (buy_params, settings as of 25-Jul-21) ---
_BUY_TREND_ABOVE_SENKOU_LEVEL = 1
_BUY_TREND_BULLISH_LEVEL = 6
_BUY_FAN_MAGNITUDE_SHIFT_VALUE = 3
_BUY_MIN_FAN_MAGNITUDE_GAIN = 1.002


def _v(x: float) -> bool:
    """Validity guard: NAN != NAN, and reject infinities."""
    return x == x and x not in (float("inf"), float("-inf"))


def signals(bars, bars_1h=None) -> tuple[list[bool], list[bool]]:
    """Return (entry, exit_signal) aligned to ``bars``, causal (signals[i] uses bars[0..i])."""
    n = len(bars)
    if n == 0:
        return [], []

    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]

    # Heikin-Ashi: source overwrites open/high/low (close stays REAL).
    ha_open, ha_high, ha_low, _ha_close = I.heikin_ashi(opens, highs, lows, closes)

    # trend_close_* — EMAs of the REAL close.
    # 5m=close, 15m=EMA3, 30m=EMA6, 1h=EMA12, 2h=EMA24, 4h=EMA48, 8h=EMA96
    tc_5m = closes
    tc_15m = I.ema(closes, 3)
    tc_30m = I.ema(closes, 6)
    tc_1h = I.ema(closes, 12)
    tc_2h = I.ema(closes, 24)
    tc_4h = I.ema(closes, 48)
    tc_8h = I.ema(closes, 96)

    # trend_open_* — EMAs of the HA open.
    to_5m = ha_open
    to_15m = I.ema(ha_open, 3)
    to_30m = I.ema(ha_open, 6)
    to_1h = I.ema(ha_open, 12)
    to_2h = I.ema(ha_open, 24)
    to_4h = I.ema(ha_open, 48)

    # fan_magnitude = trend_close_1h / trend_close_8h; gain = fan[i]/fan[i-1]
    fan = [
        (tc_1h[i] / tc_8h[i]) if (_v(tc_1h[i]) and _v(tc_8h[i]) and tc_8h[i] != 0) else float("nan")
        for i in range(n)
    ]
    fan_gain = [float("nan")] * n
    for i in range(1, n):
        if _v(fan[i]) and _v(fan[i - 1]) and fan[i - 1] != 0:
            fan_gain[i] = fan[i] / fan[i - 1]

    # ichimoku on HA high / HA low (conversion=20, base=60, lagging=120, displacement=30)
    _tenkan, _kijun, senkou_a, senkou_b = I.ichimoku(
        ha_high, ha_low, conversion=20, base=60, lagging=120, displacement=30
    )

    entry = [False] * n
    shift = _BUY_FAN_MAGNITUDE_SHIFT_VALUE  # 3

    for i in range(n):
        # ---- Trending market: trend_close_5m above the cloud (level >= 1) ----
        if not (_v(senkou_a[i]) and _v(senkou_b[i])):
            continue
        if not (tc_5m[i] > senkou_a[i] and tc_5m[i] > senkou_b[i]):
            continue

        # ---- Trends bullish (level 6): close-EMA > open-EMA across 5m..4h ----
        bulls = (
            (tc_5m, to_5m),
            (tc_15m, to_15m),
            (tc_30m, to_30m),
            (tc_1h, to_1h),
            (tc_2h, to_2h),
            (tc_4h, to_4h),
        )
        ok = True
        for c, o in bulls:
            if not (_v(c[i]) and _v(o[i]) and c[i] > o[i]):
                ok = False
                break
        if not ok:
            continue

        # ---- Trends magnitude ----
        if not (_v(fan_gain[i]) and fan_gain[i] >= _BUY_MIN_FAN_MAGNITUDE_GAIN):
            continue
        if not (_v(fan[i]) and fan[i] > 1):
            continue

        # fan strictly above each of the previous `shift` bars (x in range(shift): fan.shift(x+1) < fan)
        rising = True
        for k in range(1, shift + 1):
            j = i - k
            if j < 0 or not _v(fan[j]) or not (fan[j] < fan[i]):
                rising = False
                break
        if not rising:
            continue

        entry[i] = True

    # ---- Exit: trend_close_5m crosses below trend_close_2h (close x-under EMA(close,24)) ----
    exit_sig = I.crossed_below(tc_5m, tc_2h)

    return entry, exit_sig
