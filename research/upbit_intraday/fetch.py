"""Fetch Upbit KRW 15-minute candles into hashed files outside git (hk 6035).

GET only, no keys. Upbit creates no candle for a 15-minute slot without
trades, so a market's file simply has no row for such slots; nothing here
fills or interpolates them. Candle numbers are stored as the exact decimal
text Upbit sent. Per-market files are deterministic gzip CSV; their sha256,
row counts and the sha256 of every response page go into ``manifest.json``.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .public_http import Fetch

MARKETS_URL = "https://api.upbit.com/v1/market/all?isDetails=true"
CANDLES_URL = "https://api.upbit.com/v1/candles/minutes/15"
PAGE_SIZE = 200
BAR_SECONDS = 900
MAX_PAGES_PER_MARKET = 120
MANIFEST_FORMAT = "upbit-intraday-candles/v1"
CSV_HEADER = ("time_utc", "open", "high", "low", "close", "volume", "quote_volume")


class FetchError(RuntimeError):
    pass


class Pacer:
    """Thread-safe global spacing between Upbit requests."""

    def __init__(
        self,
        interval: float = 0.12,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            self.sleep(start - now)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def candles_url(market: str, to: datetime) -> str:
    query = {"market": market, "count": str(PAGE_SIZE), "to": iso_z(to)}
    return CANDLES_URL + "?" + urllib.parse.urlencode(query)


@dataclass(frozen=True)
class Row:
    time_utc: str  # candle_date_time_utc, "YYYY-MM-DDTHH:MM:SS"
    open: str
    high: str
    low: str
    close: str
    volume: str
    quote_volume: str


def _num_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise FetchError(f"candle number was not preserved as text: {value!r}")
    float(value)  # raises ValueError for garbage
    return value


def parse_page(market: str, body: bytes) -> list[Row]:
    try:
        payload = json.loads(body, parse_float=str, parse_int=str)
    except ValueError as exc:
        raise FetchError(f"{market}: response is not JSON") from exc
    if not isinstance(payload, list):
        raise FetchError(f"{market}: candles response is not a list")
    rows: list[Row] = []
    for item in payload:
        if (
            not isinstance(item, dict)
            or item.get("market") != market
            or item.get("unit") not in ("15", 15)
        ):
            raise FetchError(f"{market}: malformed candle {item!r}")
        stamp = datetime.fromisoformat(item["candle_date_time_utc"]).replace(tzinfo=UTC)
        if int(stamp.timestamp()) % BAR_SECONDS:
            raise FetchError(f"{market}: candle not on a 15-minute boundary {stamp}")
        rows.append(
            Row(
                time_utc=stamp.strftime("%Y-%m-%dT%H:%M:%S"),
                open=_num_text(item["opening_price"]),
                high=_num_text(item["high_price"]),
                low=_num_text(item["low_price"]),
                close=_num_text(item["trade_price"]),
                volume=_num_text(item["candle_acc_trade_volume"]),
                quote_volume=_num_text(item["candle_acc_trade_price"]),
            )
        )
    return rows


def fetch_market(
    fetch: Fetch,
    market: str,
    start: datetime,
    end_exclusive: datetime,
    pacer: Pacer,
) -> tuple[list[Row], list[str]]:
    """Rows with ``start <= open time < end_exclusive`` (ascending) and page hashes."""
    by_time: dict[str, Row] = {}
    page_hashes: list[str] = []
    to = end_exclusive
    for _ in range(MAX_PAGES_PER_MARKET):
        pacer.wait()
        resp = fetch(candles_url(market, to))
        if resp.status != 200:
            raise FetchError(f"{market}: HTTP {resp.status}")
        page_hashes.append(hashlib.sha256(resp.body).hexdigest())
        page = parse_page(market, resp.body)
        if not page:
            break
        earliest = min(
            datetime.fromisoformat(r.time_utc).replace(tzinfo=UTC) for r in page
        )
        latest = max(
            datetime.fromisoformat(r.time_utc).replace(tzinfo=UTC) for r in page
        )
        if latest >= to:
            raise FetchError(f"{market}: page is not strictly before 'to' ({to})")
        for row in page:
            when = datetime.fromisoformat(row.time_utc).replace(tzinfo=UTC)
            if start <= when < end_exclusive:
                by_time[row.time_utc] = row
        if earliest <= start or len(page) < PAGE_SIZE:
            break
        to = earliest  # exclusive upper bound of the next page
    else:
        raise FetchError(f"{market}: more than {MAX_PAGES_PER_MARKET} pages")
    return [by_time[k] for k in sorted(by_time)], page_hashes


def rows_to_gzip_csv(rows: list[Row]) -> bytes:
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow(
            [r.time_utc, r.open, r.high, r.low, r.close, r.volume, r.quote_volume]
        )
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(text.getvalue().encode("utf-8"))
    return buf.getvalue()


def krw_markets(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        raise FetchError("market/all is not a JSON list")
    out = {
        item["market"]
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("market"), str)
        and item["market"].startswith("KRW-")
    }
    if not out:
        raise FetchError("market/all has zero KRW markets")
    return sorted(out)


def canonical_json(obj: Any) -> bytes:
    return (
        json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False).encode() + b"\n"
    )


def collect(
    fetch: Fetch,
    root: Path,
    start: datetime,
    end_exclusive: datetime,
    *,
    markets: list[str] | None = None,
    workers: int = 4,
    pacer: Pacer | None = None,
    dataset: str = "upbit-intraday-v1",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """Fetch every KRW market into ``root`` and write ``manifest.json``.

    Resumable: a market whose file and sidecar already exist is skipped. The
    market list snapshot is kept byte-for-byte (``market_all.json``) and its
    hash is recorded; markets delisted before this run are absent from it and
    therefore from the dataset (survivor-only, stated in the manifest).
    """
    root.mkdir(parents=True, exist_ok=True)
    candles_dir = root / "candles15"
    candles_dir.mkdir(exist_ok=True)
    pacer = pacer or Pacer()

    market_all_path = root / "market_all.json"
    if market_all_path.exists():
        market_all_body = market_all_path.read_bytes()
        listed_at = json.loads((root / "market_all.meta.json").read_text())[
            "fetched_at"
        ]
    else:
        pacer.wait()
        resp = fetch(MARKETS_URL)
        if resp.status != 200:
            raise FetchError(f"market/all: HTTP {resp.status}")
        market_all_body = resp.body
        listed_at = iso_z(now())
        market_all_path.write_bytes(market_all_body)
        (root / "market_all.meta.json").write_text(
            json.dumps({"fetched_at": listed_at})
        )
    market_all = json.loads(market_all_body)
    all_markets = krw_markets(market_all)
    todo = markets if markets is not None else all_markets

    lock = threading.Lock()
    failures: dict[str, str] = {}

    def one(market: str) -> None:
        data_path = candles_dir / f"{market}.csv.gz"
        meta_path = candles_dir / f"{market}.meta.json"
        if data_path.exists() and meta_path.exists():
            return
        try:
            rows, hashes = fetch_market(fetch, market, start, end_exclusive, pacer)
        except (FetchError, OSError, ValueError) as exc:
            with lock:
                failures[market] = f"{type(exc).__name__}: {exc}"
            return
        blob = rows_to_gzip_csv(rows)
        data_path.write_bytes(blob)
        meta = {
            "market": market,
            "file": f"candles15/{market}.csv.gz",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "rows": len(rows),
            "first_time_utc": rows[0].time_utc if rows else None,
            "last_time_utc": rows[-1].time_utc if rows else None,
            "pages": len(hashes),
            "page_body_sha256": hashes,
        }
        meta_path.write_text(json.dumps(meta, sort_keys=True))
        progress(f"{market} rows={len(rows)} pages={len(hashes)}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))

    entries: dict[str, Any] = {}
    for market in all_markets:
        meta_path = candles_dir / f"{market}.meta.json"
        if meta_path.exists():
            entries[market] = json.loads(meta_path.read_text())
    manifest = {
        "manifest_format": MANIFEST_FORMAT,
        "dataset": dataset,
        "source": "https://api.upbit.com/v1/candles/minutes/15 (public GET, no keys)",
        "window": {
            "start_utc": iso_z(start),
            "end_exclusive_utc": iso_z(end_exclusive),
            "bar_seconds": BAR_SECONDS,
        },
        "market_list": {
            "url": MARKETS_URL,
            "fetched_at": listed_at,
            "file": "market_all.json",
            "sha256": hashlib.sha256(market_all_body).hexdigest(),
            "krw_market_count": len(all_markets),
        },
        "coverage_notes": [
            "Markets come from the market list at fetch time; a market delisted "
            "before that is absent (Upbit serves no candles for it): survivor-only.",
            "A slot without trades has no candle and no row; gaps are not filled.",
            "market_warning/caution flags in market_all.json are the state at "
            "fetch time, not point in time.",
        ],
        "markets": entries,
        "failures": failures,
    }
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    return manifest
