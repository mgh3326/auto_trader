"""Frozen, pure calculation contracts for the three-market shadow.

This module has no broker, database, clock, or order imports.  Both the
broker-neutral backtest harness and the runtime shadow runner import these
functions directly.  The rules are deliberately shadow-only: a positive
research result is evidence, never an order authorization.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

from research_contracts.canonical_hash import canonical_sha256

SignalState = Literal["SIGNAL", "NO_SIGNAL"]

_CONTRACT = {
    "version": "three-market-shadow-v1",
    "kr": {
        "key": "rev3_reclaim",
        "lookback": 20,
        "cross": "previous_close<=prior_window_low and close>prior_window_low",
        "volume_ratio_min": 1.2,
    },
    "us": {
        "key": "MOM-CONT-Z126",
        "lookback": 20,
        "momentum_bars": 6,
        "cross": "close>prior_window_high",
        "volume_ratio_min": 1.1,
    },
    "crypto": {
        "key": "SYNTHETIC_ACCEPTANCE",
        "fixture": "fixed_signal_v1",
    },
}

CONTRACT_HASH = canonical_sha256(_CONTRACT)
CRYPTO_SYNTHETIC_SIGNAL: dict[str, Any] = {
    "market": "crypto",
    "strategy": "SYNTHETIC_ACCEPTANCE",
    "signal_state": "SIGNAL",
    "signal_source": "SYNTHETIC_ACCEPTANCE",
    "fixture_id": "crypto-shadow-fixed-signal-v1",
    "decision": "NO_ORDER",
}


def _series(snapshot: Mapping[str, Any]) -> tuple[list[float], list[float]]:
    closes = [float(value) for value in snapshot.get("close", ())]
    volumes = [float(value) for value in snapshot.get("volume", ())]
    if len(closes) != len(volumes):
        raise ValueError("close and volume must have equal lengths")
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError("close values must be finite and positive")
    if any(not math.isfinite(value) or value < 0 for value in volumes):
        raise ValueError("volume values must be finite and non-negative")
    return closes, volumes


def _no_signal(market: str, strategy: str, reason: str) -> dict[str, Any]:
    return {
        "market": market,
        "strategy": strategy,
        "signal_state": "NO_SIGNAL",
        "signal_source": "UNTESTED_RESEARCH_SHADOW",
        "reason": reason,
        "decision": "NO_ORDER",
        "contract_hash": CONTRACT_HASH,
    }


def calculate_kr_rev3_reclaim(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate the frozen KR rev3 reclaim shadow predicate."""
    closes, volumes = _series(snapshot)
    lookback = 20
    if len(closes) < lookback + 1:
        return _no_signal("kr", "rev3_reclaim", "insufficient_bars")
    prior = closes[-lookback - 1 : -1]
    average_volume = sum(volumes[-lookback - 1 : -1]) / lookback
    reclaimed = closes[-2] <= min(prior) and closes[-1] > min(prior)
    volume_ok = average_volume > 0 and volumes[-1] >= average_volume * 1.2
    if not (reclaimed and volume_ok):
        return _no_signal("kr", "rev3_reclaim", "predicate_false")
    return {
        "market": "kr",
        "strategy": "rev3_reclaim",
        "signal_state": "SIGNAL",
        "signal_source": "UNTESTED_RESEARCH_SHADOW",
        "side": "buy",
        "reason": "rev3_reclaim_predicate_true",
        "decision": "NO_ORDER",
        "contract_hash": CONTRACT_HASH,
    }


def calculate_us_mom_cont_z126(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate the frozen US MOM-CONT-Z126 shadow predicate."""
    closes, volumes = _series(snapshot)
    lookback = 20
    momentum_bars = 6
    if len(closes) < lookback + momentum_bars:
        return _no_signal("us", "MOM-CONT-Z126", "insufficient_bars")
    prior_window = closes[-lookback - 1 : -1]
    average_volume = sum(volumes[-lookback - 1 : -1]) / lookback
    momentum = closes[-1] > closes[-1 - momentum_bars]
    continuation = closes[-1] > max(prior_window)
    volume_ok = average_volume > 0 and volumes[-1] >= average_volume * 1.1
    if not (momentum and continuation and volume_ok):
        return _no_signal("us", "MOM-CONT-Z126", "predicate_false")
    return {
        "market": "us",
        "strategy": "MOM-CONT-Z126",
        "signal_state": "SIGNAL",
        "signal_source": "UNTESTED_RESEARCH_SHADOW",
        "side": "buy",
        "reason": "momentum_continuation_predicate_true",
        "decision": "NO_ORDER",
        "contract_hash": CONTRACT_HASH,
    }


def calculate_crypto_synthetic(
    _snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deterministic acceptance fixture; never a research signal."""
    return {**CRYPTO_SYNTHETIC_SIGNAL, "contract_hash": CONTRACT_HASH}


def calculate_signal(market: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch to exactly one shared market calculation."""
    if market == "kr":
        return calculate_kr_rev3_reclaim(snapshot)
    if market == "us":
        return calculate_us_mom_cont_z126(snapshot)
    if market == "crypto":
        return calculate_crypto_synthetic(snapshot)
    raise ValueError(f"unsupported shadow market: {market}")


__all__ = [
    "CONTRACT_HASH",
    "CRYPTO_SYNTHETIC_SIGNAL",
    "calculate_crypto_synthetic",
    "calculate_kr_rev3_reclaim",
    "calculate_signal",
    "calculate_us_mom_cont_z126",
]
