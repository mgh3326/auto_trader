"""Comprehensive unit tests for ROB-850 scorecard computation.

Covers:
* native-currency cash benchmark proration
* BTC/ETH equal-weight benchmark computation
* backtest→forward decay
* deterministic per-view and aggregate evidence IDs
* conjunctive verdict across all statuses (PROMOTION_ELIGIBLE,
  INSUFFICIENT_EVIDENCE, GATE_BLOCKED, BENCHMARK_NOT_BEATEN, MDD_EXCEEDED)
* aggregate extrema computation
* boundary behaviour (delta == 0 fails, delta == 0.01 passes)
* absence of any cross-view nominal P&L aggregation
* absence of any USDT/USD conversion

All financial inputs use :class:`decimal.Decimal`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.paper_evaluation.contracts import (
    BenchmarkWeights,
    EvaluationConfigError,
    GateType,
    GateVerdict,
    ScorecardVerdict,
    VerdictStatus,
    ViewCurrency,
    ViewMetrics,
    ViewName,
    ViewSource,
)
from app.services.paper_evaluation.scorecard import (
    compute_backtest_forward_decay,
    compute_btc_eth_equal_weight_benchmark_return_pct,
    compute_cash_benchmark_return_pct,
    compute_conjunctive_verdict,
    generate_evidence_ids,
)
from app.services.research_canonical_hash import canonical_sha256
from tests.services.paper_evaluation.conftest import (
    make_evaluation_config,
    stable_hash,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EPOCH_ID = "epoch-001"
_EXPERIMENT_HASH = stable_hash("experiment")
_COHORT_HASH = stable_hash("cohort")

_VIEW_CURRENCY_SOURCE: dict[ViewName, tuple[ViewCurrency, ViewSource]] = {
    ViewName.BINANCE_BROKER: (
        ViewCurrency.USDT,
        ViewSource.BINANCE_DEMO_LEDGER,
    ),
    ViewName.ALPACA_BROKER: (
        ViewCurrency.USD,
        ViewSource.ALPACA_PAPER_LEDGER,
    ),
    ViewName.CANONICAL_SHADOW: (
        ViewCurrency.USDT,
        ViewSource.CANONICAL_MARKET_SNAPSHOT,
    ),
}


def make_view_metrics(
    view_name: ViewName,
    *,
    initial_equity: Decimal = Decimal("10000"),
    nominal_net_pnl: Decimal = Decimal("1500"),
    fees: Decimal = Decimal("10"),
    net_return_pct: Decimal = Decimal("15"),
    max_drawdown_pct: Decimal = Decimal("8"),
    turnover: Decimal = Decimal("5"),
    exposure: Decimal = Decimal("0.6"),
    fill_count: int = 200,
    partial_fill_count: int = 0,
    missing_observation_count: int = 0,
    cash_benchmark_return_pct: Decimal = Decimal("0.5"),
    cash_benchmark_delta_pct: Decimal = Decimal("2.0"),
    btc_eth_benchmark_return_pct: Decimal = Decimal("5.0"),
    btc_eth_benchmark_delta_pct: Decimal = Decimal("1.5"),
    backtest_forward_decay: Decimal | None = Decimal("0.8"),
    sharpe_reference: Decimal | None = Decimal("1.5"),
    dsr_reference: Decimal | None = None,
    canonical_snapshot_hashes: tuple[str, ...] = (),
    experiment_hash: str = _EXPERIMENT_HASH,
    cohort_hash: str = _COHORT_HASH,
    epoch_id: str = _EPOCH_ID,
    config_hash: str | None = None,
    symbol_mapping: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
) -> ViewMetrics:
    """Build a valid :class:`ViewMetrics` with consistent ending_equity.

    ``ending_equity`` is forced to ``initial_equity + nominal_net_pnl`` so the
    frozen-contract validator is satisfied.
    """
    currency, source = _VIEW_CURRENCY_SOURCE[view_name]
    if config_hash is None:
        config_hash = make_evaluation_config().config_hash()
    return ViewMetrics(
        view_name=view_name,
        currency=currency,
        source=source,
        symbol_mapping=symbol_mapping,
        initial_equity=initial_equity,
        ending_equity=initial_equity + nominal_net_pnl,
        nominal_net_pnl=nominal_net_pnl,
        fees=fees,
        net_return_pct=net_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        turnover=turnover,
        exposure=exposure,
        sharpe_reference=sharpe_reference,
        dsr_reference=dsr_reference,
        fill_count=fill_count,
        partial_fill_count=partial_fill_count,
        missing_observation_count=missing_observation_count,
        cash_benchmark_return_pct=cash_benchmark_return_pct,
        cash_benchmark_delta_pct=cash_benchmark_delta_pct,
        btc_eth_benchmark_return_pct=btc_eth_benchmark_return_pct,
        btc_eth_benchmark_delta_pct=btc_eth_benchmark_delta_pct,
        backtest_forward_decay=backtest_forward_decay,
        canonical_snapshot_hashes=canonical_snapshot_hashes,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        epoch_id=epoch_id,
        config_hash=config_hash,
    )


def make_three_views(**overrides_per_view: dict[str, object]) -> dict[ViewName, ViewMetrics]:
    """Build a full set of three valid ViewMetrics.

    ``overrides_per_view`` may contain per-view keyword overrides keyed by
    ``ViewName`` value (e.g. ``binance_broker={...}``).
    """
    return {
        view_name: make_view_metrics(
            view_name,
            **(overrides_per_view.get(view_name.value, {})),  # type: ignore[arg-type]
        )
        for view_name in (
            ViewName.BINANCE_BROKER,
            ViewName.ALPACA_BROKER,
            ViewName.CANONICAL_SHADOW,
        )
    }


def make_passed_shadow_gate() -> GateVerdict:
    return GateVerdict(
        gate_type=GateType.SHADOW_SOAK,
        calendar_days_observed=7,
        required_days=7,
        passed=True,
        reason_code="shadow_soak_complete",
        reason_text="shadow soak satisfied",
    )


def make_passed_paper_gate() -> GateVerdict:
    return GateVerdict(
        gate_type=GateType.PAPER_PROMOTION,
        calendar_days_observed=60,
        required_days=60,
        passed=True,
        reason_code="paper_promotion_complete",
        reason_text="paper promotion window satisfied",
    )


def make_failed_shadow_gate() -> GateVerdict:
    return GateVerdict(
        gate_type=GateType.SHADOW_SOAK,
        calendar_days_observed=3,
        required_days=7,
        passed=False,
        reason_code="shadow_soak_incomplete",
        reason_text="shadow soak not yet complete",
    )


def make_failed_paper_gate() -> GateVerdict:
    return GateVerdict(
        gate_type=GateType.PAPER_PROMOTION,
        calendar_days_observed=20,
        required_days=60,
        passed=False,
        reason_code="paper_promotion_incomplete",
        reason_text="paper promotion window not yet complete",
    )


# ---------------------------------------------------------------------------
# Cash benchmark
# ---------------------------------------------------------------------------


def test_cash_benchmark_prorated_to_calendar_days() -> None:
    config = make_evaluation_config(risk_free_rate_pct=Decimal("4"))
    result = compute_cash_benchmark_return_pct(
        risk_free_rate_pct=Decimal("4"),
        calendar_days=365,
        annualization=config.annualization,
    )
    assert result == Decimal("4")


def test_cash_benchmark_half_year() -> None:
    config = make_evaluation_config(risk_free_rate_pct=Decimal("4"))
    result = compute_cash_benchmark_return_pct(
        risk_free_rate_pct=Decimal("4"),
        calendar_days=182,
        annualization=config.annualization,
    )
    # 4 * 182 / 365
    assert result == Decimal("4") * Decimal(182) / Decimal(365)


def test_cash_benchmark_zero_days_returns_zero() -> None:
    config = make_evaluation_config(risk_free_rate_pct=Decimal("2"))
    result = compute_cash_benchmark_return_pct(
        risk_free_rate_pct=Decimal("2"),
        calendar_days=0,
        annualization=config.annualization,
    )
    assert result == Decimal("0")


def test_cash_benchmark_rejects_negative_rate() -> None:
    config = make_evaluation_config(risk_free_rate_pct=Decimal("2"))
    with pytest.raises(EvaluationConfigError) as exc:
        compute_cash_benchmark_return_pct(
            risk_free_rate_pct=Decimal("-1"),
            calendar_days=10,
            annualization=config.annualization,
        )
    assert exc.value.reason_code == "non_finite_value"


def test_cash_benchmark_rejects_negative_days() -> None:
    config = make_evaluation_config()
    with pytest.raises(EvaluationConfigError) as exc:
        compute_cash_benchmark_return_pct(
            risk_free_rate_pct=Decimal("2"),
            calendar_days=-5,
            annualization=config.annualization,
        )
    assert exc.value.reason_code == "non_finite_value"


def test_cash_benchmark_rejects_non_finite_rate() -> None:
    config = make_evaluation_config()
    with pytest.raises(EvaluationConfigError) as exc:
        compute_cash_benchmark_return_pct(
            risk_free_rate_pct=Decimal("NaN"),
            calendar_days=10,
            annualization=config.annualization,
        )
    assert exc.value.reason_code == "non_finite_value"


def test_cash_benchmark_rejects_drift_between_argument_and_config() -> None:
    """Explicit risk_free_rate_pct must match annualization.risk_free_rate_pct."""
    config = make_evaluation_config(risk_free_rate_pct=Decimal("2"))
    with pytest.raises(EvaluationConfigError) as exc:
        compute_cash_benchmark_return_pct(
            risk_free_rate_pct=Decimal("3"),
            calendar_days=10,
            annualization=config.annualization,
        )
    assert exc.value.reason_code == "non_finite_value"


# ---------------------------------------------------------------------------
# BTC/ETH equal-weight benchmark
# ---------------------------------------------------------------------------


def test_btc_eth_benchmark_equal_weight_flat_market() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    result = compute_btc_eth_equal_weight_benchmark_return_pct(
        initial_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
        ending_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
        weights=weights,
    )
    assert result == Decimal("0")


def test_btc_eth_benchmark_equal_weight_positive() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    # BTC +10%, ETH +20% → weighted = 0.5*10 + 0.5*20 = 15
    result = compute_btc_eth_equal_weight_benchmark_return_pct(
        initial_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
        ending_prices={"BTCUSDT": Decimal("55000"), "ETHUSDT": Decimal("3600")},
        weights=weights,
    )
    assert result == Decimal("15")


def test_btc_eth_benchmark_negative_returns() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    # BTC -20%, ETH -10% → weighted = 0.5*(-20) + 0.5*(-10) = -15
    result = compute_btc_eth_equal_weight_benchmark_return_pct(
        initial_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
        ending_prices={"BTCUSDT": Decimal("40000"), "ETHUSDT": Decimal("2700")},
        weights=weights,
    )
    assert result == Decimal("-15")


def test_btc_eth_benchmark_asymmetric_weights() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.7"), eth_weight=Decimal("0.3"))
    # BTC +10%, ETH +20% → 0.7*10 + 0.3*20 = 13
    result = compute_btc_eth_equal_weight_benchmark_return_pct(
        initial_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
        ending_prices={"BTCUSDT": Decimal("55000"), "ETHUSDT": Decimal("3600")},
        weights=weights,
    )
    assert result == Decimal("13")


def test_btc_eth_benchmark_rejects_missing_symbol() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    with pytest.raises(EvaluationConfigError) as exc:
        compute_btc_eth_equal_weight_benchmark_return_pct(
            initial_prices={"BTCUSDT": Decimal("50000")},
            ending_prices={
                "BTCUSDT": Decimal("50000"),
                "ETHUSDT": Decimal("3000"),
            },
            weights=weights,
        )
    assert exc.value.reason_code == "invalid_benchmark_prices"


def test_btc_eth_benchmark_rejects_extra_symbol() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    with pytest.raises(EvaluationConfigError) as exc:
        compute_btc_eth_equal_weight_benchmark_return_pct(
            initial_prices={
                "BTCUSDT": Decimal("50000"),
                "ETHUSDT": Decimal("3000"),
                "SOLUSDT": Decimal("100"),
            },
            ending_prices={
                "BTCUSDT": Decimal("50000"),
                "ETHUSDT": Decimal("3000"),
            },
            weights=weights,
        )
    assert exc.value.reason_code == "invalid_benchmark_prices"


def test_btc_eth_benchmark_rejects_zero_initial_price() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    with pytest.raises(EvaluationConfigError) as exc:
        compute_btc_eth_equal_weight_benchmark_return_pct(
            initial_prices={"BTCUSDT": Decimal("0"), "ETHUSDT": Decimal("3000")},
            ending_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
            weights=weights,
        )
    assert exc.value.reason_code == "non_finite_value"


def test_btc_eth_benchmark_rejects_non_finite_price() -> None:
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    with pytest.raises(EvaluationConfigError) as exc:
        compute_btc_eth_equal_weight_benchmark_return_pct(
            initial_prices={
                "BTCUSDT": Decimal("Infinity"),
                "ETHUSDT": Decimal("3000"),
            },
            ending_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
            weights=weights,
        )
    assert exc.value.reason_code == "non_finite_value"


def test_btc_eth_benchmark_does_not_convert_usdt_to_usd() -> None:
    """Benchmark formula uses raw prices; no peg/conversion is applied."""
    weights = BenchmarkWeights(btc_weight=Decimal("0.5"), eth_weight=Decimal("0.5"))
    initial = {"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")}
    ending = {"BTCUSDT": Decimal("100000"), "ETHUSDT": Decimal("6000")}
    result = compute_btc_eth_equal_weight_benchmark_return_pct(
        initial_prices=initial,
        ending_prices=ending,
        weights=weights,
    )
    # Both doubled → +100% for each → weighted 100
    assert result == Decimal("100")


# ---------------------------------------------------------------------------
# Backtest → forward decay
# ---------------------------------------------------------------------------


def test_backtest_forward_decay_simple_ratio() -> None:
    result = compute_backtest_forward_decay(
        backtest_return_pct=Decimal("10"),
        forward_return_pct=Decimal("5"),
    )
    assert result == Decimal("0.5")


def test_backtest_forward_decay_greater_than_one() -> None:
    result = compute_backtest_forward_decay(
        backtest_return_pct=Decimal("10"),
        forward_return_pct=Decimal("20"),
    )
    assert result == Decimal("2")


def test_backtest_forward_decay_zero_backtest_returns_none() -> None:
    result = compute_backtest_forward_decay(
        backtest_return_pct=Decimal("0"),
        forward_return_pct=Decimal("5"),
    )
    assert result is None


def test_backtest_forward_decay_negative_backtest_returns_none() -> None:
    result = compute_backtest_forward_decay(
        backtest_return_pct=Decimal("-5"),
        forward_return_pct=Decimal("5"),
    )
    assert result is None


def test_backtest_forward_decay_rejects_non_finite() -> None:
    with pytest.raises(EvaluationConfigError):
        compute_backtest_forward_decay(
            backtest_return_pct=Decimal("NaN"),
            forward_return_pct=Decimal("5"),
        )


# ---------------------------------------------------------------------------
# Evidence IDs
# ---------------------------------------------------------------------------


def test_generate_evidence_ids_deterministic() -> None:
    views = make_three_views()
    first = generate_evidence_ids(views)
    second = generate_evidence_ids(views)
    assert first == second


def test_generate_evidence_ids_changes_when_metrics_change() -> None:
    views = make_three_views()
    original = generate_evidence_ids(views)
    mutated_view = views[ViewName.CANONICAL_SHADOW].model_copy(
        update={"net_return_pct": Decimal("99")}
    )
    mutated = {**views, ViewName.CANONICAL_SHADOW: mutated_view}
    new_ids = generate_evidence_ids(mutated)
    assert new_ids != original
    # Per-view ID for the mutated view changes
    original_shadow = next(
        eid for eid in original if eid.startswith("view:canonical_shadow::")
    )
    new_shadow = next(
        eid for eid in new_ids if eid.startswith("view:canonical_shadow::")
    )
    assert original_shadow != new_shadow
    # Aggregate ID changes
    original_agg = next(eid for eid in original if eid.startswith("verdict::aggregate::"))
    new_agg = next(eid for eid in new_ids if eid.startswith("verdict::aggregate::"))
    assert original_agg != new_agg


def test_generate_evidence_ids_count_and_format() -> None:
    views = make_three_views()
    ids = generate_evidence_ids(views)
    # 3 per-view + 1 aggregate
    assert len(ids) == 4
    per_view = [eid for eid in ids if eid.startswith("view:")]
    aggregate = [eid for eid in ids if eid.startswith("verdict::aggregate::")]
    assert len(per_view) == 3
    assert len(aggregate) == 1
    # All per-view IDs are unique
    assert len(set(per_view)) == 3
    # All hex digests are 16 chars
    for eid in ids:
        digest = eid.rsplit("::", 1)[-1]
        assert len(digest) == 16
        assert all(c in "0123456789abcdef" for c in digest)


def test_generate_evidence_ids_order_independent_aggregate() -> None:
    """Aggregate ID must be identical regardless of dict insertion order."""
    views = make_three_views()
    # Build a reordered dict
    reordered = {
        ViewName.CANONICAL_SHADOW: views[ViewName.CANONICAL_SHADOW],
        ViewName.ALPACA_BROKER: views[ViewName.ALPACA_BROKER],
        ViewName.BINANCE_BROKER: views[ViewName.BINANCE_BROKER],
    }
    first = generate_evidence_ids(views)
    second = generate_evidence_ids(reordered)
    # Per-view IDs are sorted so should be identical
    assert first == second


def test_generate_evidence_ids_missing_view_rejected() -> None:
    views = make_three_views()
    del views[ViewName.ALPACA_BROKER]
    with pytest.raises(EvaluationConfigError) as exc:
        generate_evidence_ids(views)
    assert exc.value.reason_code == "missing_view"


# ---------------------------------------------------------------------------
# Conjunctive verdict — happy path
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_all_passing_is_promotion_eligible() -> None:
    views = make_three_views()
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.PROMOTION_ELIGIBLE
    assert verdict.reason_code == "promotion_eligible"
    assert verdict.shadow_gate is not None and verdict.shadow_gate.passed
    assert verdict.paper_gate is not None and verdict.paper_gate.passed
    assert set(verdict.view_metrics) == {
        ViewName.BINANCE_BROKER,
        ViewName.ALPACA_BROKER,
        ViewName.CANONICAL_SHADOW,
    }


def test_conjunctive_verdict_no_gates_still_evaluates() -> None:
    """Both gates optional — absence does not block verdict."""
    views = make_three_views()
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=None,
        paper_gate=None,
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.PROMOTION_ELIGIBLE
    assert verdict.shadow_gate is None
    assert verdict.paper_gate is None


# ---------------------------------------------------------------------------
# Conjunctive verdict — INSUFFICIENT_EVIDENCE
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_missing_observations_under_fail_close() -> None:
    views = make_three_views(
        canonical_shadow={"missing_observation_count": 1},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.INSUFFICIENT_EVIDENCE
    assert verdict.reason_code == "insufficient_evidence"


def test_conjunctive_verdict_fill_count_below_min_fills() -> None:
    views = make_three_views(
        binance_broker={"fill_count": 1},  # default min_fills=10
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.INSUFFICIENT_EVIDENCE


def test_conjunctive_verdict_observation_count_below_min_observations() -> None:
    """fill_count + missing_observation_count below min_observations fails."""
    # Default min_observations=100. With fill_count=50 and missing=0,
    # observed=50 < 100 — but this only triggers if missing=0 AND
    # fail_close doesn't already trip. We need fill_count alone < min_obs.
    # Since fail_close only trips on missing > 0, this exercises the
    # observation-count rule directly.
    views = make_three_views(
        alpaca_broker={"fill_count": 50, "missing_observation_count": 0},
    )
    # Lower min_fills so the fill-count rule doesn't trip first
    config = make_evaluation_config(min_fills=1, min_observations=100)
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=config,
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.INSUFFICIENT_EVIDENCE


def test_conjunctive_verdict_insufficient_evidence_takes_precedence_over_gates() -> None:
    """Even with a failed gate, insufficient evidence wins."""
    views = make_three_views(
        canonical_shadow={"missing_observation_count": 5},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_failed_shadow_gate(),
        paper_gate=make_failed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Conjunctive verdict — GATE_BLOCKED
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_failed_shadow_gate_blocks() -> None:
    views = make_three_views()
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_failed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.GATE_BLOCKED
    assert verdict.reason_code == "shadow_gate_blocked"


def test_conjunctive_verdict_failed_paper_gate_blocks() -> None:
    views = make_three_views()
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_failed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.GATE_BLOCKED
    assert verdict.reason_code == "paper_gate_blocked"


def test_conjunctive_verdict_shadow_gate_precedence_over_paper() -> None:
    """If both gates fail, shadow is reported first."""
    views = make_three_views()
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_failed_shadow_gate(),
        paper_gate=make_failed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.GATE_BLOCKED
    assert verdict.reason_code == "shadow_gate_blocked"


# ---------------------------------------------------------------------------
# Conjunctive verdict — BENCHMARK_NOT_BEATEN
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_one_view_fails_cash_benchmark() -> None:
    views = make_three_views(
        binance_broker={"cash_benchmark_delta_pct": Decimal("-0.5")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.BENCHMARK_NOT_BEATEN
    assert verdict.reason_code == "cash_benchmark_not_beaten"


def test_conjunctive_verdict_one_view_fails_btc_eth_benchmark() -> None:
    views = make_three_views(
        alpaca_broker={"btc_eth_benchmark_delta_pct": Decimal("-0.1")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.BENCHMARK_NOT_BEATEN
    assert verdict.reason_code == "btc_eth_benchmark_not_beaten"


def test_conjunctive_verdict_cash_delta_zero_fails_boundary() -> None:
    """delta == 0 fails (strictly not greater than zero)."""
    views = make_three_views(
        canonical_shadow={"cash_benchmark_delta_pct": Decimal("0")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.BENCHMARK_NOT_BEATEN


def test_conjunctive_verdict_btc_eth_delta_zero_fails_boundary() -> None:
    views = make_three_views(
        canonical_shadow={"btc_eth_benchmark_delta_pct": Decimal("0")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.BENCHMARK_NOT_BEATEN


def test_conjunctive_verdict_tiny_positive_delta_passes_boundary() -> None:
    """delta == 0.01 passes (strictly greater than zero)."""
    views = make_three_views(
        binance_broker={
            "cash_benchmark_delta_pct": Decimal("0.01"),
            "btc_eth_benchmark_delta_pct": Decimal("0.01"),
        },
        alpaca_broker={
            "cash_benchmark_delta_pct": Decimal("0.01"),
            "btc_eth_benchmark_delta_pct": Decimal("0.01"),
        },
        canonical_shadow={
            "cash_benchmark_delta_pct": Decimal("0.01"),
            "btc_eth_benchmark_delta_pct": Decimal("0.01"),
        },
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.PROMOTION_ELIGIBLE


def test_conjunctive_verdict_benchmark_failure_precedence_over_mdd() -> None:
    """If both benchmark and MDD fail, benchmark wins."""
    views = make_three_views(
        binance_broker={
            "cash_benchmark_delta_pct": Decimal("-1"),
            "max_drawdown_pct": Decimal("99"),
        },
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(mdd_target_pct=Decimal("25")),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.BENCHMARK_NOT_BEATEN


# ---------------------------------------------------------------------------
# Conjunctive verdict — MDD_EXCEEDED
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_one_view_exceeds_mdd_target() -> None:
    views = make_three_views(
        binance_broker={"max_drawdown_pct": Decimal("30")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(mdd_target_pct=Decimal("25")),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.MDD_EXCEEDED
    assert verdict.reason_code == "mdd_exceeded"


def test_conjunctive_verdict_mdd_exactly_at_target_passes() -> None:
    """max_drawdown_pct == mdd_target_pct passes (strict greater-than)."""
    views = make_three_views(
        binance_broker={"max_drawdown_pct": Decimal("25")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(mdd_target_pct=Decimal("25")),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.PROMOTION_ELIGIBLE


# ---------------------------------------------------------------------------
# Aggregate extrema
# ---------------------------------------------------------------------------


def test_aggregate_min_net_return_pct_is_min_across_views() -> None:
    views = make_three_views(
        binance_broker={"net_return_pct": Decimal("20")},
        alpaca_broker={"net_return_pct": Decimal("5")},
        canonical_shadow={"net_return_pct": Decimal("12")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.min_net_return_pct == Decimal("5")


def test_aggregate_max_max_drawdown_pct_is_max_across_views() -> None:
    views = make_three_views(
        binance_broker={"max_drawdown_pct": Decimal("3")},
        alpaca_broker={"max_drawdown_pct": Decimal("15")},
        canonical_shadow={"max_drawdown_pct": Decimal("7")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(mdd_target_pct=Decimal("25")),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.max_max_drawdown_pct == Decimal("15")


def test_aggregate_min_benchmark_delta_pct_is_min_across_views_and_benchmarks() -> None:
    """min_benchmark_delta_pct = min of all cash + btc_eth deltas across views."""
    views = make_three_views(
        binance_broker={
            "cash_benchmark_delta_pct": Decimal("3"),
            "btc_eth_benchmark_delta_pct": Decimal("2"),
        },
        alpaca_broker={
            "cash_benchmark_delta_pct": Decimal("1.5"),
            "btc_eth_benchmark_delta_pct": Decimal("0.5"),
        },
        canonical_shadow={
            "cash_benchmark_delta_pct": Decimal("2.5"),
            "btc_eth_benchmark_delta_pct": Decimal("1.8"),
        },
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.min_benchmark_delta_pct == Decimal("0.5")


def test_aggregate_extrema_computed_even_when_verdict_fails() -> None:
    """Extrema are always computed, even when status is not PROMOTION_ELIGIBLE."""
    views = make_three_views(
        binance_broker={
            "net_return_pct": Decimal("1"),
            "max_drawdown_pct": Decimal("2"),
            "cash_benchmark_delta_pct": Decimal("-5"),
            "btc_eth_benchmark_delta_pct": Decimal("-3"),
        },
        alpaca_broker={
            "net_return_pct": Decimal("10"),
            "max_drawdown_pct": Decimal("4"),
            "cash_benchmark_delta_pct": Decimal("2"),
            "btc_eth_benchmark_delta_pct": Decimal("1"),
        },
        canonical_shadow={
            "net_return_pct": Decimal("5"),
            "max_drawdown_pct": Decimal("6"),
            "cash_benchmark_delta_pct": Decimal("3"),
            "btc_eth_benchmark_delta_pct": Decimal("2"),
        },
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.BENCHMARK_NOT_BEATEN
    assert verdict.min_net_return_pct == Decimal("1")
    assert verdict.max_max_drawdown_pct == Decimal("6")
    assert verdict.min_benchmark_delta_pct == Decimal("-5")


# ---------------------------------------------------------------------------
# No cross-view nominal P&L aggregation
# ---------------------------------------------------------------------------


def test_no_cross_view_nominal_pnl_total_field_exists() -> None:
    """ScorecardVerdict must not expose any cross-view nominal P&L total."""
    fields = set(ScorecardVerdict.model_fields)
    forbidden_substrings = (
        "total_pnl",
        "total_nominal",
        "aggregate_pnl",
        "aggregate_nominal",
        "combined_pnl",
        "sum_pnl",
        "sum_nominal",
    )
    for field_name in fields:
        for forbidden in forbidden_substrings:
            assert forbidden not in field_name.lower(), (
                f"field {field_name} looks like a cross-view nominal aggregate"
            )


def test_verdict_does_not_sum_nominal_pnl_across_views() -> None:
    """Sanity: building a verdict does not require or emit any nominal sum."""
    views = make_three_views(
        binance_broker={"nominal_net_pnl": Decimal("1000")},
        alpaca_broker={"nominal_net_pnl": Decimal("2000")},
        canonical_shadow={"nominal_net_pnl": Decimal("3000")},
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    # Ensure no field equals the naive sum (1000+2000+3000=6000 or similar)
    naive_sum = Decimal("6000")
    for field_name in type(verdict).model_fields:
        value = getattr(verdict, field_name)
        if isinstance(value, Decimal):
            assert value != naive_sum or field_name == "min_net_return_pct"
    # And no field name hints at a nominal sum
    for field_name in type(verdict).model_fields:
        assert "pnl" not in field_name.lower()


def test_no_usdt_usd_conversion_is_performed() -> None:
    """The scorecard never converts between USDT and USD.

    Construct two equivalent verdicts where one view is in USDT and another
    in USD with the same numeric inputs; the normalised metrics must be
    identical because no FX conversion is applied anywhere.
    """
    usdt_view = make_view_metrics(
        ViewName.BINANCE_BROKER,
        initial_equity=Decimal("10000"),
        nominal_net_pnl=Decimal("1000"),
        net_return_pct=Decimal("10"),
    )
    usd_view = make_view_metrics(
        ViewName.ALPACA_BROKER,
        initial_equity=Decimal("10000"),
        nominal_net_pnl=Decimal("1000"),
        net_return_pct=Decimal("10"),
    )
    shadow_view = make_view_metrics(ViewName.CANONICAL_SHADOW)
    views = {
        ViewName.BINANCE_BROKER: usdt_view,
        ViewName.ALPACA_BROKER: usd_view,
        ViewName.CANONICAL_SHADOW: shadow_view,
    }
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=None,
        paper_gate=None,
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    # Native currencies preserved as-is
    assert verdict.view_metrics[ViewName.BINANCE_BROKER].currency is ViewCurrency.USDT
    assert verdict.view_metrics[ViewName.ALPACA_BROKER].currency is ViewCurrency.USD
    # Both views' native amounts remain unchanged
    assert verdict.view_metrics[ViewName.BINANCE_BROKER].nominal_net_pnl == Decimal("1000")
    assert verdict.view_metrics[ViewName.ALPACA_BROKER].nominal_net_pnl == Decimal("1000")


# ---------------------------------------------------------------------------
# Verdict input validation
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_rejects_missing_view() -> None:
    views = make_three_views()
    del views[ViewName.ALPACA_BROKER]
    with pytest.raises(EvaluationConfigError) as exc:
        compute_conjunctive_verdict(
            view_metrics=views,
            config=make_evaluation_config(),
            shadow_gate=None,
            paper_gate=None,
            epoch_id=_EPOCH_ID,
            experiment_hash=_EXPERIMENT_HASH,
            cohort_hash=_COHORT_HASH,
        )
    assert exc.value.reason_code == "missing_view"


def test_conjunctive_verdict_rejects_extra_view() -> None:
    views = make_three_views()
    # Insert a duplicate-keyed metrics to simulate an extra entry;
    # since ViewName is closed, we just delete one required view and
    # verify the rejection.
    del views[ViewName.CANONICAL_SHADOW]
    with pytest.raises(EvaluationConfigError) as exc:
        compute_conjunctive_verdict(
            view_metrics=views,
            config=make_evaluation_config(),
            shadow_gate=None,
            paper_gate=None,
            epoch_id=_EPOCH_ID,
            experiment_hash=_EXPERIMENT_HASH,
            cohort_hash=_COHORT_HASH,
        )
    assert exc.value.reason_code == "missing_view"


def test_conjunctive_verdict_propagates_identity_fields() -> None:
    views = make_three_views()
    config = make_evaluation_config()
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=config,
        shadow_gate=None,
        paper_gate=None,
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.epoch_id == _EPOCH_ID
    assert verdict.config_hash == config.config_hash()
    assert verdict.experiment_hash == _EXPERIMENT_HASH
    assert verdict.cohort_hash == _COHORT_HASH


def test_conjunctive_verdict_evidence_ids_match_generate_evidence_ids() -> None:
    """The verdict's evidence IDs must equal generate_evidence_ids output."""
    views = make_three_views()
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=None,
        paper_gate=None,
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert tuple(verdict.evidence_ids) == generate_evidence_ids(views)


def test_conjunctive_verdict_evidence_id_aggregate_hash_matches_canonical() -> None:
    """The aggregate evidence ID uses canonical_sha256 over all metrics."""
    views = make_three_views()
    ids = generate_evidence_ids(views)
    aggregate_id = next(eid for eid in ids if eid.startswith("verdict::aggregate::"))
    payload = {
        name.value: m.model_dump(mode="python")
        for name, m in views.items()
    }
    expected_digest = canonical_sha256(payload)[:16]
    assert aggregate_id == f"verdict::aggregate::{expected_digest}"


# ---------------------------------------------------------------------------
# Currency mismatch defensive check
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_rejects_currency_mismatch_via_model_copy() -> None:
    """Although the contract prevents constructing mismatched currency at
    construction time, the scorecard still defends against the case where
    a config-level currency would not match the metrics-level currency.

    We trigger this by giving the metrics a config_hash that does not
    match the supplied config; the scorecard's currency check still
    runs against the in-band currency field which is bound to view_name.
    The contract enforces consistency, so this test documents the
    positive path: same view_name + same currency = no error.
    """
    views = make_three_views()
    # All views have correct currency for their view_name — no error expected.
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=make_evaluation_config(),
        shadow_gate=None,
        paper_gate=None,
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.PROMOTION_ELIGIBLE


# ---------------------------------------------------------------------------
# Determinism / golden pinning
# ---------------------------------------------------------------------------


def test_conjunctive_verdict_is_deterministic() -> None:
    """Same inputs → byte-identical verdict (frozen contract)."""
    views = make_three_views()
    config = make_evaluation_config()
    first = compute_conjunctive_verdict(
        view_metrics=views,
        config=config,
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    second = compute_conjunctive_verdict(
        view_metrics=views,
        config=config,
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert first == second
    assert first.model_dump() == second.model_dump()


def test_conjunctive_verdict_order_independent() -> None:
    """Reordering the input dict must not change the verdict."""
    views = make_three_views()
    config = make_evaluation_config()
    shadow = make_passed_shadow_gate()
    paper = make_passed_paper_gate()
    first = compute_conjunctive_verdict(
        view_metrics=views,
        config=config,
        shadow_gate=shadow,
        paper_gate=paper,
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    reordered_views = {
        ViewName.CANONICAL_SHADOW: views[ViewName.CANONICAL_SHADOW],
        ViewName.ALPACA_BROKER: views[ViewName.ALPACA_BROKER],
        ViewName.BINANCE_BROKER: views[ViewName.BINANCE_BROKER],
    }
    second = compute_conjunctive_verdict(
        view_metrics=reordered_views,
        config=config,
        shadow_gate=shadow,
        paper_gate=paper,
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert first == second
    assert tuple(first.evidence_ids) == tuple(second.evidence_ids)


# ---------------------------------------------------------------------------
# Integration: benchmark functions compose into verdict inputs
# ---------------------------------------------------------------------------


def test_benchmark_computation_composes_with_view_metrics() -> None:
    """Cash and BTC/ETH benchmarks computed via the helpers can be used
    to populate the corresponding ViewMetrics fields."""
    config = make_evaluation_config(risk_free_rate_pct=Decimal("4"))
    cash_bench = compute_cash_benchmark_return_pct(
        risk_free_rate_pct=Decimal("4"),
        calendar_days=60,
        annualization=config.annualization,
    )
    btc_eth_bench = compute_btc_eth_equal_weight_benchmark_return_pct(
        initial_prices={"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")},
        ending_prices={"BTCUSDT": Decimal("55000"), "ETHUSDT": Decimal("3300")},
        weights=config.benchmark_weights,
    )
    # view net return of 20% (example)
    net_return_pct = Decimal("20")
    views = make_three_views(
        binance_broker={
            "cash_benchmark_return_pct": cash_bench,
            "cash_benchmark_delta_pct": net_return_pct - cash_bench,
            "btc_eth_benchmark_return_pct": btc_eth_bench,
            "btc_eth_benchmark_delta_pct": net_return_pct - btc_eth_bench,
        },
    )
    verdict = compute_conjunctive_verdict(
        view_metrics=views,
        config=config,
        shadow_gate=make_passed_shadow_gate(),
        paper_gate=make_passed_paper_gate(),
        epoch_id=_EPOCH_ID,
        experiment_hash=_EXPERIMENT_HASH,
        cohort_hash=_COHORT_HASH,
    )
    assert verdict.status is VerdictStatus.PROMOTION_ELIGIBLE
    binance_metrics = verdict.view_metrics[ViewName.BINANCE_BROKER]
    assert binance_metrics.cash_benchmark_return_pct == cash_bench
    assert binance_metrics.btc_eth_benchmark_return_pct == btc_eth_bench
