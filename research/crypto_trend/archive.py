"""Binance USD-M monthly kline archive: URLs, checksum-verified zips, parsing.

A monthly zip is admitted only after its published ``.CHECKSUM`` (SHA-256)
matches the downloaded bytes. Verified pairs are cached under the artifact
root (outside git) so a rerun does not refetch. Timestamps are normalised
from milliseconds or microseconds to epoch milliseconds.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .http import Fetch

ARCHIVE_BASE = "https://data.binance.vision"
KLINE_PREFIX = "data/futures/um/monthly/klines/"
_MS_UPPER_BOUND = 10**14  # epoch ms stays below this until year 5138


class ArchiveError(RuntimeError):
    """A checksum, zip or CSV did not meet the contract."""


@dataclass(frozen=True)
class Bar:
    open_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class FetchedMonth:
    bars: list[Bar]
    zip_sha256: str
    from_cache: bool


def month_list(start: str, end_exclusive: str) -> list[str]:
    """``YYYY-MM`` strings from ``start`` (inclusive) to ``end_exclusive``."""
    y, m = (int(x) for x in start.split("-"))
    end_y, end_m = (int(x) for x in end_exclusive.split("-"))
    months: list[str] = []
    while (y, m) < (end_y, end_m):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months


def month_bounds_ms(month: str) -> tuple[int, int]:
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1, tzinfo=UTC)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = datetime(ny, nm, 1, tzinfo=UTC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def zip_name(symbol: str, interval: str, month: str) -> str:
    return f"{symbol}-{interval}-{month}.zip"


def zip_url(symbol: str, interval: str, month: str) -> str:
    return (
        f"{ARCHIVE_BASE}/{KLINE_PREFIX}{symbol}/{interval}/"
        f"{zip_name(symbol, interval, month)}"
    )


def _to_ms(raw: int) -> int:
    if raw >= _MS_UPPER_BOUND:
        if raw % 1000:
            raise ArchiveError(f"sub-millisecond open time: {raw}")
        return raw // 1000
    return raw


def parse_kline_csv(text: str) -> list[Bar]:
    """Parse one archive CSV (a header row is optional) into bars."""
    bars: list[Bar] = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if not row[0].strip().isdigit():
            if bars:
                raise ArchiveError(f"non-numeric row after data: {row[:2]}")
            continue  # header
        if len(row) < 5:
            raise ArchiveError(f"malformed kline row: {row[:5]}")
        try:
            prices = [Decimal(x) for x in row[1:5]]
        except InvalidOperation as exc:
            raise ArchiveError(f"non-decimal price in row: {row[:5]}") from exc
        if any(not p.is_finite() or p <= 0 for p in prices):
            raise ArchiveError(f"non-positive or non-finite price: {row[:5]}")
        open_, high, low, close = prices
        bars.append(
            Bar(
                open_ms=_to_ms(int(row[0])), open=open_, high=high, low=low, close=close
            )
        )
    for prev, cur in zip(bars, bars[1:], strict=False):
        if cur.open_ms <= prev.open_ms:
            raise ArchiveError("kline open times are not strictly increasing")
    return bars


def verify_zip(zip_bytes: bytes, checksum_text: str, name: str) -> str:
    """Return the single CSV inside a zip whose SHA-256 matches the checksum."""
    tokens = checksum_text.split()
    if not tokens:
        raise ArchiveError(f"empty checksum for {name}")
    if hashlib.sha256(zip_bytes).hexdigest() != tokens[0].strip().lower():
        raise ArchiveError(f"checksum mismatch for {name}")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            members = [m for m in archive.namelist() if m.endswith(".csv")]
            if len(members) != 1:
                raise ArchiveError(f"{name}: expected one CSV, got {members}")
            return archive.read(members[0]).decode()
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"corrupt zip {name}") from exc


def _check_month(bars: list[Bar], month: str, name: str) -> list[Bar]:
    lo, hi = month_bounds_ms(month)
    outside = [b.open_ms for b in bars if not lo <= b.open_ms < hi]
    if outside:
        raise ArchiveError(f"{name}: bars outside {month}: {outside[:3]}")
    return bars


def _cache_paths(cache_dir: Path, symbol: str, interval: str, month: str):
    folder = cache_dir / "futures_um" / interval / symbol
    name = zip_name(symbol, interval, month)
    return folder / name, folder / f"{name}.CHECKSUM"


def fetch_month(
    fetch: Fetch | None,
    cache_dir: Path,
    symbol: str,
    interval: str,
    month: str,
) -> FetchedMonth:
    """Checksum-verified bars of one symbol-interval-month.

    With ``fetch=None`` the month must already be cached; nothing is downloaded.
    A cached pair that no longer verifies is discarded and refetched (or
    raises when there is no fetcher), never silently trusted.
    """
    name = zip_name(symbol, interval, month)
    zip_path, sum_path = _cache_paths(cache_dir, symbol, interval, month)
    if zip_path.is_file() and sum_path.is_file():
        try:
            body = zip_path.read_bytes()
            text = verify_zip(body, sum_path.read_text(), name)
            bars = _check_month(parse_kline_csv(text), month, name)
            return FetchedMonth(bars, hashlib.sha256(body).hexdigest(), True)
        except (ArchiveError, UnicodeDecodeError):
            zip_path.unlink(missing_ok=True)
            sum_path.unlink(missing_ok=True)
    if fetch is None:
        raise ArchiveError(f"not cached and no fetcher: {symbol} {interval} {month}")
    url = zip_url(symbol, interval, month)
    checksum = _get(fetch, url + ".CHECKSUM").decode()
    body = _get(fetch, url)
    text = verify_zip(body, checksum, name)
    bars = _check_month(parse_kline_csv(text), month, name)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(zip_path, body)
    _write_atomic(sum_path, checksum.encode())
    return FetchedMonth(bars, hashlib.sha256(body).hexdigest(), False)


def _get(fetch: Fetch, url: str) -> bytes:
    resp = fetch(url)
    if resp.status != 200:
        raise ArchiveError(f"HTTP {resp.status} for {url}")
    return resp.body


def _write_atomic(path: Path, body: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(path)
