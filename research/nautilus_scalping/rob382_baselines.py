"""ROB-382 — micro-breakout + seeded-random baselines for the gate (pure, stdlib).

The ROB-382 acceptance criteria require each ported signal to BEAT micro-breakout + random
baselines (not just be OOS-net-positive). The cost-blind campaign funnel runs baseline-free,
so we build the baselines here and feed them to ``validated_gate.evaluate_gate`` directly.

  * micro-breakout: the harness's own ``families.breakout_continuation_trades`` (range-
    expansion long + short hold) on the SAME bars — the "is this just trend-following?" control.
  * random-entry: entries fired by a SEEDED RNG at the candidate's own empirical entry rate,
    closed by the candidate's OWN exit model + exit signal — isolates whether the ENTRY edge
    beats random timing with identical exits ("is the signal better than a coin flip?").
"""
from __future__ import annotations

import random
from collections.abc import Sequence

import families
import rob382_backtest as bt
from rob382_bars import OHLCVBar
from validated_gate import Trade


def _to_family_bars(bars: Sequence[OHLCVBar]) -> list[families.Bar]:
    return [families.Bar(ts=b.ts, high=b.high, low=b.low, close=b.close) for b in bars]


def breakout_baseline(bars: Sequence[OHLCVBar]) -> list[Trade]:
    """Micro-breakout control on the same bars (frozen ROB-351 family params)."""
    return families.breakout_continuation_trades(_to_family_bars(bars), notional=bt.NOTIONAL)


def random_baseline(
    bars: Sequence[OHLCVBar],
    exit_sig: Sequence[bool],
    exit_model: bt.ExitModel,
    n_entries: int,
    *,
    seed: int,
) -> list[Trade]:
    """Random-entry control: ``n_entries`` seeded random entry bars, SAME exit as the candidate.

    Matching the candidate's entry COUNT and reusing its exit model + exit signal isolates the
    value of entry TIMING. Deterministic given ``seed`` (no wall-clock / Math.random).
    """
    n = len(bars)
    if n_entries <= 0 or n < 2:
        return []
    rng = random.Random(seed)
    # candidate fires on a fraction of bars; mirror that density with a per-bar probability.
    prob = min(1.0, n_entries / n)
    entry = [rng.random() < prob for _ in range(n)]
    return bt.simulate(bars, entry, list(exit_sig), exit_model)
