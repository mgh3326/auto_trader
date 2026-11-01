"""Public daily bars: a fetch step that writes a snapshot, and a verified reader.

Sources, all unauthenticated:

* KR: Yahoo Finance ``<code>.KS``, then ``<code>.KQ``. KRX regular session only.
* US: Yahoo Finance, symbol converted by ``app.core.symbol.to_yahoo_symbol``.
* crypto: Upbit public ``/v1/candles/days``. The bar date is the UTC date.
* Benchmarks: ``^KS11`` (KOSPI), ``^GSPC`` (S&P 500) and ``KRW-BTC`` on Upbit.

Both index series are price indices and ``close`` is not dividend-adjusted, so
every comparison here is price return against price return. Yahoo closes are
split-adjusted as of the fetch, so callers convert ledger quantities and prices
with :meth:`Bars.split_factor_after`.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from app.core.symbol import to_yahoo_symbol
from research.trade_retro.export import ExportIntegrityError, sha256_file

LOG = logging.getLogger(__name__)

SNAPSHOT_MANIFEST = "prices_manifest.json"
BENCHMARKS = {"kr": "^KS11", "us": "^GSPC", "crypto": "BTC"}
BENCHMARK_NAMES = {
    "kr": "KOSPI (^KS11, Yahoo)",
    "us": "S&P 500 (^GSPC, Yahoo)",
    "crypto": "BTC/KRW (KRW-BTC, Upbit)",
}
UPBIT_DAYS_URL = "https://api.upbit.com/v1/candles/days"


@dataclass
class Bars:
    """One symbol's daily bars, sorted by date."""

    dates: np.ndarray  # datetime64[D]
    open: np.ndarray
    close: np.ndarray
    splits: list[tuple[date, float]] = field(default_factory=list)

    @classmethod
    def from_rows(
        cls,
        rows: list[tuple[date, float, float]],
        splits: list[tuple[date, float]] | None = None,
    ) -> Bars:
        ordered = sorted(rows)
        return cls(
            dates=np.array([r[0] for r in ordered], dtype="datetime64[D]"),
            open=np.array([r[1] for r in ordered], dtype=float),
            close=np.array([r[2] for r in ordered], dtype=float),
            splits=list(splits or []),
        )

    def __len__(self) -> int:
        return len(self.dates)

    def idx_on_or_after(self, day: date) -> int | None:
        i = int(np.searchsorted(self.dates, np.datetime64(day), side="left"))
        return i if i < len(self.dates) else None

    def idx_on_or_before(self, day: date) -> int | None:
        i = int(np.searchsorted(self.dates, np.datetime64(day), side="right")) - 1
        return i if i >= 0 else None

    def date_at(self, i: int) -> date:
        return self.dates[i].astype("datetime64[D]").astype(object)

    def split_factor_after(self, day: date) -> float:
        """Shares held on ``day`` become this many shares in the bars' units."""
        factor = 1.0
        for ex_date, ratio in self.splits:
            if ex_date > day:
                factor *= ratio
        return factor


@dataclass
class PriceBook:
    bars: dict[tuple[str, str], Bars]
    benchmarks: dict[str, Bars]
    missing: dict[tuple[str, str], str]
    snapshot_sha256: str = ""
    meta: dict = field(default_factory=dict)

    def get(self, market: str, symbol: str) -> Bars | None:
        return self.bars.get((market, symbol))

    def index_return(self, market: str, start: date, end: date) -> float | None:
        """Benchmark close-to-close return, using the last bar on or before each day."""
        bars = self.benchmarks.get(market)
        if bars is None:
            return None
        i, j = bars.idx_on_or_before(start), bars.idx_on_or_before(end)
        if i is None or j is None:
            return None
        return float(bars.close[j] / bars.close[i] - 1.0)

    def index_return_from_open(
        self, market: str, start: date, end: date
    ) -> float | None:
        """Benchmark return from the open of the first bar on or after ``start``."""
        bars = self.benchmarks.get(market)
        if bars is None:
            return None
        i, j = bars.idx_on_or_after(start), bars.idx_on_or_before(end)
        if i is None or j is None or j < i:
            return None
        return float(bars.close[j] / bars.open[i] - 1.0)


def _yahoo_candidates(market: str, symbol: str) -> list[str]:
    if market == "kr":
        return [f"{symbol}.KS", f"{symbol}.KQ"]
    return [to_yahoo_symbol(symbol)]


def _fetch_yahoo(ticker: str, start: date, end: date) -> tuple[list[dict], str]:
    import yfinance as yf

    frame = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
        actions=True,
    )
    rows: list[dict] = []
    for stamp, row in frame.iterrows():
        if not (row["Close"] > 0 and row["Open"] > 0):
            continue
        rows.append(
            {
                "date": stamp.date().isoformat(),
                "open": repr(float(row["Open"])),
                "close": repr(float(row["Close"])),
                "split": repr(float(row.get("Stock Splits", 0.0) or 0.0)),
            }
        )
    return rows, ticker


def _fetch_upbit(market_code: str, start: date, end: date) -> list[dict]:
    rows: dict[str, dict] = {}
    cursor = datetime.combine(end, datetime.min.time(), tzinfo=UTC)
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        while True:
            response = client.get(
                UPBIT_DAYS_URL,
                params={
                    "market": market_code,
                    "count": 200,
                    "to": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            if response.status_code == 404:
                return []
            response.raise_for_status()
            page = response.json()
            if not page:
                break
            for candle in page:
                day = candle["candle_date_time_utc"][:10]
                rows[day] = {
                    "date": day,
                    "open": repr(float(candle["opening_price"])),
                    "close": repr(float(candle["trade_price"])),
                    "split": "0.0",
                }
            oldest = min(c["candle_date_time_utc"][:10] for c in page)
            if oldest <= start.isoformat() or len(page) < 200:
                break
            cursor = datetime.fromisoformat(oldest).replace(tzinfo=UTC)
            time.sleep(0.15)
    return [rows[d] for d in sorted(rows) if start.isoformat() <= d < end.isoformat()]


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "open", "close", "split"])
        writer.writeheader()
        writer.writerows(rows)


def fetch_snapshot(
    out_dir: Path,
    symbols: dict[str, list[str]],
    start: date,
    end_exclusive: date,
) -> dict:
    """Download public bars for ``symbols`` and write a hashed snapshot.

    Refuses a directory that already holds a snapshot: a re-fetch can pick up
    revised prices, so it must go to a new directory and a new report.
    """
    out_dir = Path(out_dir).expanduser()
    if (out_dir / SNAPSHOT_MANIFEST).exists():
        raise FileExistsError(f"{out_dir} already holds a price snapshot")
    entries: dict[str, dict] = {}
    missing: dict[str, str] = {}

    def record(key: str, rel: str, rows: list[dict], source: str) -> None:
        _write_rows(out_dir / rel, rows)
        entries[key] = {
            "file": rel,
            "source": source,
            "rows": len(rows),
            "first": rows[0]["date"],
            "last": rows[-1]["date"],
            "sha256": sha256_file(out_dir / rel),
        }

    def fetch_equity(key: str, rel: str, candidates: list[str]) -> None:
        for ticker in candidates:
            try:
                rows, source = _fetch_yahoo(ticker, start, end_exclusive)
            except Exception as exc:  # the row is recorded as missing, not hidden
                LOG.warning("%s: %s", ticker, type(exc).__name__)
                continue
            if rows:
                record(key, rel, rows, f"yahoo:{source}")
                return
            time.sleep(0.1)
        missing[key] = "no bars from yahoo"

    for market, code in (("kr", "^KS11"), ("us", "^GSPC")):
        fetch_equity(f"benchmark/{market}", f"benchmark/{market}.csv", [code])
    for market, names in sorted(symbols.items()):
        for symbol in sorted(names):
            key, rel = f"{market}/{symbol}", f"{market}/{symbol}.csv"
            if market == "crypto":
                rows = _fetch_upbit(f"KRW-{symbol}", start, end_exclusive)
                if rows:
                    record(key, rel, rows, f"upbit:KRW-{symbol}")
                else:
                    missing[key] = "no bars from upbit"
                time.sleep(0.15)
            else:
                fetch_equity(key, rel, _yahoo_candidates(market, symbol))
    btc = _fetch_upbit("KRW-BTC", start, end_exclusive)
    if btc:
        record("benchmark/crypto", "benchmark/crypto.csv", btc, "upbit:KRW-BTC")
    else:
        missing["benchmark/crypto"] = "no bars from upbit"

    manifest = {
        "schema": "trade-retro-prices/v1",
        "fetched_at_utc": datetime.now(UTC).isoformat(),
        "start": start.isoformat(),
        "end_exclusive": end_exclusive.isoformat(),
        "entries": entries,
        "missing": missing,
    }
    path = out_dir / SNAPSHOT_MANIFEST
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def _read_bars(path: Path) -> Bars:
    rows: list[tuple[date, float, float]] = []
    splits: list[tuple[date, float]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            day = date.fromisoformat(row["date"])
            rows.append((day, float(row["open"]), float(row["close"])))
            ratio = float(row["split"])
            if ratio > 0:
                splits.append((day, ratio))
    return Bars.from_rows(rows, splits)


def load_snapshot(root: Path) -> PriceBook:
    """Read a snapshot, refusing any file whose hash differs from its manifest."""
    root = Path(root).expanduser()
    path = root / SNAPSHOT_MANIFEST
    if not path.is_file():
        raise ExportIntegrityError(f"no {SNAPSHOT_MANIFEST} in {root}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    bars: dict[tuple[str, str], Bars] = {}
    benchmarks: dict[str, Bars] = {}
    for key, entry in manifest["entries"].items():
        file_path = root / entry["file"]
        if not file_path.is_file() or sha256_file(file_path) != entry["sha256"]:
            raise ExportIntegrityError(f"price file does not match manifest: {key}")
        group, name = key.split("/", 1)
        if group == "benchmark":
            benchmarks[name] = _read_bars(file_path)
        else:
            bars[(group, name)] = _read_bars(file_path)
    absent = sorted(set(BENCHMARKS) - set(benchmarks))
    if absent:
        raise ExportIntegrityError(f"price snapshot has no benchmark for: {absent}")
    missing = {tuple(k.split("/", 1)): v for k, v in manifest["missing"].items()}
    return PriceBook(
        bars=bars,
        benchmarks=benchmarks,
        missing=missing,
        snapshot_sha256=sha256_file(path),
        meta={
            "fetched_at_utc": manifest["fetched_at_utc"],
            "start": manifest["start"],
            "end_exclusive": manifest["end_exclusive"],
        },
    )


def snapshot_range(window_since: date, window_until: date) -> tuple[date, date]:
    """Bars from 40 days before the window, so the first order has a reference close."""
    return window_since - timedelta(days=40), window_until
