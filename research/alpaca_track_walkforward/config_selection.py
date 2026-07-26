"""ROB-1062 H4 (Run A SS15, AC7-AC10) — TRAIN-only config selection.

Selection rule, applied IN ORDER, exactly as SS15 states it:

    TRAIN closed >= 30
    AND passes the PnL-blind annualized stress cost cap
    AND, for AP-A2, sealed replacement p is inside its turnover band
    AND TRAIN E120 > 0
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
        if type(self.turnover_p) is not float or not 0.0 <= self.turnover_p <= 1.0:
            raise ValueError("turnover_p must be a built-in float in [0, 1]")


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
    turnover_band: tuple[float, float] | None = None,
) -> ConfigSelectionResult:
    if data_window != "TRAIN":
        raise OOSDataReachedSelectionError(
            f"select_config only ever accepts data_window='TRAIN', got "
            f"{data_window!r} — OOS information must never reach config "
            "selection (Run A SS15/AC8)"
        )
    # AC9 is procedural, not merely a final-result predicate: the
    # PnL-blind cost cap must run before *any* E120 value is inspected.
    structurally_eligible = [
        m for m in metrics if m.closed_trades_count >= MIN_TRAIN_CLOSED_TRADES
    ]
    cost_eligible = [
        m
        for m in structurally_eligible
        if m.annualized_stress_cost_pct <= stress_cost_cap_pct
    ]
    contains_ap_a2 = any(m.config_id.startswith("AP-A2-") for m in metrics)
    if contains_ap_a2 and turnover_band is None:
        raise ValueError("AP-A2 selection requires the sealed turnover band")
    if turnover_band is not None:
        lower, upper = turnover_band
        if (
            type(lower) is not float
            or type(upper) is not float
            or not 0.0 <= lower <= upper <= 1.0
        ):
            raise ValueError("turnover_band must contain two ordered floats in [0, 1]")
        turnover_eligible = [
            m for m in cost_eligible if m.turnover_p >= lower and m.turnover_p <= upper
        ]
    else:
        turnover_eligible = cost_eligible
    eligible = [
        m
        for m in turnover_eligible
        if m.median_trade_e120_bp is not None and m.median_trade_e120_bp > 0.0
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
