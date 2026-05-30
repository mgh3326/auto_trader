"""Pure-stdlib GROSS per-trade edge probe for a 1m mean-reversion fade.

This is a *falsification* probe, not a validator. It measures whether a canonical
1m z-score fade (the reversal family the repo has repeatedly studied) has any
GROSS per-trade edge above the economic-triviality floor — BEFORE fees — on a
tiny bounded sample. It deliberately reuses the reversal idea behind
``research/nautilus_scalping/meanrev_signal.py`` but in ~stdlib so it runs with no
Nautilus build.

Trades are NON-OVERLAPPING (open, hold ``hold`` bars, close, skip past the exit)
so each trade return is independent — no double-counting inflating the sample.

Verdict mirrors the funnel label space already in the repo
(``screened_out`` / ``needs_more_data`` / promote-to-full-validation). A positive
gross result does NOT claim profitability: net (after fees) and statistical
validation are explicitly left to the full Nautilus gate.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

from fees import (
    ECONOMIC_TRIVIALITY_FLOOR_BPS,
    MAKER_ROUND_TRIP_BPS,
    TAKER_ROUND_TRIP_BPS,
    net_bps,
)

MIN_TRADES = 30  # below this, the sample can't support any read -> needs_more_data


@dataclass(frozen=True)
class ProbeParams:
    lookback: int = 20
    z_entry: float = 2.0
    hold: int = 10


@dataclass(frozen=True)
class SymbolResult:
    symbol: str
    demo_executable: bool
    n_bars: int
    n_trades: int
    mean_gross_bps: float
    median_gross_bps: float
    stdev_gross_bps: float
    t_stat: float
    win_rate_pct: float
    mean_net_taker_bps: float
    mean_net_maker_bps: float
    verdict: str


def _rolling_z(closes: list[float], lookback: int) -> list[float | None]:
    z: list[float | None] = [None] * len(closes)
    for i in range(lookback, len(closes)):
        window = closes[i - lookback : i]
        mean = statistics.fmean(window)
        sd = statistics.pstdev(window)
        if sd > 0:
            z[i] = (closes[i] - mean) / sd
    return z


def trade_records(
    closes: list[float], z: list[float | None], p: ProbeParams
) -> list[tuple[int, int, float]]:
    """Non-overlapping fade trades -> list of (entry_idx, exit_idx, gross_bps).

    Non-overlapping: after a trade opened at ``i`` and held ``p.hold`` bars, the
    scan resumes at ``exit_i + 1``, so consecutive entries are always more than
    ``p.hold`` apart and no bar is counted in two trades.
    """
    out: list[tuple[int, int, float]] = []
    n = len(closes)
    i = 0
    while i < n:
        zi = z[i]
        if zi is None:
            i += 1
            continue
        if zi <= -p.z_entry:
            direction = 1  # oversold -> fade up (go long)
        elif zi >= p.z_entry:
            direction = -1  # overbought -> fade down (go short)
        else:
            i += 1
            continue
        exit_i = i + p.hold
        if exit_i >= n:
            break
        entry, exitp = closes[i], closes[exit_i]
        if entry > 0:
            ret = (exitp - entry) / entry * 10_000.0 * direction
            out.append((i, exit_i, ret))
        i = exit_i + 1  # non-overlapping
    return out


def _trade_returns_bps(
    closes: list[float], z: list[float | None], p: ProbeParams
) -> list[float]:
    """Non-overlapping fade trades -> list of GROSS per-trade returns in bps."""
    return [ret for (_entry, _exit, ret) in trade_records(closes, z, p)]


def _verdict(n_trades: int, mean_gross: float) -> str:
    if n_trades < MIN_TRADES:
        return "needs_more_data"
    if mean_gross <= ECONOMIC_TRIVIALITY_FLOOR_BPS:
        return "screened_out_gross"  # no gross edge above the floor; reject
    return "gross_edge_present_needs_full_validation"


def probe_symbol(
    symbol: str, closes: list[float], *, demo_executable: bool, params: ProbeParams
) -> SymbolResult:
    z = _rolling_z(closes, params.lookback)
    rets = _trade_returns_bps(closes, z, params)
    n = len(rets)
    if n == 0:
        return SymbolResult(
            symbol,
            demo_executable,
            len(closes),
            0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            "needs_more_data",
        )
    mean = statistics.fmean(rets)
    median = statistics.median(rets)
    sd = statistics.pstdev(rets)
    t_stat = (mean / (sd / (n**0.5))) if (sd > 0 and n > 1) else 0.0
    win_rate = 100.0 * sum(1 for r in rets if r > 0) / n
    return SymbolResult(
        symbol=symbol,
        demo_executable=demo_executable,
        n_bars=len(closes),
        n_trades=n,
        mean_gross_bps=round(mean, 4),
        median_gross_bps=round(median, 4),
        stdev_gross_bps=round(sd, 4),
        t_stat=round(t_stat, 4),
        win_rate_pct=round(win_rate, 2),
        mean_net_taker_bps=round(net_bps(mean, round_trip_bps=TAKER_ROUND_TRIP_BPS), 4),
        mean_net_maker_bps=round(net_bps(mean, round_trip_bps=MAKER_ROUND_TRIP_BPS), 4),
        verdict=_verdict(n, mean),
    )


def result_to_dict(r: SymbolResult) -> dict:
    return asdict(r)
