"""Unit tests for the read-only US earnings coverage probe CLI (ROB-371)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.services.market_events.coverage_gate import (
    CoverageMeasurement,
    Section5Thresholds,
    evaluate_section5_gate,
)
from scripts.probe_us_earnings_coverage import build_artifact, parse_args

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _measurement(**overrides) -> CoverageMeasurement:
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
def test_dry_run_is_default():
    args = parse_args(["--from-date", "2025-01-01", "--to-date", "2025-12-31"])
    assert args.run is False
    assert args.dry_run is True


@pytest.mark.unit
def test_run_flag_disables_dry_run():
    args = parse_args(["--run"])
    assert args.run is True
    assert args.dry_run is False


@pytest.mark.unit
def test_defaults_span_last_year():
    args = parse_args([])
    assert isinstance(args.from_date, date)
    assert isinstance(args.to_date, date)
    assert args.from_date < args.to_date


@pytest.mark.unit
def test_help_runs_without_secrets():
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])
    assert exc.value.code == 0


@pytest.mark.unit
def test_parse_args_does_not_require_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    args = parse_args(["--from-date", "2025-01-01", "--to-date", "2025-06-30"])
    assert args.from_date == date(2025, 1, 1)


@pytest.mark.unit
def test_build_artifact_is_counts_only():
    # B3: every measurement value must be a scalar — no symbol / bar-date arrays
    # can leak into the committed-adjacent artifact.
    m = _measurement()
    gate = evaluate_section5_gate(m, Section5Thresholds())
    artifact = build_artifact(
        m,
        gate,
        from_date=date(2025, 1, 1),
        to_date=date(2025, 12, 31),
        backfill_performed=False,
    )
    assert all(
        isinstance(v, (int, float, bool)) for v in artifact["measurement"].values()
    )
    # criteria carry only names/numbers/notes — no raw symbols.
    for crit in artifact["criteria"]:
        assert set(crit) == {"name", "observed", "threshold", "passed", "note"}


@pytest.mark.unit
def test_build_artifact_carries_verdict_and_schema():
    m = _measurement()
    gate = evaluate_section5_gate(m, Section5Thresholds())
    artifact = build_artifact(
        m,
        gate,
        from_date=date(2025, 1, 1),
        to_date=date(2025, 12, 31),
        backfill_performed=False,
    )
    assert artifact["schema_version"] == "us_earnings_coverage.v1"
    assert artifact["passed"] is True
    assert "PASS" in artifact["verdict"].upper()
    assert artifact["backfill_performed"] is False


@pytest.mark.integration
def test_dry_run_subprocess_needs_no_secrets(tmp_path):
    # Blocker 1 regression: the default (no --run) dry-run must exit 0 WITHOUT
    # loading app Settings — i.e. with no KIS/Upbit/OpenDART/DATABASE_URL etc.
    # Run from a temp CWD (so pydantic's env_file=".env" finds nothing) with a
    # stripped environment; if the dry-run imported app.core.config the required
    # fields would fail validation and exit non-zero.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(_REPO_ROOT),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.probe_us_earnings_coverage",
            "--from-date",
            "2025-01-01",
            "--to-date",
            "2025-01-31",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "[DRY-RUN]" in (proc.stdout + proc.stderr)
