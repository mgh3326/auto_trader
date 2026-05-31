"""ROB-382 — ported signal: VWAPStrategy (@jilv220).

Faithful port of the SIGNAL logic only from /tmp/rob382_src/VWAP.py (no freqtrade /
talib / pandas_ta / pandas runtime import — repo boundary). Source shape:

    VWAP band dip + CTI + multi-period RSI cluster (5m native timeframe).

populate_indicators (source):
    vwap_low, vwap, vwap_high = VWAPB(dataframe, 20, 1)   # rolling_vwap(20); bands = vwap ± 1.0*std(vwap,20)
    tcp_percent_4 = top_percent_change(dataframe, 4)
    cti           = pta.cti(close, length=20)
    rsi           = RSI(close, 14)
    rsi_84        = RSI(close, 84)
    rsi_112       = RSI(close, 112)

populate_buy_trend (source — ALL must hold):
    close < vwap_low
    tcp_percent_4 > 0.04
    cti < -0.8
    rsi < 35
    rsi_84 < 60
    rsi_112 < 60
    volume > 0

populate_sell_trend (source): EMPTY → no exit signal. Exit is the PUBLISHED
``minimal_roi = {"0": 0.02}`` take-profit and ``stoploss = -0.15`` (NOT hyperopted) —
that IS the strategy's exit, so the native 5m horizon is preserved (not coerced).

Causality: every value at index i derives only from bars[0..i]. The indicator lib is
already causal; the VWAP band uses rolling_std OF the vwap series (a causal transform of
a causal series), and all RSI/CTI/tcp are backward-looking windows.
"""
from __future__ import annotations

import rob382_backtest as bt
import rob382_indicators as I

# ----------------------------------------------------------------------------- #
# Interface constants (faithful to the source's published parameters)
# ----------------------------------------------------------------------------- #
NATIVE_INTERVAL: str = "5m"  # source: timeframe = '5m'
NEEDS_INFORMATIVE_1H: bool = False  # source uses no informative pair

# Published exit: minimal_roi {"0": 0.02} (2% take-profit), stoploss -0.15 (-15%).
# Empty populate_sell_trend → roi_sl exit (no signal). max_hold_bars left at the
# generous shared default (288) to bound the spike without changing the horizon.
EXIT_MODEL: bt.ExitModel = bt.ExitModel(
    type="roi_sl",
    hard_sl_pct=0.15,
    roi_pct=0.02,
    max_hold_bars=288,
)

HOLD_SEMANTICS: str = (
    "No exit signal in source (empty populate_sell_trend); exit is the PUBLISHED "
    "minimal_roi 2% take-profit / stoploss -15% (not hyperopted). 5m native timeframe; "
    "this roi/sl IS the strategy's exit, so the horizon is NOT changed."
)

# Source default buy params (literals in populate_buy_trend).
_VWAP_WINDOW = 20
_VWAP_NUM_STD = 1.0
_TCP_LENGTH = 4
_TCP_THRESHOLD = 0.04
_CTI_LENGTH = 20
_CTI_MAX = -0.8
_RSI_FAST_LEN = 14
_RSI_FAST_MAX = 35
_RSI_MID_LEN = 84
_RSI_MID_MAX = 60
_RSI_SLOW_LEN = 112
_RSI_SLOW_MAX = 60


def _valid(x: float) -> bool:
    # NAN != NAN; guard against the warmup tail of every indicator.
    return x == x  # noqa: PLR0124


def signals(bars, bars_1h=None):  # noqa: ARG001 (bars_1h unused; NEEDS_INFORMATIVE_1H=False)
    """Return (entry, exit_signal), each a list[bool] aligned to ``bars``.

    Exit is empty (roi_sl handled by EXIT_MODEL), so exit_signal is all-False.
    """
    n = len(bars)
    if n == 0:
        return [], []

    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]

    # VWAPB(dataframe, 20, 1): rolling_vwap(20), then bands from the rolling std OF the vwap series.
    vwap = I.rolling_vwap(highs, lows, closes, volumes, _VWAP_WINDOW)
    vwap_sd = I.rolling_std(vwap, _VWAP_WINDOW)  # df['vwap'].rolling(20).std()
    vwap_low = [
        vwap[i] - _VWAP_NUM_STD * vwap_sd[i] if (_valid(vwap[i]) and _valid(vwap_sd[i])) else float("nan")
        for i in range(n)
    ]

    tcp4 = I.top_percent_change(opens, closes, _TCP_LENGTH)
    cti = I.cti(closes, _CTI_LENGTH)
    rsi14 = I.rsi(closes, _RSI_FAST_LEN)
    rsi84 = I.rsi(closes, _RSI_MID_LEN)
    rsi112 = I.rsi(closes, _RSI_SLOW_LEN)

    entry = [False] * n
    for i in range(n):
        if not (
            _valid(vwap_low[i])
            and _valid(tcp4[i])
            and _valid(cti[i])
            and _valid(rsi14[i])
            and _valid(rsi84[i])
            and _valid(rsi112[i])
        ):
            continue
        if (
            closes[i] < vwap_low[i]
            and tcp4[i] > _TCP_THRESHOLD
            and cti[i] < _CTI_MAX
            and rsi14[i] < _RSI_FAST_MAX
            and rsi84[i] < _RSI_MID_MAX
            and rsi112[i] < _RSI_SLOW_MAX
            and volumes[i] > 0
        ):
            entry[i] = True

    exit_signal = [False] * n  # empty populate_sell_trend → roi_sl exit
    return entry, exit_signal
