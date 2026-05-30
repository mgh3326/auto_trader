"""Unit tests for the ROB-367 §5 coverage threshold gate (ROB-371)."""

from __future__ import annotations

import pytest

from app.services.market_events.coverage_gate import (
    CoverageMeasurement,
    Section5Thresholds,
    evaluate_section5_gate,
)


def _passing_measurement(**overrides) -> CoverageMeasurement:
    base = {
        "realized_events": 600,
        "events_with_bars_present": 590,
        "events_with_zero_bars": 10,
        "joinable_events": 560,
        "joinable_symbols": 250,
        "window_coverage_p50": 0.98,
        "date_only_ratio": 1.0,
        "unknown_time_ratio": 0.05,
        "intraday_labeled_events": 0,
        "dup_ambiguous_ratio": 0.0,
        "tradability_coverage": 0.95,
        "benchmark_coverage": 0.97,
        "delisted_events": 40,
        "delisted_recoverable": 38,
        "session_calendar_present": True,
    }
    base.update(overrides)
    return CoverageMeasurement(**base)


@pytest.mark.unit
def test_full_coverage_passes():
    result = evaluate_section5_gate(_passing_measurement(), Section5Thresholds())
    assert result.passed is True
    assert result.verdict.upper().startswith("PASS")
    assert all(c.passed for c in result.criteria)


@pytest.mark.unit
def test_too_few_events_fails_that_criterion():
    result = evaluate_section5_gate(
        _passing_measurement(realized_events=120, joinable_events=110),
        Section5Thresholds(),
    )
    assert result.passed is False
    failed = [c.name for c in result.criteria if not c.passed]
    assert "min_realized_events" in failed


@pytest.mark.unit
def test_low_joinable_event_ratio_fails():
    # 600 events but only 400 joinable -> ratio 0.667 < 0.90.
    result = evaluate_section5_gate(
        _passing_measurement(joinable_events=400), Section5Thresholds()
    )
    assert result.passed is False
    assert any(
        c.name == "min_joinable_event_ratio" and not c.passed for c in result.criteria
    )


@pytest.mark.unit
def test_too_few_joinable_symbols_fails():
    result = evaluate_section5_gate(
        _passing_measurement(joinable_symbols=120), Section5Thresholds()
    )
    assert result.passed is False
    assert any(
        c.name == "min_joinable_symbols" and not c.passed for c in result.criteria
    )


@pytest.mark.unit
def test_intraday_labeled_events_hard_fail():
    result = evaluate_section5_gate(
        _passing_measurement(intraday_labeled_events=3), Section5Thresholds()
    )
    assert result.passed is False
    assert any(
        c.name == "no_intraday_labeling" and not c.passed for c in result.criteria
    )


@pytest.mark.unit
def test_dup_ratio_above_one_percent_fails():
    result = evaluate_section5_gate(
        _passing_measurement(dup_ambiguous_ratio=0.02), Section5Thresholds()
    )
    assert result.passed is False
    assert any(c.name == "max_dup_ambiguous" and not c.passed for c in result.criteria)


@pytest.mark.unit
def test_low_benchmark_coverage_fails():
    result = evaluate_section5_gate(
        _passing_measurement(benchmark_coverage=0.50), Section5Thresholds()
    )
    assert result.passed is False
    assert any(c.name == "min_benchmark" and not c.passed for c in result.criteria)


@pytest.mark.unit
def test_missing_session_calendar_fails():
    result = evaluate_section5_gate(
        _passing_measurement(session_calendar_present=False), Section5Thresholds()
    )
    assert result.passed is False
    assert any(
        c.name == "session_calendar_present" and not c.passed for c in result.criteria
    )


@pytest.mark.unit
def test_zero_events_reports_no_data_not_quality_failure():
    m = _passing_measurement(
        realized_events=0,
        events_with_bars_present=0,
        events_with_zero_bars=0,
        joinable_events=0,
        joinable_symbols=0,
    )
    result = evaluate_section5_gate(m, Section5Thresholds())
    assert result.passed is False
    assert "no earnings events" in result.verdict.lower()


@pytest.mark.unit
def test_zero_bars_everywhere_reports_not_materialized_not_join_failure():
    # FALSE-FAIL guard: events present but every window empty -> "not materialized".
    m = _passing_measurement(
        events_with_bars_present=0,
        events_with_zero_bars=600,
        joinable_events=0,
        joinable_symbols=0,
    )
    result = evaluate_section5_gate(m, Section5Thresholds())
    assert result.passed is False
    assert "not materialized" in result.verdict.lower()


@pytest.mark.unit
def test_verdict_contains_machine_parsed_keyword():
    # Verdict keywords (PASS/FAIL) are machine-parsed by operators/dashboards.
    passing = evaluate_section5_gate(_passing_measurement(), Section5Thresholds())
    assert "PASS" in passing.verdict.upper()
    failing = evaluate_section5_gate(
        _passing_measurement(joinable_symbols=1), Section5Thresholds()
    )
    assert "FAIL" in failing.verdict.upper()
