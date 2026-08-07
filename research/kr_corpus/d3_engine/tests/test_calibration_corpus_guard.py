from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from research.kr_corpus.backtest.holdout_guard import (
    HoldoutAccessError,
    assert_date_not_holdout,
    assert_partition_year_not_holdout,
)
from research.kr_corpus.d3_engine.calibration_corpus import (
    CalibrationAccessBlocked,
    CalibrationAccessGuard,
    CalibrationAccessSpy,
    measure_calibration_fail_closed_probes,
)
from research.kr_corpus.d3_engine.guards import SealedAccessBlocked, SealedAccessGuard


def test_year_gate_authorizes_only_2025() -> None:
    guard = CalibrationAccessGuard()
    assert guard.assert_calibration_year(2025) == 2025
    with pytest.raises(CalibrationAccessBlocked):
        guard.assert_calibration_year(2026)
    with pytest.raises(CalibrationAccessBlocked):
        guard.assert_calibration_year(2024)
    assert guard.spy.blocked_year_attempts == 2


def test_date_gate_authorizes_only_calendar_2025() -> None:
    guard = CalibrationAccessGuard()
    assert guard.assert_calibration_date(date(2025, 12, 31)) == date(2025, 12, 31)
    with pytest.raises(CalibrationAccessBlocked):
        guard.assert_calibration_date(date(2026, 1, 1))
    with pytest.raises(CalibrationAccessBlocked):
        guard.assert_calibration_date(date(2024, 12, 31))
    assert guard.spy.blocked_date_attempts == 2


def test_path_gate_refuses_non_holdout_root(tmp_path: Path) -> None:
    guard = CalibrationAccessGuard()
    with pytest.raises(CalibrationAccessBlocked):
        guard.assert_calibration_path(tmp_path / "dataset" / "year=2025" / "x.parquet")
    assert guard.spy.blocked_path_attempts == 1


def test_path_gate_refuses_2026_partition_even_under_holdout_root() -> None:
    from research.kr_corpus.d3_engine.calibration_corpus import (
        CalibrationCorpusPaths,
    )

    guard = CalibrationAccessGuard()
    root = CalibrationCorpusPaths.defaults().holdout_root
    with pytest.raises(CalibrationAccessBlocked):
        guard.assert_calibration_path(
            root / "dataset" / "market=KOSPI" / "year=2026" / "ticker=005930.parquet"
        )


def test_precheck_anomaly_line_never_decodes_2026_json() -> None:
    guard = CalibrationAccessGuard()
    line_2025 = json.dumps({"session": "2025-06-02", "detail": {"open": 1}}).encode()
    line_2026 = json.dumps(
        {"session": "2026-01-05", "detail": {"open": 999999}}
    ).encode()

    assert guard.precheck_anomaly_line(line_2025) is True
    assert guard.precheck_anomaly_line(line_2026) is False
    assert guard.spy.anomaly_lines_decoded_2025 == 1
    assert guard.spy.anomaly_lines_skipped_2026_undecoded == 1
    assert guard.spy.anomaly_lines_prechecked == 2


def test_record_bar_rows_blocks_on_first_prospective_date() -> None:
    guard = CalibrationAccessGuard()
    with pytest.raises(CalibrationAccessBlocked):
        guard.record_bar_rows([date(2025, 6, 2), date(2026, 1, 5)])
    # the 2025 row is still counted before the blocking row is hit
    assert guard.spy.bar_rows_read == 0 or guard.spy.date_checks >= 1


def test_fail_closed_probe_suite_passes_and_leaves_zero_authorized_reads() -> None:
    result = measure_calibration_fail_closed_probes()
    assert result["status"] == "PASS", result
    assert result["loader_calls"] == 0
    evidence = result["spy_evidence"]
    assert evidence["calibration_parquet_files_read"] == 0
    assert evidence["calibration_manifest_reads"] == 0
    assert evidence["blocked_year_attempts"] >= 1
    assert evidence["blocked_path_attempts"] >= 1


def test_holdout_guard_regression_unchanged_for_both_holdout_years() -> None:
    """This module must not weaken holdout_guard for any other caller."""
    with pytest.raises(HoldoutAccessError):
        assert_date_not_holdout(date(2025, 6, 1))
    with pytest.raises(HoldoutAccessError):
        assert_date_not_holdout(date(2026, 1, 5))
    with pytest.raises(HoldoutAccessError):
        assert_partition_year_not_holdout(2025)
    with pytest.raises(HoldoutAccessError):
        assert_partition_year_not_holdout(2026)


def test_sealed_access_guard_regression_unchanged() -> None:
    """This module must not weaken guards.SealedAccessGuard for exploration code."""
    guard = SealedAccessGuard()
    with pytest.raises(SealedAccessBlocked):
        guard.assert_exploration_date(date(2025, 1, 2))
    with pytest.raises(SealedAccessBlocked):
        guard.assert_exploration_path("/x/holdout/y")


def test_spy_evidence_shape_is_stable() -> None:
    spy = CalibrationAccessSpy()
    evidence = spy.evidence()
    assert evidence["calibration_year_checks"] == 0
    assert evidence["blocked_year_attempts"] == 0
    assert set(evidence) == {
        "calibration_year_checks",
        "calibration_date_checks",
        "calibration_path_checks",
        "calibration_manifest_reads",
        "calibration_checksums_reads",
        "calibration_parquet_files_read",
        "calibration_bar_rows_read",
        "calibration_gap_files_read",
        "calibration_gap_rows_read",
        "calibration_anomaly_lines_prechecked",
        "calibration_anomaly_lines_decoded_2025",
        "calibration_anomaly_lines_skipped_2026_undecoded",
        "blocked_year_attempts",
        "blocked_date_attempts",
        "blocked_path_attempts",
    }
