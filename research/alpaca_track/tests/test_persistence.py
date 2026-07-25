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
#
# Adversarial-review finding: the previous version of this test monkeypatched
# `rp.sha256_file` specifically and then asserted `calls["n"] == 0` -- i.e. it
# asserted an IMPLEMENTATION DETAIL (that helper is never called), not the
# actual guaranteed EFFECT. Because the fixed code happens not to call that
# helper, the injected swap never fires and `assert loaded == rows` was
# trivially true regardless of whether the swap logic even worked. A
# reimplementation that reintroduces the exact TOCTOU by using
# `path.read_bytes()` to hash and a SEPARATE `pq.read_table(path)` disk read
# to parse would still pass that old test unchanged.
#
# This version hooks the actual I/O primitive (`pathlib.Path.read_bytes`) any
# correct OR TOCTOU-vulnerable implementation must use for its first read, and
# simulates a real concurrent overwrite landing immediately after that read
# returns. It then asserts the EFFECT, independent of implementation: the
# loader must either (a) return the original, hash-verified rows -- proving
# whatever it parsed was the SAME snapshot it hashed -- or (b) fail closed via
# one of this module's own declared `ShardLoadError` subclasses. Any OTHER
# exception (e.g. a raw `pyarrow.lib.ArrowInvalid`/`OSError` leaking out
# because a second, independent disk read hit the now-corrupted file) is a
# genuine regression and must fail this test.
# --------------------------------------------------------------------------- #
def test_load_returns_hash_verified_bytes_even_if_the_file_is_swapped_mid_load(
    tmp_path, monkeypatch
):
    import pathlib

    rows = _rows("RACEUSDC")
    rel_path, file_sha256 = p.write_symbol_shard(tmp_path, "RACEUSDC", rows)
    content_sha256 = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    shard_path = (tmp_path / rel_path).resolve()

    real_read_bytes = pathlib.Path.read_bytes
    state = {"swapped": False}

    def racing_read_bytes(self):
        # Always return whatever is ACTUALLY on disk right now (never
        # fabricate a value) -- this hooks the real I/O path rather than an
        # internal helper, so it fires for ANY implementation that reads the
        # shard via `Path.read_bytes()` at least once, correct or not.
        data = real_read_bytes(self)
        if self.resolve() == shard_path and not state["swapped"]:
            state["swapped"] = True
            # Simulate a concurrent writer completing its overwrite in the
            # instant right after this read call returned its snapshot --
            # any LATER, independent read of this same path (whether via
            # `Path.read_bytes()` again or PyArrow opening the file itself)
            # must see this corrupted content, never the original.
            self.write_bytes(b"corrupted-bytes-mid-load")
        return data

    monkeypatch.setattr(pathlib.Path, "read_bytes", racing_read_bytes)

    try:
        loaded = p.load_symbol_shard(
            tmp_path,
            rel_path,
            expected_file_sha256=file_sha256,
            expected_content_sha256=content_sha256,
            expected_row_count=len(rows),
        )
    except p.ShardLoadError:
        # Failing closed via this module's own declared error taxonomy is an
        # acceptable outcome of the race -- anything else (an uncaught
        # pyarrow/OS exception) is not, and propagates out of this test.
        return
    assert loaded == rows
