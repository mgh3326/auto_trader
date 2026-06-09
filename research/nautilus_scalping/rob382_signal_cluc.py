"""ROB-382 — ClucHAnix signal port (Heikin-Ashi + Bollinger squeeze dip + 1h ROCR; fisher exit).

Faithful port of the SIGNAL logic only (no freqtrade / talib / qtpylib / pandas import) from
the canonical public source ``ClucHAnix.py`` (@PeetCrypto freqtrade-stuff). Entry/exit
conditions and default buy/sell params match the source's ``buy_params`` / ``sell_params``.

NATIVE_INTERVAL='1m' with a 1h informative ``rocr_1h`` gate (preserved, not coerced).

Causality (ROB-382 hard rule): every freqtrade ``.shift(k)`` here is PAST (k>0):
  ``x.shift(1)[i] == x[i-1]``. ``signals[i]`` reads only ``bars[0..i]``. The 1h informative
  uses ``merge_informative`` (last fully-closed 1h bar as of base ts → lookahead-safe).

Source mapping (ClucHAnix.populate_indicators / populate_buy_trend / populate_sell_trend):
  * heikinashi via qtpylib              -> I.heikin_ashi(opens,highs,lows,closes)
  * bollinger_bands(ha_typical_price, 40, 2)  (mean/std over (ha_high+ha_low+ha_close)/3, ddof=1)
        NOTE: source wraps mid/lower in np.nan_to_num(...) -> NaN warmup becomes 0.0. We
        replicate that (warmup mid/lower == 0.0) so the published ``lower.shift().gt(0)`` and
        ``close_bblower * bb_lowerband`` semantics match exactly.
  * bbdelta = |mid - lower|; closedelta = |ha_close - ha_close.shift|; tail = |ha_close - ha_low|
  * ema_fast = EMA(ha_close, 3); ema_slow = EMA(ha_close, 50)
  * rsi = RSI(close, 14) on the REAL close; fisher = (e^2x - 1)/(e^2x + 1), x = 0.1*(rsi-50)
  * informative 1h: ha_close_1h = heikinashi(1h)[close]; rocr = ROCR(ha_close_1h, 168);
        merged onto base via merge_informative_pair (ffill, lookahead-safe).

Exit: the published signal cluster (fisher + descending HA highs + ema_fast above + bb-middle
proximity). Custom trailing-stop + minimal_roi {"70": 0} time-exit are NOT modeled; approximated
by the published hard stop (pHSL = -0.32) + a 24h (1440 x 1m) max-hold cap. The 1m native
timeframe and the 1h informative are preserved; only the exit MECHANISM is approximated, so
``horizon_changed_during_port`` is True.
"""
from __future__ import annotations

import rob382_backtest
import rob382_indicators as I

# --- source default params (ClucHAnix.buy_params / sell_params) --------------------------- #
ROCR_1H = 0.54904
BBDELTA_CLOSE = 0.01965
CLOSEDELTA_CLOSE = 0.00556
BBDELTA_TAIL = 0.95089
CLOSE_BBLOWER = 0.00799

SELL_FISHER = 0.38414
SELL_BBMIDDLE_CLOSE = 1.07634

# Bollinger config (source: window_size=40, num_of_std=2 over ha_typical_price)
BB_WINDOW = 40
BB_NUM_STD = 2.0
EMA_FAST = 3
EMA_SLOW = 50
RSI_LEN = 14
ROCR_1H_LEN = 168

# --- public interface --------------------------------------------------------------------- #
NATIVE_INTERVAL = "1m"
NEEDS_INFORMATIVE_1H = True

# Published pHSL = -0.32 (hard stop). Custom trailing-stop + minimal_roi {"70": 0} time-exit
# NOT modeled -> approximated by hard stop + 24h (1440 x 1m) max-hold cap.
EXIT_MODEL = rob382_backtest.ExitModel(type="signal", hard_sl_pct=0.32, max_hold_bars=1440)

HOLD_SEMANTICS = (
    "signal exit (fisher cluster) preserved; published hard stop -32%; custom trailing-stop "
    "and minimal_roi {70:0} time-exit NOT modeled, approximated by hard-stop + 24h max-hold. "
    "1m native + 1h informative."
)


def _nan_to_num(xs: list[float]) -> list[float]:
    """Mirror np.nan_to_num: NaN -> 0.0 (source wraps bollinger mid/lower in nan_to_num)."""
    return [x if (x == x) else 0.0 for x in xs]


def signals(bars, bars_1h=None):
    """Return (entry, exit_signal) boolean lists aligned to ``bars`` (causal)."""
    assert bars_1h is not None, "ClucHAnix requires the 1h informative (rocr_1h)"
    n = len(bars)
    if n == 0:
        return [], []

    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]

    ha_open, ha_high, ha_low, ha_close = I.heikin_ashi(opens, highs, lows, closes)

    # Bollinger over Heikin-Ashi typical price (ha_high + ha_low + ha_close)/3
    ha_typ = [(ha_high[i] + ha_low[i] + ha_close[i]) / 3.0 for i in range(n)]
    mid_raw, lower_raw, _upper = I.bollinger(ha_typ, BB_WINDOW, BB_NUM_STD, ddof=1)
    # Source applies np.nan_to_num to mid/lower (warmup NaN -> 0.0).
    mid = _nan_to_num(mid_raw)
    lower = _nan_to_num(lower_raw)

    # bbdelta = |mid - lower|; closedelta = |ha_close - ha_close.shift|; tail = |ha_close - ha_low|
    bbdelta = [abs(mid[i] - lower[i]) for i in range(n)]
    closedelta = [float("nan")] * n
    for i in range(1, n):
        closedelta[i] = abs(ha_close[i] - ha_close[i - 1])
    tail = [abs(ha_close[i] - ha_low[i]) for i in range(n)]

    ema_fast = I.ema(ha_close, EMA_FAST)
    ema_slow = I.ema(ha_close, EMA_SLOW)

    rsi = I.rsi(closes, RSI_LEN)  # RSI on REAL close (source: ta.RSI(dataframe) -> default close,14)
    fisher = I.fisher_from_rsi(rsi)

    # --- 1h informative rocr_1h (lookahead-safe merge) ------------------------------------- #
    o1h = [b.open for b in bars_1h]
    h1h = [b.high for b in bars_1h]
    l1h = [b.low for b in bars_1h]
    c1h = [b.close for b in bars_1h]
    ha_close_1h = I.heikin_ashi(o1h, h1h, l1h, c1h)[3]
    rocr_1h_value = I.rocr(ha_close_1h, ROCR_1H_LEN)
    rocr_1h = I.merge_informative(
        [b.ts for b in bars], [b.close_ts for b in bars_1h], rocr_1h_value
    )

    entry = [False] * n
    exit_sig = [False] * n

    for i in range(n):
        # --- ENTRY ----------------------------------------------------------------------- #
        # rocr_1h gate (NaN-safe: NaN > x is False)
        rocr_ok = rocr_1h[i] == rocr_1h[i] and rocr_1h[i] > ROCR_1H

        if rocr_ok and i >= 1:
            lower_prev = lower[i - 1]  # lower.shift()
            ha_close_prev = ha_close[i - 1]  # ha_close.shift()

            cond_a = (
                lower_prev > 0
                and bbdelta[i] > ha_close[i] * BBDELTA_CLOSE
                and (closedelta[i] == closedelta[i])
                and closedelta[i] > ha_close[i] * CLOSEDELTA_CLOSE
                and tail[i] < bbdelta[i] * BBDELTA_TAIL
                and ha_close[i] < lower_prev
                and ha_close[i] <= ha_close_prev
            )

            # cond_b: ema_slow may be NaN during warmup -> comparison False (faithful: warmup no fire)
            ema_slow_i = ema_slow[i]
            cond_b = (
                ema_slow_i == ema_slow_i
                and ha_close[i] < ema_slow_i
                and ha_close[i] < CLOSE_BBLOWER * lower[i]  # bb_lowerband == lower
            )

            if cond_a or cond_b:
                entry[i] = True

        # --- EXIT ------------------------------------------------------------------------- #
        if i >= 2:
            ema_fast_i = ema_fast[i]
            fisher_i = fisher[i]
            if (
                fisher_i == fisher_i
                and fisher_i > SELL_FISHER
                and ha_high[i] <= ha_high[i - 1]
                and ha_high[i - 1] <= ha_high[i - 2]
                and ha_close[i] <= ha_close[i - 1]
                and ema_fast_i == ema_fast_i
                and ema_fast_i > ha_close[i]
                and (ha_close[i] * SELL_BBMIDDLE_CLOSE) > mid[i]  # bb_middleband == mid
                and volumes[i] > 0
            ):
                exit_sig[i] = True

    return entry, exit_sig
