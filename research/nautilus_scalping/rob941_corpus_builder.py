"""ROB-941 — corpus builder orchestration: fetch -> checksum -> extract -> normalize
-> gap-detect, per symbol, across the frozen 12-month window.

Pure orchestration over the fetch/schema/gaps layers; the ``opener`` is
injectable so the default test suite never touches the network (see
``tests/test_rob941_corpus_builder.py``). The live path (``build_rob941_corpus.py``,
operator-gated behind ``--run``) passes the real ``rob941_archive_fetch.urllib_opener``.

Zero broker/order/fill/scheduler wiring and zero DB access by construction — this
module imports only the ``rob941_*`` data layer plus the existing pure
``funding_oi_archive`` parser (enforced structurally by
``tests/test_rob941_corpus_builder.py::test_corpus_builder_module_has_no_broker_order_or_db_imports``
and by the shared ``test_pit_data_layer_guard.py`` no-``app``-import guard).
"""

from __future__ import annotations

from collections.abc import Callable

import rob941_archive_fetch as af
import rob941_frozen_scope as frozen
import rob941_gaps as gaps
import rob941_kline_schema as ks
from funding_oi_archive import FundingRow, parse_funding_csv

# ROB-941 R1 I1 remediation: an injectable sink so the live materialize path
# (build_rob941_corpus.py) can persist each checksum-verified raw archive as it
# is fetched, without making this pure fetch/normalize orchestration layer
# itself do disk I/O (and without breaking its existing network-0 fixture
# tests, which never pass a sink -- default ``None`` keeps prior memory-only
# behavior byte-for-byte). ``(symbol, kind, year, month, zip_bytes) ->
# artifact-root-relative POSIX path``.
RawArchiveSink = Callable[[str, str, int, int, bytes], str]


def build_symbol_kline_shard(
    symbol: str,
    interval: str = "1m",
    opener: af.Opener = af.urllib_opener,
    raw_archive_sink: RawArchiveSink | None = None,
) -> tuple[list[ks.NormalizedKline], list[af.ArchiveProvenance], list[tuple[int, int]]]:
    """Fetch+verify+normalize every frozen-window monthly kline archive for
    ``symbol``, merging months into one sorted, deduped, gap-accounted shard.

    A checksum mismatch/missing archive/corrupt ZIP/invalid OHLCV/conflicting
    duplicate anywhere in the window aborts the WHOLE symbol build (fail-closed;
    no partial silent corpus).
    """
    merged: dict[int, ks.NormalizedKline] = {}
    provenance: list[af.ArchiveProvenance] = []
    for year, month in frozen.months_in_window():
        url = af.kline_archive_url(symbol, interval, year, month)
        fetched = af.fetch_verified_archive(url, opener=opener)
        csv_text = af.extract_single_csv(fetched.zip_bytes)
        month_rows = ks.parse_kline_csv(
            symbol, csv_text, frozen.WINDOW_START_MS, frozen.WINDOW_END_MS
        )
        for row in month_rows:
            existing = merged.get(row.open_time_ms)
            if existing is not None and existing != row:
                raise ks.ConflictingDuplicateError(
                    f"{symbol}@{row.open_time_ms}: conflicting duplicate rows across monthly archives"
                )
            merged[row.open_time_ms] = row
        local_path = (
            raw_archive_sink(symbol, "klines", year, month, fetched.zip_bytes)
            if raw_archive_sink is not None
            else None
        )
        provenance.append(fetched.provenance(local_path=local_path))

    ordered = [merged[t] for t in sorted(merged)]
    gap_ranges = gaps.detect_gap_ranges(
        [r.open_time_ms for r in ordered], frozen.WINDOW_START_MS, frozen.WINDOW_END_MS
    )
    return ordered, provenance, gap_ranges


def build_symbol_funding_shard(
    symbol: str,
    opener: af.Opener = af.urllib_opener,
    raw_archive_sink: RawArchiveSink | None = None,
) -> tuple[list[FundingRow], list[af.ArchiveProvenance]]:
    """Fetch+verify+normalize every frozen-window monthly fundingRate archive
    for ``symbol``, merging months into one sorted, deduped, window-clipped shard."""
    merged: dict[int, FundingRow] = {}
    provenance: list[af.ArchiveProvenance] = []
    for year, month in frozen.months_in_window():
        url = af.funding_archive_url(symbol, year, month)
        fetched = af.fetch_verified_archive(url, opener=opener)
        csv_text = af.extract_single_csv(fetched.zip_bytes)
        for row in parse_funding_csv(csv_text):
            if not (frozen.WINDOW_START_MS <= row.calc_time < frozen.WINDOW_END_MS):
                continue
            existing = merged.get(row.calc_time)
            if existing is not None and existing != row:
                raise ks.ConflictingDuplicateError(
                    f"{symbol}@{row.calc_time}: conflicting duplicate funding rows across monthly archives"
                )
            merged[row.calc_time] = row
        local_path = (
            raw_archive_sink(symbol, "fundingRate", year, month, fetched.zip_bytes)
            if raw_archive_sink is not None
            else None
        )
        provenance.append(fetched.provenance(local_path=local_path))

    ordered = [merged[t] for t in sorted(merged)]
    return ordered, provenance
