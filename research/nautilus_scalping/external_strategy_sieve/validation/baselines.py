"""ROB-383 Phase 3 - gate baselines."""

from __future__ import annotations

import random
from collections.abc import Sequence

import families
from families import REF_FEE_BPS, make_taker_trade
from validated_gate import Trade


def random_entry_trades(
    bars: Sequence[families.Bar],
    n_trades: int,
    hold: int = 5,
    notional: float = 1000.0,
    ref_fee_bps: float = REF_FEE_BPS,
    seed: int = 42,
) -> list[Trade]:
    """Turnover-matched random-entry baseline, seeded for reproducibility."""
    rng = random.Random(seed)
    n = len(bars)
    trades: list[Trade] = []
    if n <= hold:
        return trades
    for _ in range(n_trades):
        i = rng.randrange(0, n - hold)
        entry = bars[i].close
        exit_ = bars[i + hold].close
        ret = (exit_ - entry) / entry if entry else 0.0
        trades.append(
            make_taker_trade(ret * notional, bars[i].ts, notional, ref_fee_bps)
        )
    return trades


def breakout_baseline(bars: Sequence[families.Bar]) -> list[Trade]:
    """Reuse the canonical breakout family as the structured baseline."""
    return families.breakout_continuation_trades(bars)
