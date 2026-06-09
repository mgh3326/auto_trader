"""ROB-382 — ported signal logic for ElliotV7 (EWO + SMA-offset dip, 1h uptrend filter).

Faithful port of the ElliotV5_SMA shape as published in ElliotV7.py (@Rallipanos). ONLY the
signal logic is ported — no freqtrade / talib / pandas / qtpylib / execution machinery. The
ROI/stoploss CONFIG is not re-implemented; only the published numbers needed for EXIT_MODEL
(signal exit + hard stop -0.32 + 24h max-hold) are carried over.

Source mapping (ElliotV7.populate_indicators / _buy_trend / _sell_trend):
  ma_buy_14   = ta.EMA(close, 14)          # base_nb_candles_buy=14
  ma_sell_24  = ta.EMA(close, 24)          # base_nb_candles_sell=24
  EWO         = (EMA(close,50) - EMA(close,200)) / close * 100   # fast_ewo=50, slow_ewo=200
  rsi_fast    = ta.RSI(close, 4)
  rsi         = ta.RSI(close, 14)
  rsi_slow    = ta.RSI(close, 20)
  sma_9       = ta.SMA(close, 9)
  hma_50      = qtpylib.hull_moving_average(close, 50)
  uptrend_1h  = merge_informative_pair(ffill) of (EMA(close_1h,20) > EMA(close_1h,25)) as int

Causality: ElliotV7 uses NO ``.shift(k)`` in either populate_*_trend — every gate reads the
SAME bar's columns, so signals[i] depends only on bars[0..i]. The 1h merge is the lookahead-
safe ``merge_informative`` (last FULLY-CLOSED 1h bar as of the 5m bar's open time).
"""
from __future__ import annotations

import rob382_backtest
import rob382_indicators as I
from rob382_bars import OHLCVBar

# ----------------------------------------------------------------------------- #
# Strategy-published parameters (ElliotV7 buy_params / sell_params + protections)
# ----------------------------------------------------------------------------- #
# Buy hyperspace params
BASE_NB_CANDLES_BUY = 14
EWO_HIGH = 2.327
EWO_LOW = -19.988
LOW_OFFSET = 0.975
RSI_BUY = 69

# Sell hyperspace params
BASE_NB_CANDLES_SELL = 24
HIGH_OFFSET = 0.991
HIGH_OFFSET_2 = 0.997

# Protection (EWO window)
FAST_EWO = 50
SLOW_EWO = 200

# ----------------------------------------------------------------------------- #
# Module interface
# ----------------------------------------------------------------------------- #
NATIVE_INTERVAL = "5m"
NEEDS_INFORMATIVE_1H = True
EXIT_MODEL = rob382_backtest.ExitModel(
    type="signal",
    hard_sl_pct=0.32,  # published stoploss -0.32
    max_hold_bars=288,  # 288 * 5m == 24h generous time cap
)
HOLD_SEMANTICS = (
    "signal exit preserved (ElliotV7 populate_sell_trend); hard stop -32% bounds ruin; "
    "24h max-hold bounds the spike. 5m native + 1h informative uptrend; horizon NOT changed."
)


def _uptrend_1h_values(bars_1h: list[OHLCVBar]) -> list[float]:
    """1.0 where EMA(close_1h,20) > EMA(close_1h,25) else 0.0 (NAN during warmup)."""
    closes_1h = [b.close for b in bars_1h]
    ema_fast = I.ema(closes_1h, 20)
    ema_slow = I.ema(closes_1h, 25)
    out: list[float] = []
    for ef, es in zip(ema_fast, ema_slow, strict=True):
        if ef == ef and es == es:  # both valid (NAN != NAN)
            out.append(1.0 if ef > es else 0.0)
        else:
            out.append(float("nan"))
    return out


def signals(
    bars: list[OHLCVBar], bars_1h: list[OHLCVBar] | None = None
) -> tuple[list[bool], list[bool]]:
    """Return (entry, exit_signal) boolean lists aligned to ``bars`` (causal)."""
    assert bars_1h is not None, "ElliotV7 requires the 1h informative pair (bars_1h)"
    n = len(bars)
    entry = [False] * n
    exit_sig = [False] * n
    if n == 0:
        return entry, exit_sig

    closes = [b.close for b in bars]

    ma_buy = I.ema(closes, BASE_NB_CANDLES_BUY)  # EMA 14
    ma_sell = I.ema(closes, BASE_NB_CANDLES_SELL)  # EMA 24
    ewo = I.ewo(closes, FAST_EWO, SLOW_EWO)
    rsi_fast = I.rsi(closes, 4)
    rsi = I.rsi(closes, 14)
    rsi_slow = I.rsi(closes, 20)
    sma9 = I.sma(closes, 9)
    hma50 = I.hma(closes, 50)

    # 1h uptrend, lookahead-safe merge onto the 5m base timeline.
    uptrend_vals = _uptrend_1h_values(bars_1h)
    uptrend_1h = I.merge_informative(
        [b.ts for b in bars],
        [b.close_ts for b in bars_1h],
        uptrend_vals,
    )

    def v(x: float) -> bool:
        return x == x  # NAN != NAN

    for i in range(n):
        c = closes[i]
        vol = bars[i].volume
        ut = uptrend_1h[i]
        mb = ma_buy[i]
        ms = ma_sell[i]
        e = ewo[i]
        rf = rsi_fast[i]
        r = rsi[i]
        rs = rsi_slow[i]
        s9 = sma9[i]
        h50 = hma50[i]

        # ---- Entry (populate_buy_trend): condA OR condB ----
        if v(ut) and v(mb) and v(ms) and v(rf):
            base_dip = (
                ut > 0
                and rf < 35
                and c < mb * LOW_OFFSET
                and vol > 0
                and c < ms * HIGH_OFFSET
            )
            if base_dip:
                # condA: EWO high regime + rsi gate
                condA = v(e) and v(r) and e > EWO_HIGH and r < RSI_BUY
                # condB: EWO deeply negative regime (no rsi gate)
                condB = v(e) and e < EWO_LOW
                if condA or condB:
                    entry[i] = True

        # ---- Exit (populate_sell_trend): c1 OR c2 ----
        if v(s9) and v(h50) and v(ms) and v(rf) and v(rs):
            # c1: sma9 > hma50 & close > ma_sell*0.997 & rsi>50 & vol>0 & rsi_fast>rsi_slow
            c1 = (
                s9 > h50
                and c > ms * HIGH_OFFSET_2
                and v(r)
                and r > 50
                and vol > 0
                and rf > rs
            )
            # c2: sma9 < hma50 & close > ma_sell*0.991 & vol>0 & rsi_fast>rsi_slow
            c2 = s9 < h50 and c > ms * HIGH_OFFSET and vol > 0 and rf > rs
            if c1 or c2:
                exit_sig[i] = True

    return entry, exit_sig
