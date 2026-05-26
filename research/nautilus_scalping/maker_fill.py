# research/nautilus_scalping/maker_fill.py
"""ROB-324 — PURE maker/limit-fill scenario builders (no nautilus import).

Consumes ``MakerTradeRecord``s emitted by the maker re-sim (run on a ZERO-FEE
instrument, so ``gross`` is pure price P&L) and produces plain
``validated_gate.Trade`` lists for the unchanged gate. Fees are the REAL Binance
USDⓈ-M Futures Demo schedule (maker 2.0 / taker 4.0 bps) captured in
``results/rob324/binance_usdm_commission_rates.json``.

Fee model (per leg): the entry is a passive limit (MAKER, 2 bps); the exit is the
maker-limit take-profit (MAKER, 2 bps) when ``tp_hit`` else the taker stop
(TAKER, 4 bps). Applying fees here — rather than via a single Nautilus commission
rate — keeps the mixed maker/taker mix exact.

Gate convention (spec §3.5): each ``Trade`` carries the TRUE net at real fees in
``net_ref_pnl`` and the fee magnitude in ``commission_ref``; the driver evaluates
maker scenarios at ``validated_gate.REF_FEE_BPS`` (scale=0 → net_after_cost =
as-run) and 0 (gross adds the fee back). The taker baseline, being single-rate,
uses the gate's NATIVE rescale (``evaluate_gate`` at 4.0 bps on the raw 10-bps
taker trades) — no builder needed for it.

Missed fills (entry limit cancelled on timeout) earn nothing and are simply
ABSENT from the record list; the re-sim reports the missed count separately.
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
    gross: float                 # realized price P&L on the zero-fee re-sim (fee-free)
    entry_notional: float        # entry leg notional (price * qty)
    exit_notional: float         # exit leg notional (price * qty)
    ts_opened: int
    filled: bool                 # False = limit cancelled (missed fill)
    tp_hit: bool                 # exit was the maker-limit TP (vs the taker stop SL)
    adverse_excursion_bps: float # worst adverse move between fill and exit, bps


def _legs_fee(record: MakerTradeRecord) -> float:
    """Real per-leg fee: maker on the entry + maker on the TP leg / taker on the SL leg."""
    entry_fee = MAKER_FEE_BPS * record.entry_notional / 10_000.0
    exit_rate = MAKER_FEE_BPS if record.tp_hit else TAKER_BASELINE_BPS
    exit_fee = exit_rate * record.exit_notional / 10_000.0
    return entry_fee + exit_fee


def build_maker_optimistic(records: list[MakerTradeRecord]) -> list[Trade]:
    """Filled maker trades, real per-leg fees applied to the zero-fee gross.

    Optimistic = every touched TP is assumed to fill (no queue loss); that
    pessimism is added by build_maker_conservative."""
    out: list[Trade] = []
    for r in records:
        if not r.filled:
            continue
        fee = _legs_fee(r)
        out.append(Trade(net_ref_pnl=r.gross - fee, commission_ref=fee,
                         notional=r.entry_notional, ts_opened=r.ts_opened))
    return out


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
    """Conservative maker scenario: an honest lower bound on the optimistic re-sim.

    Two haircuts on top of the data-derived fills:
      1. Queue loss — deterministically drop ``queue_loss_pct`` of the easy-TP fills
         (a touch-based TP fill assumes front-of-queue priority we would not always win).
      2. Adverse selection — charge ``adverse_bps`` on every surviving maker entry.
    Missed fills are excluded (they earn nothing)."""
    out: list[Trade] = []
    for r in records:
        if not r.filled:
            continue
        if classify_easy_tp(r, excursion_eps_bps) and _uniform_from_ts(r.ts_opened) < queue_loss_pct:
            continue  # queue loss
        fee = _legs_fee(r)
        adverse_cost = adverse_bps * r.entry_notional / 10_000.0
        out.append(Trade(net_ref_pnl=r.gross - fee - adverse_cost, commission_ref=fee,
                         notional=r.entry_notional, ts_opened=r.ts_opened))
    return out
