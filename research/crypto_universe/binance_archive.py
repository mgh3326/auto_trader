"""Binance public spot archive (data.binance.vision): symbols, months, daily bars.

The archive keeps every pair that ever traded, including delisted ones, which
is why it is the source for the survivorship-free universe. Each monthly zip
is admitted only after its published ``.CHECKSUM`` (SHA-256) matches. Verified
zips are cached so an interrupted rebuild resumes without refetching.

Spot archive timestamps switched from milliseconds to microseconds on
2025-01-01; :func:`parse_kline_csv` normalises both.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .public_http import Fetch

S3_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ARCHIVE_BASE = "https://data.binance.vision"
MONTHLY_KLINES_PREFIX = "data/spot/monthly/klines/"
INTERVAL = "1d"
_MS_UPPER_BOUND = 10**14  # epoch ms stays below this until year 5138


class ArchiveError(RuntimeError):
    """A listing, checksum or archive file did not meet the contract."""


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: str
    high: str
    low: str
    close: str
    volume: str
    quote_volume: str
    trades: int


def _get(fetch: Fetch, url: str) -> bytes:
    resp = fetch(url)
    if resp.status != 200:
        raise ArchiveError(f"HTTP {resp.status} for {url}")
    return resp.body


def _s3_list(
    fetch: Fetch, prefix: str, *, delimiter: bool
) -> tuple[list[str], list[str]]:
    """All common prefixes and keys under ``prefix`` (follows pagination)."""
    prefixes: list[str] = []
    keys: list[str] = []
    marker = ""
    while True:
        query = {"prefix": prefix}
        if delimiter:
            query["delimiter"] = "/"
        if marker:
            query["marker"] = marker
        xml = _get(fetch, S3_LIST_URL + "?" + urllib.parse.urlencode(query)).decode()
        page_prefixes = re.findall(r"<Prefix>([^<]+)</Prefix>", xml)
        page_prefixes = [p for p in page_prefixes if p != prefix]
        page_keys = re.findall(r"<Key>([^<]+)</Key>", xml)
        prefixes.extend(page_prefixes)
        keys.extend(page_keys)
        if "<IsTruncated>true</IsTruncated>" not in xml:
            return prefixes, keys
        next_marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", xml)
        last = (page_keys or page_prefixes or [""])[-1]
        marker = next_marker.group(1) if next_marker else last
        if not marker:
            raise ArchiveError(f"truncated S3 listing without a marker: {prefix}")


def list_archive_symbols(fetch: Fetch, quote: str = "USDT") -> list[str]:
    """Every spot symbol with a monthly klines folder whose name ends in ``quote``."""
    prefixes, _ = _s3_list(fetch, MONTHLY_KLINES_PREFIX, delimiter=True)
    symbols = sorted({p[len(MONTHLY_KLINES_PREFIX) :].rstrip("/") for p in prefixes})
    return [s for s in symbols if s.endswith(quote) and len(s) > len(quote)]


def list_months(fetch: Fetch, symbol: str) -> list[str]:
    """``YYYY-MM`` months that have a published 1d monthly zip for ``symbol``."""
    prefix = f"{MONTHLY_KLINES_PREFIX}{symbol}/{INTERVAL}/"
    _, keys = _s3_list(fetch, prefix, delimiter=False)
    pattern = re.compile(
        re.escape(prefix) + re.escape(f"{symbol}-{INTERVAL}-") + r"(\d{4}-\d{2})\.zip$"
    )
    return sorted({m.group(1) for k in keys if (m := pattern.match(k))})


def month_zip_url(symbol: str, month: str) -> str:
    """Archive URL; the symbol is percent-encoded (some are not ASCII)."""
    quoted = urllib.parse.quote(symbol, safe="")
    return (
        f"{ARCHIVE_BASE}/{MONTHLY_KLINES_PREFIX}{quoted}/{INTERVAL}/"
        f"{quoted}-{INTERVAL}-{month}.zip"
    )


def _to_utc_day(open_time: int) -> date:
    seconds = (
        open_time / 1_000_000 if open_time >= _MS_UPPER_BOUND else open_time / 1000
    )
    return datetime.fromtimestamp(seconds, tz=UTC).date()


def parse_kline_csv(text: str) -> list[DailyBar]:
    """Parse one archive CSV (header row optional) into daily bars."""
    bars: list[DailyBar] = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if not row[0].strip().isdigit():
            if bars:
                raise ArchiveError(f"non-numeric row after data: {row[:2]}")
            continue  # header
        if len(row) < 9 or not row[8].strip().isdigit():
            raise ArchiveError(f"malformed kline row: {row[:9]}")
        bars.append(
            DailyBar(
                day=_to_utc_day(int(row[0])),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
                quote_volume=row[7],
                trades=int(row[8]),
            )
        )
    return bars


def _verify_and_read(zip_bytes: bytes, checksum_text: str, name: str) -> str:
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


def _check_month(bars: list[DailyBar], month: str, name: str) -> list[DailyBar]:
    wrong = [b.day for b in bars if b.day.strftime("%Y-%m") != month]
    if wrong:
        raise ArchiveError(f"{name}: bars outside {month}: {wrong[:3]}")
    return bars


def _write_atomic(path: Path, body: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(path)


def fetch_month(
    fetch: Fetch, symbol: str, month: str, cache_dir: Path | None = None
) -> list[DailyBar]:
    """Checksum-verified daily bars of one symbol-month (cached when possible).

    A cached pair that no longer verifies (an interrupted earlier write) is
    discarded and fetched again, never trusted and never fatal by itself.
    """
    url = month_zip_url(symbol, month)
    name = f"{symbol}-{INTERVAL}-{month}.zip"
    cached_zip = cached_sum = None
    if cache_dir is not None:
        folder = cache_dir / "binance_spot" / INTERVAL / symbol
        cached_zip, cached_sum = folder / name, folder / f"{name}.CHECKSUM"
        if cached_zip.is_file() and cached_sum.is_file():
            try:
                text = _verify_and_read(
                    cached_zip.read_bytes(), cached_sum.read_text(), name
                )
                return _check_month(parse_kline_csv(text), month, name)
            except (ArchiveError, UnicodeDecodeError):
                cached_zip.unlink(missing_ok=True)
                cached_sum.unlink(missing_ok=True)
    checksum = _get(fetch, url + ".CHECKSUM").decode()
    body = _get(fetch, url)
    text = _verify_and_read(body, checksum, name)
    bars = _check_month(parse_kline_csv(text), month, name)
    if cached_zip is not None and cached_sum is not None:
        cached_zip.parent.mkdir(parents=True, exist_ok=True)
        # both files are written atomically; the zip is the commit point
        _write_atomic(cached_sum, checksum.encode())
        _write_atomic(cached_zip, body)
    return bars
