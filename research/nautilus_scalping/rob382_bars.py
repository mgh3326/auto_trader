"""ROB-382 — OHLCV bar loader for the strat.ninja falsification spike (pure, stdlib).

Why a new loader (not ``pit_bars``): ``pit_bars`` drops ``open`` and ``volume`` (its
``Bar`` is ts/high/low/close only). The ported external strategies need full OHLCV —
Heikin-Ashi needs open, VWAP/ClucHAnix/ElliotV7 gate on volume. This reads the SAME
gitignored CSVs ``pit_klines_fetcher`` writes, into a full ``OHLCVBar``.

PUBLIC data only; no network here (the fetch is ``fetch_rob382_data.py``). No secrets.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from artifact_paths import pit_data_root


@dataclass(frozen=True)
class OHLCVBar:
    ts: int  # open_time, epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_ts: int  # close_time, epoch ms


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _parse_csv(path: Path) -> list[OHLCVBar]:
    bars: list[OHLCVBar] = []
    with path.open() as fh:
        for line in fh:
            parts = line.rstrip("\n").split(",")
            if len(parts) < 7 or not _is_number(parts[0]):
                continue  # skip header / malformed
            ts = int(float(parts[0]))
            bars.append(
                OHLCVBar(
                    ts=ts,
                    open=float(parts[1]),
                    high=float(parts[2]),
                    low=float(parts[3]),
                    close=float(parts[4]),
                    volume=float(parts[5]),
                    close_ts=int(float(parts[6])),
                )
            )
    return bars


def load_ohlcv(
    symbol: str,
    interval: str,
    *,
    root: Path | None = None,
    from_month: str | None = None,
    to_month: str | None = None,
) -> list[OHLCVBar]:
    """Load all monthly CSVs for ``symbol``/``interval`` into a deduped, sorted list.

    Optional ``from_month``/``to_month`` (``YYYY-MM``) bound the months loaded. Bars are
    deduplicated by open_time and sorted chronologically.
    """
    base = (Path(root) if root else pit_data_root()) / "klines" / interval / symbol
    if not base.is_dir():
        return []
    by_ts: dict[int, OHLCVBar] = {}
    for csv in sorted(base.glob(f"{symbol}-{interval}-*.csv")):
        stem = csv.stem  # e.g. BTCUSDT-5m-2024-03
        month = stem.rsplit("-", 2)[-2] + "-" + stem.rsplit("-", 2)[-1]
        if from_month and month < from_month:
            continue
        if to_month and month > to_month:
            continue
        for bar in _parse_csv(csv):
            by_ts[bar.ts] = bar
    return [by_ts[t] for t in sorted(by_ts)]


def available_symbols(interval: str, *, root: Path | None = None) -> list[str]:
    base = (Path(root) if root else pit_data_root()) / "klines" / interval
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())
