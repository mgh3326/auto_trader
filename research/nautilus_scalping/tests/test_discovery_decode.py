"""ROB-339 — decode the Nautilus catalog fixed-point price/size encoding.

A ParquetDataCatalog stores price/size as fixed_size_binary[16] (int128
little-endian raw = value * 10**precision), with precision in the parquet schema
metadata. read_ticks must decode these to floats while leaving plain-float parquet
(the other unit tests) untouched.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from discovery.data import _decode_int128_le, read_ticks


def test_decode_int128_le_fixed_point() -> None:
    raw = (3_650_012).to_bytes(16, "little", signed=True)
    assert _decode_int128_le(raw, 2) == 36500.12


def test_decode_int128_le_negative_and_zero_precision() -> None:
    assert _decode_int128_le((-5).to_bytes(16, "little", signed=True), 0) == -5.0


def test_read_ticks_decodes_binary_price_size(tmp_path) -> None:
    p = tmp_path / "t.parquet"
    ts0 = pd.Timestamp("2026-03-02", tz="UTC").value
    ts = [ts0, ts0 + 1, ts0 + 2]
    # 128-bit Nautilus: raw = value * 10**16 (fixed), display precision only rounds.
    price_raw = [int(0.6012 * 10**16).to_bytes(16, "little", signed=True)] * 3
    size_raw = [int(1.5 * 10**16).to_bytes(16, "little", signed=True)] * 3
    table = pa.table(
        {
            "ts_event": pa.array(ts, pa.uint64()),
            "price": pa.array(price_raw, pa.binary(16)),
            "size": pa.array(size_raw, pa.binary(16)),
        }
    )
    pq.write_table(table, p)
    df = read_ticks(p, None, None, price_precision=4, size_precision=5)
    assert round(df["price"].iloc[0], 4) == 0.6012
    assert round(df["size"].iloc[0], 5) == 1.5


def test_read_ticks_binary_still_respects_window(tmp_path) -> None:
    p = tmp_path / "t.parquet"
    ts0 = pd.Timestamp("2026-03-02", tz="UTC").value
    ts1 = pd.Timestamp("2026-03-03", tz="UTC").value
    ts = [ts0, ts0 + 1, ts1]  # two in day2, one in day3
    raw = [(6012).to_bytes(16, "little", signed=True)] * 3
    table = pa.table(
        {
            "ts_event": pa.array(ts, pa.uint64()),
            "price": pa.array(raw, pa.binary(16)),
            "size": pa.array(raw, pa.binary(16)),
        }
    )
    pq.write_table(table, p)
    df = read_ticks(p, ts_from=ts0, ts_to=ts1, price_precision=4, size_precision=4)
    assert len(df) == 2  # day3 row filtered out at scan time
