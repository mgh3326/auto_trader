"""Holm order, null-centred bootstrap p, reproducibility, verdict gating."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from research.rob1391_us_resubmit_shadow import engine
from research.rob1391_us_resubmit_shadow import report as R
from research.rob1391_us_resubmit_shadow.stats import (
    PairedRow,
    cluster_bootstrap,
    holm,
    null_centred_p,
    ratio,
)
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    FakeMarket,
    FakeSource,
    context,
    seal,
)

pytestmark = pytest.mark.unit


def test_holm_smallest_against_0025_then_005():
    assert holm({"m1": 0.02, "m2": 0.04}) == {"m1": True, "m2": True}
    assert holm({"m1": 0.04, "m2": 0.02}) == {"m2": True, "m1": True}
    assert holm({"m1": 0.026, "m2": 0.03}) == {
        "m1": False,
        "m2": False,
    }  # first fails -> all fail
    assert holm({"m1": 0.025, "m2": 0.05}) == {
        "m1": True,
        "m2": True,
    }  # inclusive bounds
    assert holm({"m1": 0.01, "m2": 0.051}) == {"m1": True, "m2": False}


def test_holm_nan_never_passes_and_ties_are_deterministic():
    assert holm({"a": float("nan"), "b": 0.001}) == {"b": True, "a": False}
    assert list(holm({"b": 0.01, "a": 0.01})) == ["a", "b"]


def test_null_centred_p_formula():
    boot = np.array([0.0, 0.1, 0.2, 0.3])
    # boot - 0.1 >= 0.1  <=> boot >= 0.2 -> 2 of 4
    assert null_centred_p(boot, 0.1) == pytest.approx(3 / 5)


def rows(n_symbols=12, per=3, effect=0.1, noise=0.05, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for s in range(n_symbols):
        for _ in range(per):
            out.append(
                PairedRow(
                    cluster=f"S{s:02d}",
                    budget=300.0,
                    diff=300.0 * (effect + rng.normal(0, noise)),
                )
            )
    return out


def test_cluster_bootstrap_is_seeded_and_reports_denominator():
    data = rows()
    a, b = cluster_bootstrap(data), cluster_bootstrap(data)
    assert a == b
    assert a.n_clusters == 12 and a.n_rows == 36 and a.denominator == 36 * 300.0
    assert a.estimate == pytest.approx(ratio(data))
    assert a.ci_low < a.estimate < a.ci_high and a.p_one_sided < 0.01


def test_null_effect_has_large_p():
    res = cluster_bootstrap(rows(effect=0.0, noise=0.05, seed=3))
    assert res.p_one_sided > 0.05


def test_empty_is_nan_not_zero():
    res = cluster_bootstrap([])
    assert math.isnan(res.estimate) and math.isnan(res.p_one_sided)


def _metrics(**over):
    m = {
        "n_parents": 40,
        "n_symbols": 12,
        "unknown_total": 0,
        "M2_T": 0.01,
        "holm": {"delta_M1": True, "delta_M2": True},
        "primary": {"delta_M1": {"estimate": 0.2}, "delta_M2": {"estimate": 0.01}},
    }
    m.update(over)
    return m


def _state(tmp_path, fee=True):
    ledger = seal(tmp_path, fee=fee)
    return context(ledger, FakeSource(), FakeMarket()).state


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"n_parents": 29}, "parents_below_30"),
        ({"n_symbols": 9}, "symbols_below_10"),
        ({"unknown_total": 1}, "unknown_nonzero"),
        ({"holm": {"delta_M1": True, "delta_M2": False}}, "holm_not_passed"),
        (
            {
                "primary": {
                    "delta_M1": {"estimate": 0.0},
                    "delta_M2": {"estimate": 0.01},
                }
            },
            "delta_M1_not_positive",
        ),
        ({"M2_T": 0.0}, "M2_T_not_positive"),
    ],
)
def test_every_candidate_condition_is_required(tmp_path, over, reason):
    state = _state(tmp_path)
    assert R.verdict(state, _metrics()) == (R.CANDIDATE, [])
    verdict, reasons = R.verdict(state, _metrics(**over))
    assert verdict == R.RESERVED and reason in reasons


def test_stop_or_net_loss_is_risk_signal(tmp_path):
    state = _state(tmp_path)
    assert R.verdict(state, _metrics(M2_T=-0.001))[0] == R.RISK
    state.latches.append({"latch_type": "loss", "reason": "x", "session": None})
    assert R.verdict(state, _metrics())[0] == R.RISK


def test_pending_fee_schedule_withholds_net_tests(tmp_path):
    state = _state(tmp_path, fee=False)
    verdict, reasons = R.verdict(state, _metrics())
    assert (
        verdict == R.RESERVED and "net_tests_withheld_pending_fee_schedule" in reasons
    )


def test_interim_report_has_no_metrics_or_p_values(tmp_path):
    ledger = seal(tmp_path)
    ctx = context(ledger, FakeSource(), FakeMarket())
    engine.advance(ctx, date(2025, 11, 20))
    rep = R.build_report(ctx.state)
    assert rep["verdict"] == R.INTERIM and "metrics" not in rep
    assert "인과 확증" not in R.render_markdown(rep).replace("인과 확증이 아니며", "")
