"""ROB-1059 H1 (spec §14.1/AC1) — Binance PUBLIC SPOT archive fetch + REST backfill.

Primary source is the Binance public archive (monthly, falling back to daily
zips for a month the monthly archive doesn't cover yet); the published
per-file SHA-256 checksum is verified before any archive's bytes are admitted
(reusing ``rob941_archive_fetch.fetch_verified_archive`` — imported, not
forked; that module is FUTURES-market-specific in its URL builders, but its
checksum-fetch/extract/``ArchiveProvenance`` primitives are market-agnostic).

Only an archive-UNCOVERED range (a day with neither a monthly nor a daily
archive available, e.g. the still-forming current month) may be backfilled
from the unauthenticated ``data-api.binance.vision`` klines REST endpoint —
those rows carry no archive checksum, so ``ShardRow.source`` distinguishes
them (``"backfill_rest"``) from checksum-verified archive rows
(``"archive_monthly"``/``"archive_daily"``) everywhere downstream.

Every network call goes through an injectable ``Opener``/``RestOpener`` so the
default test suite is network-0 (fixture-driven); the real ``urllib_opener``/
``rest_urllib_opener`` are only exercised by an opt-in live test, mirroring
``rob941_archive_fetch``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

import rob941_archive_fetch as af
import rob941_kline_schema as ks

SPOT_BASE = "https://data.binance.vision/data/spot"
REST_BASE = "https://data-api.binance.vision/api/v3/klines"
REST_MAX_LIMIT = 1000

# Re-exported for composition convenience (single canonical definitions).
ArchiveMissingError = af.ArchiveMissingError
ChecksumMissingError = af.ChecksumMissingError
ChecksumMismatchError = af.ChecksumMismatchError
CorruptArchiveError = af.CorruptArchiveError
ArchiveProvenance = af.ArchiveProvenance
Opener = af.Opener
urllib_opener = af.urllib_opener

RestOpener = Callable[[str], "bytes | None"]

__all__ = [
    "REST_MAX_LIMIT",
    "REST_BASE",
    "SPOT_BASE",
    "ArchiveMissingError",
    "ArchiveProvenance",
    "ChecksumMismatchError",
    "ChecksumMissingError",
    "CorruptArchiveError",
    "MalformedRestResponseError",
    "Opener",
    "RestOpener",
    "fetch_rest_klines",
    "rest_klines_url",
    "rest_urllib_opener",
    "spot_kline_daily_url",
    "spot_kline_monthly_url",
    "urllib_opener",
]


class MalformedRestResponseError(ValueError):
    """The REST klines JSON response is not the expected array-of-arrays shape."""


def spot_kline_monthly_url(symbol: str, interval: str, year: int, month: int) -> str:
    stem = f"{symbol}-{interval}-{year:04d}-{month:02d}"
    return f"{SPOT_BASE}/monthly/klines/{symbol}/{interval}/{stem}.zip"


def spot_kline_daily_url(
    symbol: str, interval: str, year: int, month: int, day: int
) -> str:
    stem = f"{symbol}-{interval}-{year:04d}-{month:02d}-{day:02d}"
    return f"{SPOT_BASE}/daily/klines/{symbol}/{interval}/{stem}.zip"


def rest_klines_url(
    symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = REST_MAX_LIMIT
) -> str:
    if limit <= 0 or limit > REST_MAX_LIMIT:
        raise ValueError(f"limit must be in (0, {REST_MAX_LIMIT}]")
    return (
        f"{REST_BASE}?symbol={symbol}&interval={interval}"
        f"&startTime={start_ms}&endTime={end_ms}&limit={limit}"
    )


def rest_urllib_opener(url: str, timeout: int = 30) -> bytes | None:
    """Real network opener for the REST klines endpoint. ``None`` for a 404;
    raises otherwise -- mirrors ``rob941_archive_fetch.urllib_opener``."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (public data-api.binance.vision only)
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def fetch_rest_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    opener: RestOpener,
) -> list[ks.NormalizedKline]:
    """Backfill klines from the unauthenticated REST endpoint for an
    archive-uncovered ``[start_ms, end_ms)`` range. NEVER checksum-verified
    (no archive sidecar exists for this path) — callers must record the
    distinct ``"backfill_rest"`` source so this is never confused with a
    checksum-verified archive row downstream (§14.1/AC1/AC3).
    """
    url = rest_klines_url(symbol, interval, start_ms, end_ms)
    raw = opener(url)
    if raw is None:
        raise ArchiveMissingError(f"REST klines endpoint returned 404: {url}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedRestResponseError(
            f"REST klines response is not JSON: {exc}"
        ) from exc
    if not isinstance(payload, list):
        raise MalformedRestResponseError(
            f"REST klines response must be a JSON array, got {type(payload).__name__}"
        )
    rows: list[ks.NormalizedKline] = []
    for entry in payload:
        if not isinstance(entry, list) or len(entry) < 11:
            raise MalformedRestResponseError(
                f"REST klines entry must be an array of >=11 fields, got {entry!r}"
            )
        fields = [str(v) for v in entry]
        row = ks.parse_kline_row(symbol, fields)
        if not (start_ms <= row.open_time_ms < end_ms):
            continue
        rows.append(row)
    return rows


def utc_day_range(day_start_ms: int) -> tuple[int, int]:
    """``[day_start_ms, day_start_ms + 1 day)`` — a small convenience used when
    iterating one calendar day of REST backfill at a time."""
    return day_start_ms, day_start_ms + 86_400_000


def month_bounds_ms(year: int, month: int) -> tuple[int, int]:
    """Half-open ``[month_start_ms, month_end_ms)`` UTC epoch-ms bounds for a
    calendar month -- used to enumerate the days a monthly archive would have
    covered, for daily-archive/REST fallback."""
    start = datetime(year, month, 1, tzinfo=UTC)
    end = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def days_in_month(year: int, month: int) -> list[tuple[int, int, int]]:
    """``(year, month, day)`` for every calendar day in the month."""
    start_ms, end_ms = month_bounds_ms(year, month)
    out = []
    t = start_ms
    while t < end_ms:
        dt = datetime.fromtimestamp(t / 1000, tz=UTC)
        out.append((dt.year, dt.month, dt.day))
        t += 86_400_000
    return out
