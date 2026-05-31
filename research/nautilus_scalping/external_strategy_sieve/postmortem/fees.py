"""ROB-384 — fee-grid recompute.

Net PnL (or per-trade bps) is linear in the per-leg taker fee because cost is
``fee * turnover`` and turnover is fixed once the trade list is fixed. So any
fee point is an exact linear interpolation between the **gross** run (fee = 0,
identical to the ``zero_fee`` fold — verified against the source artifacts) and
the **reference-fee** run (``REF_FEE_BPS``):

    net@fee = gross - (fee / ref) * (gross - net@ref)

This is the same model as ``cost_model.net_at_fee`` and the sieve's
``fee_sweep`` / ``compare_strategies`` (ROB-320/383). It is applied identically
whether the endpoints are absolute PnL or per-trade bps, since both scale
linearly with fee.
"""

from __future__ import annotations

# Canonical reference fee for the GateReport family (per leg, bps).
REF_FEE_BPS = 10.0
# The grid ROB-384 reports (per leg, bps), matching the sieve fee_grid.
FEE_GRID_BPS: tuple[float, ...] = (0.0, 2.0, 4.0, 7.5, 10.0)


def net_at_fee(
    gross: float, net_ref: float, fee_bps: float, ref_fee_bps: float = REF_FEE_BPS
) -> float:
    """Net (PnL or bps) at ``fee_bps``, interpolated from gross (fee=0) and net@ref.

    ``gross`` is the zero-fee value; ``net_ref`` is the value at ``ref_fee_bps``.
    At ``fee_bps == 0`` returns ``gross``; at ``fee_bps == ref_fee_bps`` returns
    ``net_ref``. ``ref_fee_bps`` must be non-zero.
    """
    if ref_fee_bps == 0:
        raise ValueError("ref_fee_bps must be non-zero")
    return gross - (fee_bps / ref_fee_bps) * (gross - net_ref)


def fee_grid(
    gross: float,
    net_ref: float,
    *,
    ref_fee_bps: float = REF_FEE_BPS,
    grid: tuple[float, ...] = FEE_GRID_BPS,
) -> dict[str, float]:
    """Map each grid fee (as a string key, e.g. ``"7.5"``) to net@fee."""
    return {_fee_key(fee): net_at_fee(gross, net_ref, fee, ref_fee_bps) for fee in grid}


def _fee_key(fee_bps: float) -> str:
    """Stable string key for a fee point: ``2.0 -> "2"``, ``7.5 -> "7.5"``."""
    return f"{fee_bps:g}"


def expectancy_to_bps(expectancy: float, notional: float = 1000.0) -> float:
    """Per-trade PnL (quote units) -> per-trade bps of notional.

    ``notional = 1000`` is the sieve/validated-gate convention (ROB-383
    ``classify``: ``oos.expectancy / notional * 1e4``). Keep it explicit so the
    bps values are reproducible and comparable across re-parsed candidates.
    """
    if notional == 0:
        raise ValueError("notional must be non-zero")
    return (expectancy / notional) * 1e4
