"""ROB-1059 H1 (AC23) — persist + offline fail-closed reload of a normalized
kline shard, zero network / zero DB.
"""

import canonical_hash
import persistence as p
import pytest
import rob941_kline_schema as ks


def _rows(symbol: str, n: int = 5) -> list[ks.NormalizedKline]:
    out = []
    for i in range(n):
        ts = i * 60_000
        out.append(
            ks.NormalizedKline(
                symbol=symbol,
                open_time_ms=ts,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                base_volume=10.0,
                close_time_ms=ts + 59_999,
                quote_volume=1000.0,
                trade_count=5,
                taker_buy_volume=4.0,
                taker_buy_quote_volume=400.0,
            )
        )
    return out


def test_write_then_load_round_trip(tmp_path):
    rows = _rows("BTCUSDC")
    rel_path, file_sha256 = p.write_symbol_shard(tmp_path, "BTCUSDC", rows)
    content_sha256 = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    loaded = p.load_symbol_shard(
        tmp_path,
        rel_path,
        expected_file_sha256=file_sha256,
        expected_content_sha256=content_sha256,
        expected_row_count=len(rows),
    )
    assert loaded == rows


def test_missing_file_raises_shard_file_missing(tmp_path):
    with pytest.raises(p.ShardFileMissingError):
        p.load_symbol_shard(
            tmp_path,
            "shards/klines/does-not-exist.parquet",
            expected_file_sha256="a" * 64,
            expected_content_sha256="b" * 64,
            expected_row_count=0,
        )


def test_tampered_file_raises_shard_file_tampered(tmp_path):
    rows = _rows("ETHUSDC")
    rel_path, file_sha256 = p.write_symbol_shard(tmp_path, "ETHUSDC", rows)
    (tmp_path / rel_path).write_bytes(b"corrupted-bytes")
    content_sha256 = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    with pytest.raises(p.ShardFileTamperedError):
        p.load_symbol_shard(
            tmp_path,
            rel_path,
            expected_file_sha256=file_sha256,
            expected_content_sha256=content_sha256,
            expected_row_count=len(rows),
        )


def test_path_traversal_is_refused(tmp_path):
    with pytest.raises(p.ShardPathEscapesArtifactRootError):
        p.load_symbol_shard(
            tmp_path,
            "../../etc/passwd",
            expected_file_sha256="a" * 64,
            expected_content_sha256="b" * 64,
            expected_row_count=0,
        )


def test_row_count_mismatch_raises(tmp_path):
    rows = _rows("SOLUSDC", n=5)
    rel_path, file_sha256 = p.write_symbol_shard(tmp_path, "SOLUSDC", rows)
    content_sha256 = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    with pytest.raises(p.ShardRowCountMismatchError):
        p.load_symbol_shard(
            tmp_path,
            rel_path,
            expected_file_sha256=file_sha256,
            expected_content_sha256=content_sha256,
            expected_row_count=999,  # wrong on purpose
        )
