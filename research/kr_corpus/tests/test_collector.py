from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from research.kr_corpus.collector import CorpusCollector
from research.kr_corpus.config import FROZEN_CONFIG
from research.kr_corpus.pacing import RequestPacer
from research.kr_corpus.source import LifecycleMasterBound
from research.kr_corpus.state import StateStore
from research.kr_corpus.storage import build_snapshot_paths


class FakeSource:
    def __init__(
        self,
        memberships: dict[tuple[str, str], tuple[str, ...]],
        bars: dict[str, pd.DataFrame],
    ):
        self.memberships = memberships
        self.bars = bars
        self.membership_calls = 0
        self.ohlcv_calls = 0
        self.redactor = None

    @contextlib.contextmanager
    def runtime(self):
        yield self

    def lifecycle_master_bound(self) -> LifecycleMasterBound:
        tickers = frozenset(
            ticker for values in self.memberships.values() for ticker in values
        )
        return LifecycleMasterBound(
            tickers=tickers, listed_count=len(tickers), delisted_count=0
        )

    def session_calendar(self) -> tuple[str, ...]:
        return tuple(sorted({session for session, _market in self.memberships}))

    def membership(self, session: str, market: str) -> tuple[str, ...]:
        self.membership_calls += 1
        return self.memberships[(session, market)]

    def adjusted_ohlcv(self, ticker: str) -> pd.DataFrame:
        self.ohlcv_calls += 1
        return self.bars[ticker]


def _config(tmp_path: Path, *, max_requests: int = 100) -> object:
    crosscheck = tmp_path / "crosscheck.csv"
    crosscheck.write_text(
        "symbol,session_date,open,high,low,close,volume,value\n"
        "051170,2024-01-02,10,12,9,11,100,\n",
        encoding="utf-8",
    )
    return replace(
        FROZEN_CONFIG,
        run_id="test-run",
        artifact_root=str(tmp_path / "artifacts") + "/",
        holdout_dir=str(tmp_path / "holdout") + "/",
        holdout_access_log=str(tmp_path / "holdout-access.log"),
        progress_path=str(tmp_path / "progress.md"),
        crosscheck_file=str(crosscheck),
        crosscheck_file_sha256=hashlib.sha256(crosscheck.read_bytes()).hexdigest(),
        start_date="2024-01-01",
        cutoff_session="2024-01-02",
        historical_oos="2025-01-01..2025-12-31",
        max_requests=max_requests,
        max_wall_clock_hours=12,
        min_request_interval_sec=0.0,
    )


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "시가": [10],
            "고가": [12],
            "저가": [9],
            "종가": [11],
            "거래량": [100],
        },
        index=pd.to_datetime(["2024-01-02"]),
    )


def _run(tmp_path: Path, source: FakeSource, *, max_requests: int = 100):
    config = _config(tmp_path, max_requests=max_requests)
    paths = build_snapshot_paths(config)
    main_state = StateStore(paths.main_state)
    holdout_state = StateStore(paths.holdout_state)
    try:
        pacer = RequestPacer(min_interval_sec=0.0, max_requests=max_requests)
        collector = CorpusCollector(config, source, pacer, main_state, holdout_state)
        return collector.run(), config
    finally:
        main_state.close()
        holdout_state.close()


def test_collects_positive_membership_then_bars_into_market_year_partitions(
    tmp_path: Path,
):
    memberships = {
        ("2024-01-02", "KOSPI"): ("051170",),
        ("2024-01-02", "KOSDAQ"): ("123456",),
    }
    source = FakeSource(memberships, {"051170": _bars(), "123456": _bars()})

    result, config = _run(tmp_path, source)

    assert result.terminal_verdict == "READY_FOR_RESEARCH"
    assert source.membership_calls == 2
    assert source.ohlcv_calls == 2
    final_root = Path(config.artifact_root) / "runs" / config.run_id
    assert (
        final_root / "membership/market=KOSPI/year=2024/session=2024-01-02.parquet"
    ).is_file()
    assert (
        final_root / "dataset/market=KOSDAQ/year=2024/ticker=123456.parquet"
    ).is_file()
    assert (final_root / "derived/common_stock_proven/manifest.json").is_file()
    assert (final_root / "checksums.sha256").is_file()


def test_preflight_budget_blocks_before_any_membership_or_ohlcv_fetch(tmp_path: Path):
    memberships = {
        ("2024-01-02", "KOSPI"): ("051170",),
        ("2024-01-02", "KOSDAQ"): ("123456",),
    }
    source = FakeSource(memberships, {"051170": _bars(), "123456": _bars()})

    result, _ = _run(tmp_path, source, max_requests=3)

    assert result.terminal_verdict == "BLOCKED_PRECONDITION"
    assert source.membership_calls == 0
    assert source.ohlcv_calls == 0
    assert result.request_budget_projected is not None
    assert result.request_budget_projected > 3


def test_empty_source_history_becomes_explicit_gap_without_forward_fill(tmp_path: Path):
    memberships = {
        ("2024-01-02", "KOSPI"): ("051170",),
        ("2024-01-02", "KOSDAQ"): ("123456",),
    }
    source = FakeSource(
        memberships,
        {
            "051170": _bars(),
            "123456": pd.DataFrame(columns=["시가", "고가", "저가", "종가", "거래량"]),
        },
    )

    result, config = _run(tmp_path, source)

    assert result.terminal_verdict == "BUILT_WITH_GAPS"
    final_root = Path(config.artifact_root) / "runs" / config.run_id
    assert (final_root / "gaps/market=KOSDAQ/year=2024/missing.parquet").is_file()
    manifest = json.loads((final_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["forward_fill_used"] is False
    assert manifest["source_fallback_used"] is False
    assert "holdout_written_not_read" not in manifest
    assert manifest["field_scopes"]["metrics"] == "main_plus_holdout"
    assert manifest["adjusted_ohlcv_value"]["availability"] == "NULL"
    assert manifest["holdout_custody"]["audit_measurement"].startswith("not emitted:")
