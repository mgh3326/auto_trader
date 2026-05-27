"""ROB-339 (D3/D4) — read catalog trade-tick parquet with a real window filter.

pandas/pyarrow only; NO Nautilus engine boot. ``read_ticks`` applies the
``[ts_from, ts_to)`` window as a pyarrow predicate-pushdown filter at scan time, so
``--window-from/--window-to`` constrains *processed data*, not just artifact
metadata.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pyarrow.types as patypes


def _date_to_ns(date_str: str, *, plus_one_day: bool = False) -> int:
    ts = pd.Timestamp(date_str.strip(), tz="UTC")
    if plus_one_day:
        ts = ts + pd.Timedelta(days=1)
    return int(ts.value)


def window_bounds_ns(window_from: str, window_to: str) -> tuple[int | None, int | None]:
    """Parse ``YYYY-MM-DD`` window edges to epoch-ns; ``to`` date is inclusive.

    Blank edges map to ``None`` (unbounded on that side). The returned bounds are
    half-open ``[lo, hi)`` so a same-day ``from==to`` spans that whole UTC day.
    """
    lo = _date_to_ns(window_from) if window_from and window_from.strip() else None
    hi = (
        _date_to_ns(window_to, plus_one_day=True)
        if window_to and window_to.strip()
        else None
    )
    return lo, hi


def _decode_int128_le(raw: bytes, precision: int) -> float:
    """Decode a Nautilus fixed-point value: little-endian signed int / 10**precision."""
    return int.from_bytes(raw, byteorder="little", signed=True) / (10**precision)


# Nautilus fixed-point RAW scale by byte width: 128-bit build -> 10**16, 64-bit -> 10**9.
# This is fixed by the build, independent of an instrument's *display* precision (the
# price_precision/size_precision metadata, which only governs rounding for presentation).
_RAW_PRECISION_BY_WIDTH = {16: 16, 8: 9}


def _decode_if_binary(
    df: pd.DataFrame, schema, col: str, display_precision: int | None, meta_key: bytes
) -> None:
    """Decode ``col`` in-place if it's fixed-point binary; leave plain numerics alone.

    The decode divisor is the fixed raw scale (by byte width); ``display_precision``
    (arg or schema metadata) only rounds the decoded float for presentation.
    """
    ftype = schema.field(col).type
    if not (patypes.is_fixed_size_binary(ftype) or patypes.is_binary(ftype)):
        return
    width = ftype.byte_width if patypes.is_fixed_size_binary(ftype) else 16
    raw_precision = _RAW_PRECISION_BY_WIDTH.get(width, 16)
    df[col] = df[col].map(lambda b: _decode_int128_le(bytes(b), raw_precision))
    if display_precision is None:
        md = schema.metadata or {}
        display_precision = int(md[meta_key]) if meta_key in md else None
    if display_precision is not None:
        df[col] = df[col].round(display_precision)


def read_ticks(
    source: str | Path,
    ts_from: int | None,
    ts_to: int | None,
    *,
    price_precision: int | None = None,
    size_precision: int | None = None,
) -> pd.DataFrame:
    """Read trade ticks from a parquet file/dir, filtering ``ts_event`` in ``[from, to)``.

    The filter is a pyarrow dataset expression -> predicate pushdown, so out-of-window
    rows are never materialized. Nautilus stores price/size as fixed_size_binary
    (int128 fixed-point, raw = value * 10**16 in 128-bit builds); those columns are
    decoded to floats and rounded to ``*_precision`` (the display precision, from the
    arg or schema metadata). Plain float parquet is returned unchanged.
    """
    dataset = ds.dataset(str(source))
    expr = None
    if ts_from is not None:
        expr = ds.field("ts_event") >= ts_from
    if ts_to is not None:
        upper = ds.field("ts_event") < ts_to
        expr = upper if expr is None else (expr & upper)
    table = dataset.to_table(filter=expr)
    df = table.to_pandas()
    _decode_if_binary(df, table.schema, "price", price_precision, b"price_precision")
    _decode_if_binary(df, table.schema, "size", size_precision, b"size_precision")
    return df


def aggregate_to_bars(ticks: pd.DataFrame, freq: str = "1min") -> pd.DataFrame:
    """Aggregate trade ticks to OHLCV bars (open/high/low/close/volume) at ``freq``.

    Empty time buckets are dropped. Output is index-reset with a ``dt`` column.
    """
    df = ticks.copy()
    df["dt"] = pd.to_datetime(df["ts_event"], unit="ns", utc=True)
    df = df.set_index("dt").sort_index()
    bars = df["price"].resample(freq).ohlc()
    bars["volume"] = df["size"].resample(freq).sum()
    bars = bars.dropna(subset=["open"])
    return bars.reset_index()


def _locate_trade_tick_parquet(catalog: str | Path, symbol: str) -> Path:
    """Find the Nautilus catalog trade-tick parquet dir for ``symbol`` (best-effort).

    Layout: ``<catalog>/data/trade_tick/<INSTRUMENT_ID>/``. Matches the first
    instrument dir whose name starts with ``symbol`` (mirrors backtest_runner's
    ``startswith`` resolution).
    """
    base = Path(catalog) / "data" / "trade_tick"
    matches = sorted(d for d in base.glob(f"{symbol}*") if d.is_dir())
    if not matches:
        raise FileNotFoundError(f"no trade_tick parquet for {symbol!r} under {base}")
    return matches[0]


def _read_precisions(tick_dir: Path) -> tuple[int | None, int | None]:
    """Read price/size precision from the first parquet file's schema metadata."""
    files = sorted(Path(tick_dir).glob("*.parquet"))
    if not files:
        return (None, None)
    md = pq.read_schema(files[0]).metadata or {}
    pp = int(md[b"price_precision"]) if b"price_precision" in md else None
    sp = int(md[b"size_precision"]) if b"size_precision" in md else None
    return (pp, sp)


def load_bars(
    catalog: str | Path,
    symbol: str,
    window_from: str = "",
    window_to: str = "",
    freq: str = "1min",
) -> pd.DataFrame:
    """Catalog symbol -> windowed OHLCV bars (pandas/pyarrow; no Nautilus engine).

    Locates the trade-tick parquet dir, reads price/size precision from its schema
    metadata, applies the real ``[from, to)`` window filter, decodes the fixed-point
    price/size, and aggregates to OHLCV bars.
    """
    lo, hi = window_bounds_ns(window_from, window_to)
    tick_dir = _locate_trade_tick_parquet(catalog, symbol)
    pp, sp = _read_precisions(tick_dir)
    ticks = read_ticks(tick_dir, lo, hi, price_precision=pp, size_precision=sp)
    return aggregate_to_bars(ticks, freq=freq)
