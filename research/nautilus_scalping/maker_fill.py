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

from validated_gate import Trade  # pure import (stdlib-only module)

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


def build_maker_optimistic(records: list[MakerTradeRecord]) -> list[Trade]:
    """Filled maker trades at real fees; missed fills contribute nothing.

    net_ref_pnl carries the true net (maker 2 / taker 4 bps already applied);
    commission_ref carries the true commission magnitude so the gate's gross
    column reconstructs correctly. Evaluate at REF_FEE_BPS for as-run net."""
    return [
        Trade(net_ref_pnl=r.net_at_real_fees, commission_ref=r.commission_real,
              notional=r.notional, ts_opened=r.ts_opened)
        for r in records if r.filled
    ]


def classify_easy_tp(record: MakerTradeRecord, excursion_eps_bps: float = 2.0) -> bool:
    """A TP fill that barely moved against us before reaching target — i.e. a
    front-of-queue fill we would not realistically win against real queue priority."""
    return record.tp_hit and record.adverse_excursion_bps <= excursion_eps_bps


def _uniform_from_ts(ts_opened: int) -> float:
    """Deterministic uniform [0,1) from the trade timestamp (reproducible, no RNG)."""
    digest = blake2b(str(ts_opened).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0 ** 64


def build_maker_conservative(
    records: list[MakerTradeRecord],
    *,
    queue_loss_pct: float = 0.25,
    adverse_bps: float = 1.0,
    excursion_eps_bps: float = 2.0,
) -> list[Trade]:
    """Conservative maker scenario: an honest lower bound on the re-sim.

    Two haircuts on top of the data-derived fills:
      1. Queue loss — deterministically drop ``queue_loss_pct`` of the easy-TP fills
         (Nautilus has no order-queue model, so it over-fills passive limits).
      2. Adverse selection — charge ``adverse_bps`` on every surviving maker entry.
    Missed fills are excluded (they earn nothing)."""
    out: list[Trade] = []
    for r in records:
        if not r.filled:
            continue
        if classify_easy_tp(r, excursion_eps_bps) and _uniform_from_ts(r.ts_opened) < queue_loss_pct:
            continue  # queue loss
        adverse_cost = adverse_bps * r.notional / 10_000.0
        out.append(Trade(
            net_ref_pnl=r.net_at_real_fees - adverse_cost,
            commission_ref=r.commission_real,
            notional=r.notional, ts_opened=r.ts_opened,
        ))
    return out
