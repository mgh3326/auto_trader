"""Measured acceptance probes for the A2 carve-out.

Two independent obligations live here.

1. :func:`measure_calibration_fail_closed_probes` — the three new fail-closed
   routes A2 requires (2026 date, prospective path, file outside the
   ``D3_CALIBRATION_2025`` manifest) plus regression pins proving the base
   deny-list guards are unchanged. Every outcome is a measured counter, never a
   statement.
2. :func:`measure_primary_path_isolation` — the behavioural proof that the
   *primary* runner never gains the carve-out. It instantiates the real
   ``PrimaryPortfolioEngine`` and probes the guard that runner installs. The A2
   mutant "inject the calibration guard into the primary runner" is killed by
   this predicate: a calibration-scoped guard admits 2025 and the holdout root,
   so the isolation status flips to FAIL.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from research.kr_corpus.backtest import holdout_guard as _holdout_guard
from research.kr_corpus.backtest.holdout_guard import HoldoutAccessError
from research.kr_corpus.d3_engine.calibration_guard import CalibrationAccessGuard
from research.kr_corpus.d3_engine.constants import ArtifactPaths
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)
from research.kr_corpus.d3_engine.models import DataView
from research.kr_corpus.d3_engine.primary import PrimaryPortfolioEngine
from research.kr_corpus.d3_engine.primary_corpus import LoadedCorpusView
from research.kr_corpus.d3_engine.tick import TickTable, load_tick_table

PROBE_PROSPECTIVE_DATE = date(2026, 1, 5)
PROBE_PRIMARY_SEALED_DATE = date(2025, 1, 2)


def clone_probe_guard(template: CalibrationAccessGuard) -> CalibrationAccessGuard:
    """A fresh guard with the same A2 scope and its own spy.

    ``type(template)`` — not the base class — so the probes exercise the exact
    implementation the run uses. A mutated guard is probed as itself.
    """

    return template.fresh_clone()


def measure_calibration_fail_closed_probes(
    *,
    template: CalibrationAccessGuard,
    prospective_path: Path,
    outside_manifest_path: Path,
    authorized_path: Path,
) -> dict[str, Any]:
    """Exercise the three A2 blocked routes; no loader may ever execute."""

    guard = clone_probe_guard(template)
    loader_calls = 0

    def loader() -> str:
        nonlocal loader_calls
        loader_calls += 1
        return "forbidden"

    outcomes: dict[str, str] = {}

    # (1) a 2026 date, reached through the same read path a bar takes.
    try:
        guard.read_bar(
            path=authorized_path, session=PROBE_PROSPECTIVE_DATE, loader=loader
        )
    except SealedAccessBlocked:
        outcomes["prospective_2026_date"] = "PASS"
    else:
        outcomes["prospective_2026_date"] = "FAIL"

    # (1b) the same date observed mid-stream, after its file gate already passed.
    try:
        guard.record_bar_rows([date(2025, 1, 2), PROBE_PROSPECTIVE_DATE])
    except SealedAccessBlocked:
        outcomes["prospective_2026_date_mid_stream"] = "PASS"
    else:
        outcomes["prospective_2026_date_mid_stream"] = "FAIL"

    # (1c) a 2025 date that the sealed calendar does not carry (a KRX holiday).
    try:
        guard.assert_exploration_date(date(2025, 1, 1))
    except SealedAccessBlocked:
        outcomes["offcalendar_2025_date"] = "PASS"
    else:
        outcomes["offcalendar_2025_date"] = "FAIL"

    # (2) a real, existing prospective partition file.
    try:
        guard.read_parquet(path=prospective_path, loader=loader)
    except SealedAccessBlocked:
        outcomes["prospective_partition_path"] = "PASS"
    else:
        outcomes["prospective_partition_path"] = "FAIL"

    # (3) an in-scope-year holdout path the manifest does not enumerate. This
    #     isolates the manifest membership check from the year check.
    try:
        guard.read_parquet(path=outside_manifest_path, loader=loader)
    except SealedAccessBlocked:
        outcomes["outside_manifest_file"] = "PASS"
    else:
        outcomes["outside_manifest_file"] = "FAIL"

    # (3b) a sealed segment that is not under the holdout root at all.
    try:
        guard.read_file(path=Path("/tmp/prospective/bars.parquet"), loader=loader)
    except SealedAccessBlocked:
        outcomes["sealed_token_outside_holdout_root"] = "PASS"
    else:
        outcomes["sealed_token_outside_holdout_root"] = "FAIL"

    # (4) regression pin: the base deny-list guard is unchanged for every
    #     other caller — 2025 dates and holdout paths still hard-block.
    base = SealedAccessGuard(SealedAccessSpy())
    base_blocks = 0
    for probe in (
        lambda: base.assert_exploration_date(PROBE_PRIMARY_SEALED_DATE),
        lambda: base.assert_exploration_path(prospective_path),
        lambda: base.assert_exploration_path(authorized_path),
    ):
        try:
            probe()
        except SealedAccessBlocked:
            base_blocks += 1
    outcomes["sealed_access_guard_regression_unchanged"] = (
        "PASS" if base_blocks == 3 else "FAIL"
    )

    # (5) regression pin: the shared holdout guard is unchanged.
    holdout_blocks = 0
    for probe_date in (date(2025, 6, 1), PROBE_PROSPECTIVE_DATE):
        try:
            _holdout_guard.assert_date_not_holdout(probe_date)
        except HoldoutAccessError:
            holdout_blocks += 1
    outcomes["holdout_guard_regression_unchanged"] = (
        "PASS" if holdout_blocks == 2 else "FAIL"
    )

    evidence = guard.spy.evidence()
    passed = (
        all(value == "PASS" for value in outcomes.values())
        and loader_calls == 0
        and evidence["sealed_access_spy"] == 0
        and evidence["measured_file_reads"] == 0
        and evidence["sealed_access_blocked_attempts"] == 6
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "outcomes": outcomes,
        "loader_calls": loader_calls,
        "authorized_sealed_reads_during_probes": evidence["sealed_access_spy"],
        "blocked_attempts": evidence["sealed_access_blocked_attempts"],
        "probe_paths": {
            "prospective": str(prospective_path),
            "outside_manifest": str(outside_manifest_path),
            "authorized_reference": str(authorized_path),
        },
        "spy_evidence": evidence,
    }


def _empty_corpus_view() -> LoadedCorpusView:
    return LoadedCorpusView(
        data_view=DataView.ORIGINAL_VALID_BAR,
        bars=(),
        signals={},
        clamp_rows={},
        market_periods={},
        manifest_sha256="0" * 64,
        checksums_sha256="0" * 64,
        parquet_files=0,
        row_count=0,
        signal_tape_sha256="0" * 64,
        access_evidence={},
    )


def measure_primary_path_isolation(
    *,
    tick_table: TickTable | None = None,
    guard_override: object | None = None,
) -> dict[str, Any]:
    """Probe the guard the *primary* runner installs on its own engine.

    ``guard_override`` is the mutant hook: passing a calibration-scoped guard
    simulates the forbidden wiring, and every predicate below must then fail.
    """

    ticks = tick_table or load_tick_table(ArtifactPaths.defaults().tick_yaml)
    engine = PrimaryPortfolioEngine(
        ticks,
        view=_empty_corpus_view(),
        all_clamp_rows={},
        market_sessions=(),
    )
    if guard_override is not None:
        engine._guard = guard_override  # noqa: SLF001 - deliberate mutant injection
    guard = engine._guard  # noqa: SLF001 - the property under test

    exact_type = type(guard) is SealedAccessGuard

    def _blocks(probe: Callable[[], object]) -> bool:
        try:
            probe()
        except SealedAccessBlocked:
            return True
        except Exception:  # noqa: BLE001 - any other error is not a clean block
            return False
        return False

    blocks_2025 = _blocks(
        lambda: guard.assert_exploration_date(PROBE_PRIMARY_SEALED_DATE)
    )
    blocks_holdout = _blocks(
        lambda: guard.assert_exploration_path(
            _holdout_guard.HOLDOUT_DIR
            / "runs"
            / "kr-corpus-v1-20260803-1001"
            / "dataset"
            / "market=KOSPI"
            / "year=2025"
            / "ticker=005930.parquet"
        )
    )
    blocks_2026 = _blocks(lambda: guard.assert_exploration_date(PROBE_PROSPECTIVE_DATE))
    passed = exact_type and blocks_2025 and blocks_holdout and blocks_2026
    return {
        "status": "PASS" if passed else "FAIL",
        "primary_engine_class": type(engine).__name__,
        "installed_guard_class": type(guard).__name__,
        "guard_is_sealed_access_guard_exactly": exact_type,
        "blocks_2025_date": blocks_2025,
        "blocks_2026_date": blocks_2026,
        "blocks_holdout_2025_partition_path": blocks_holdout,
        "note": (
            "primary.py is unedited; this reads the guard its own __init__ hard-codes"
        ),
    }
