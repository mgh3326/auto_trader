"""ROB-351 (eng-review Issue 4) — shared analytic net-at-fee primitive (pure, stdlib).

The linear fee rescale was duplicated in ``validated_gate``, ``fee_sweep``, and
``compare_strategies``. Extracted here so every call-site shares one
implementation (3 -> 1).

    net(fee) = net_ref_pnl + commission_ref * (1 - fee_bps / ref_fee_bps)

EXACT for the engine cost model: commission scales linearly with the per-leg fee
rate, so a run executed at ``ref_fee_bps`` rescales analytically to any other
fee. ``commission_ref`` is the (positive) commission magnitude paid at the
reference run; ``fee_bps = 0`` adds it fully back (gross), ``fee_bps = ref``
leaves the as-run net.

CAVEAT (ROB-351 eng-review Issue 3): this rescale is valid ONLY when fills are
fee-independent (taker entry/exit at price levels). Maker scenarios change WHICH
fills occur (queue loss, missed fills, adverse selection), so a maker breakeven
must NOT use this rescale — build explicit per-leg maker fees with
``maker_fill.py`` instead.
"""

from __future__ import annotations

REF_FEE_BPS = 10.0


def net_at_fee(
    net_ref_pnl: float,
    commission_ref: float,
    fee_bps: float,
    ref_fee_bps: float = REF_FEE_BPS,
) -> float:
    """Net PnL of one reference-fee trade rescaled to ``fee_bps`` per leg."""
    scale = 1.0 - fee_bps / ref_fee_bps
    return net_ref_pnl + commission_ref * scale
