"""ROB-384 — adapters from the three local result schemas to CandidateEvidence.

All three are counts-only artifacts already on disk (no raw market data):

* ``validated_signal_gate.v1`` — ROB-320 ``meanrev.json`` (single report) and
  ROB-383 ``phase3_validation.json`` (``plan`` + per-candidate reports).
* ``rob382_falsification.v1``  — ROB-382 ``rob382_falsification.v1.json``
  (per-candidate explicit gross/net bps + t-stats).
* ``rob351_campaign.v1``       — ROB-353 ``rob351_campaign.v1.json``
  (per-family gross-expectancy screen).

Fee subtlety, verified against the artifacts: in the GateReport family the
``gross`` fold is the zero-fee run (fee = 0). ROB-320's ``net_after_cost`` is at
10 bps (REF), but ROB-383's ``net_after_cost`` is at 4 bps (demo taker) and the
report also carries an authoritative ``fee_sweep_net_pnl`` whose ``0.0bps`` ==
gross and whose ``10.0bps`` is the REF endpoint. We always anchor the fee grid
on (gross @ 0 bps, net @ 10 bps), preferring the explicit sweep's 10 bps point
when present and falling back to ``net_after_cost`` only when no sweep exists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from external_strategy_sieve.postmortem import fees
from external_strategy_sieve.postmortem.evidence import CandidateEvidence

_NOTIONAL = 1000.0  # sieve / validated-gate bps convention
_FOLDS = ("train", "val", "oos")


def load_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# validated_signal_gate.v1 — shared report extractor
# --------------------------------------------------------------------------- #


def _fold_net_map(per_fold: list[dict]) -> dict[str, float]:
    return {f.get("fold", ""): float(f.get("net_pnl", 0.0)) for f in per_fold}


def _single_fold_edge(
    *,
    gross_pnl: float,
    fold_net: dict[str, float],
    reasons: list[str],
    explicit_flag: bool | None,
) -> bool:
    """Authoritative flag if recorded, else text reason, else fold-sign heuristic.

    Heuristic: a gross-positive candidate whose net is positive in exactly one of
    train/val/oos has its edge concentrated in a single fold.
    """
    if explicit_flag is not None:
        return bool(explicit_flag)
    if any(("one fold" in r) or ("single fold" in r) for r in reasons):
        return True
    positive = sum(1 for f in _FOLDS if fold_net.get(f, 0.0) > 0)
    return gross_pnl > 0 and positive == 1


def _net10_pnl(report: dict) -> tuple[float, str]:
    """Net PnL at 10 bps (REF) + a note on where it came from."""
    sweep = report.get("fee_sweep_net_pnl")
    if isinstance(sweep, dict) and "10.0bps" in sweep:
        return float(sweep["10.0bps"]), "fee_sweep_net_pnl[10.0bps]"
    # No sweep -> net_after_cost is the REF (10 bps) run (ROB-320 convention).
    return float(
        report["results"]["net_after_cost"]["net_pnl"]
    ), "results.net_after_cost (REF=10bps)"


def _baseline_beat(
    report: dict, candidate_net_pnl: float
) -> tuple[dict[str, bool], str]:
    """Candidate net vs each recorded baseline's net_after_cost (same fee)."""
    out: dict[str, bool] = {}
    base = report.get("baselines", {})
    for name, b in base.items():
        if isinstance(b, dict) and "net_after_cost" in b:
            out[name] = candidate_net_pnl > float(b["net_after_cost"])
    note = "" if out else "no baselines recorded"
    return out, note


def _from_gate_report(
    *,
    report: dict,
    issue: str,
    candidate: str,
    family: str,
    citation: str,
    verdict_override: str = "",
) -> CandidateEvidence:
    results = report["results"]
    trade_count = int(report.get("trade_count") or results["gross"].get("trades") or 0)
    gross_pnl = float(results["gross"]["net_pnl"])
    net10_pnl, net10_src = _net10_pnl(report)

    grid_pnl = fees.fee_grid(gross_pnl, net10_pnl)
    per_trade = (
        (lambda pnl: fees.expectancy_to_bps(pnl / trade_count, _NOTIONAL))
        if trade_count
        else (lambda _pnl: 0.0)
    )
    net_bps = {fee: per_trade(pnl) for fee, pnl in grid_pnl.items()}
    gross_bps = per_trade(gross_pnl)

    per_fold = report.get("per_fold", [])
    fold_net = _fold_net_map(per_fold)
    reasons = list(report.get("reasons", [])) + list(report.get("verdict_reasons", []))
    explicit = (report.get("overfit_flags") or {}).get("single_fold_edge")
    single_fold = _single_fold_edge(
        gross_pnl=gross_pnl, fold_net=fold_net, reasons=reasons, explicit_flag=explicit
    )

    # Baselines are recorded at the report's own net fee (its net_after_cost run),
    # so compare the candidate's net_after_cost against them at that same fee.
    native_net = float(results["net_after_cost"]["net_pnl"])
    beat, beat_note = _baseline_beat(report, native_net)

    oos_trades = next(
        (
            int(f["trades"])
            for f in per_fold
            if f.get("fold") == "oos" and f.get("trades") is not None
        ),
        None,
    )
    verdict = verdict_override or report.get("verdict", "")
    return CandidateEvidence(
        issue=issue,
        candidate=candidate,
        family=family,
        source="reparsed",
        schema="validated_signal_gate.v1",
        citation=citation,
        gross_bps=gross_bps,
        net_bps_by_fee=net_bps,
        trade_count=trade_count,
        oos_trade_count=oos_trades,
        n_folds=len([f for f in per_fold if f.get("fold") in _FOLDS]) or None,
        fold_net=fold_net,
        single_fold_edge=single_fold,
        verdict=verdict,
        baseline_beat=beat,
        baseline_note=f"net@10bps via {net10_src}. " + beat_note,
        notes="; ".join(reasons),
    )


def from_meanrev(path: str | Path, issue: str = "ROB-320") -> list[CandidateEvidence]:
    report = load_json(path)
    return [
        _from_gate_report(
            report=report,
            issue=issue,
            candidate=report.get("candidate", "meanrev_zscore_fade"),
            family="mean-reversion z-score fade",
            citation=f"{path} (validated_signal_gate.v1)",
        )
    ]


_PHASE3_FAMILY = {
    "freqtrade_supertrend": "trend (Supertrend ATR trail)",
    "freqtrade_bbrsi_naive": "mean-reversion (Bollinger + RSI)",
    "tv_squeeze_momentum": "volatility (TTM squeeze momentum)",
    "tv_range_filter": "trend (range filter)",
    "tv_chandelier_exit": "trend (chandelier exit)",
}


def from_phase3(path: str | Path, issue: str = "ROB-383") -> list[CandidateEvidence]:
    doc = load_json(path)
    results = doc.get("results", doc)
    out: list[CandidateEvidence] = []
    for name, report in results.items():
        klass = report.get("class", "")
        ev = _from_gate_report(
            report=report,
            issue=issue,
            candidate=name,
            family=_PHASE3_FAMILY.get(name, "other"),
            citation=f"{path}::results.{name} (validated_signal_gate.v1, sieve class={klass})",
            verdict_override=f"{report.get('verdict', '')} / sieve_class={klass}",
        )
        caveat = report.get("caveat")
        if caveat:
            ev.notes = (ev.notes + "; caveat: " + caveat).strip("; ")
        out.append(ev)
    return out


# --------------------------------------------------------------------------- #
# rob382_falsification.v1
# --------------------------------------------------------------------------- #


def from_falsification(
    path: str | Path, issue: str = "ROB-382"
) -> list[CandidateEvidence]:
    doc = load_json(path)
    out: list[CandidateEvidence] = []
    for c in doc.get("candidates", []):
        gross_bps = _f(c.get("our_gross_bps"))
        net10 = _f(c.get("our_net_bps_retail_ref"))  # retail 20 bps RT == 10 bps/leg
        net_bps: dict[str, float] = {}
        if gross_bps is not None and net10 is not None:
            net_bps = fees.fee_grid(gross_bps, net10)
        # gate_verdict / our_verdict / t-stats are top-level keys in this schema.
        gate_v = c.get("gate_verdict", "")
        our_v = c.get("our_verdict", "")
        decisive = c.get("meets_decisive_survivor_bar")
        beat: dict[str, bool] = {}
        if "beats_micro_breakout_baseline" in c:
            beat["micro_breakout"] = bool(c["beats_micro_breakout_baseline"])
        if "beats_random_baseline" in c:
            beat["random_same_turnover"] = bool(c["beats_random_baseline"])
        out.append(
            CandidateEvidence(
                issue=issue,
                candidate=c.get("name", "?"),
                family=c.get("family_shape")
                or f"freqtrade-leaderboard {c.get('native_timeframe', '?')} signal",
                source="reparsed",
                schema="rob382_falsification.v1",
                citation=f"{path}::candidates[{c.get('name')}] (rob382_falsification.v1)",
                gross_bps=gross_bps,
                net_bps_by_fee=net_bps,
                trade_count=_i(c.get("trade_count")),
                oos_trade_count=_i(c.get("oos_trade_count")),
                t_stat_gross=_f(c.get("our_t_stat_gross")),
                t_stat_oos=_f(c.get("our_t_stat_oos_gross")),
                verdict=f"{our_v} / gate={gate_v} / decisive_survivor={decisive}",
                baseline_beat=beat,
                baseline_note=(
                    "beats_* flags are vs micro-breakout and random baselines (re-derived on "
                    "USDⓈ-M); strat.ninja in-sample SPOT numbers are CONTRAST ONLY, not evidence"
                ),
                notes=(
                    f"native_tf={c.get('native_timeframe')}; "
                    f"oos gross {c.get('our_oos_gross_bps')} bps, "
                    f"oos net@frozen-taker {c.get('our_oos_net_bps_frozen_taker')} bps, "
                    f"oos net@retail {c.get('our_oos_net_bps_retail_ref')} bps; "
                    f"target_t={c.get('target_t')}; decisive_survivor={decisive}"
                ).strip(),
            )
        )
    return out


def falsification_overall_verdict(path: str | Path) -> str:
    return load_json(path).get("overall_verdict", "")


# --------------------------------------------------------------------------- #
# rob351_campaign.v1  (ROB-353)
# --------------------------------------------------------------------------- #

_GROSS_BPS_RE = re.compile(r"gross[^-\d]*(-?\d+(?:\.\d+)?)\s*bps", re.IGNORECASE)


def _gross_bps_from_reason(reason: str) -> float | None:
    m = _GROSS_BPS_RE.search(reason or "")
    return float(m.group(1)) if m else None


def from_campaign(path: str | Path, issue: str = "ROB-353") -> list[CandidateEvidence]:
    doc = load_json(path)
    table = doc.get("verdict_table", doc)
    controls = doc.get("controls", {})
    sample_counts = doc.get("spec_sample_counts", {})
    btc_bh = controls.get("btc_buy_hold_bps")
    out: list[CandidateEvidence] = []
    for fam in table.get("families", []):
        name = fam.get("name", "?")
        gross_bps = _gross_bps_from_reason(fam.get("screen_reason", ""))
        cost_binding = bool(fam.get("cost_binding_screen", False))
        # All families screened on gross; gross <= 0 makes net moot.
        beat: dict[str, bool] = {}
        if btc_bh is not None and gross_bps is not None:
            beat["buy_and_hold_btc"] = gross_bps > float(btc_bh)
        out.append(
            CandidateEvidence(
                issue=issue,
                candidate=name,
                family=name.replace("family", "family ").replace("_", " "),
                source="reparsed",
                schema="rob351_campaign.v1",
                citation=f"{path}::verdict_table.families[{name}] (rob351_campaign.v1)",
                gross_bps=gross_bps,
                net_moot_reason=(
                    f"gross {gross_bps} bps <= 0 -> net is moot; cost_binding_screen={cost_binding} "
                    "(fees are NOT the bottleneck)"
                ),
                trade_count=_i(sample_counts.get(name)),
                verdict=f"{fam.get('screen', '')} (gate={fam.get('gate_verdict')}, label_343={fam.get('label_343')})",
                baseline_beat=beat,
                baseline_note=(
                    f"BTC buy&hold over window +{btc_bh:.0f} bps; every family far below "
                    "(passive long-BTC / cash dominates)"
                    if btc_bh is not None
                    else "no baseline recorded"
                ),
                notes=fam.get("screen_reason", ""),
            )
        )
    return out


def campaign_controls(path: str | Path) -> dict:
    return load_json(path).get("controls", {})


# --------------------------------------------------------------------------- #


def _f(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _i(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
