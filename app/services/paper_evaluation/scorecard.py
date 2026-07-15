"""ROB-850 scorecard: native-currency benchmark computation and the
conjunctive verdict across the three evaluation views.

This module is read-only, side-effect free, and deterministic.  It never:

* performs USDT/USD conversion or assumes any peg between the two,
* computes a cross-view nominal P&L total,
* makes network or broker-service calls,
* imports broker service modules.

All financial math uses :class:`decimal.Decimal`.

The public surface is:

* :func:`compute_cash_benchmark_return_pct`
* :func:`compute_btc_eth_equal_weight_benchmark_return_pct`
* :func:`compute_conjunctive_verdict`
* :func:`compute_backtest_forward_decay`
* :func:`generate_evidence_ids`
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from app.services.paper_evaluation.contracts import (
    AnnualizationRules,
    BenchmarkWeights,
    EvaluationConfig,
    EvaluationConfigError,
    GateType,
    GateVerdict,
    MissingDataPolicy,
    ScorecardVerdict,
    VerdictStatus,
    ViewMetrics,
    ViewName,
)
from app.services.research_canonical_hash import canonical_sha256

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REQUIRED_VIEW_NAMES: frozenset[ViewName] = frozenset(
    {
        ViewName.BINANCE_BROKER,
        ViewName.ALPACA_BROKER,
        ViewName.CANONICAL_SHADOW,
    }
)

_DAYS_PER_YEAR = Decimal(365)
_PERCENT = Decimal(100)
_BTC_SYMBOL = "BTCUSDT"
_ETH_SYMBOL = "ETHUSDT"
_REQUIRED_BENCHMARK_SYMBOLS: frozenset[str] = frozenset({_BTC_SYMBOL, _ETH_SYMBOL})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_finite(value: Decimal, *, name: str) -> None:
    """Reject non-finite Decimal values with ``non_finite_value``."""
    if not isinstance(value, Decimal):
        raise EvaluationConfigError(
            "non_finite_value",
            f"{name} must be a Decimal",
        )
    if not value.is_finite():
        raise EvaluationConfigError(
            "non_finite_value",
            f"{name} must be finite",
        )


def _require_required_views(view_metrics: Mapping[ViewName, ViewMetrics]) -> None:
    """Validate that all three views are present, else ``missing_view``."""
    if set(view_metrics) != _REQUIRED_VIEW_NAMES:
        missing = _REQUIRED_VIEW_NAMES - set(view_metrics)
        extra = set(view_metrics) - _REQUIRED_VIEW_NAMES
        parts: list[str] = []
        if missing:
            parts.append(f"missing={sorted(v.value for v in missing)}")
        if extra:
            parts.append(f"extra={sorted(v.value for v in extra)}")
        raise EvaluationConfigError(
            "missing_view",
            f"exactly {_REQUIRED_VIEW_NAMES} views required ({', '.join(parts)})",
        )


# ---------------------------------------------------------------------------
# Benchmark computations
# ---------------------------------------------------------------------------


def compute_cash_benchmark_return_pct(
    *,
    risk_free_rate_pct: Decimal,
    calendar_days: int,
    annualization: AnnualizationRules,
) -> Decimal:
    """Native-currency cash benchmark = risk-free rate prorated to ``calendar_days``.

    The risk-free rate is expressed in percent (e.g. ``Decimal("2")`` for 2 %
    per annum).  The benchmark is the linear proration over a 365-day year:

        benchmark_pct = risk_free_rate_pct * calendar_days / 365

    ``annualization.risk_free_rate_pct`` is cross-checked against the supplied
    ``risk_free_rate_pct`` so the canonical config and the explicit argument
    cannot silently drift.
    """
    _require_finite(risk_free_rate_pct, name="risk_free_rate_pct")
    if risk_free_rate_pct < 0:
        raise EvaluationConfigError(
            "non_finite_value",
            "risk_free_rate_pct must be non-negative",
        )
    if calendar_days < 0:
        raise EvaluationConfigError(
            "non_finite_value",
            "calendar_days must be non-negative",
        )
    # Cross-check: the explicit value must equal the canonical annualization
    # value.  This catches drift between the caller and the frozen config.
    if annualization.risk_free_rate_pct != risk_free_rate_pct:
        raise EvaluationConfigError(
            "non_finite_value",
            "risk_free_rate_pct must match annualization.risk_free_rate_pct",
        )
    if calendar_days == 0:
        return Decimal("0")
    return risk_free_rate_pct * Decimal(calendar_days) / _DAYS_PER_YEAR


def compute_btc_eth_equal_weight_benchmark_return_pct(
    *,
    initial_prices: dict[str, Decimal],
    ending_prices: dict[str, Decimal],
    weights: BenchmarkWeights,
) -> Decimal:
    """BTC/ETH weighted benchmark return in native currency (percent).

    Computed as::

        return_pct = btc_weight * (ending_btc / initial_btc - 1) * 100
                   + eth_weight * (ending_eth / initial_eth - 1) * 100

    No USDT/USD conversion is applied.  ``initial_prices`` and
    ``ending_prices`` must each contain exactly the keys ``BTCUSDT`` and
    ``ETHUSDT``.
    """
    if set(initial_prices) != _REQUIRED_BENCHMARK_SYMBOLS:
        raise EvaluationConfigError(
            "invalid_benchmark_prices",
            f"initial_prices must contain {sorted(_REQUIRED_BENCHMARK_SYMBOLS)}",
        )
    if set(ending_prices) != _REQUIRED_BENCHMARK_SYMBOLS:
        raise EvaluationConfigError(
            "invalid_benchmark_prices",
            f"ending_prices must contain {sorted(_REQUIRED_BENCHMARK_SYMBOLS)}",
        )
    for symbol in _REQUIRED_BENCHMARK_SYMBOLS:
        _require_finite(initial_prices[symbol], name=f"initial_prices[{symbol}]")
        _require_finite(ending_prices[symbol], name=f"ending_prices[{symbol}]")
        if initial_prices[symbol] <= 0:
            raise EvaluationConfigError(
                "non_finite_value",
                f"initial_prices[{symbol}] must be positive",
            )
        if ending_prices[symbol] <= 0:
            raise EvaluationConfigError(
                "non_finite_value",
                f"ending_prices[{symbol}] must be positive",
            )

    btc_return_pct = (
        ending_prices[_BTC_SYMBOL] / initial_prices[_BTC_SYMBOL] - Decimal(1)
    ) * _PERCENT
    eth_return_pct = (
        ending_prices[_ETH_SYMBOL] / initial_prices[_ETH_SYMBOL] - Decimal(1)
    ) * _PERCENT
    return weights.btc_weight * btc_return_pct + weights.eth_weight * eth_return_pct


# ---------------------------------------------------------------------------
# Backtest → forward decay
# ---------------------------------------------------------------------------


def compute_backtest_forward_decay(
    *,
    backtest_return_pct: Decimal,
    forward_return_pct: Decimal,
) -> Decimal | None:
    """Ratio of forward to backtest return.

    Returns ``None`` when the backtest return is zero or negative, because
    the ratio is undefined or misleading in those cases.
    """
    _require_finite(backtest_return_pct, name="backtest_return_pct")
    _require_finite(forward_return_pct, name="forward_return_pct")
    if backtest_return_pct <= 0:
        return None
    return forward_return_pct / backtest_return_pct


# ---------------------------------------------------------------------------
# Evidence IDs
# ---------------------------------------------------------------------------


def _view_evidence_id(metrics: ViewMetrics) -> str:
    """Deterministic per-view evidence ID."""
    payload = metrics.model_dump(mode="python")
    digest = canonical_sha256(payload)[:16]
    return f"view:{metrics.view_name.value}::{digest}"


def _aggregate_evidence_id(
    view_metrics: Mapping[ViewName, ViewMetrics],
) -> str:
    """Deterministic aggregate verdict evidence ID.

    Order-independent: ``canonical_sha256`` sorts dict keys, so the result
    does not depend on insertion order.
    """
    payload = {
        name.value: metrics.model_dump(mode="python")
        for name, metrics in view_metrics.items()
    }
    digest = canonical_sha256(payload)[:16]
    return f"verdict::aggregate::{digest}"


def generate_evidence_ids(
    view_metrics: Mapping[ViewName, ViewMetrics],
) -> tuple[str, ...]:
    """Generate deterministic evidence IDs from view metrics.

    Returns a tuple of length ``len(view_metrics) + 1`` containing one
    per-view ID followed by the aggregate verdict ID::

        ("view:{view_name}::{digest16}", ..., "verdict::aggregate::{digest16}")

    The per-view portion is sorted by ``ViewName.value`` for deterministic
    ordering; the aggregate hash itself is order-independent.
    """
    _require_required_views(view_metrics)
    per_view = tuple(
        _view_evidence_id(view_metrics[name])
        for name in sorted(view_metrics, key=lambda v: v.value)
    )
    return (*per_view, _aggregate_evidence_id(view_metrics))


# ---------------------------------------------------------------------------
# Conjunctive verdict
# ---------------------------------------------------------------------------


def _aggregate_extrema(
    view_metrics: Mapping[ViewName, ViewMetrics],
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(min_net_return_pct, max_max_drawdown_pct, min_benchmark_delta_pct)``.

    Always computed regardless of verdict status; consumed by the
    :class:`ScorecardVerdict` aggregate-extrema validators.
    """
    returns = [m.net_return_pct for m in view_metrics.values()]
    mdds = [m.max_drawdown_pct for m in view_metrics.values()]
    deltas: list[Decimal] = []
    for metrics in view_metrics.values():
        deltas.append(metrics.cash_benchmark_delta_pct)
        deltas.append(metrics.btc_eth_benchmark_delta_pct)
    return min(returns), max(mdds), min(deltas)


def _determine_status(
    *,
    view_metrics: Mapping[ViewName, ViewMetrics],
    config: EvaluationConfig,
    shadow_gate: GateVerdict | None,
    paper_gate: GateVerdict | None,
) -> tuple[VerdictStatus, str, str]:
    """Apply the ROB-850 AC 6/9 conjunctive rules in precedence order.

    Returns ``(status, reason_code, reason_text)``.
    """
    min_observations = config.minimum_evidence.min_observations
    min_fills = config.minimum_evidence.min_fills
    fail_close = config.missing_data_policy is MissingDataPolicy.FAIL_CLOSE

    # 1. Insufficient evidence — evaluated before any other rule.
    for name in sorted(view_metrics, key=lambda v: v.value):
        metrics = view_metrics[name]
        observed_count = metrics.fill_count + metrics.missing_observation_count
        if fail_close and metrics.missing_observation_count > 0:
            return (
                VerdictStatus.INSUFFICIENT_EVIDENCE,
                "insufficient_evidence",
                (
                    f"view {name.value} has {metrics.missing_observation_count} "
                    "missing observations under fail_close policy"
                ),
            )
        if metrics.fill_count < min_fills:
            return (
                VerdictStatus.INSUFFICIENT_EVIDENCE,
                "insufficient_evidence",
                (
                    f"view {name.value} fill_count {metrics.fill_count} "
                    f"below min_fills {min_fills}"
                ),
            )
        if observed_count < min_observations:
            return (
                VerdictStatus.INSUFFICIENT_EVIDENCE,
                "insufficient_evidence",
                (
                    f"view {name.value} observation count {observed_count} "
                    f"below min_observations {min_observations}"
                ),
            )

    # 2. Both authoritative transition-derived gates are mandatory.
    if shadow_gate is None or paper_gate is None:
        return (
            VerdictStatus.GATE_BLOCKED,
            "missing_gate_evidence",
            "authoritative shadow and paper gate evidence are required",
        )
    # 3. Shadow gate.
    if not shadow_gate.passed:
        return (
            VerdictStatus.GATE_BLOCKED,
            "shadow_gate_blocked",
            f"shadow gate not passed: {shadow_gate.reason_text}",
        )

    # 4. Paper gate.
    if not paper_gate.passed:
        return (
            VerdictStatus.GATE_BLOCKED,
            "paper_gate_blocked",
            f"paper gate not passed: {paper_gate.reason_text}",
        )

    minimum_days = config.minimum_evidence.min_calendar_days
    if (
        shadow_gate.calendar_days_observed < minimum_days
        or paper_gate.calendar_days_observed < minimum_days
    ):
        return (
            VerdictStatus.INSUFFICIENT_EVIDENCE,
            "insufficient_calendar_days",
            f"each evaluation view requires at least {minimum_days} full days",
        )

    # 4 & 5. Benchmarks (cash and BTC/ETH).  delta <= 0 fails.
    for name in sorted(view_metrics, key=lambda v: v.value):
        metrics = view_metrics[name]
        if metrics.cash_benchmark_delta_pct <= 0:
            return (
                VerdictStatus.BENCHMARK_NOT_BEATEN,
                "cash_benchmark_not_beaten",
                (
                    f"view {name.value} cash_benchmark_delta_pct "
                    f"{metrics.cash_benchmark_delta_pct} <= 0"
                ),
            )
        if metrics.btc_eth_benchmark_delta_pct <= 0:
            return (
                VerdictStatus.BENCHMARK_NOT_BEATEN,
                "btc_eth_benchmark_not_beaten",
                (
                    f"view {name.value} btc_eth_benchmark_delta_pct "
                    f"{metrics.btc_eth_benchmark_delta_pct} <= 0"
                ),
            )

    # 6. MDD.
    mdd_target = config.mdd_target_pct
    for name in sorted(view_metrics, key=lambda v: v.value):
        metrics = view_metrics[name]
        if metrics.max_drawdown_pct > mdd_target:
            return (
                VerdictStatus.MDD_EXCEEDED,
                "mdd_exceeded",
                (
                    f"view {name.value} max_drawdown_pct "
                    f"{metrics.max_drawdown_pct} exceeds target {mdd_target}"
                ),
            )

    # 7. Promotion eligible.
    return (
        VerdictStatus.PROMOTION_ELIGIBLE,
        "promotion_eligible",
        "all views passed conjunctive promotion criteria",
    )


def compute_conjunctive_verdict(
    *,
    view_metrics: dict[ViewName, ViewMetrics],
    config: EvaluationConfig,
    shadow_gate: GateVerdict | None,
    paper_gate: GateVerdict | None,
    epoch_id: str,
    experiment_hash: str,
    cohort_hash: str,
) -> ScorecardVerdict:
    """Compute the conjunctive :class:`ScorecardVerdict` across all three views.

    The verdict is the single deterministic output consumed by ROB-848.
    Status precedence (ROB-850 AC 6, 9):

    1. ``INSUFFICIENT_EVIDENCE`` — any view fails the
       minimum-evidence / fail-close missing-observation policy.
    2. ``GATE_BLOCKED`` — shadow or paper gate did not pass.
    3. ``BENCHMARK_NOT_BEATEN`` — any view's cash or BTC/ETH benchmark
       delta is ``<= 0``.
    4. ``MDD_EXCEEDED`` — any view's max drawdown exceeds the target.
    5. ``PROMOTION_ELIGIBLE`` — all checks passed.

    Aggregate extrema (``min_net_return_pct``, ``max_max_drawdown_pct``,
    ``min_benchmark_delta_pct``) are always computed regardless of status.

    No cross-view nominal P&L total is computed or emitted.
    """
    _require_required_views(view_metrics)

    # Currency mismatch defensive check.  The frozen contract already binds
    # each view's currency at construction, so this guards against future
    # contract relaxation and provides a stable reason code for callers.
    for name, metrics in view_metrics.items():
        if metrics.view_name != name:
            raise EvaluationConfigError(
                "currency_mismatch",
                f"view key {name.value} mismatches metrics.view_name "
                f"{metrics.view_name.value}",
            )
        expected_currency = config.views[name].currency
        if metrics.currency != expected_currency:
            raise EvaluationConfigError(
                "currency_mismatch",
                f"view {name.value} currency {metrics.currency.value} "
                f"!= config currency {expected_currency.value}",
            )

    min_net_return_pct, max_max_drawdown_pct, min_benchmark_delta_pct = (
        _aggregate_extrema(view_metrics)
    )

    status, reason_code, reason_text = _determine_status(
        view_metrics=view_metrics,
        config=config,
        shadow_gate=shadow_gate,
        paper_gate=paper_gate,
    )

    evidence_ids = generate_evidence_ids(view_metrics)

    if shadow_gate is None:
        shadow_gate = GateVerdict(
            gate_type=GateType.SHADOW_SOAK,
            calendar_days_observed=0,
            required_days=config.shadow_soak_days,
            passed=False,
            reason_code="missing_shadow_transition",
            reason_text="authoritative shadow_soak transition is missing",
        )
    if paper_gate is None:
        paper_gate = GateVerdict(
            gate_type=GateType.PAPER_PROMOTION,
            calendar_days_observed=0,
            required_days=config.paper_promotion_days,
            passed=False,
            reason_code="missing_paper_transition",
            reason_text="authoritative paper_active transition is missing",
        )

    return ScorecardVerdict(
        status=status,
        epoch_id=epoch_id,
        config_hash=config.config_hash(),
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        view_metrics=dict(view_metrics),
        min_net_return_pct=min_net_return_pct,
        max_max_drawdown_pct=max_max_drawdown_pct,
        min_benchmark_delta_pct=min_benchmark_delta_pct,
        shadow_gate=shadow_gate,
        paper_gate=paper_gate,
        evidence_ids=evidence_ids,
        reason_code=reason_code,
        reason_text=reason_text,
    )


__all__ = [
    "compute_backtest_forward_decay",
    "compute_btc_eth_equal_weight_benchmark_return_pct",
    "compute_cash_benchmark_return_pct",
    "compute_conjunctive_verdict",
    "generate_evidence_ids",
]
