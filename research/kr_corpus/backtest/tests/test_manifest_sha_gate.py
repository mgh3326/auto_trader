"""Manifest SHA-256 gate: mismatch is hard refusal before/at load."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loader import (
    ManifestEntry,
    ManifestShaMismatchError,
    PathEscapesArtifactRootError,
    RowCountMismatchError,
    ShardFileMissingError,
    load_manifest,
    load_shard,
    sha256_bytes,
)
from schema_contract import arrow_schema_for


def _write_ohlcv_shard(tmp_path, rel: str, rows: list[dict]) -> tuple[str, int]:
    schema = arrow_schema_for("ohlcv")
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path)
    data = path.read_bytes()
    return sha256_bytes(data), len(rows)


def _sample_row(session: str = "2023-01-03") -> dict:
    return {
        "symbol": "005930",
        "session_date": session,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000.0,
        "trading_value": 1e9,
        "market": "KOSPI",
    }


def test_load_round_trip_with_matching_sha(tmp_path):
    rel = "ohlcv/KOSPI/2023/bars.parquet"
    digest, n = _write_ohlcv_shard(tmp_path, rel, [_sample_row()])
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=digest,
        row_count=n,
        dataset="ohlcv",
        market="KOSPI",
        year=2023,
    )
    table = load_shard(tmp_path, entry)
    assert table.num_rows == 1


def test_sha_mismatch_is_refusal_not_warning(tmp_path):
    rel = "ohlcv/KOSPI/2023/bars.parquet"
    digest, n = _write_ohlcv_shard(tmp_path, rel, [_sample_row()])
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256="0" * 64,  # wrong on purpose
        row_count=n,
        dataset="ohlcv",
        market="KOSPI",
        year=2023,
    )
    with pytest.raises(ManifestShaMismatchError) as exc_info:
        load_shard(tmp_path, entry)
    assert (
        "refused" in str(exc_info.value).lower()
        or "mismatch" in str(exc_info.value).lower()
    )
    # Confirm we did not silently return data: exception path only.
    assert digest != entry.file_sha256


def test_tampered_file_refused(tmp_path):
    rel = "ohlcv/KOSPI/2023/bars.parquet"
    digest, n = _write_ohlcv_shard(tmp_path, rel, [_sample_row()])
    (tmp_path / rel).write_bytes(b"corrupted-not-parquet")
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=digest,
        row_count=n,
        dataset="ohlcv",
        market="KOSPI",
        year=2023,
    )
    with pytest.raises(ManifestShaMismatchError):
        load_shard(tmp_path, entry)


def test_missing_file_refused(tmp_path):
    entry = ManifestEntry(
        relative_path="ohlcv/KOSPI/2023/missing.parquet",
        file_sha256="a" * 64,
        row_count=0,
        dataset="ohlcv",
        market="KOSPI",
        year=2023,
    )
    with pytest.raises(ShardFileMissingError):
        load_shard(tmp_path, entry)


def test_path_escape_refused(tmp_path):
    entry = ManifestEntry(
        relative_path="../../etc/passwd",
        file_sha256="a" * 64,
        row_count=0,
        dataset="ohlcv",
        market="KOSPI",
        year=2023,
    )
    with pytest.raises(PathEscapesArtifactRootError):
        load_shard(tmp_path, entry)


def test_row_count_mismatch_refused(tmp_path):
    rel = "ohlcv/KOSPI/2023/bars.parquet"
    digest, n = _write_ohlcv_shard(tmp_path, rel, [_sample_row()])
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=digest,
        row_count=n + 99,
        dataset="ohlcv",
        market="KOSPI",
        year=2023,
    )
    with pytest.raises(RowCountMismatchError):
        load_shard(tmp_path, entry)


def test_load_manifest_round_trip(tmp_path):
    rel = "ohlcv/KOSPI/2023/bars.parquet"
    digest, n = _write_ohlcv_shard(tmp_path, rel, [_sample_row()])
    manifest_path = tmp_path / "manifest.json"
    import json

    manifest_path.write_text(
        json.dumps(
            [
                {
                    "relative_path": rel,
                    "file_sha256": digest,
                    "row_count": n,
                    "dataset": "ohlcv",
                    "market": "KOSPI",
                    "year": 2023,
                }
            ]
        ),
        encoding="utf-8",
    )
    entries = load_manifest(manifest_path)
    assert len(entries) == 1
    assert entries[0].file_sha256 == digest
