from __future__ import annotations

import os

import pytest

from research.kr_backfill.collect import _build_surface_runtime, _prepare_surface_env
from research.kr_backfill.dual_surface import (
    OverlapMismatch,
    SurfaceAuthError,
    SurfaceBackoffExhausted,
    SurfaceContractError,
    SurfaceManifest,
    SurfacePacer,
    compare_overlap_exact,
    validate_surface_rows,
)
from research.kr_backfill.split_assign import (
    assign_surfaces,
    write_dual_surface_assignment,
)


def _row(ticker: str, surface: str, kind: str = "bulk") -> dict[str, str]:
    return {
        "ticker": ticker,
        "surface": surface,
        "assignment_kind": kind,
    }


def test_bulk_symbol_cannot_be_assigned_to_both_surfaces():
    with pytest.raises(SurfaceContractError, match="cross-surface split"):
        validate_surface_rows([_row("005930", "mock"), _row("005930", "live")])


def test_only_explicit_overlap_rows_may_name_both_surfaces():
    rows = [_row("005930", "mock", "overlap"), _row("005930", "live", "overlap")]
    assert validate_surface_rows(rows) == {"005930": ["mock", "live"]}


def test_deterministic_bulk_assignment_has_one_surface_per_symbol():
    assignment = assign_surfaces(["000001", "000002", "000003", "000004"])
    assert set(assignment) == {"000001", "000002", "000003", "000004"}
    assert set(assignment.values()) <= {"mock", "live"}


def test_surface_pacing_is_independent_and_never_recovers():
    mock = SurfacePacer("mock")
    live = SurfacePacer("live")
    assert mock.interval == 2.0
    assert live.interval == 0.5

    live.note_status(429)
    assert live.interval == 1.0
    assert mock.interval == 2.0
    live.note_status(200)
    assert live.interval == 1.0
    live.note_status(429)
    assert live.interval == 2.0
    assert mock.interval == 2.0

    with pytest.raises(SurfaceBackoffExhausted):
        live.note_status(429)
    with pytest.raises(SurfaceAuthError):
        mock.note_status(401)


def test_overlap_is_exact_and_mismatch_is_stop_condition():
    row = {
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 10.0,
        "value": 15.0,
    }
    assert (
        compare_overlap_exact("005930", {1: row}, {1: dict(row)})["cells_compared"] == 6
    )
    changed = dict(row, close=1.5000000001)
    with pytest.raises(OverlapMismatch, match="exact overlap mismatch"):
        compare_overlap_exact("005930", {1: row}, {1: changed})


def test_manifest_seals_surface_field_and_overlap_design():
    payload = SurfaceManifest(
        batch_id="batch",
        assignment_path="split.csv",
        assignment_sha256="0" * 64,
    ).to_dict()
    assert payload["manifest_surface_field"] == "surface"
    assert payload["cross_surface_split_prevented"] is True
    assert payload["overlap_sample"] == {
        "size": 2,
        "every_completed_batches": 50,
        "reason": payload["overlap_sample"]["reason"],
    }
    assert payload["backoff"]["per_surface"] is True
    assert payload["backoff"]["auto_recovery"] is False


def test_split_artifact_records_bulk_owner_and_explicit_overlap(tmp_path):
    top500 = tmp_path / "top500.csv"
    top500.write_text(
        "rank,ticker,market\n1,000001,KOSPI\n2,000002,KOSPI\n3,000003,KOSDAQ\n",
        encoding="utf-8",
    )
    split_path, manifest_path = write_dual_surface_assignment(
        top500=top500,
        out_dir=tmp_path / "split",
        overlap_symbols=("000002",),
    )
    rows = split_path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "rank,ticker,market,assignment_kind,surface"
    assert sum("000002" in row for row in rows) == 2
    assert "cross_surface_split_prevented" in manifest_path.read_text(encoding="utf-8")


def test_mock_only_runtime_never_constructs_live_client():
    dispatched: list[tuple[str, str]] = []

    def mock_factory():
        dispatched.append(("mock", "constructed"))
        return object()

    def live_factory():
        raise AssertionError("live factory must not run for --surface mock")

    raw, clients, pacers = _build_surface_runtime(
        ("mock",), mock_factory=mock_factory, live_factory=live_factory
    )
    assert list(raw) == ["mock"]
    assert list(clients) == ["mock"]
    assert list(pacers) == ["mock"]
    assert dispatched == [("mock", "constructed")]


def test_dual_runtime_uses_one_accounted_pacer_per_surface():
    def factory(surface):
        return lambda: object()

    raw, clients, pacers = _build_surface_runtime(
        ("mock", "live"),
        mock_factory=factory("mock"),
        live_factory=factory("live"),
    )
    assert set(raw) == {"mock", "live"}
    assert set(clients) == set(pacers) == {"mock", "live"}


def test_mock_only_env_does_not_require_or_read_live_file(tmp_path):
    mock_file = tmp_path / ".env.kiwoom-mock"
    mock_file.write_text("KIWOOM_MOCK_ENABLED=true\n", encoding="utf-8")
    missing_live_file = tmp_path / ".env.kiwoom-live"

    _prepare_surface_env(("mock",), mock_file, missing_live_file)

    assert os.environ["ENV_FILE"] == str(mock_file)
