# tests/test_validated_gate_stats.py
"""ROB-328 (ROB-327 F1) — statistical-significance + run-card additions to the
pure validated-signal gate.

These are additive: bootstrap Sharpe CI, Monte-Carlo permutation p-values,
config/strategy/artifact SHA-256 hashes, and a json+markdown run card. All
pure-stdlib (no numpy), seeded for reproducibility. The framing is *not* "find
an edge" but "prove the net-after-fee result is statistically robust" — see
ROB-316/320 (fees kill the scalper).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from validated_gate import (
    GateReport,
    Trade,
    _equity_path_metrics,
    apply_statistical_evidence,
    bootstrap_sharpe_ci,
    evaluate_gate,
    monte_carlo_permutation,
    net_pnls_at_fee,
    run_card_hashes,
    write_run_card,
)


# --------------------------------------------------------------------------- #
# net_pnls_at_fee (driver helper)
# --------------------------------------------------------------------------- #
def test_net_pnls_at_fee_mirrors_per_trade_net() -> None:
    trades = [Trade(2.0, -0.2, 100, 0), Trade(-1.0, -0.2, 100, 1)]
    # REF_FEE_BPS=10 => scale 0 => net == net_ref_pnl
    assert net_pnls_at_fee(trades, fee_bps=10.0) == [2.0, -1.0]
    # fee 0 => scale 1 => net == net_ref_pnl + commission_ref
    assert net_pnls_at_fee(trades, fee_bps=0.0) == [1.8, -1.2]


# --------------------------------------------------------------------------- #
# bootstrap_sharpe_ci
# --------------------------------------------------------------------------- #
def test_bootstrap_all_positive_ci_strictly_positive() -> None:
    pnls = [1.0, 2.0, 1.5, 2.5, 1.0, 2.0, 3.0, 1.5] * 8  # all > 0, varied
    out = bootstrap_sharpe_ci(pnls, n_bootstrap=500, seed=7)
    assert out["observed_sharpe"] > 0
    assert out["ci_lower"] <= out["ci_upper"]
    assert out["ci_lower"] > 0           # every resample of positives is positive
    assert out["prob_positive"] == 1.0


def test_bootstrap_all_negative_ci_upper_below_zero() -> None:
    pnls = [-1.0, -2.0, -1.5, -2.5, -1.0, -2.0, -3.0, -1.5] * 8
    out = bootstrap_sharpe_ci(pnls, n_bootstrap=500, seed=7)
    assert out["observed_sharpe"] < 0
    assert out["ci_upper"] < 0           # net edge statistically <= 0
    assert out["prob_positive"] == 0.0


def test_bootstrap_is_seeded_reproducible() -> None:
    pnls = [0.5, -0.3, 0.2, -0.1, 0.4, -0.6, 0.1, 0.3] * 8
    a = bootstrap_sharpe_ci(pnls, n_bootstrap=300, seed=42)
    b = bootstrap_sharpe_ci(pnls, n_bootstrap=300, seed=42)
    assert a == b


def test_bootstrap_insufficient_data() -> None:
    out = bootstrap_sharpe_ci([1.0], n_bootstrap=100, seed=1)
    assert out.get("error") is not None


def test_bootstrap_zero_variance_is_bounded_and_signed() -> None:
    # constant series => std 0; sign preserved, magnitude bounded (no 5e11 blowup)
    out = bootstrap_sharpe_ci([-0.5] * 100, n_bootstrap=200, seed=1)
    assert out["observed_sharpe"] < 0
    assert out["ci_upper"] < 0
    assert abs(out["observed_sharpe"]) <= 1e6 + 1
    assert out["prob_positive"] == 0.0


# --------------------------------------------------------------------------- #
# equity path metrics + Monte-Carlo permutation
# --------------------------------------------------------------------------- #
def test_equity_path_max_drawdown_is_absolute_on_cumulative_pnl() -> None:
    sharpe, max_dd = _equity_path_metrics([100.0, -50.0, -30.0], base_capital=1000.0)
    # cumsum 100, 50, 20 ; running peak 100 ; deepest dd = 20 - 100 = -80
    assert round(max_dd, 6) == -80.0
    assert isinstance(sharpe, float)


def test_monte_carlo_pvalues_in_unit_interval_and_seeded() -> None:
    pnls = [1.0, -0.8, 0.5, -1.2, 0.9, -0.3, 0.4, -0.7] * 8
    a = monte_carlo_permutation(pnls, n_sim=300, seed=11)
    b = monte_carlo_permutation(pnls, n_sim=300, seed=11)
    assert a == b
    assert 0.0 <= a["p_value_sharpe"] <= 1.0
    assert 0.0 <= a["p_value_maxdd"] <= 1.0
    assert a["n_sim"] == 300


def test_monte_carlo_insufficient_data() -> None:
    out = monte_carlo_permutation([1.0, -1.0], n_sim=100, seed=1)
    assert out.get("error") is not None


# --------------------------------------------------------------------------- #
# run_card_hashes
# --------------------------------------------------------------------------- #
def test_config_hash_is_key_order_independent_sha256() -> None:
    h1 = run_card_hashes({"a": 1, "b": 2})["config_hash"]
    h2 = run_card_hashes({"b": 2, "a": 1})["config_hash"]
    assert h1 == h2
    expected = hashlib.sha256(
        json.dumps({"a": 1, "b": 2}, sort_keys=True).encode()
    ).hexdigest()
    assert h1 == expected


def test_strategy_and_artifact_hashes_match_file_contents(tmp_path: Path) -> None:
    strat = tmp_path / "signal_engine.py"
    strat.write_bytes(b"print('hi')\n")
    art = tmp_path / "equity.csv"
    art.write_bytes(b"ts,equity\n0,0\n")

    out = run_card_hashes({"x": 1}, strategy_path=strat, artifact_paths=[art])
    assert out["strategy_hash"] == hashlib.sha256(strat.read_bytes()).hexdigest()
    assert out["artifacts"][0]["path"] == str(art)
    assert out["artifacts"][0]["sha256"] == hashlib.sha256(art.read_bytes()).hexdigest()


def test_strategy_hash_none_when_absent() -> None:
    assert run_card_hashes({"x": 1})["strategy_hash"] is None


# --------------------------------------------------------------------------- #
# write_run_card
# --------------------------------------------------------------------------- #
def _losing_report() -> GateReport:
    losing = [Trade(-0.5, -0.1, 100, ts) for ts in range(400)]
    return evaluate_gate(
        candidate_runs={"z2.0/tp30/sl30": losing},
        baseline_breakout=losing, baseline_random=losing,
        fee_bps=10.0, min_trades=100, fractions=(0.5, 0.25, 0.25),
        candidate_name="meanrev_demo", hypothesis="mean_reversion",
    )


def test_write_run_card_emits_json_and_md(tmp_path: Path) -> None:
    report = _losing_report()
    bs = bootstrap_sharpe_ci([-0.5] * 400, n_bootstrap=200, seed=3)
    paths = write_run_card(
        report, tmp_path, config={"fee_bps": 10.0},
        data_sources=["binance_demo"], bootstrap=bs,
    )
    assert paths["json"].exists() and paths["md"].exists()

    card = json.loads(paths["json"].read_text())
    assert card["schema_version"] == "validated_run_card.v1"
    assert card["verdict"] == report.verdict
    assert card["reproducibility"]["config_hash"]
    assert card["validation"]["bootstrap"] == bs

    md = paths["md"].read_text()
    assert "net-after-fee" in md.lower()
    assert "ROB-316" in md          # fee-kill framing is explicit in the card
    assert report.verdict in md


# --------------------------------------------------------------------------- #
# apply_statistical_evidence (verdict augmentation)
# --------------------------------------------------------------------------- #
def test_negative_ci_appends_reason_and_keeps_not_validated() -> None:
    report = _losing_report()
    assert report.verdict == "not_validated"
    bs = bootstrap_sharpe_ci([-0.5, -0.7, -0.4, -0.6] * 50, n_bootstrap=200, seed=5)
    apply_statistical_evidence(report, bs)
    assert report.verdict == "not_validated"
    assert any("statistic" in r.lower() for r in report.verdict_reasons)


def test_negative_ci_downgrades_a_validated_verdict() -> None:
    report = GateReport(verdict="validated", verdict_reasons=["passed gate"])
    bs = bootstrap_sharpe_ci([-1.0, -2.0, -1.5, -0.5] * 50, n_bootstrap=200, seed=5)
    apply_statistical_evidence(report, bs)
    assert report.verdict == "not_validated"   # statistical guard overrides
    assert any("statistic" in r.lower() for r in report.verdict_reasons)


def test_positive_ci_does_not_upgrade_not_validated() -> None:
    report = GateReport(verdict="not_validated", verdict_reasons=["thin oos"])
    bs = bootstrap_sharpe_ci([1.0, 2.0, 1.5, 0.5] * 50, n_bootstrap=200, seed=5)
    apply_statistical_evidence(report, bs)
    assert report.verdict == "not_validated"    # never flips up; gate owns that
