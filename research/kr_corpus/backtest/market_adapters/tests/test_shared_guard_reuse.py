"""Both adapters bind the parent harness guards; no market-local copies."""

from __future__ import annotations

from datetime import date
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


def _adapter(market: str, policy: HoldoutPolicy):
    if market == "us":
        return USMarketAdapter(holdout_policy=policy)
    if market == "crypto":
        return CryptoVenueAdapter("upbit_krw", holdout_policy=policy)
    raise AssertionError(f"unexpected market {market!r}")


def _market_field(market: str) -> str:
    return "US" if market == "us" else "upbit_krw"


def _row(market: str) -> dict:
    common = {
        "symbol": "ABC" if market == "us" else "KRW-ABC",
        "timestamp_utc": "2024-07-03T20:00:00+00:00",
        "session_date": "2024-07-03",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000.0,
        "trading_value": 100_000.0,
    }
    if market == "us":
        return {**common, "market": "US"}
    return {**common, "venue": "upbit_krw"}


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
        market=_market_field(market),
        year=2024,
    )
    with pytest.raises(HoldoutPathBlocked):
        adapter.load_shard(tmp_path, entry)


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_shared_schema_mismatch_is_loud(market: str, tmp_path: Path):
    policy = HoldoutPolicy(tmp_path / "holdout", HOLDOUT_START, HOLDOUT_END)
    adapter = _adapter(market, policy)
    with pytest.raises(SchemaMismatchError) as exc_info:
        adapter.corpus.validate_table_schema(pa.table({"symbol": ["only"]}), "ohlcv")
    assert "mismatch" in str(exc_info.value).lower()


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_shared_manifest_sha_gate_rejects_tampered_fixture(market: str, tmp_path: Path):
    """Synthetic fixture only: digest mismatch fails before parquet parsing."""
    policy = HoldoutPolicy(tmp_path / "holdout", HOLDOUT_START, HOLDOUT_END)
    adapter = _adapter(market, policy)
    rel = f"ohlcv/{_market_field(market)}/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    table = pa.Table.from_pylist(
        [_row(market)],
        schema=adapter.corpus.arrow_schema_for("ohlcv"),
    )
    pq.write_table(table, path)

    entry = ManifestEntry(
        relative_path=rel,
        file_sha256="f" * 64,
        row_count=1,
        dataset="ohlcv",
        market=_market_field(market),
        year=2024,
    )
    with pytest.raises(ManifestShaMismatchError):
        adapter.load_shard(tmp_path, entry)
