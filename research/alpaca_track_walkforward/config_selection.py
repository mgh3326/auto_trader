"""ROB-1062 H4 (Run A SS15, AC7-AC10) — TRAIN-only config selection.

Selection rule, applied IN ORDER, exactly as SS15 states it:

    TRAIN closed >= 30
    AND TRAIN E120 > 0
    AND passes the PnL-blind annualized stress cost cap
    -> max median-trade E120
    -> tie: lower turnover
    -> tie: canonical config_id ascending (AP-A1-00..07 / AP-A2-00..07 sort
       lexicographically in their own canonical nested-loop order, per
       ``alpaca_track_seal.configs``)

No config passing -> ``NO_SELECTED_CONFIG`` — never an arbitrary fallback
(AC10).

AC8 ("selection uses TRAIN data only; OOS information reaching selection is
a terminal error") is enforced structurally here, not merely by caller
discipline: ``select_config`` requires an explicit ``data_window`` argument
that must be the literal string ``"TRAIN"`` — there is no other window value
this function will accept, so a caller that tried to feed it OOS-derived
metrics under an ``"OOS"`` (or any other) label fails closed immediately,
before any metric is even inspected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "OOSDataReachedSelectionError",
    "ConfigSelectionResult",
    "ConfigTrainMetrics",
    "MIN_TRAIN_CLOSED_TRADES",
    "select_config",
]

MIN_TRAIN_CLOSED_TRADES = 30

SelectionStatus = Literal["SELECTED", "NO_SELECTED_CONFIG"]


class OOSDataReachedSelectionError(ValueError):
    """``select_config`` was called with anything other than
    ``data_window="TRAIN"`` — AC8's terminal-error enforcement point."""


@dataclass(frozen=True)
class ConfigTrainMetrics:
    """One config's TRAIN-window-only metrics — the only shape
    ``select_config`` ever looks at."""

    config_id: str
    closed_trades_count: int
    median_trade_e120_bp: float | None  # None iff closed_trades_count == 0
    turnover_p: float
    annualized_stress_cost_pct: float

    def __post_init__(self) -> None:
        if self.closed_trades_count < 0:
            raise ValueError("closed_trades_count must be non-negative")
        if self.closed_trades_count == 0 and self.median_trade_e120_bp is not None:
            raise ValueError("zero closed trades cannot carry a median E120")
        if self.closed_trades_count > 0 and self.median_trade_e120_bp is None:
            raise ValueError("a config with closed trades must carry a median E120")


@dataclass(frozen=True)
class ConfigSelectionResult:
    status: SelectionStatus
    selected_config_id: str | None

    def __post_init__(self) -> None:
        if self.status == "SELECTED" and self.selected_config_id is None:
            raise ValueError("a SELECTED result must carry a selected_config_id")
        if self.status == "NO_SELECTED_CONFIG" and self.selected_config_id is not None:
            raise ValueError("NO_SELECTED_CONFIG must not carry a selected_config_id")


def select_config(
    metrics: Sequence[ConfigTrainMetrics],
    *,
    data_window: str,
    stress_cost_cap_pct: float,
) -> ConfigSelectionResult:
    if data_window != "TRAIN":
        raise OOSDataReachedSelectionError(
            f"select_config only ever accepts data_window='TRAIN', got "
            f"{data_window!r} — OOS information must never reach config "
            "selection (Run A SS15/AC8)"
        )
    eligible = [
        m
        for m in metrics
        if m.closed_trades_count >= MIN_TRAIN_CLOSED_TRADES
        and m.median_trade_e120_bp is not None
        and m.median_trade_e120_bp > 0.0
        and m.annualized_stress_cost_pct <= stress_cost_cap_pct
    ]
    if not eligible:
        return ConfigSelectionResult(
            status="NO_SELECTED_CONFIG", selected_config_id=None
        )
    best = min(
        eligible,
        key=lambda m: (
            -m.median_trade_e120_bp,  # type: ignore[operator]  # narrowed non-None above
            m.turnover_p,
            m.config_id,
        ),
    )
    return ConfigSelectionResult(status="SELECTED", selected_config_id=best.config_id)
