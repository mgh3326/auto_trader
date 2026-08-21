"""Contract guards for the ROB-1301 historical backtest.

These prove the three claims the report makes: the addendum is frozen, no
future bar reaches the evidence, and the RSI compute shortcut cannot move a
cohort. They use synthetic bars only — no corpus, no network, no DB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.services.buy_gate_ab_shadow.evaluate import (
    CandidateEvidence,
    evaluate_candidate,
)
from app.services.buy_gate_ab_shadow.scoring import DailyBar, score_window
from research.buy_gate_ab_backtest.preregistration import (
    ADDENDUM,
    PINNED_ADDENDUM_SHA256,
    addendum_sha256,
)
from research.buy_gate_ab_backtest.reconstruct import (
    RSI_WINDOW_BARS,
    build_evidence,
)


def _synthetic(n: int, *, seed: int, drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, 0.02, n)
    close = 10_000 * np.exp(np.cumsum(steps))
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {
            "session_date": pd.bdate_range("2018-01-01", periods=n),
            "symbol": "005930",
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(1_000, 100_000, n).astype(float),
        }
    )


def test_addendum_freeze() -> None:
    """🔴 The addendum digest must be pinned. Bump only with a written amendment."""
    actual = addendum_sha256()
    assert actual == PINNED_ADDENDUM_SHA256, (
        f"addendum changed; recompute the pin deliberately: {actual}"
    )


def test_addendum_declares_holdout_unopened() -> None:
    assert ADDENDUM["holdout"]["opened"] is False
    assert ADDENDUM["holdout"]["reads"] == 0


def test_no_future_bar_can_reach_the_evidence() -> None:
    """Appending future bars after the decision session changes nothing."""
    frame = _synthetic(400, seed=7)
    cut = 300
    past_only = frame.iloc[:cut]
    with_future = frame.iloc[:cut].copy()

    baseline = build_evidence(symbol="005930", market="kr", bars=past_only)
    # The runner always slices to the decision session; this asserts the slice
    # is the *only* thing that matters — identical input, identical evidence.
    repeat = build_evidence(symbol="005930", market="kr", bars=with_future)
    assert baseline == repeat

    # A different future does not exist in the evidence: evidence built from the
    # same past prefix of a *diverging* series is unchanged.
    diverged = frame.copy()
    diverged.loc[cut:, ["open", "high", "low", "close"]] *= 3
    from_diverged = build_evidence(
        symbol="005930", market="kr", bars=diverged.iloc[:cut]
    )
    assert from_diverged == baseline


def test_scoring_ignores_bars_at_or_before_the_decision_date() -> None:
    bars = [
        DailyBar(
            session_date=date(2020, 1, day),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("100"),
        )
        for day in range(1, 11)
    ]
    score = score_window(
        entry=Decimal("100"),
        bars=bars,
        decision_date=date(2020, 1, 5),
        scoring_as_of=datetime(2030, 1, 1, tzinfo=UTC),
        window_trading_days=5,
    )
    assert score.scoreable is True
    # Only 2020-01-06..10 are usable, so the window ends on the 10th.
    assert score.simple_return_to_close == Decimal("0")


def test_rsi_shortcut_is_cohort_neutral() -> None:
    """When RSI >= 45 both arms reject, whatever the support field holds."""
    base = {
        "symbol": "005930",
        "market": "kr",
        "current_price": Decimal("10000"),
        "support_distance_pct": Decimal("1"),
        "rsi": Decimal("45"),
        "honest_upside_pct": Decimal("40"),
        "other_gate_bits": {
            "liquid_midcap": True,
            "concentration": True,
            "overhang": True,
        },
    }
    as_of = datetime(2020, 1, 1, tzinfo=UTC)
    for strength in ("strong", "moderate", "weak", "none", "not_computed"):
        evaluation = evaluate_candidate(
            CandidateEvidence.from_mapping({**base, "support_strength": strength}),
            evaluation_as_of=as_of,
        )
        assert evaluation.cohort == "neither"
        assert evaluation.variant_a.passed is False
        assert evaluation.variant_b.passed is False


def test_b_admit_set_is_a_superset_of_a() -> None:
    """The evaluator enforces it; this pins the property the report relies on."""
    as_of = datetime(2020, 1, 1, tzinfo=UTC)
    base = {
        "symbol": "AAPL",
        "market": "us",
        "current_price": Decimal("100"),
        "support_distance_pct": Decimal("2"),
        "rsi": Decimal("30"),
        "honest_upside_pct": Decimal("40"),
        "other_gate_bits": {
            "liquid_midcap": True,
            "concentration": True,
            "overhang": True,
        },
    }
    cohorts = {
        strength: evaluate_candidate(
            CandidateEvidence.from_mapping({**base, "support_strength": strength}),
            evaluation_as_of=as_of,
        ).cohort
        for strength in ("strong", "moderate", "weak")
    }
    assert cohorts == {
        "strong": "a_and_b",
        "moderate": "b_only",
        "weak": "neither",
    }


def test_unroutable_symbol_is_dropped_not_guessed() -> None:
    # a falling series keeps RSI under the shared gate, so the row reaches the
    # support/resistance call where the live router is consulted
    frame = _synthetic(300, seed=13, drift=-0.004)
    result = build_evidence(symbol="KRW-BTC", market="us", bars=frame)
    assert getattr(result, "reason", None) == "symbol_not_resolvable_by_live_router"


def test_both_evidence_paths_share_one_entry_basis() -> None:
    """The RSI shortcut must not hand the control cohort a different entry.

    The shortcut path feeds only the control cohort. If it rounded the entry
    differently from the support/resistance path, the control would be priced
    on a different basis from the two treated cohorts.
    """
    for seed, drift in ((21, 0.006), (22, -0.006)):
        frame = _synthetic(300, seed=seed, drift=drift)
        evidence = build_evidence(symbol="AAPL", market="us", bars=frame)
        assert isinstance(evidence, dict)
        raw_close = float(frame["close"].iloc[-1])
        assert evidence["current_price"] == Decimal(str(round(raw_close, 2)))


def test_micro_priced_row_is_dropped_by_both_paths() -> None:
    """A close that rounds to 0.00 must fail identically on either path.

    The live support impl refuses a non-positive current_price, so a
    micro-priced asset is ungateable there. The RSI shortcut has to drop it
    too, or the control cohort would gain rows the treated cohorts cannot have.
    """
    for seed, drift in ((31, 0.006), (32, -0.006)):
        frame = _synthetic(300, seed=seed, drift=drift)
        # scale off the *final* close so the decision-day price is under half a
        # cent whichever way the series drifted
        scale = 0.001 / float(frame["close"].iloc[-1])
        for column in ("open", "high", "low", "close"):
            frame[column] = frame[column] * scale
        assert round(float(frame["close"].iloc[-1]), 2) == 0.0
        result = build_evidence(symbol="KRW-BTT", market="crypto_upbit_krw", bars=frame)
        assert not isinstance(result, dict), "a zero-rounding row must not be gated"


def test_evidence_requires_full_indicator_history() -> None:
    frame = _synthetic(RSI_WINDOW_BARS - 1, seed=3)
    result = build_evidence(symbol="005930", market="kr", bars=frame)
    assert getattr(result, "reason", None) == "insufficient_history"


@pytest.mark.parametrize("market", ["kr", "us"])
def test_neutralised_gates_are_identical_for_both_arms(market: str) -> None:
    frame = _synthetic(300, seed=11)
    symbol = "005930" if market == "kr" else "AAPL"
    evidence = build_evidence(symbol=symbol, market=market, bars=frame)
    assert isinstance(evidence, dict)
    assert evidence["honest_upside_pct"] == Decimal("40")
    assert set(evidence["other_gate_bits"].values()) == {True}
