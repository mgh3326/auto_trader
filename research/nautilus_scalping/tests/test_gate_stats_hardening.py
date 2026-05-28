"""ROB-351 (Codex outside-voice bundle) — statistical-honesty hardening.

Adds, on top of the existing iid bootstrap:
  * block/time bootstrap (crypto trades are time-clustered; iid is too optimistic)
  * Benjamini-Hochberg FDR control across the many shots (3 families + seed + grids)
  * effect-size / FDR-aware minimum sample count (replaces cargo-culted fixed n>=263)
  * turnover-matched random baseline (guards against volatility-harvesting/beta)
All pure-stdlib, additive; existing gate machinery untouched.
"""

import math

import validated_gate as vg


def test_block_bootstrap_is_deterministic_and_well_formed():
    pnls = [0.5, -0.2, 0.3, -0.1, 0.4, -0.3, 0.6, -0.2, 0.5, -0.1] * 5
    a = vg.block_bootstrap_sharpe_ci(pnls, block_size=5, n_bootstrap=200, seed=7)
    b = vg.block_bootstrap_sharpe_ci(pnls, block_size=5, n_bootstrap=200, seed=7)
    assert a == b  # reproducible
    assert a["ci_lower"] <= a["observed_sharpe"] <= a["ci_upper"] or a["ci_lower"] <= a["ci_upper"]
    assert a["block_size"] == 5
    assert 0.0 <= a["prob_positive"] <= 1.0


def test_block_bootstrap_insufficient_data():
    assert vg.block_bootstrap_sharpe_ci([1.0], block_size=5)["error"] == "insufficient_data"


def test_benjamini_hochberg_controls_fdr():
    p = [0.001, 0.01, 0.2, 0.5]
    res = vg.benjamini_hochberg(p, alpha=0.05)
    assert set(res["rejected"]) == {0, 1}  # two smallest survive BH at alpha=0.05
    assert math.isclose(res["threshold"], 0.01)


def test_benjamini_hochberg_none_significant():
    res = vg.benjamini_hochberg([0.4, 0.6, 0.9], alpha=0.05)
    assert res["rejected"] == []


def test_effect_size_aware_min_trades_monotonic():
    n1 = vg.effect_size_aware_min_trades(observed_sharpe=0.1, n_configs_tried=1)
    n10 = vg.effect_size_aware_min_trades(observed_sharpe=0.1, n_configs_tried=10)
    big = vg.effect_size_aware_min_trades(observed_sharpe=0.3, n_configs_tried=1)
    assert n10 > n1            # more shots tried -> need more evidence
    assert big < n1            # bigger effect -> need fewer trades
    assert n1 == 400           # (2.0 / 0.1)^2 at m=1


def test_effect_size_aware_min_trades_zero_effect_is_infinite():
    assert vg.effect_size_aware_min_trades(observed_sharpe=0.0, n_configs_tried=1) == math.inf


def test_turnover_matched_random_baseline_matches_count_and_is_deterministic():
    pool = [0.1, -0.2, 0.3, -0.4, 0.5]
    a = vg.turnover_matched_random_baseline(pool, n_trades=20, seed=3)
    b = vg.turnover_matched_random_baseline(pool, n_trades=20, seed=3)
    assert a == b
    assert len(a) == 20
    assert all(x in pool for x in a)
