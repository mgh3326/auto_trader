"""ROB-383 Phase 3 - one frozen parameter set per candidate, no sweep."""

from __future__ import annotations

import hashlib
import json

FROZEN_PARAMS: dict[str, dict] = {
    "freqtrade_supertrend": {
        "signal": "supertrend_trades",
        "interval": "1h",
        "params": {"atr_period": 10, "multiplier": 3.0},
    },
    "freqtrade_bbrsi_naive": {
        "signal": "bbrsi_trades",
        "interval": "5m",
        "params": {
            "bb_period": 20,
            "bb_k": 2.0,
            "rsi_period": 14,
            "rsi_oversold": 30.0,
        },
    },
    "tv_squeeze_momentum": {
        "signal": "squeeze_momentum_trades",
        "interval": "1h",
        "params": {"length": 20, "bb_k": 2.0, "kc_mult": 1.5},
        "caveat": (
            "non_faithful_clean_room_spec: momentum simplified from LazyBear "
            "linreg to close-SMA"
        ),
    },
    "tv_range_filter": {
        "signal": "range_filter_trades",
        "interval": "1h",
        "params": {"period": 20, "mult": 1.0},
    },
    "tv_chandelier_exit": {
        "signal": "chandelier_trades",
        "interval": "1h",
        "params": {"atr_period": 22, "multiplier": 3.0},
    },
}

PARAMS_VERSION = "rob383.phase3.v1"


def params_hash() -> str:
    payload = {"version": PARAMS_VERSION, "frozen": FROZEN_PARAMS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
