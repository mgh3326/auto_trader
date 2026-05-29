"""ROB-353 (PR2) — bridge real PIT bars/panel into ROB-351 funnel family specs (pure).

Turns the data the PR1 layer produces (``pit_bars.load_bars`` / ``load_panel``) into the
``{name, summary, kind, data, maker_conservative_net}`` specs ``campaign.run_campaign``
consumes. Family params are FROZEN to the ROB-351 defaults (ex-ante; recorded in the
report). No market data is read here — the harness passes already-loaded bars/panels in.

Round-trip reference fee: 2 * 10.0 bps (taker in + taker out at cost_model.REF_FEE_BPS).
"""
from __future__ import annotations

from collections.abc import Sequence

import families
from discovery.screen import HypothesisSummary
from validated_gate import PortfolioPeriod, Trade

NOTIONAL = 1000.0
OOS_SPLIT_TS = 1_735_689_600_000  # 2025-01-01T00:00:00Z in epoch ms (ROB-349 train/test boundary)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _summary_from_trades(name: str, trades: Sequence[Trade], oos_split_ts: int) -> HypothesisSummary:
    gross = [(t.net_ref_pnl + t.commission_ref) / t.notional * 1e4 for t in trades]
    net = [t.net_ref_pnl / t.notional * 1e4 for t in trades]
    oos_g = [g for g, t in zip(gross, trades, strict=True) if t.ts_opened > oos_split_ts]
    oos_n = [n for n, t in zip(net, trades, strict=True) if t.ts_opened > oos_split_ts]
    return HypothesisSummary(
        name=name, conditions=f"frozen ROB-351 family params; OOS split {oos_split_ts}",
        sample_count=len(trades),
        gross_expectancy_bps=_mean(gross), fee_adjusted_bps=_mean(net),
        oos_gross_bps=(_mean(oos_g) if oos_g else None),
        oos_fee_adjusted_bps=(_mean(oos_n) if oos_n else None),
    )


def _summary_from_periods(name: str, periods: Sequence[PortfolioPeriod], oos_split_ts: int,
                          notional: float = NOTIONAL) -> HypothesisSummary:
    gross = [(p.gross_ref_pnl + p.commission_ref) / notional * 1e4 for p in periods]
    net = [p.gross_ref_pnl / notional * 1e4 for p in periods]
    oos_g = [g for g, p in zip(gross, periods, strict=True) if p.ts > oos_split_ts]
    oos_n = [n for n, p in zip(net, periods, strict=True) if p.ts > oos_split_ts]
    return HypothesisSummary(
        name=name, conditions=f"frozen ROB-351 family params; OOS split {oos_split_ts}",
        sample_count=len(periods),
        gross_expectancy_bps=_mean(gross), fee_adjusted_bps=_mean(net),
        oos_gross_bps=(_mean(oos_g) if oos_g else None),
        oos_fee_adjusted_bps=(_mean(oos_n) if oos_n else None),
    )


def _panel_to_bars(series: Sequence[tuple[int, float]]) -> list[families.Bar]:
    """Build single-symbol Bars from a (ts, close) series (high=low=close; OHLC-from-close)."""
    return [families.Bar(ts=ts, high=c, low=c, close=c) for ts, c in series]


def breakout_spec(panel: dict[str, Sequence[tuple[int, float]]], oos_split_ts: int = OOS_SPLIT_TS) -> dict:
    pooled: list[Trade] = []
    for symbol in sorted(panel):
        pooled.extend(families.breakout_continuation_trades(_panel_to_bars(panel[symbol]), notional=NOTIONAL))
    pooled.sort(key=lambda t: t.ts_opened)
    return {"name": "family1_breakout_continuation",
            "summary": _summary_from_trades("family1_breakout_continuation", pooled, oos_split_ts),
            "kind": "trade", "data": pooled, "maker_conservative_net": None}


def ts_trend_spec(panel: dict[str, Sequence[tuple[int, float]]], oos_split_ts: int = OOS_SPLIT_TS) -> dict:
    periods = families.ts_trend_basket_periods(panel, notional=NOTIONAL)
    return {"name": "family2_ts_trend_basket",
            "summary": _summary_from_periods("family2_ts_trend_basket", periods, oos_split_ts),
            "kind": "portfolio", "data": periods, "maker_conservative_net": None}


def xs_momentum_spec(panel, rebalances, manifest, oos_split_ts: int = OOS_SPLIT_TS) -> dict:
    periods = families.xs_momentum_periods(panel, rebalances, notional=NOTIONAL, manifest=manifest)
    return {"name": "family3_xs_momentum",
            "summary": _summary_from_periods("family3_xs_momentum", periods, oos_split_ts),
            "kind": "portfolio", "data": periods, "maker_conservative_net": None}
