"""Tests for ROB-850 shadow/paper promotion gates and evidence checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.paper_evaluation.contracts import (
    EvaluationConfigError,
    GateType,
    GateVerdict,
    MinimumEvidence,
    ViewCurrency,
    ViewMetrics,
    ViewName,
    ViewSource,
)
from app.services.paper_evaluation.gate import (
    evaluate_insufficient_evidence,
    evaluate_paper_gate,
    evaluate_shadow_gate,
)

pytestmark = pytest.mark.unit

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shadow gate (7 days)
# ---------------------------------------------------------------------------


def test_shadow_gate_blocked_at_zero_days() -> None:
    verdict = evaluate_shadow_gate(
        shadow_started_at=_START,
        evaluated_at=_START,
    )
    assert verdict.passed is False
    assert verdict.gate_type is GateType.SHADOW_SOAK
    assert verdict.reason_code == "shadow_soak_incomplete"
    assert verdict.required_days == 7
    assert verdict.calendar_days_observed == 0


def test_shadow_gate_blocked_at_six_days() -> None:
    verdict = evaluate_shadow_gate(
        shadow_started_at=_START,
        evaluated_at=_START + timedelta(days=6),
    )
    assert verdict.passed is False
    assert verdict.reason_code == "shadow_soak_incomplete"
    assert verdict.calendar_days_observed == 6


def test_shadow_gate_boundary_six_days_23h59m59s_still_six() -> None:
    midnight = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    verdict = evaluate_shadow_gate(
        shadow_started_at=midnight,
        evaluated_at=midnight
        + timedelta(days=6, hours=23, minutes=59, seconds=59),
    )
    assert verdict.passed is False
    assert verdict.calendar_days_observed == 6
    assert verdict.reason_code == "shadow_soak_incomplete"


def test_shadow_gate_passes_at_exactly_seven_days() -> None:
    verdict = evaluate_shadow_gate(
        shadow_started_at=_START,
        evaluated_at=_START + timedelta(days=7),
    )
    assert verdict.passed is True
    assert verdict.reason_code == "shadow_soak_complete"
    assert verdict.calendar_days_observed == 7


def test_shadow_gate_passes_at_eight_days() -> None:
    verdict = evaluate_shadow_gate(
        shadow_started_at=_START,
        evaluated_at=_START + timedelta(days=8),
    )
    assert verdict.passed is True
    assert verdict.calendar_days_observed == 8


# ---------------------------------------------------------------------------
# Paper gate (60 days)
# ---------------------------------------------------------------------------


def test_paper_gate_blocked_at_59_days() -> None:
    verdict = evaluate_paper_gate(
        paper_started_at=_START,
        evaluated_at=_START + timedelta(days=59),
        config_hash=_HASH_A,
        current_config_hash=_HASH_A,
    )
    assert verdict.passed is False
    assert verdict.gate_type is GateType.PAPER_PROMOTION
    assert verdict.reason_code == "paper_promotion_incomplete"
    assert verdict.required_days == 60
    assert verdict.calendar_days_observed == 59


def test_paper_gate_passes_at_exactly_60_days_matching_config() -> None:
    verdict = evaluate_paper_gate(
        paper_started_at=_START,
        evaluated_at=_START + timedelta(days=60),
        config_hash=_HASH_A,
        current_config_hash=_HASH_A,
    )
    assert verdict.passed is True
    assert verdict.reason_code == "paper_promotion_complete"
    assert verdict.calendar_days_observed == 60


def test_paper_gate_passes_at_61_days_matching_config() -> None:
    verdict = evaluate_paper_gate(
        paper_started_at=_START,
        evaluated_at=_START + timedelta(days=61),
        config_hash=_HASH_A,
        current_config_hash=_HASH_A,
    )
    assert verdict.passed is True
    assert verdict.calendar_days_observed == 61


def test_paper_gate_blocked_on_config_mismatch() -> None:
    verdict = evaluate_paper_gate(
        paper_started_at=_START,
        evaluated_at=_START + timedelta(days=60),
        config_hash=_HASH_A,
        current_config_hash=_HASH_B,
    )
    assert verdict.passed is False
    assert verdict.reason_code == "config_changed_mid_cohort"
    assert "cannot be spliced" in verdict.reason_text


# ---------------------------------------------------------------------------
# GateVerdict validator boundary consistency
# ---------------------------------------------------------------------------


def test_gate_verdict_rejects_passed_true_with_insufficient_days() -> None:
    with pytest.raises(EvaluationConfigError):
        GateVerdict(
            gate_type=GateType.SHADOW_SOAK,
            calendar_days_observed=6,
            required_days=7,
            passed=True,
            reason_code="invalid_state",
            reason_text="must not construct",
        )


def test_gate_verdict_rejects_passed_false_with_sufficient_days() -> None:
    with pytest.raises(EvaluationConfigError):
        GateVerdict(
            gate_type=GateType.SHADOW_SOAK,
            calendar_days_observed=7,
            required_days=7,
            passed=False,
            reason_code="invalid_state",
            reason_text="must not construct",
        )


# ---------------------------------------------------------------------------
# evaluate_insufficient_evidence
# ---------------------------------------------------------------------------


def _view_metrics(
    *,
    fill_count: int = 100,
    missing_observation_count: int = 0,
    view_name: ViewName = ViewName.BINANCE_BROKER,
) -> ViewMetrics:
    currency, source = {
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
    }[view_name]
    initial_equity = Decimal("10000")
    nominal_net_pnl = Decimal("0")
    return ViewMetrics(
        view_name=view_name,
        currency=currency,
        source=source,
        symbol_mapping=("BTCUSDT", "ETHUSDT"),
        initial_equity=initial_equity,
        ending_equity=initial_equity + nominal_net_pnl,
        nominal_net_pnl=nominal_net_pnl,
        fees=Decimal("0"),
        net_return_pct=Decimal("0"),
        max_drawdown_pct=Decimal("0"),
        turnover=Decimal("0"),
        exposure=Decimal("0"),
        fill_count=fill_count,
        partial_fill_count=0,
        missing_observation_count=missing_observation_count,
        cash_benchmark_return_pct=Decimal("0"),
        cash_benchmark_delta_pct=Decimal("0"),
        btc_eth_benchmark_return_pct=Decimal("0"),
        btc_eth_benchmark_delta_pct=Decimal("0"),
        experiment_hash=_HASH_A,
        cohort_hash=_HASH_A,
        epoch_id="epoch-1",
        config_hash=_HASH_A,
    )


def _all_views_sufficient_metrics() -> dict[ViewName, ViewMetrics]:
    return {
        ViewName.BINANCE_BROKER: _view_metrics(view_name=ViewName.BINANCE_BROKER),
        ViewName.ALPACA_BROKER: _view_metrics(view_name=ViewName.ALPACA_BROKER),
        ViewName.CANONICAL_SHADOW: _view_metrics(
            view_name=ViewName.CANONICAL_SHADOW
        ),
    }


def test_all_views_sufficient_returns_true_no_reasons() -> None:
    minimum = MinimumEvidence(
        min_observations=100, min_fills=10, min_calendar_days=7
    )
    sufficient, reasons = evaluate_insufficient_evidence(
        view_metrics=_all_views_sufficient_metrics(),
        minimum_evidence=minimum,
    )
    assert sufficient is True
    assert reasons == []


def test_missing_observations_flagged() -> None:
    metrics = {
        ViewName.BINANCE_BROKER: _view_metrics(
            view_name=ViewName.BINANCE_BROKER,
            fill_count=100,
            missing_observation_count=5,
        ),
    }
    minimum = MinimumEvidence(
        min_observations=100, min_fills=10, min_calendar_days=7
    )
    sufficient, reasons = evaluate_insufficient_evidence(
        view_metrics=metrics, minimum_evidence=minimum
    )
    assert sufficient is False
    assert any("missing observations" in reason for reason in reasons)


def test_low_fill_count_flagged() -> None:
    metrics = {
        ViewName.BINANCE_BROKER: _view_metrics(
            view_name=ViewName.BINANCE_BROKER,
            fill_count=5,
        ),
    }
    minimum = MinimumEvidence(
        min_observations=100, min_fills=10, min_calendar_days=7
    )
    sufficient, reasons = evaluate_insufficient_evidence(
        view_metrics=metrics, minimum_evidence=minimum
    )
    assert sufficient is False
    assert any("fill_count" in reason for reason in reasons)


def test_multiple_views_with_issues_flagged() -> None:
    metrics = {
        ViewName.BINANCE_BROKER: _view_metrics(
            view_name=ViewName.BINANCE_BROKER, fill_count=5
        ),
        ViewName.ALPACA_BROKER: _view_metrics(
            view_name=ViewName.ALPACA_BROKER,
            fill_count=100,
            missing_observation_count=3,
        ),
    }
    minimum = MinimumEvidence(
        min_observations=100, min_fills=10, min_calendar_days=7
    )
    sufficient, reasons = evaluate_insufficient_evidence(
        view_metrics=metrics, minimum_evidence=minimum
    )
    assert sufficient is False
    assert len(reasons) >= 2
    assert any("fill_count" in reason for reason in reasons)
    assert any("missing observations" in reason for reason in reasons)
