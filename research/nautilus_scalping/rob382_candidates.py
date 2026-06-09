"""ROB-382 — selected strat.ninja candidates + their CONTRAST metadata (research only).

Selection rationale (ROB-382 §1): 3-5 strategies maximizing diversity from what we've
already disproven (breakout / ts-trend / xs-momentum / meanrev z-fade / ICT), unbiased
(no lookahead), no tight-trailing, 1x (spot strategies are 1x), enough trades. ICT is
EXPLICITLY EXCLUDED — ``ict_signal.py`` / ``strategy_ict.py`` already cover it.

``contrast`` holds the strat.ninja in-sample SPOT backtest numbers, recorded FOR CONTRAST
ONLY (NOT evidence — they are spot, single-month, in-sample; freqtrade's own docs warn
not to assume they're realistic). Stats read from the strat.ninja ranking/overview on
2026-05-31 (the monthly leaderboard; "avg" columns are per-month medians).

Each candidate's signal logic is ported (no freqtrade/talib/execution import) into the
matching ``module`` from the PUBLIC canonical source in ``source_url``.
"""
from __future__ import annotations

# Ordered list of candidate descriptors. ``module`` is the ported signal module created
# by the per-candidate fan-out; ``run_candidate`` imports it by name.
CANDIDATES = [
    {
        "key": "ichi",
        "module": "rob382_signal_ichi",
        "display_name": "ichiV1 (Ichimoku cloud + EMA fan-magnitude)",
        "strat_ninja_name": "ichi_5m",
        "family_shape": "ichimoku_trend_follow",
        "diversity_rationale": (
            "Ichimoku cloud trend-follow + multi-EMA fan expansion — a trend family we "
            "have NOT run (distinct from breakout/ts-trend/xs-mom/meanrev/ICT)."
        ),
        "source_url": "https://raw.githubusercontent.com/PeetCrypto/freqtrade-stuff/main/IchisV1.py",
        "source_note": (
            "Canonical ichiV1 (@author 25-Jul-21). strat.ninja 'ichi_5m' lists timeframe 1m "
            "and is 'Not Enough Data' (avg not populated); the documented source timeframe "
            "is 5m, which we use (faithful to the published strategy)."
        ),
        "contrast": {
            "ninja_score": None,
            "their_spot_total_profit_pct": None,
            "their_win_pct": None,
            "their_sharpe": None,
            "their_avg_buys": None,
            "stoploss": -0.275,
            "note": "strat.ninja ichi_5m: insufficient data (avg statistics not populated).",
        },
    },
    {
        "key": "elliot",
        "module": "rob382_signal_elliot",
        "display_name": "ElliotV5_SMA shape (EWO + SMA-offset dip, 1h uptrend filter)",
        "strat_ninja_name": "ElliotV5_SMA",
        "family_shape": "ewo_sma_offset_meanrev",
        "diversity_rationale": (
            "Elliott Wave Oscillator (EMA50-EMA200 momentum) + SMA-offset dip-buy gated by a "
            "1h uptrend — EWO is a distinct oscillator; not our z-score meanrev nor MACD."
        ),
        "source_url": "https://raw.githubusercontent.com/PeetCrypto/freqtrade-stuff/main/ElliotV7.py",
        "source_note": (
            "Ported from ElliotV7 (@Rallipanos), the direct public successor of ElliotV5_SMA "
            "with an IDENTICAL EWO + SMA-offset entry shape. Exact ElliotV5_SMA hyperopt "
            "params were not publicly retrievable; V7 published defaults used. Contrast row is "
            "the strat.ninja ElliotV5_SMA leaderboard entry (the shape being falsified)."
        ),
        "contrast": {
            "ninja_score": 62,
            "their_spot_total_profit_pct": 2.73,
            "their_win_pct": 75.67,
            "their_sharpe": 1.91,
            "their_avg_buys": 27,
            "stoploss": -0.189,
            "note": "strat.ninja ElliotV5_SMA rank 2; 'Recursive Analysis found no issues' (unbiased).",
        },
    },
    {
        "key": "vwap",
        "module": "rob382_signal_vwap",
        "display_name": "VWAPStrategy (VWAP band dip + CTI + multi-RSI, 2% ROI / 15% SL)",
        "strat_ninja_name": "VWAPStrategy_1478",
        "family_shape": "vwap_band_dip_volume",
        "diversity_rationale": (
            "Volume-weighted (rolling VWAP) band dip + CTI + multi-period RSI cluster — a "
            "volume/orderflow-derived-from-OHLCV family we have NOT run."
        ),
        "source_url": "https://raw.githubusercontent.com/PeetCrypto/freqtrade-stuff/main/VWAP.py",
        "source_note": (
            "Canonical VWAP (@jilv220). Empty populate_sell_trend → exit is the PUBLISHED "
            "minimal_roi 2% / stoploss -15% (ported as roi_sl exit_model, not hyperopted)."
        ),
        "contrast": {
            "ninja_score": 56,
            "their_spot_total_profit_pct": -0.70,
            "their_win_pct": 93.33,
            "their_sharpe": 1.27,
            "their_avg_buys": 78,
            "stoploss": -0.20,
            "note": "strat.ninja VWAPStrategy_1478 rank 11; 93% win but NEGATIVE total profit (large tail losses).",
        },
    },
    {
        "key": "cluc",
        "module": "rob382_signal_cluc",
        "display_name": "ClucHAnix (Heikin-Ashi + Bollinger squeeze dip + 1h ROCR, fisher exit)",
        "strat_ninja_name": "ClucHAnix_3",
        "family_shape": "heikinashi_bb_squeeze_meanrev",
        "diversity_rationale": (
            "Heikin-Ashi-transformed Bollinger squeeze dip cluster + 1h ROCR trend gate + "
            "fisher exit. HA transform is new; plain BB is semi-disproven, so this doubles as "
            "a near-baseline control for the contrast."
        ),
        "source_url": "https://raw.githubusercontent.com/PeetCrypto/freqtrade-stuff/main/ClucHAnix.py",
        "source_note": (
            "Canonical ClucHAnix. 1m timeframe, signal-based exit (fisher cluster), 1h "
            "informative (rocr_1h). Custom trailing stop approximated by the published hard "
            "stop + generous max-hold (recorded as exit_model)."
        ),
        "contrast": {
            "ninja_score": 44,
            "their_spot_total_profit_pct": 0.46,
            "their_win_pct": 70.33,
            "their_sharpe": 0.23,
            "their_avg_buys": 13,
            "stoploss": -0.99,
            "note": "strat.ninja ClucHAnix_3 rank 23; uses custom trailing stop.",
        },
    },
]

# ICT family is intentionally absent: already covered by ict_signal.py / strategy_ict.py.
EXCLUDED = {"ict": "already covered by ict_signal.py / strategy_ict.py (not a new shape)"}


def by_key(key: str) -> dict:
    for c in CANDIDATES:
        if c["key"] == key:
            return c
    raise KeyError(key)
