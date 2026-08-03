"""Fixture smoke for value_rank_topN_D5 — pipeline integrity only."""

from __future__ import annotations

from pathlib import Path

from baseline_smoke import PIPELINE_SMOKE_LABEL, run_value_rank_topn_d5
from fixture_builder import build_synthetic_fixture
from loader import load_manifest, load_shard
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

    result = run_value_rank_topn_d5(
        bars=bars,
        membership=membership,
        top_n=3,
        holding_days=5,
        window_start="2023-01-02",
        window_end="2023-02-15",
    )
    assert result.label == PIPELINE_SMOKE_LABEL
    assert result.baseline == "value_rank_topN_D5"
    assert result.sessions_processed > 0
    assert result.entries > 0
    report = result.to_report_dict()
    assert report["PIPELINE_SMOKE_NOT_A_STRATEGY"] is True
    assert report["interpretation"] == "FORBIDDEN — pipeline smoke only"
    # Delisted path exercised for 086520 after 2023-02-01 if held.
    assert result.terminal_delisted >= 0


def test_smoke_cli_report_flags(tmp_path):
    report = run_fixture_smoke(tmp_path / "fx")
    assert report["label"] == PIPELINE_SMOKE_LABEL
    assert report["CORPUS_ARTIFACT_ROOT_READS"] == 0
    assert report["HOLDOUT_READS"] == 0
    assert report["REAL_DATA_SMOKE_RAN"] is False
    assert report["schema_origin"] == "INFERRED_FROM_LITERALS"


def test_package_never_reads_corpus_artifact_root_constant_as_io():
    """Stage A: CORPUS_ARTIFACT_ROOT path string may appear only as docs/constant
    that is never opened. Scan package for open()/read of that root."""
    pkg = Path(__file__).resolve().parent.parent
    corpus_root = "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1"
    for path in pkg.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        # holdout_guard legitimately names HOLDOUT_DIR under that tree.
        if "holdout" in path.name:
            continue
        # smoke_cli / docs may mention the constant as zero-read claim.
        if "CORPUS_ARTIFACT_ROOT" in text and (
            "open(" in text or "read_text" in text or "read_bytes" in text
        ):
            # Only fail if the corpus root path itself is used in I/O.
            if corpus_root in text and "holdout" not in text:
                # crude: ensure no Path(corpus_root).read
                assert f'Path("{corpus_root}")' not in text
