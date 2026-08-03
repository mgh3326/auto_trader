"""Both adapters bind the parent harness guards; no market-local copies."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from holdout_guard import (
    HOLDOUT_END,
    HOLDOUT_START,
    HoldoutDateBlocked,
    HoldoutPathBlocked,
    HoldoutPolicy,
)
from loader import ManifestEntry, ManifestShaMismatchError
from market_adapters.crypto import CryptoVenueAdapter
from market_adapters.us import USMarketAdapter
from schema_contract import SchemaMismatchError

from research.crypto_corpus.policy import label_table_for_venue


def _adapter(market: str, policy: HoldoutPolicy):
    if market == "us":
        return USMarketAdapter(holdout_policy=policy)
    if market == "crypto":
        return CryptoVenueAdapter("upbit_krw", holdout_policy=policy)
    raise AssertionError(f"unexpected market {market!r}")


def _row(market: str) -> dict:
    if market == "us":
        return {
            "symbol": "ABC",
            "session_date": datetime(2024, 7, 3),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        }
    open_t = datetime(2024, 7, 3, 0, 0, tzinfo=UTC)
    return {
        "venue": "upbit_krw",
        "symbol": "KRW-ABC",
        "frequency": "1d",
        "bucket_timezone": "UTC",
        "open_time_utc": open_t,
        "close_time_utc": open_t + timedelta(days=1),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "base_volume": 1000.0,
        "quote_volume": 100_000.0,
        "trade_count": None,
        "source_candle_date_time_utc": "2024-07-03T00:00:00",
        "source_candle_date_time_kst": None,
        "source_open_time_ms": None,
        "source_close_time_ms": None,
        "source_timestamp_ms": None,
    }


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_shared_holdout_policy_blocks_date_boundaries(market: str, tmp_path: Path):
    policy = HoldoutPolicy(tmp_path / "holdout", HOLDOUT_START, HOLDOUT_END)
    adapter = _adapter(market, policy)

    assert adapter.corpus.assert_date_allowed("2024-12-31") == date(2024, 12, 31)
    with pytest.raises(HoldoutDateBlocked):
        adapter.corpus.assert_date_allowed("2025-01-01")


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_shared_holdout_policy_blocks_case_symlink_and_dotdot(
    market: str, tmp_path: Path
):
    """The exact common path gate rejects every market's bypass attempts."""
    holdout_root = tmp_path / "holdout"
    holdout_root.mkdir()
    policy = HoldoutPolicy(holdout_root, HOLDOUT_START, HOLDOUT_END)
    adapter = _adapter(market, policy)
    symlink = tmp_path / "symlink_to_holdout"
    symlink.symlink_to(holdout_root, target_is_directory=True)

    candidates = (
        holdout_root,
        holdout_root / "child.parquet",
        holdout_root / ".." / "holdout" / "via-dotdot.parquet",
        holdout_root.parent / "HOLDOUT" / "case-variant.parquet",
        symlink / "via-symlink.parquet",
    )
    for candidate in candidates:
        with pytest.raises(HoldoutPathBlocked):
            adapter.corpus.assert_path_allowed(candidate)


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_shared_loader_entrypoints_block_holdout_paths(market: str, tmp_path: Path):
    """Both manifest and shard entrypoints use the same configured policy."""
    holdout_root = tmp_path / "holdout"
    holdout_root.mkdir()
    policy = HoldoutPolicy(holdout_root, HOLDOUT_START, HOLDOUT_END)
    adapter = _adapter(market, policy)

    with pytest.raises(HoldoutPathBlocked):
        adapter.load_manifest(holdout_root / "manifest.json")

    entry = ManifestEntry(
        relative_path=str(holdout_root / "ohlcv" / "bars.parquet"),
        file_sha256="0" * 64,
        row_count=0,
        dataset="ohlcv",
        market="US" if market == "us" else "upbit_krw",
        year=2024,
    )
    with pytest.raises(HoldoutPathBlocked):
        adapter.load_shard(tmp_path, entry)


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_shared_schema_and_sha_gates(market: str, tmp_path: Path):
    policy = HoldoutPolicy(tmp_path / "holdout", HOLDOUT_START, HOLDOUT_END)
    adapter = _adapter(market, policy)
    rel = f"ohlcv/{market}/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    table = pa.Table.from_pylist(
        [_row(market)],
        schema=adapter.corpus.arrow_schema_for("ohlcv"),
    )
    if market == "crypto":
        table = label_table_for_venue(table, "upbit_krw")
    pq.write_table(table, path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256="0" * 64,
        row_count=1,
        dataset="ohlcv",
        market="US" if market == "us" else "upbit_krw",
        year=2024,
    )
    with pytest.raises(ManifestShaMismatchError):
        adapter.load_shard(tmp_path, entry)

    bad = pa.table({"symbol": pa.array(["X"], type=pa.string())})
    with pytest.raises(SchemaMismatchError):
        adapter.corpus.validate_table_schema(bad, "ohlcv")
