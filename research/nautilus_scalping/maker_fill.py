# research/nautilus_scalping/maker_fill.py
"""ROB-324 — PURE maker/limit-fill scenario builders (no nautilus import).

Consumes ``MakerTradeRecord``s emitted by the maker re-sim and produces plain
``validated_gate.Trade`` lists for the unchanged gate. Fees are the REAL Binance
USDⓈ-M Futures Demo schedule (maker 2.0 / taker 4.0 bps) captured in
``results/rob324/binance_usdm_commission_rates.json``.

Gate convention (see spec §3.5): maker scenarios cannot use the gate's single-rate
fee rescale (mixed maker/taker legs), so each ``Trade`` carries the TRUE net at real
per-leg fees in ``net_ref_pnl`` and the true commission magnitude in
``commission_ref``. The driver evaluates maker scenarios at ``REF_FEE_BPS`` (scale=0
→ net_after_cost = as-run) and 0 (gross adds commission back). The taker baseline,
being single-rate, uses the gate's NATIVE rescale (call ``evaluate_gate`` at 4.0 bps
on the raw 10-bps taker trades) — no builder needed for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b

REF_FEE_BPS = 10.0          # mirrors validated_gate.REF_FEE_BPS (as-run reference point)
TAKER_BASELINE_BPS = 4.0    # real demo taker
MAKER_FEE_BPS = 2.0         # real demo maker


@dataclass(frozen=True)
class MakerTradeRecord:
    net_at_real_fees: float      # realized pnl already net of maker/taker per-leg fees
    commission_real: float       # total commission magnitude actually paid (>= 0)
    notional: float
    ts_opened: int
    filled: bool                 # False = limit cancelled (missed fill)
    tp_hit: bool                 # exit was the maker-limit TP (vs taker-stop SL)
    adverse_excursion_bps: float # worst adverse move between fill and exit, bps
