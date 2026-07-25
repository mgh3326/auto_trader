"""ROB-941 R1 I1 remediation — persist raw archives + normalized Parquet shards
under a gitignored artifact root (``artifact_paths.pit_data_root()`` convention).

Covers: deterministic relative-path layout, physical file SHA-256 pinning, and
that a written Parquet shard round-trips to byte-identical row content (so the
canonical row hash computed at build time reproduces at load time).
"""

import hashlib

import pyarrow.parquet as pq
import rob941_kline_schema as ks
import rob941_persistence as persist
from funding_oi_archive import FundingRow


def _kline(open_time_ms: int, symbol: str = "XRPUSDT") -> ks.NormalizedKline:
    return ks.NormalizedKline(
        symbol=symbol,
        open_time_ms=open_time_ms,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        base_volume=10.0,
        close_time_ms=open_time_ms + 59_999,
        quote_volume=1000.0,
        trade_count=5,
        taker_buy_volume=4.0,
        taker_buy_quote_volume=400.0,
    )


def _funding(calc_time: int) -> FundingRow:
    return FundingRow(
        calc_time=calc_time, funding_interval_hours=8, last_funding_rate=0.0001
    )


def test_raw_archive_relative_path_is_artifact_root_relative_posix():
    rel = persist.raw_archive_relative_path("XRPUSDT", "klines", 2025, 7, "a" * 64)
    assert not rel.startswith("/")
    assert "\\" not in rel
    assert rel.endswith(f"XRPUSDT-klines-2025-07.{'a' * 64}.zip")


def test_raw_archive_relative_path_is_content_addressed_by_checksum():
    a = persist.raw_archive_relative_path("XRPUSDT", "klines", 2025, 7, "a" * 64)
    b = persist.raw_archive_relative_path("XRPUSDT", "klines", 2025, 7, "b" * 64)
    assert (
        a != b
    )  # different checksum -> different path, even for the same symbol/month


def test_write_raw_archive_is_idempotent_for_identical_bytes(tmp_path):
    zip_bytes = b"identical content"
    rel1 = persist.write_raw_archive(tmp_path, "XRPUSDT", "klines", 2025, 7, zip_bytes)
    rel2 = persist.write_raw_archive(tmp_path, "XRPUSDT", "klines", 2025, 7, zip_bytes)
    assert rel1 == rel2
    assert (tmp_path / rel1).read_bytes() == zip_bytes


def test_write_raw_archive_different_bytes_land_at_different_paths(tmp_path):
    rel1 = persist.write_raw_archive(
        tmp_path, "XRPUSDT", "klines", 2025, 7, b"content A"
    )
    rel2 = persist.write_raw_archive(
        tmp_path, "XRPUSDT", "klines", 2025, 7, b"content B"
    )
    assert rel1 != rel2
    assert (tmp_path / rel1).read_bytes() == b"content A"
    assert (tmp_path / rel2).read_bytes() == b"content B"


def test_write_raw_archive_persists_exact_bytes_under_artifact_root(tmp_path):
    zip_bytes = b"not a real zip but exact bytes must round-trip"
    rel = persist.write_raw_archive(tmp_path, "XRPUSDT", "klines", 2025, 7, zip_bytes)
    written = tmp_path / rel
    assert written.is_file()
    assert written.read_bytes() == zip_bytes


def test_write_kline_shard_produces_parquet_with_pinned_schema_and_column_order(
    tmp_path,
):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(5)]
    rel_path, file_sha256 = persist.write_kline_shard(tmp_path, "XRPUSDT", rows)
    dest = tmp_path / rel_path
    assert dest.is_file()
    table = pq.read_table(dest)
    assert table.column_names == list(persist.KLINE_COLUMN_ORDER)
    assert table.num_rows == 5


def test_write_kline_shard_file_sha256_matches_actual_file_bytes(tmp_path):
    rows = [_kline(1751328000000)]
    rel_path, file_sha256 = persist.write_kline_shard(tmp_path, "XRPUSDT", rows)
    actual = hashlib.sha256((tmp_path / rel_path).read_bytes()).hexdigest()
    assert file_sha256 == actual


def test_write_kline_shard_round_trips_row_values_exactly(tmp_path):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(3)]
    rel_path, _ = persist.write_kline_shard(tmp_path, "XRPUSDT", rows)
    table = pq.read_table(tmp_path / rel_path)
    reloaded = [ks.NormalizedKline(**d) for d in table.to_pylist()]
    assert reloaded == rows


def test_write_funding_shard_produces_parquet_with_pinned_schema(tmp_path):
    rows = [_funding(1751328000000), _funding(1751356800000)]
    rel_path, file_sha256 = persist.write_funding_shard(tmp_path, "DOGEUSDT", rows)
    dest = tmp_path / rel_path
    assert dest.is_file()
    table = pq.read_table(dest)
    assert table.column_names == list(persist.FUNDING_COLUMN_ORDER)
    actual = hashlib.sha256(dest.read_bytes()).hexdigest()
    assert file_sha256 == actual


def test_write_funding_shard_round_trips_row_values_exactly(tmp_path):
    rows = [_funding(1751328000000), _funding(1751356800000)]
    rel_path, _ = persist.write_funding_shard(tmp_path, "DOGEUSDT", rows)
    table = pq.read_table(tmp_path / rel_path)
    reloaded = [FundingRow(**d) for d in table.to_pylist()]
    assert reloaded == rows


def test_write_kline_shard_is_content_addressed_and_idempotent(tmp_path):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(5)]
    rel1, sha1 = persist.write_kline_shard(tmp_path, "XRPUSDT", rows)
    rel2, sha2 = persist.write_kline_shard(tmp_path, "XRPUSDT", rows)
    assert rel1 == rel2
    assert sha1 == sha2

    different_rows = [_kline(1751328000000 + i * 60_000) for i in range(3)]
    rel3, _ = persist.write_kline_shard(tmp_path, "XRPUSDT", different_rows)
    assert rel3 != rel1  # different content -> different content-addressed path


def test_write_funding_shard_is_content_addressed_and_idempotent(tmp_path):
    rows = [_funding(1751328000000)]
    rel1, sha1 = persist.write_funding_shard(tmp_path, "DOGEUSDT", rows)
    rel2, sha2 = persist.write_funding_shard(tmp_path, "DOGEUSDT", rows)
    assert rel1 == rel2
    assert sha1 == sha2


def test_kline_and_funding_shard_paths_do_not_collide_across_symbols():
    same_hash = "c" * 64
    a = persist.kline_shard_relative_path("XRPUSDT", same_hash)
    b = persist.kline_shard_relative_path("DOGEUSDT", same_hash)
    assert a != b
    fa = persist.funding_shard_relative_path("XRPUSDT", same_hash)
    fb = persist.funding_shard_relative_path("DOGEUSDT", same_hash)
    assert fa != fb
    assert a != fa  # kline vs funding shard paths are namespaced apart
