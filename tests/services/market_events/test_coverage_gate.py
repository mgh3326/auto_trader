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
        "intraday_excluded_events": 0,
        "dup_ambiguous_ratio": 0.0,
        "tradability_coverage": 0.95,
        "benchmark_coverage": 0.97,
        "delisted_events": 40,
        "delisted_recoverable": 38,
        "session_calendar_present": True,
    }
    base.update(overrides)
    # eligible_events defaults to the (post-intraday-exclusion) realized total so
    # callers that only tweak realized/joinable stay self-consistent (ROB-378).
    base.setdefault(
        "eligible_events", base["realized_events"] - base["intraday_excluded_events"]
    )
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
def test_intraday_excluded_does_not_hard_fail():
    # ROB-378: intraday (during_market) events are excluded from the eligible
    # population and reported, NOT a hard gate failure. With a healthy eligible
    # population the gate still PASSes.
    result = evaluate_section5_gate(
        _passing_measurement(
            realized_events=618,
            intraday_excluded_events=18,
            eligible_events=600,
            joinable_events=560,
        ),
        Section5Thresholds(),
    )
    assert result.passed is True
    crit = {c.name: c for c in result.criteria}
    assert "intraday_excluded" in crit
    assert crit["intraday_excluded"].passed is True
    assert crit["intraday_excluded"].observed == 18
    # The legacy hard-fail criterion no longer exists.
    assert "no_intraday_labeling" not in crit


@pytest.mark.unit
def test_intraday_excluded_from_joinable_ratio_denominator():
    # 600 eligible events, 540 joinable -> 540/600 = 0.90 passes. The 200 excluded
    # intraday events must NOT enter the denominator (else 540/800 = 0.675 fails).
    result = evaluate_section5_gate(
        _passing_measurement(
            realized_events=800,
            intraday_excluded_events=200,
            eligible_events=600,
            joinable_events=540,
        ),
        Section5Thresholds(),
    )
    ratio = next(c for c in result.criteria if c.name == "min_joinable_event_ratio")
    assert ratio.observed == pytest.approx(0.90)
    assert ratio.passed is True


@pytest.mark.unit
def test_all_intraday_reports_no_eligible_population_not_quality_failure():
    # FALSE-FAIL guard: events exist but every one is intraday-excluded -> the
    # eligible daily-granularity population is empty (a scope limit, not a
    # join-quality or materialization failure).
    m = _passing_measurement(
        realized_events=600,
        intraday_excluded_events=600,
        eligible_events=0,
        events_with_bars_present=0,
        events_with_zero_bars=0,
        joinable_events=0,
        joinable_symbols=0,
    )
    result = evaluate_section5_gate(m, Section5Thresholds())
    assert result.passed is False
    assert "eligible" in result.verdict.lower()
    assert "not materialized" not in result.verdict.lower()


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
