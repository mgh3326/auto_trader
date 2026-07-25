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


# --------------------------------------------------------------------------- #
# CodeRabbit fix: hash and parse the SAME in-memory bytes -- `load_symbol_shard`
# used to hash the file once (via `rp.sha256_file`) and then have
# `pq.read_table(path)` re-open+re-read the SAME path a second time. If the
# on-disk file changed between those two independent reads, bytes that never
# matched `expected_file_sha256` could still be parsed and returned.
# --------------------------------------------------------------------------- #
def test_load_returns_hash_verified_bytes_even_if_the_file_is_swapped_mid_load(
    tmp_path, monkeypatch
):
    rows = _rows("RACEUSDC")
    rel_path, file_sha256 = p.write_symbol_shard(tmp_path, "RACEUSDC", rows)
    content_sha256 = canonical_hash.canonical_sha256([r.__dict__ for r in rows])

    other_rel_path, _ = p.write_symbol_shard(
        tmp_path, "OTHERUSDC", _rows("OTHERUSDC", n=1)
    )
    swapped_bytes = (tmp_path / other_rel_path).read_bytes()
    shard_path = (tmp_path / rel_path).resolve()

    real_sha256_file = p.rp.sha256_file
    calls = {"n": 0}

    def racing_sha256_file(path):
        # Compute the hash over the file's CURRENT (pre-swap) content, exactly
        # like the pre-fix code did, then simulate a concurrent overwrite that
        # lands in the window between the hash check and the (old code's)
        # SECOND, independent `pq.read_table(path)` disk read.
        digest = real_sha256_file(path)
        calls["n"] += 1
        if path == shard_path:
            path.write_bytes(swapped_bytes)
        return digest

    monkeypatch.setattr(p.rp, "sha256_file", racing_sha256_file)

    loaded = p.load_symbol_shard(
        tmp_path,
        rel_path,
        expected_file_sha256=file_sha256,
        expected_content_sha256=content_sha256,
        expected_row_count=len(rows),
    )
    # The fixed code never calls `rp.sha256_file` at all (it hashes its own
    # single `path.read_bytes()` read), so the race window above is never
    # entered and the swap has zero effect -- the ORIGINAL, hash-verified
    # rows must come back, never the swapped-in `OTHERUSDC` content.
    assert loaded == rows
    assert calls["n"] == 0
