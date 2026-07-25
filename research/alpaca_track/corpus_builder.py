"""ROB-1059 H1 (spec §14.1/AC1/AC3) — spot corpus builder orchestration.

Per symbol, per calendar month in the requested half-open window: try the
monthly archive first (checksum-verified); if it 404s, fall back to daily
archives for each day of that month still inside the window (each
checksum-verified independently); if a specific day ALSO has no archive
(e.g. the still-forming current month), backfill ONLY that day from the
unauthenticated REST klines endpoint — never checksum-verified, and recorded
with a distinct ``source`` so it is never confused with an archive row.

Zero broker/order/fill/scheduler/DB wiring by construction (mirrors
``rob941_corpus_builder``): this module imports only the local
``spot_archive_fetch``/``corpus_manifest`` layer plus ``rob941_archive_fetch``/
``rob941_gaps``/``rob941_kline_schema`` (composition, not a fork) and stdlib.
Every opener is injectable so the default test suite is network-0.
"""

from __future__ import annotations

from datetime import UTC, datetime

import canonical_hash
import rob941_archive_fetch as af
import rob941_gaps as gaps
import rob941_kline_schema as ks
import spot_archive_fetch as saf
from corpus_manifest import ShardSource, SymbolCorpusManifest

MINUTE_MS = 60_000
DAY_MS = 86_400_000


def build_symbol_corpus(
    symbol: str,
    quote_mode: str,
    window_start_ms: int,
    window_end_ms: int,
    *,
    interval: str = "1m",
    archive_opener: saf.Opener = saf.urllib_opener,
    rest_opener: saf.RestOpener = saf.rest_urllib_opener,
) -> tuple[list[ks.NormalizedKline], SymbolCorpusManifest]:
    """Fetch+verify+normalize every month in ``[window_start_ms, window_end_ms)``
    for ``symbol``, falling back month->day->REST-backfill on 404s. A checksum
    mismatch/corrupt ZIP/invalid OHLCV/conflicting duplicate anywhere aborts
    the whole symbol build (fail-closed; no partial silent corpus) — the SAME
    discipline as ``rob941_corpus_builder.build_symbol_kline_shard``.
    """
    if window_end_ms <= window_start_ms:
        raise ValueError("window_end_ms must be after window_start_ms")

    merged: dict[int, ks.NormalizedKline] = {}
    sources: list[ShardSource] = []

    for year, month in _months_spanning(window_start_ms, window_end_ms):
        month_start_ms, month_end_ms = saf.month_bounds_ms(year, month)
        clip_start = max(month_start_ms, window_start_ms)
        clip_end = min(month_end_ms, window_end_ms)
        if clip_start >= clip_end:
            continue

        url = saf.spot_kline_monthly_url(symbol, interval, year, month)
        try:
            fetched = af.fetch_verified_archive(url, opener=archive_opener)
        except af.ArchiveMissingError:
            _fill_via_daily_then_rest(
                symbol,
                interval,
                year,
                month,
                clip_start,
                clip_end,
                archive_opener,
                rest_opener,
                merged,
                sources,
            )
            continue

        csv_text = af.extract_single_csv(fetched.zip_bytes)
        month_rows = ks.parse_kline_csv(symbol, csv_text, clip_start, clip_end)
        _merge_rows(symbol, merged, month_rows)
        sources.append(
            ShardSource(
                source="archive_monthly",
                year=year,
                month=month,
                day=None,
                url=url,
                checksum_sha256=fetched.checksum_sha256,
            )
        )

    ordered = [merged[t] for t in sorted(merged)]
    expected_count = (window_end_ms - window_start_ms) // MINUTE_MS
    gap_ranges = gaps.detect_gap_ranges(
        [r.open_time_ms for r in ordered], window_start_ms, window_end_ms
    )
    missing_open_times = tuple(
        t for g0, g1 in gap_ranges for t in range(g0, g1, MINUTE_MS)
    )
    content_hash = canonical_hash.canonical_sha256([r.__dict__ for r in ordered])

    manifest = SymbolCorpusManifest(
        symbol=symbol,
        quote_mode=quote_mode,
        sources=tuple(sources),
        row_count=len(ordered),
        expected_count=int(expected_count),
        missing_open_times_ms=missing_open_times,
        normalized_content_sha256=content_hash,
    )
    return ordered, manifest


def _fill_via_daily_then_rest(
    symbol: str,
    interval: str,
    year: int,
    month: int,
    clip_start: int,
    clip_end: int,
    archive_opener: saf.Opener,
    rest_opener: saf.RestOpener,
    merged: dict[int, ks.NormalizedKline],
    sources: list[ShardSource],
) -> None:
    for y, m, d in saf.days_in_month(year, month):
        day_start = _day_start_ms(y, m, d)
        day_end = day_start + DAY_MS
        c_start = max(day_start, clip_start)
        c_end = min(day_end, clip_end)
        if c_start >= c_end:
            continue

        url = saf.spot_kline_daily_url(symbol, interval, y, m, d)
        try:
            fetched = af.fetch_verified_archive(url, opener=archive_opener)
        except af.ArchiveMissingError:
            # archive-uncovered range: REST backfill ONLY, distinct source,
            # never checksum-verified (AC1).
            rows = saf.fetch_rest_klines(symbol, interval, c_start, c_end, rest_opener)
            _merge_rows(symbol, merged, rows)
            sources.append(
                ShardSource(
                    source="backfill_rest",
                    year=y,
                    month=m,
                    day=d,
                    url=saf.rest_klines_url(symbol, interval, c_start, c_end),
                    checksum_sha256=None,
                )
            )
            continue

        csv_text = af.extract_single_csv(fetched.zip_bytes)
        day_rows = ks.parse_kline_csv(symbol, csv_text, c_start, c_end)
        _merge_rows(symbol, merged, day_rows)
        sources.append(
            ShardSource(
                source="archive_daily",
                year=y,
                month=m,
                day=d,
                url=url,
                checksum_sha256=fetched.checksum_sha256,
            )
        )


def _merge_rows(
    symbol: str,
    merged: dict[int, ks.NormalizedKline],
    rows: list[ks.NormalizedKline],
) -> None:
    for row in rows:
        existing = merged.get(row.open_time_ms)
        if existing is not None and existing != row:
            raise ks.ConflictingDuplicateError(
                f"{symbol}@{row.open_time_ms}: conflicting duplicate rows across sources"
            )
        merged[row.open_time_ms] = row


def _months_spanning(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    end_dt = datetime.fromtimestamp((end_ms - 1) / 1000, tz=UTC)
    months: list[tuple[int, int]] = []
    y, m = start_dt.year, start_dt.month
    while (y, m) <= (end_dt.year, end_dt.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _day_start_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)
