"""ROB-1062 H4 (Run A SS3, SS16, AC17-AC21) — the 3 independent PnL views
for one CLOSED trade (one entry-fill + one exit-fill round trip; a trade
that never exits is not a closed trade and never reaches this module).

    gross        = Binance reference-close-to-close move only (no
                   execution friction of any kind — the theoretical
                   best case).
    actual_fill  = this backtest's MODELED fill prices
                   (``fill_model.FillOutcome.fill_price`` on both legs) —
                   captures the fill model's own price slippage (paying up
                   to +0.5% on entry / down to -0.5% on exit), but still no
                   spread/fee/venue-mismatch deduction.
    shadow_net   = actual_fill MINUS exactly one cost-scenario bp value
                   (C50/C100/C120/C150 — Run A SS3's own table already
                   bakes the 50bp round-trip paper fee INTO every one of
                   those bp figures, e.g. C100 = "50 fee + ~36 spread + 14
                   buffer"). This is the ONLY place a cost scenario is
                   deducted; ``shadow_net`` is the promotion-canonical view
                   (SS16).

AC19 (no double fee deduction) is enforced STRUCTURALLY, not by convention:
no function in this module accepts a separate fee parameter, and this
module never imports ``wf_seal_consumption.paper_fee_bp`` — there is no
second deduction path to accidentally wire in.
``tests/test_pnl_views.py::test_module_never_imports_or_references_a_
separate_paper_fee_bp_deduction`` statically proves this by AST scan.

AC17 (scenarios never derive from one another): every scenario's
``shadow_net`` is computed as ``actual_fill_pnl_bp - scenario_bp[name]``
directly from the caller-supplied ``cost_scenarios_bp`` mapping — never as
an offset/interpolation from another scenario's own result. Because the
implementation reads the mapping by KEY rather than by relative position,
scenario bp values need not even be monotonic for this property to hold
(``tests/test_pnl_views.py`` proves this with a deliberately non-monotonic,
non-evenly-spaced fake scenario table).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ThreeViewPnL",
    "TradeFill",
    "actual_fill_pnl_bp",
    "gross_pnl_bp",
    "shadow_net_pnl_bp",
    "three_view_pnl_bp",
]


def _float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    return value


@dataclass(frozen=True)
class TradeFill:
    """One CLOSED round trip's price evidence — both the pure Binance
    reference closes (for ``gross``) and this backtest's modeled fill
    prices (for ``actual_fill``/``shadow_net``). Never a per-leg record:
    one ``TradeFill`` == one trade, always (AC21)."""

    entry_reference_close: float
    entry_fill_price: float
    exit_reference_close: float
    exit_fill_price: float

    def __post_init__(self) -> None:
        for name in (
            "entry_reference_close",
            "entry_fill_price",
            "exit_reference_close",
            "exit_fill_price",
        ):
            value = _float(getattr(self, name), name)
            if value <= 0.0:
                raise ValueError(f"{name} must be positive, got {value!r}")


@dataclass(frozen=True)
class ThreeViewPnL:
    gross_bp: float
    actual_fill_bp: float
    shadow_net_bp_by_scenario: Mapping[str, float]


def gross_pnl_bp(trade: TradeFill) -> float:
    """Binance reference-close-to-close move, in bp. No execution friction
    of any kind mixed in (AC18)."""
    return (
        (trade.exit_reference_close - trade.entry_reference_close)
        / trade.entry_reference_close
        * 10_000.0
    )


def actual_fill_pnl_bp(trade: TradeFill) -> float:
    """This backtest's modeled-fill-price move, in bp. Still no spread/fee/
    venue-mismatch deduction (AC13/AC18) — those arrive only in
    ``shadow_net_pnl_bp``."""
    return (
        (trade.exit_fill_price - trade.entry_fill_price)
        / trade.entry_fill_price
        * 10_000.0
    )


def shadow_net_pnl_bp(
    trade: TradeFill, *, scenario: str, cost_scenarios_bp: Mapping[str, int]
) -> float:
    """``actual_fill_pnl_bp(trade) - cost_scenarios_bp[scenario]`` — exactly
    ONE deduction, looked up directly by scenario NAME (never derived from
    another scenario's value or position in the table)."""
    if scenario not in cost_scenarios_bp:
        raise KeyError(
            f"unknown cost scenario {scenario!r}: {sorted(cost_scenarios_bp)}"
        )
    return actual_fill_pnl_bp(trade) - float(cost_scenarios_bp[scenario])


def three_view_pnl_bp(
    trade: TradeFill, *, cost_scenarios_bp: Mapping[str, int]
) -> ThreeViewPnL:
    """Bundle all 3 views for one closed trade — every scenario's
    ``shadow_net`` computed independently (AC17), from a single shared
    ``actual_fill_pnl_bp`` base (never from ``gross``, and never from each
    other)."""
    by_scenario = {
        name: shadow_net_pnl_bp(
            trade, scenario=name, cost_scenarios_bp=cost_scenarios_bp
        )
        for name in cost_scenarios_bp
    }
    return ThreeViewPnL(
        gross_bp=gross_pnl_bp(trade),
        actual_fill_bp=actual_fill_pnl_bp(trade),
        shadow_net_bp_by_scenario=by_scenario,
    )
