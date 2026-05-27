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


def read_ticks(
    source: str | Path, ts_from: int | None, ts_to: int | None
) -> pd.DataFrame:
    """Read trade ticks from a parquet file/dir, filtering ``ts_event`` in ``[from, to)``.

    The filter is a pyarrow dataset expression -> predicate pushdown, so out-of-window
    rows are never materialized.
    """
    dataset = ds.dataset(str(source))
    expr = None
    if ts_from is not None:
        expr = ds.field("ts_event") >= ts_from
    if ts_to is not None:
        upper = ds.field("ts_event") < ts_to
        expr = upper if expr is None else (expr & upper)
    table = dataset.to_table(filter=expr)
    return table.to_pandas()


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


def load_bars(
    catalog: str | Path,
    symbol: str,
    window_from: str = "",
    window_to: str = "",
    freq: str = "1min",
) -> pd.DataFrame:
    """Catalog symbol -> windowed OHLCV bars (integration; verified at smoke).

    NOTE: price/size in a Nautilus ParquetDataCatalog may be stored as fixed-point
    int64 (raw = value * 10**precision); the decode to float is catalog-encoding
    dependent and must be confirmed against the real catalog at smoke time. The pure
    read_ticks / aggregate_to_bars / window_bounds_ns helpers are the unit-tested core.
    """
    lo, hi = window_bounds_ns(window_from, window_to)
    ticks = read_ticks(_locate_trade_tick_parquet(catalog, symbol), lo, hi)
    return aggregate_to_bars(ticks, freq=freq)
