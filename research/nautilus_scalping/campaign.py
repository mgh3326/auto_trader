"""ROB-351 (Stage 3) — cost-blind funnel driver (pure, stdlib).

Ties the pieces into one verdict table:

  Stage 1  cost-blind gross screen   (discovery.screen.classify cost_blind=True)
  Stage 2  cost/OOS gauntlet         (validated_gate.evaluate_gate[_portfolio])
  Stage 3  ROB-343 hand-off label    (rob343_label.label_343_candidate)

Each family spec carries: ``name``; a ``HypothesisSummary`` for Stage 1; the gate
``kind`` ("trade"|"portfolio") + ``data`` (Trade list or PortfolioPeriod list);
and an optional ``maker_conservative_net`` (the maker_fill pure-path evidence
needed to call a candidate closable — without it a cost-binding family cannot be
a 343 hand-off, only ``reject``).

The run records the frozen-config hash (ex-ante evidence). NO market data is read
here; the empirical RUN against Binance USDⓈ-M data is the operator's PR2 step.
"""

from __future__ import annotations

import rob343_label
import validated_gate as vg
from discovery.screen import classify
from frozen_config import FROZEN_CONFIG, CampaignConfig

SCHEMA_VERSION = "rob351_campaign.v1"


def _gate_trade(data: list, fee_bps: float, min_trades: int) -> tuple[vg.GateReport, float, float, float]:
    rep = vg.evaluate_gate(
        candidate_runs={"p": data},
        baseline_breakout=[], baseline_random=[],
        fee_bps=fee_bps, min_trades=min_trades,
    )
    gross = rep.results.get("gross", {}).get("net_pnl", 0.0)
    net = rep.results.get("net_after_cost", {}).get("net_pnl", 0.0)
    breakeven = rob343_label.breakeven_taker_fee_bps(data)
    return rep, gross, net, breakeven


def _gate_portfolio(data: list, fee_bps: float, min_periods: int) -> tuple[vg.GateReport, float, float, float]:
    rep = vg.evaluate_gate_portfolio(
        candidate_runs={"p": data}, baseline_periods=[],
        fee_bps=fee_bps, min_periods=min_periods,
    )
    gross = rep.results.get("gross", {}).get("net_pnl", 0.0)
    net = rep.results.get("net_after_cost", {}).get("net_pnl", 0.0)
    breakeven = rob343_label.breakeven_taker_fee_bps_from_sums(
        sum(p.gross_ref_pnl for p in data), sum(p.commission_ref for p in data)
    )
    return rep, gross, net, breakeven


def run_campaign(
    specs: list[dict],
    config: CampaignConfig = FROZEN_CONFIG,
    min_trades: int = 100,
    fee_bps: float | None = None,
) -> dict:
    """Run every family spec through the funnel; return the verdict-table artifact."""
    fee_bps = config.taker_bps if fee_bps is None else fee_bps
    rows: list[dict] = []
    for spec in specs:
        name = spec["name"]
        summary = spec["summary"]
        classified = classify(
            summary, cost_blind=True,
            min_samples=min(summary.sample_count, min_trades) if min_trades else 1,
            min_gross_bps=config.economic_triviality_floor_bps,
        )
        row = {
            "name": name,
            "screen": classified.recommendation,
            "cost_binding_screen": classified.cost_binding,
            "screen_reason": classified.reason,
            "gate_verdict": None,
            "label_343": None,
            "label_343_reason": None,
        }
        if classified.recommendation != "promote_to_full_validation":
            rows.append(row)
            continue

        if spec["kind"] == "portfolio":
            rep, gross, net, breakeven = _gate_portfolio(spec["data"], fee_bps, min_trades)
        else:
            rep, gross, net, breakeven = _gate_trade(spec["data"], fee_bps, min_trades)
        row["gate_verdict"] = rep.verdict
        oos_significant = rep.verdict != "insufficient_data"

        verdict = rob343_label.label_343_candidate(
            taker_net_pnl=net, gross_pnl=gross,
            maker_conservative_net=(spec.get("maker_conservative_net") or 0.0),
            oos_significant=oos_significant,
            breakeven_taker_bps=breakeven,
        )
        row["label_343"] = verdict.label
        row["label_343_reason"] = verdict.reason
        row["closable"] = verdict.closable
        row["breakeven_taker_bps"] = breakeven
        rows.append(row)

    return {
        "schema_version": SCHEMA_VERSION,
        "config_hash": config.config_hash(),
        "config": config.to_dict(),
        "families": rows,
        "note": (
            "Non-canonical research funnel. Labels are reject / needs_more_data / "
            "promote_to_pilot / cost_binding_343_candidate. canonical 'validated' is "
            "owned by the conservative gate; ROB-343 is only RECOMMENDED, never run here. "
            "Empirical verdicts require the operator data RUN (no market data committed)."
        ),
    }
