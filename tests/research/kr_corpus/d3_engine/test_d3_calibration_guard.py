"""Amendment A2 acceptance: fail-closed routes, mutants, and regression pins.

Hermetic — every probe runs against a synthetic holdout root under ``tmp_path``
patched over ``HOLDOUT_DIR``. No sealed byte is opened here; the real run's
measured access log lives in the artifact, not in this suite.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.kr_corpus.backtest import holdout_guard
from research.kr_corpus.d3_engine import calibration_acceptance, calibration_guard
from research.kr_corpus.d3_engine.calibration_acceptance import (
    measure_calibration_fail_closed_probes,
    measure_primary_path_isolation,
)
from research.kr_corpus.d3_engine.calibration_corpus import (
    CalibrationCorpusInvalid,
    load_calibration_corpus,
    load_calibration_manifest,
)
from research.kr_corpus.d3_engine.calibration_guard import (
    AMENDMENT_A2_SHA256,
    CALIBRATION_INDEX_SHA256,
    HOLDOUT_RUN_ID,
    CalibrationAccessGuard,
    CalibrationAccessSpy,
    CalibrationScopeError,
    partition_year,
)
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)
from research.kr_corpus.d3_engine.tick import TickTable

SESSIONS = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 6, 2), date(2025, 12, 30))


def _ticks() -> TickTable:
    return TickTable.from_mapping(
        {
            "schema_version": "d3.krx_tick_table.v1",
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 2000, "tick": 1},
                {"lower_inclusive": 2000, "upper_exclusive": 5000, "tick": 5},
                {"lower_inclusive": 5000, "upper_exclusive": 20000, "tick": 10},
                {"lower_inclusive": 20000, "upper_exclusive": 50000, "tick": 50},
                {"lower_inclusive": 50000, "upper_exclusive": 200000, "tick": 100},
                {"lower_inclusive": 200000, "upper_exclusive": 500000, "tick": 500},
                {"lower_inclusive": 500000, "upper_exclusive": None, "tick": 1000},
            ],
        }
    )


@pytest.fixture
def sealed_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic sealed corpus: 2025 and 2026 partitions in one manifest."""

    root = tmp_path / "kr-corpus-v1" / "holdout"
    run_root = root / "runs" / HOLDOUT_RUN_ID
    files = {
        "dataset/market=KOSPI/year=2025/ticker=005930.parquet": b"authorized-2025",
        "dataset/market=KOSDAQ/year=2025/ticker=0004Y0.parquet": b"authorized-2025-b",
        "dataset/market=KOSPI/year=2026/ticker=005930.parquet": b"prospective",
        "gaps/market=KOSPI/year=2025/missing.parquet": b"gap-2025",
        "gaps/market=KOSPI/year=2026/missing.parquet": b"gap-2026",
        "source-anomalies.jsonl": b'{"session":"2025-01-02"}\n{"session":"2026-01-05"}\n',
        "coverage.json": b"{}",
    }
    rows = []
    for relative, payload in files.items():
        target = run_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        rows.append(f"{hashlib.sha256(payload).hexdigest()}  {relative}")
    checksums = ("\n".join(rows) + "\n").encode("utf-8")
    (run_root / "checksums.sha256").write_bytes(checksums)
    (run_root / "manifest.json").write_text(
        json.dumps(
            {
                "scope": "holdout",
                "corpus_id": "kr-corpus-v1",
                "files_list_location": "checksums.sha256",
                "checksums_sha256": hashlib.sha256(checksums).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    # A file that physically exists inside the 2025 partition but that the
    # manifest never enumerates.
    stray = (
        run_root / "dataset" / "market=KOSPI" / "year=2025" / "ticker=999999.parquet"
    )
    stray.write_bytes(b"not-enumerated")
    monkeypatch.setattr(holdout_guard, "HOLDOUT_DIR", root)
    monkeypatch.setattr(calibration_guard, "HOLDOUT_DIR", root)
    monkeypatch.setattr(
        calibration_acceptance, "PROBE_PROSPECTIVE_DATE", date(2026, 1, 5)
    )
    return root


def _bound_guard(sealed_root: Path) -> CalibrationAccessGuard:
    guard = CalibrationAccessGuard(
        calibration_sessions=SESSIONS,
        spy=CalibrationAccessSpy(),
        holdout_root=sealed_root,
    )
    load_calibration_manifest(guard, holdout_root=sealed_root)
    return guard


# -- allow-list construction ------------------------------------------------


def test_manifest_allowlist_admits_only_the_2025_partition(sealed_root: Path) -> None:
    guard = _bound_guard(sealed_root)
    names = sorted(path.name for path in guard.allowed_paths)
    assert names == [
        "checksums.sha256",
        "coverage.json",
        "manifest.json",
        "missing.parquet",
        "source-anomalies.jsonl",
        "ticker=0004Y0.parquet",
        "ticker=005930.parquet",
    ]
    assert all(partition_year(path) in (None, 2025) for path in guard.allowed_paths)
    assert guard.spy.manifest_excluded_out_of_scope == 2


def test_allowlist_binds_exactly_once(sealed_root: Path) -> None:
    guard = _bound_guard(sealed_root)
    with pytest.raises(CalibrationScopeError):
        guard.bind_manifest_allowlist([], excluded_out_of_scope=0)


def test_guard_refuses_a_non_2025_calendar() -> None:
    with pytest.raises(CalibrationScopeError):
        CalibrationAccessGuard(calibration_sessions=[date(2026, 1, 5)])
    with pytest.raises(CalibrationScopeError):
        CalibrationAccessGuard(calibration_sessions=[])


def test_checksums_drift_is_fatal(sealed_root: Path) -> None:
    run_root = sealed_root / "runs" / HOLDOUT_RUN_ID
    (run_root / "checksums.sha256").write_bytes(b"0" * 64 + b"  coverage.json\n")
    guard = CalibrationAccessGuard(
        calibration_sessions=SESSIONS, holdout_root=sealed_root
    )
    with pytest.raises(CalibrationCorpusInvalid):
        load_calibration_manifest(guard, holdout_root=sealed_root)


# -- the three A2 fail-closed routes ----------------------------------------


def test_fail_closed_three_routes_are_measured(sealed_root: Path) -> None:
    guard = _bound_guard(sealed_root)
    run_root = sealed_root / "runs" / HOLDOUT_RUN_ID
    report = measure_calibration_fail_closed_probes(
        template=guard,
        prospective_path=(
            run_root
            / "dataset"
            / "market=KOSPI"
            / "year=2026"
            / "ticker=005930.parquet"
        ),
        outside_manifest_path=(
            run_root
            / "dataset"
            / "market=KOSPI"
            / "year=2025"
            / "ticker=999999.parquet"
        ),
        authorized_path=(
            run_root
            / "dataset"
            / "market=KOSPI"
            / "year=2025"
            / "ticker=005930.parquet"
        ),
    )
    assert report["status"] == "PASS"
    assert report["outcomes"]["prospective_2026_date"] == "PASS"
    assert report["outcomes"]["prospective_partition_path"] == "PASS"
    assert report["outcomes"]["outside_manifest_file"] == "PASS"
    # measured, not declared: nothing was read and every route was counted
    assert report["loader_calls"] == 0
    assert report["authorized_sealed_reads_during_probes"] == 0
    assert report["blocked_attempts"] == 6
    evidence = report["spy_evidence"]
    assert evidence["calibration_blocked_prospective_date_attempts"] == 2
    assert evidence["calibration_blocked_offcalendar_2025_date_attempts"] == 1
    assert evidence["calibration_blocked_prospective_path_attempts"] == 1
    assert evidence["calibration_blocked_outside_manifest_attempts"] == 1
    assert evidence["calibration_blocked_sealed_token_attempts"] == 1


def test_authorized_route_stays_open(sealed_root: Path) -> None:
    guard = _bound_guard(sealed_root)
    authorized = (
        sealed_root
        / "runs"
        / HOLDOUT_RUN_ID
        / "dataset"
        / "market=KOSPI"
        / "year=2025"
        / "ticker=005930.parquet"
    )
    assert guard.read_parquet(path=authorized, loader=lambda: "ok") == "ok"
    guard.assert_exploration_date(date(2024, 12, 30))
    guard.assert_exploration_date(date(2025, 6, 2))
    guard.record_bar_rows([date(2025, 1, 2), date(2019, 5, 5)])
    evidence = guard.spy.evidence()
    assert evidence["calibration_authorized_parquet_reads"] == 1
    assert evidence["calibration_warmup_date_checks"] == 2
    assert evidence["sealed_access_blocked_attempts"] == 0


def test_datetime_input_is_gated_on_its_date(sealed_root: Path) -> None:
    guard = _bound_guard(sealed_root)
    guard.assert_exploration_date(datetime(2025, 6, 2, 15, 30))
    with pytest.raises(SealedAccessBlocked):
        guard.assert_exploration_date(datetime(2026, 1, 5, 9, 0))


# -- mutant 1: injecting the carve-out into the primary runner --------------


def test_primary_path_isolation_passes_unmutated() -> None:
    report = measure_primary_path_isolation(tick_table=_ticks())
    assert report["status"] == "PASS"
    assert report["installed_guard_class"] == "SealedAccessGuard"
    assert report["guard_is_sealed_access_guard_exactly"] is True
    assert report["blocks_2025_date"] is True
    assert report["blocks_2026_date"] is True
    assert report["blocks_holdout_2025_partition_path"] is True


def test_mutant_primary_injection_fails_acceptance(sealed_root: Path) -> None:
    """A2 mutant: wiring the calibration guard into primary must be caught."""

    mutant = measure_primary_path_isolation(
        tick_table=_ticks(), guard_override=_bound_guard(sealed_root)
    )
    assert mutant["status"] == "FAIL"
    assert mutant["installed_guard_class"] == "CalibrationAccessGuard"
    assert mutant["guard_is_sealed_access_guard_exactly"] is False
    assert mutant["blocks_2025_date"] is False


# -- mutant 2: a guard that stops checking manifest membership --------------


class _AllowsOutsideManifest(CalibrationAccessGuard):
    """Mutant: keep the year gate, drop the manifest membership gate."""

    def _authorize_path(self, value: str | Path) -> tuple[Path, str]:
        path = Path(value).expanduser().resolve(strict=False)
        if path.is_relative_to(self._holdout_root):
            return path, "calibration"
        return super()._authorize_path(path)


def test_mutant_guard_allowing_outside_manifest_is_killed(sealed_root: Path) -> None:
    guard = _AllowsOutsideManifest(
        calibration_sessions=SESSIONS,
        spy=CalibrationAccessSpy(),
        holdout_root=sealed_root,
    )
    load_calibration_manifest(guard, holdout_root=sealed_root)
    run_root = sealed_root / "runs" / HOLDOUT_RUN_ID
    report = measure_calibration_fail_closed_probes(
        template=guard,
        prospective_path=(
            run_root
            / "dataset"
            / "market=KOSPI"
            / "year=2026"
            / "ticker=005930.parquet"
        ),
        outside_manifest_path=(
            run_root
            / "dataset"
            / "market=KOSPI"
            / "year=2025"
            / "ticker=999999.parquet"
        ),
        authorized_path=(
            run_root
            / "dataset"
            / "market=KOSPI"
            / "year=2025"
            / "ticker=005930.parquet"
        ),
    )
    assert report["status"] == "FAIL"
    assert report["outcomes"]["outside_manifest_file"] == "FAIL"
    assert report["outcomes"]["prospective_partition_path"] == "FAIL"
    assert report["loader_calls"] > 0


# -- regression pins: the base guards are untouched -------------------------


def test_base_sealed_access_guard_still_blocks_everything_2025() -> None:
    guard = SealedAccessGuard(SealedAccessSpy())
    with pytest.raises(SealedAccessBlocked):
        guard.assert_exploration_date(date(2025, 1, 2))
    with pytest.raises(SealedAccessBlocked):
        guard.assert_exploration_date(date(2026, 1, 5))
    with pytest.raises(SealedAccessBlocked):
        guard.assert_exploration_path("/tmp/holdout/dataset/x.parquet")
    with pytest.raises(SealedAccessBlocked):
        guard.assert_metadata_key("D3_CALIBRATION_2025")
    assert guard.spy.evidence()["sealed_access_spy"] == 0


def test_holdout_guard_still_blocks_both_in_window_years() -> None:
    for probe in (date(2025, 6, 1), date(2026, 1, 5)):
        with pytest.raises(holdout_guard.HoldoutAccessError):
            holdout_guard.assert_date_not_holdout(probe)


def test_amendment_and_calendar_digests_are_pinned() -> None:
    assert AMENDMENT_A2_SHA256 == (
        "37b3045cd20678815c49066862ddd89dfebd5e852c57d84dcd1ce062b69dc020"
    )
    assert CALIBRATION_INDEX_SHA256 == (
        "17e95d0b30ade5e6fbd744cf719bc15224ef5177431cc51cfff8f2abb6930508"
    )


# -- shared metric extractor -------------------------------------------------


def test_gap03_censoring_and_shared_extractor() -> None:
    from research.kr_corpus.d3_engine.calibration_metrics import (
        CycleFill,
        SessionAxis,
        classify_gap03,
        compute_cycle_metrics,
        reconstruct_cycles,
    )

    axis = SessionAxis(
        (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 12, 30))
    )
    fills = [
        # carry-in cycle: opened 2024, trimmed inside the window -> excluded
        CycleFill("AAA", "BUY", Decimal(10), Decimal(1000), date(2024, 12, 2), 0),
        CycleFill("AAA", "SELL", Decimal(5), Decimal(1200), date(2025, 1, 3), 1),
        # clean in-window cycle: buy, add, full exit -> eligible
        CycleFill("BBB", "BUY", Decimal(10), Decimal(1000), date(2025, 1, 2), 2),
        CycleFill("BBB", "BUY", Decimal(20), Decimal(500), date(2025, 1, 3), 3),
        CycleFill("BBB", "SELL", Decimal(30), Decimal(1100), date(2025, 1, 6), 4),
        # in-window cycle still open at cutoff -> right-censored
        CycleFill("CCC", "BUY", Decimal(1), Decimal(9000), date(2025, 1, 6), 5),
    ]
    recon = reconstruct_cycles(fills)
    gap03 = classify_gap03(recon)
    assert len(gap03.eligible_closed) == 1
    assert len(gap03.carry_in_all) == 1
    assert len(gap03.right_censored) == 2

    metrics = compute_cycle_metrics(recon, gap03, axis)
    assert metrics["adds_per_cycle"]["median_decimal"] == "1"
    assert metrics["add_sizing_multiple"]["median_decimal"] == "1"
    assert metrics["holding_period_sessions"]["median_decimal"] == "2"
    assert metrics["add_interval_sessions"]["median_decimal"] == "1"
    assert metrics["trim_share"]["median_decimal"] == "1"
    # the carry-in cycle's first fill has no 2025 session_seq: excluded, not clipped
    assert metrics["open_lot_age_sessions"]["n"] == 1
    assert metrics["open_lot_age_sessions"]["excluded_observation_count"] == 1
    assert metrics["_census"]["carry_in_excluded_count"] == 1
    assert metrics["_census"]["right_censored_count"] == 2


def test_empty_sample_stays_not_computable() -> None:
    from research.kr_corpus.d3_engine.calibration_result import compare_view

    decision, detail = compare_view(
        "adds_per_cycle",
        {"n": 0, "aggregate_decimal": None},
        {"n": 4, "aggregate_decimal": "2"},
    )
    assert decision == "NOT_COMPUTABLE"
    assert detail["reason"] == "empty_or_invalid_sample_on_at_least_one_side"


def test_zero_rule_is_positive_scale_only() -> None:
    from research.kr_corpus.d3_engine.calibration_result import compare_view

    decision, detail = compare_view(
        "adds_per_cycle",
        {"n": 5, "aggregate_decimal": "0"},
        {"n": 5, "aggregate_decimal": "0.5"},
    )
    assert decision == "FAIL"
    assert detail["zero_rule_applied"] is True

    # GAP-10: bounded-share rows carry no extra zero condition
    decision, detail = compare_view(
        "trim_share",
        {"n": 5, "aggregate_decimal": "0"},
        {"n": 5, "aggregate_decimal": "0.1"},
    )
    assert decision == "PASS"
    assert detail["zero_rule_applied"] is False


# -- PR #1807 salvage: unique guard behaviours not otherwise pinned ---------
#
# ba3c1907c9 (research/kr_corpus/d3_engine/tests/test_calibration_corpus_guard.py)
# predates the Amendment A2 manifest-scoped rewrite and cannot be merged
# (2,911 lines of drift). These two tests port the two behaviours from that
# suite that this file does not otherwise cover, rewritten against the
# current CalibrationAccessGuard/load_calibration_corpus surface.


@pytest.fixture
def sealed_root_with_valid_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A synthetic sealed corpus with a real dataset/gap parquet pair and an
    anomalies.jsonl that interleaves one valid 2025 record with one
    prospective 2026 record whose JSON body is deliberately malformed.

    The 2026 record still matches the byte-level ``"session": "2026-`` regex
    so it reaches the precheck; if the precheck's year gate were ever removed
    and the line were handed to ``json.loads`` regardless, that call would
    raise ``json.JSONDecodeError`` and ``load_calibration_corpus`` would blow
    up instead of returning cleanly.
    """

    root = tmp_path / "kr-corpus-v1" / "holdout"
    run_root = root / "runs" / HOLDOUT_RUN_ID

    dataset_relative = "dataset/market=KOSPI/year=2025/ticker=005930.parquet"
    dataset_path = run_root / dataset_relative
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "session": "2025-01-02",
                    "market": "KOSPI",
                    "ticker": "005930",
                    "open": 100,
                    "high": 110,
                    "low": 90,
                    "close": 105,
                }
            ]
        ),
        dataset_path,
    )

    gap_relative = "gaps/market=KOSPI/year=2025/missing.parquet"
    gap_path = run_root / gap_relative
    gap_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "session": "2025-06-02",
                    "market": "KOSPI",
                    "ticker": "005930",
                    "reason": "ohlc_invariant_violation",
                }
            ]
        ),
        gap_path,
    )

    # A 2026 dataset entry so the manifest has its required prospective
    # example; it is never read (excluded before any guard check runs).
    prospective_relative = "dataset/market=KOSPI/year=2026/ticker=005930.parquet"
    (run_root / prospective_relative).parent.mkdir(parents=True, exist_ok=True)
    (run_root / prospective_relative).write_bytes(b"prospective")

    anomalies_relative = "source-anomalies.jsonl"
    valid_2025_line = json.dumps(
        {
            "session": "2025-06-02",
            "kind": "ohlc_invariant_violation",
            "ticker": "005930",
            "detail": {"open": 100, "high": 105, "low": 95, "close": 110},
        }
    ).encode()
    # Matches the byte-level session-year regex but is not valid JSON: if
    # this were ever decoded, json.loads raises and the corpus load aborts.
    malformed_2026_line = b'{"session": "2026-01-05", this is not json}'
    anomalies_bytes = valid_2025_line + b"\n" + malformed_2026_line + b"\n"
    (run_root / anomalies_relative).write_bytes(anomalies_bytes)

    files = {
        dataset_relative: dataset_path.read_bytes(),
        gap_relative: gap_path.read_bytes(),
        prospective_relative: (run_root / prospective_relative).read_bytes(),
        anomalies_relative: anomalies_bytes,
    }
    rows = [
        f"{hashlib.sha256(payload).hexdigest()}  {relative}"
        for relative, payload in files.items()
    ]
    checksums = ("\n".join(rows) + "\n").encode("utf-8")
    (run_root / "checksums.sha256").write_bytes(checksums)
    (run_root / "manifest.json").write_text(
        json.dumps(
            {
                "scope": "holdout",
                "corpus_id": "kr-corpus-v1",
                "files_list_location": "checksums.sha256",
                "checksums_sha256": hashlib.sha256(checksums).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(holdout_guard, "HOLDOUT_DIR", root)
    monkeypatch.setattr(calibration_guard, "HOLDOUT_DIR", root)
    return root


def test_anomaly_precheck_never_decodes_a_prospective_json_line(
    sealed_root_with_valid_corpus: Path,
) -> None:
    guard = CalibrationAccessGuard(
        calibration_sessions=SESSIONS,
        spy=CalibrationAccessSpy(),
        holdout_root=sealed_root_with_valid_corpus,
    )
    corpus = load_calibration_corpus(
        guard, holdout_root=sealed_root_with_valid_corpus
    )
    assert corpus.anomaly_lines_prechecked == 2
    assert corpus.anomaly_lines_decoded_2025 == 1
    assert corpus.anomaly_lines_skipped_prospective == 1
    assert len(corpus.clamp_rows) == 1


def test_spy_evidence_shape_is_stable() -> None:
    """Regression pin on the serialized evidence contract's key set.

    ``EngineResult.evidence`` embeds this dict verbatim (see
    ``CalibrationAccessGuard.fresh_clone``'s docstring on byte-identical
    determinism); an accidental key rename or drop here would silently
    reshape every downstream artifact.
    """

    spy = CalibrationAccessSpy()
    evidence = spy.evidence()
    assert evidence["calibration_authorized_date_checks"] == 0
    assert evidence["calibration_blocked_prospective_date_attempts"] == 0
    assert set(evidence) == {
        "sealed_access_spy",
        "sealed_access_blocked_attempts",
        "sealed_access_path_checks",
        "sealed_access_date_checks",
        "sealed_access_metadata_key_checks",
        "measured_file_reads",
        "measured_manifest_reads",
        "measured_parquet_reads",
        "measured_bar_rows_read",
        "measured_metadata_key_reads",
        "sealed_file_reads",
        "sealed_manifest_reads",
        "sealed_parquet_reads",
        "sealed_bar_rows_read",
        "sealed_metadata_key_reads",
        "calibration_warmup_date_checks",
        "calibration_authorized_date_checks",
        "calibration_blocked_prospective_date_attempts",
        "calibration_blocked_offcalendar_2025_date_attempts",
        "calibration_exploration_path_checks",
        "calibration_authorized_path_checks",
        "calibration_blocked_outside_manifest_attempts",
        "calibration_blocked_prospective_path_attempts",
        "calibration_blocked_sealed_token_attempts",
        "calibration_authorized_manifest_document_reads",
        "calibration_authorized_file_reads",
        "calibration_authorized_parquet_reads",
        "calibration_authorized_bar_rows",
        "calibration_manifest_allowlist_size",
        "calibration_manifest_excluded_out_of_scope",
        "calibration_sessions_authorized",
    }


def test_dual_view_disagreement_is_data_bias() -> None:
    from research.kr_corpus.d3_engine.calibration_result import view_outcome

    assert view_outcome("PASS", "PASS") == "PASS"
    assert view_outcome("FAIL", "FAIL") == "CALIBRATION_MISMATCH"
    assert view_outcome("PASS", "FAIL") == "CALIBRATION_DATA_BIAS"
    assert view_outcome("FAIL", "PASS") == "CALIBRATION_DATA_BIAS"
    assert view_outcome("PASS", "NOT_COMPUTABLE") == "NOT_COMPUTABLE"
