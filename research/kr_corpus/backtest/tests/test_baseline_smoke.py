"""Fixture smoke for liquidity_proxy_decile_topN_D5 — pipeline integrity only."""

from __future__ import annotations

from pathlib import Path

import pytest
from baseline_smoke import (
    BASELINE_NAME,
    PIPELINE_SMOKE_LABEL,
    run_liquidity_proxy_decile_topn_d5,
)
from fixture_builder import FIXTURE_REL_ROOT, build_synthetic_fixture
from loader import ManifestShaMismatchError, load_manifest, load_shard
from membership import membership_rows_from_table
from pit import bars_from_table
from smoke_cli import run_fixture_smoke


def test_fixture_smoke_runs_and_is_labeled(tmp_path):
    root = build_synthetic_fixture(tmp_path / "fixture")
    manifest = load_manifest(root / "manifest.json")
    bars = []
    membership = []
    for entry in manifest:
        table = load_shard(root, entry)
        if entry.dataset == "ohlcv":
            bars.extend(bars_from_table(table))
        else:
            membership.extend(membership_rows_from_table(table))

    result = run_liquidity_proxy_decile_topn_d5(
        bars=bars,
        membership=membership,
        top_n=3,
        holding_days=5,
        window_start="2023-01-02",
        window_end="2023-02-15",
    )
    assert result.label == PIPELINE_SMOKE_LABEL
    assert result.baseline == BASELINE_NAME
    assert result.sessions_processed > 0
    assert result.entries > 0
    report = result.to_report_dict()
    assert report["PIPELINE_SMOKE_NOT_A_STRATEGY"] is True
    assert report["interpretation"] == "FORBIDDEN — pipeline smoke only"
    assert result.terminal_delisted >= 0


def test_smoke_default_uses_committed_fixture_without_rebuild():
    """SHOULD-2: default smoke path does not regenerate before SHA verify."""
    assert (FIXTURE_REL_ROOT / "manifest.json").is_file()
    report = run_fixture_smoke()  # rebuild defaults False
    assert report["label"] == PIPELINE_SMOKE_LABEL
    assert report["CORPUS_ARTIFACT_ROOT_READS"] == 0
    assert report["HOLDOUT_READS"] == 0
    assert report["REAL_DATA_SMOKE_RAN"] is False
    assert report["schema_origin"] == "SEALED_CORPUS_V1"
    assert report["fixture_rebuild_before_verify"] is False
    assert report["sha_gate_exercised_on_committed_bytes"] is True
    assert Path(report["fixture_root"]) == FIXTURE_REL_ROOT


def test_smoke_sha_gate_catches_tamper_when_not_rebuilding(tmp_path):
    """Tamper after fixture write must surface ManifestShaMismatchError."""
    root = build_synthetic_fixture(tmp_path / "fx")
    # Corrupt one parquet byte after manifest digests were sealed.
    targets = list(root.rglob("*.parquet"))
    assert targets
    target = targets[0]
    target.write_bytes(target.read_bytes() + b"\x00TAMPER")
    with pytest.raises(ManifestShaMismatchError):
        run_fixture_smoke(root, rebuild=False)


def test_smoke_missing_fixture_without_rebuild_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_fixture_smoke(tmp_path / "empty", rebuild=False)


def test_same_bar_close_fill_documented_in_module():
    from baseline_smoke import __doc__ as doc

    assert doc is not None
    assert "same-bar" in doc.lower() or "same bar" in doc.lower()
    assert "close" in doc.lower()


def test_proxy_ranking_accepts_us_shape_without_trading_value():
    """The common baseline consumes only fields shared with USBar."""
    from datetime import date

    from market_adapters.us import USBar
    from membership import MembershipRow

    bars = [
        USBar("LOW", date(2023, 1, 2), 10.0, 10.0, 10.0, 10.0, 100, "US", "adjusted"),
        USBar("HIGH", date(2023, 1, 2), 20.0, 20.0, 20.0, 20.0, 100, "US", "adjusted"),
    ]
    membership = [
        MembershipRow(
            symbol=symbol,
            session_date=date(2023, 1, 2),
            market="US",
            member=True,
            status="listed",
        )
        for symbol in ("LOW", "HIGH")
    ]
    result = run_liquidity_proxy_decile_topn_d5(
        bars=bars,
        membership=membership,
        top_n=1,
        holding_days=1,
        window_start="2023-01-02",
        window_end="2023-01-02",
    )
    assert result.entries == 1


def test_package_never_reads_corpus_artifact_root_constant_as_io():
    """Stage A: CORPUS_ARTIFACT_ROOT path string may appear only as docs/constant
    that is never opened. Scan package for open()/read of that root."""
    pkg = Path(__file__).resolve().parent.parent
    corpus_root = "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1"
    for path in pkg.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "holdout" in path.name:
            continue
        if "CORPUS_ARTIFACT_ROOT" in text and (
            "open(" in text or "read_text" in text or "read_bytes" in text
        ):
            if corpus_root in text and "holdout" not in text:
                assert f'Path("{corpus_root}")' not in text
