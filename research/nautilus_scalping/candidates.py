"""ROB-320 — PURE candidate registry (no Nautilus import).

Maps a candidate name to its pure signal function, a config factory, default
params, and a hypothesis label. The Nautilus Strategy factory is resolved
LAZILY in ``backtest_runner`` (keyed by name) so this module — and the pure
test layer — never import ``nautilus_trader``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from meanrev_signal import MeanRevConfig, evaluate_meanrev

from app.services.brokers.binance.demo_scalping.signal import (
    SignalConfig,
    evaluate_signal,
)


@dataclass(frozen=True)
class Candidate:
    name: str
    hypothesis: str
    pure_signal: Callable[..., Any]
    config_factory: Callable[[Mapping[str, Any]], Any]
    default_params: dict[str, Any]


def _breakout_cfg(p: Mapping[str, Any]) -> SignalConfig:
    return SignalConfig(
        sma_fast=int(p.get("sma_fast", 7)),
        sma_slow=int(p.get("sma_slow", 25)),
        breakout_lookback=int(p.get("breakout_lookback", 20)),
        tp_bps=Decimal(str(p.get("tp_bps", 30))),
        sl_bps=Decimal(str(p.get("sl_bps", 20))),
        allow_short=bool(p.get("allow_short", False)),
    )


def _meanrev_cfg(p: Mapping[str, Any]) -> MeanRevConfig:
    return MeanRevConfig(
        lookback=int(p.get("lookback", 20)),
        z_entry=Decimal(str(p.get("z_entry", "2.0"))),
        tp_bps=Decimal(str(p.get("tp_bps", 30))),
        sl_bps=Decimal(str(p.get("sl_bps", 30))),
        require_vol=bool(p.get("require_vol", True)),
        allow_short=bool(p.get("allow_short", False)),
    )


def _random_cfg(p: Mapping[str, Any]) -> dict[str, Any]:
    # random_entry has no pure signal; params drive the Nautilus control strategy.
    return {"entry_prob": float(p.get("entry_prob", 0.02)), "seed": int(p.get("seed", 42)),
            "tp_bps": int(p.get("tp_bps", 30)), "sl_bps": int(p.get("sl_bps", 30))}


def _random_signal(*_args: Any, **_kwargs: Any) -> None:  # no pure signal
    raise NotImplementedError("random_entry is a Nautilus-only control; no pure signal")


REGISTRY: dict[str, Candidate] = {
    "micro_breakout": Candidate(
        name="micro_breakout", hypothesis="trend_breakout",
        pure_signal=evaluate_signal, config_factory=_breakout_cfg,
        default_params={"tp_bps": 30, "sl_bps": 20},
    ),
    "meanrev_zscore_fade": Candidate(
        name="meanrev_zscore_fade", hypothesis="mean_reversion",
        pure_signal=evaluate_meanrev, config_factory=_meanrev_cfg,
        default_params={"lookback": 20, "z_entry": "2.0", "tp_bps": 30, "sl_bps": 30},
    ),
    "random_entry": Candidate(
        name="random_entry", hypothesis="no_skill_control",
        pure_signal=_random_signal, config_factory=_random_cfg,
        default_params={"entry_prob": 0.02, "seed": 42, "tp_bps": 30, "sl_bps": 30},
    ),
}


def get_candidate(name: str) -> Candidate:
    return REGISTRY[name]
