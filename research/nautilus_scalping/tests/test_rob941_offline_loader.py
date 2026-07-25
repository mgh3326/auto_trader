"""ROB-941 R1 I1 remediation — offline, network-0 loader for the persisted
corpus. The fail-closed verification chain (per-shard: raw archive
path/existence/SHA-256 -> shard path traversal -> file existence -> file
SHA-256 -> EXACT Parquet schema [names+types+nullability+order] -> row
canonical hash -> row count/min/max; corpus-level: exact symbol coverage,
window/universe, per-row window membership, gap_ranges recomputation) is
exercised end to end with ZERO network access: every test here uses only
``rob941_persistence`` writes to a ``tmp_path`` artifact root and
``rob941_offline_loader`` reads back from it. No ``opener``/urllib import is
ever touched by this module or these tests.
"""

import dataclasses
import hashlib

import canonical_hash
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rob941_frozen_scope as scope
import rob941_gaps as gaps
import rob941_kline_schema as ks
import rob941_offline_loader as loader
import rob941_persistence as persist
from funding_oi_archive import FundingRow
from rob941_manifest import (
    ArchiveProvenance,
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)


def _kline(open_time_ms: int, symbol: str = "XRPUSDT") -> ks.NormalizedKline:
    return ks.NormalizedKline(
        symbol=symbol,
        open_time_ms=open_time_ms,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        base_volume=10.0,
        close_time_ms=open_time_ms + 59_999,
        quote_volume=1000.0,
        trade_count=5,
        taker_buy_volume=4.0,
        taker_buy_quote_volume=400.0,
    )


def _funding(calc_time: int) -> FundingRow:
    return FundingRow(
        calc_time=calc_time, funding_interval_hours=8, last_funding_rate=0.0001
    )


def _write_archive(
    tmp_path,
    symbol: str,
    kind: str = "klines",
    year: int = 2025,
    month: int = 7,
    content: bytes = b"fake-verified-archive-bytes",
) -> ArchiveProvenance:
    """A REAL persisted raw archive (arbitrary bytes -- verify_archives only
    checks the physical SHA-256, it never parses the ZIP), with a matching
    verified checksum -- exercises the exact same content-addressed write path
    ``build_rob941_corpus.py`` uses."""
    rel = persist.write_raw_archive(tmp_path, symbol, kind, year, month, content)
    checksum = hashlib.sha256(content).hexdigest()
    return ArchiveProvenance(
        url=f"https://data.binance.vision/data/futures/um/monthly/{kind}/{symbol}/{symbol}-{kind}-{year:04d}-{month:02d}.zip",
        checksum_url=f"https://data.binance.vision/data/futures/um/monthly/{kind}/{symbol}/{symbol}-{kind}-{year:04d}-{month:02d}.zip.CHECKSUM",
        checksum_sha256=checksum,
        local_path=rel,
    )


def _kline_manifest_from_written_shard(
    tmp_path, symbol: str, rows: list[ks.NormalizedKline], archives=None, gap_ranges=()
) -> SymbolKlineManifest:
    if archives is None:
        archives = (_write_archive(tmp_path, symbol, "klines"),)
    rel_path, file_sha256 = persist.write_kline_shard(tmp_path, symbol, rows)
    return SymbolKlineManifest(
        symbol=symbol,
        interval="1m",
        archives=tuple(archives),
        normalized_shard_sha256=canonical_hash.canonical_sha256(
            [r.__dict__ for r in rows]
        ),
        shard_path=rel_path,
        shard_file_sha256=file_sha256,
        row_count=len(rows),
        min_open_time_ms=rows[0].open_time_ms,
        max_open_time_ms=rows[-1].open_time_ms,
        gap_ranges=tuple(gap_ranges),
    )


def _funding_manifest_from_written_shard(
    tmp_path, symbol: str, rows: list[FundingRow], archives=None
) -> SymbolFundingManifest:
    if archives is None:
        archives = (_write_archive(tmp_path, symbol, "fundingRate"),)
    rel_path, file_sha256 = persist.write_funding_shard(tmp_path, symbol, rows)
    return SymbolFundingManifest(
        symbol=symbol,
        archives=tuple(archives),
        normalized_shard_sha256=canonical_hash.canonical_sha256(
            [r.__dict__ for r in rows]
        ),
        shard_path=rel_path,
        shard_file_sha256=file_sha256,
        row_count=len(rows),
        min_calc_time_ms=rows[0].calc_time,
        max_calc_time_ms=rows[-1].calc_time,
    )


def _kline_gap_ranges(rows: list[ks.NormalizedKline]) -> tuple:
    return tuple(
        gaps.detect_gap_ranges(
            [r.open_time_ms for r in rows], scope.WINDOW_START_MS, scope.WINDOW_END_MS
        )
    )


# --------------------------------------------------------------------------- #
# happy path: fully offline round trip
# --------------------------------------------------------------------------- #
def test_load_kline_shard_offline_reproduces_written_rows(tmp_path):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(5)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    loaded = loader.load_kline_shard(manifest, tmp_path)
    assert loaded == rows


def test_load_funding_shard_offline_reproduces_written_rows(tmp_path):
    rows = [_funding(1751328000000), _funding(1751356800000)]
    manifest = _funding_manifest_from_written_shard(tmp_path, "DOGEUSDT", rows)
    loaded = loader.load_funding_shard(manifest, tmp_path)
    assert loaded == rows


# --------------------------------------------------------------------------- #
# fail-closed chain: raw archive verification (captain review follow-up)
# --------------------------------------------------------------------------- #
def test_load_kline_shard_raises_when_archive_local_path_is_none(tmp_path):
    rows = [_kline(1751328000000)]
    unmaterialized_archive = ArchiveProvenance(
        url="https://data.binance.vision/x.zip",
        checksum_url="https://data.binance.vision/x.zip.CHECKSUM",
        checksum_sha256="a" * 64,
        local_path=None,
    )
    manifest = _kline_manifest_from_written_shard(
        tmp_path, "XRPUSDT", rows, archives=(unmaterialized_archive,)
    )
    with pytest.raises(loader.ArchiveFileMissingError):
        loader.load_kline_shard(manifest, tmp_path)


def test_load_kline_shard_raises_when_archive_file_missing_on_disk(tmp_path):
    rows = [_kline(1751328000000)]
    archive = _write_archive(tmp_path, "XRPUSDT")
    (tmp_path / archive.local_path).unlink()
    manifest = _kline_manifest_from_written_shard(
        tmp_path, "XRPUSDT", rows, archives=(archive,)
    )
    with pytest.raises(loader.ArchiveFileMissingError):
        loader.load_kline_shard(manifest, tmp_path)


def test_load_kline_shard_raises_when_archive_bytes_tampered(tmp_path):
    rows = [_kline(1751328000000)]
    archive = _write_archive(tmp_path, "XRPUSDT")
    path = tmp_path / archive.local_path
    path.write_bytes(path.read_bytes() + b"\x00")
    manifest = _kline_manifest_from_written_shard(
        tmp_path, "XRPUSDT", rows, archives=(archive,)
    )
    with pytest.raises(loader.ArchiveFileTamperedError):
        loader.load_kline_shard(manifest, tmp_path)


def test_load_kline_shard_raises_when_archive_local_path_escapes_artifact_root(
    tmp_path,
):
    rows = [_kline(1751328000000)]
    archive = _write_archive(tmp_path, "XRPUSDT")
    escaped = dataclasses.replace(archive, local_path="../../outside.zip")
    manifest = _kline_manifest_from_written_shard(
        tmp_path, "XRPUSDT", rows, archives=(escaped,)
    )
    with pytest.raises(loader.ShardPathEscapesArtifactRootError):
        loader.load_kline_shard(manifest, tmp_path)


def test_load_funding_shard_raises_when_archive_bytes_tampered(tmp_path):
    rows = [_funding(1751328000000)]
    archive = _write_archive(tmp_path, "DOGEUSDT", "fundingRate")
    path = tmp_path / archive.local_path
    path.write_bytes(b"totally different bytes")
    manifest = _funding_manifest_from_written_shard(
        tmp_path, "DOGEUSDT", rows, archives=(archive,)
    )
    with pytest.raises(loader.ArchiveFileTamperedError):
        loader.load_funding_shard(manifest, tmp_path)


# --------------------------------------------------------------------------- #
# fail-closed chain: path traversal (shard)
# --------------------------------------------------------------------------- #
def test_load_kline_shard_rejects_shard_path_escaping_artifact_root(tmp_path):
    rows = [_kline(1751328000000)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    escaped = dataclasses.replace(manifest, shard_path="../outside.parquet")
    with pytest.raises(loader.ShardPathEscapesArtifactRootError):
        loader.load_kline_shard(escaped, tmp_path)


def test_load_kline_shard_rejects_absolute_shard_path(tmp_path):
    rows = [_kline(1751328000000)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    escaped = dataclasses.replace(manifest, shard_path="/etc/passwd")
    with pytest.raises(loader.ShardPathEscapesArtifactRootError):
        loader.load_kline_shard(escaped, tmp_path)


# --------------------------------------------------------------------------- #
# fail-closed chain: file existence
# --------------------------------------------------------------------------- #
def test_load_kline_shard_raises_when_shard_path_is_none(tmp_path):
    rows = [_kline(1751328000000)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    unmaterialized = dataclasses.replace(manifest, shard_path=None)
    with pytest.raises(loader.ShardFileMissingError):
        loader.load_kline_shard(unmaterialized, tmp_path)


def test_load_kline_shard_raises_when_file_absent_on_disk(tmp_path):
    rows = [_kline(1751328000000)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    (tmp_path / manifest.shard_path).unlink()
    with pytest.raises(loader.ShardFileMissingError):
        loader.load_kline_shard(manifest, tmp_path)


# --------------------------------------------------------------------------- #
# fail-closed chain: physical file SHA-256 (tamper detection)
# --------------------------------------------------------------------------- #
def test_load_kline_shard_raises_when_file_bytes_tampered(tmp_path):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(3)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    path = tmp_path / manifest.shard_path
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(loader.ShardFileTamperedError):
        loader.load_kline_shard(manifest, tmp_path)


# --------------------------------------------------------------------------- #
# fail-closed chain: EXACT schema (names + types + nullability + order)
# --------------------------------------------------------------------------- #
def test_load_kline_shard_raises_on_schema_mismatch(tmp_path):
    rows = [_kline(1751328000000)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    path = tmp_path / manifest.shard_path
    # overwrite with a structurally different (but validly-formed) parquet file
    bad_table = pa.table({"only_one_column": [1, 2, 3]})
    pq.write_table(bad_table, path)
    bad_manifest = dataclasses.replace(
        manifest, shard_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    with pytest.raises(loader.ShardSchemaMismatchError):
        loader.load_kline_shard(bad_manifest, tmp_path)


def test_load_kline_shard_raises_on_schema_type_mismatch_even_with_matching_column_names(
    tmp_path,
):
    rows = [_kline(1751328000000)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    path = tmp_path / manifest.shard_path
    # SAME column names/order as the pinned schema, but open_time_ms is int32
    # instead of int64 -- a names-only check would miss this.
    wrong_type_schema = pa.schema(
        [
            pa.field(name, pa.int32() if name == "open_time_ms" else field.type)
            for name, field in zip(
                persist.KLINE_COLUMN_ORDER, persist.KLINE_SCHEMA, strict=True
            )
        ]
    )
    columns = {
        "symbol": ["XRPUSDT"],
        "open_time_ms": [1751328000000 % (2**31)],
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.5],
        "base_volume": [10.0],
        "close_time_ms": [1751328059999],
        "quote_volume": [1000.0],
        "trade_count": [5],
        "taker_buy_volume": [4.0],
        "taker_buy_quote_volume": [400.0],
    }
    pq.write_table(pa.table(columns, schema=wrong_type_schema), path)
    bad_manifest = dataclasses.replace(
        manifest, shard_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    with pytest.raises(loader.ShardSchemaMismatchError):
        loader.load_kline_shard(bad_manifest, tmp_path)


# --------------------------------------------------------------------------- #
# fail-closed chain: canonical row content hash
# --------------------------------------------------------------------------- #
def test_load_kline_shard_raises_when_declared_row_hash_does_not_match_file_content(
    tmp_path,
):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(3)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    # file bytes are intact (file_sha256 still correct) but the manifest's
    # declared logical-content hash has drifted from what's actually on disk
    corrupted = dataclasses.replace(manifest, normalized_shard_sha256="f" * 64)
    with pytest.raises(loader.ShardContentTamperedError):
        loader.load_kline_shard(corrupted, tmp_path)


# --------------------------------------------------------------------------- #
# fail-closed chain: row count / min / max (independent metadata fields)
# --------------------------------------------------------------------------- #
def test_load_kline_shard_raises_on_row_count_mismatch(tmp_path):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(5)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    corrupted = dataclasses.replace(manifest, row_count=999)
    with pytest.raises(loader.ShardRowCountMismatchError):
        loader.load_kline_shard(corrupted, tmp_path)


def test_load_kline_shard_raises_on_min_open_time_mismatch(tmp_path):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(5)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    corrupted = dataclasses.replace(manifest, min_open_time_ms=1)
    with pytest.raises(loader.ShardTimeRangeMismatchError):
        loader.load_kline_shard(corrupted, tmp_path)


def test_load_kline_shard_raises_on_max_open_time_mismatch(tmp_path):
    rows = [_kline(1751328000000 + i * 60_000) for i in range(5)]
    manifest = _kline_manifest_from_written_shard(tmp_path, "XRPUSDT", rows)
    corrupted = dataclasses.replace(manifest, max_open_time_ms=99_999_999_999)
    with pytest.raises(loader.ShardTimeRangeMismatchError):
        loader.load_kline_shard(corrupted, tmp_path)


def test_load_funding_shard_raises_on_row_count_mismatch(tmp_path):
    rows = [_funding(1751328000000), _funding(1751356800000)]
    manifest = _funding_manifest_from_written_shard(tmp_path, "DOGEUSDT", rows)
    corrupted = dataclasses.replace(manifest, row_count=999)
    with pytest.raises(loader.ShardRowCountMismatchError):
        loader.load_funding_shard(corrupted, tmp_path)


# --------------------------------------------------------------------------- #
# corpus-level: window/universe (validate_frozen_scope) + all-4-symbol offline load
# --------------------------------------------------------------------------- #
def _full_corpus_manifest(tmp_path) -> CorpusManifest:
    klines = []
    funding = []
    for idx, symbol in enumerate(scope.UNIVERSE):
        krows = [
            _kline(scope.WINDOW_START_MS + (idx * 10 + i) * 60_000, symbol=symbol)
            for i in range(5)
        ]
        klines.append(
            _kline_manifest_from_written_shard(
                tmp_path, symbol, krows, gap_ranges=_kline_gap_ranges(krows)
            )
        )
        frows = [_funding(scope.WINDOW_START_MS + idx * 8 * 3_600_000)]
        funding.append(_funding_manifest_from_written_shard(tmp_path, symbol, frows))
    eligibility = tuple(
        SymbolEligibility(symbol=s, **scope.eligibility(s)) for s in scope.UNIVERSE
    )
    return CorpusManifest(
        window_start_iso=scope.WINDOW_START_ISO,
        window_end_iso=scope.WINDOW_END_ISO,
        universe=scope.UNIVERSE,
        eligibility=eligibility,
        klines=tuple(klines),
        funding=tuple(funding),
    )


def test_load_corpus_offline_loads_all_four_symbols_with_zero_network(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    result = loader.load_corpus(manifest, tmp_path)
    assert set(result["klines"]) == set(scope.UNIVERSE)
    assert set(result["funding"]) == set(scope.UNIVERSE)
    for symbol in scope.UNIVERSE:
        assert len(result["klines"][symbol]) == 5
        assert len(result["funding"][symbol]) == 1


def test_load_corpus_rejects_tampered_window(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    tampered = dataclasses.replace(manifest, window_start_iso="2020-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="window"):
        loader.load_corpus(tampered, tmp_path)


def test_load_corpus_round_trip_from_saved_manifest_json_is_fully_offline(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest.save(manifest_path)
    reloaded_manifest = CorpusManifest.load(manifest_path)
    result = loader.load_corpus(reloaded_manifest, tmp_path)
    assert set(result["klines"]) == set(scope.UNIVERSE)


# --------------------------------------------------------------------------- #
# corpus-level (captain review follow-up): exact symbol coverage
# --------------------------------------------------------------------------- #
def test_load_corpus_raises_on_duplicate_kline_symbol(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    tampered = dataclasses.replace(
        manifest, klines=manifest.klines + (manifest.klines[0],)
    )
    with pytest.raises(ValueError, match="klines"):
        loader.load_corpus(tampered, tmp_path)


def test_load_corpus_raises_on_missing_funding_symbol(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    tampered = dataclasses.replace(manifest, funding=manifest.funding[:-1])
    with pytest.raises(ValueError, match="funding"):
        loader.load_corpus(tampered, tmp_path)


# --------------------------------------------------------------------------- #
# corpus-level (captain review follow-up): every row inside the frozen window
# --------------------------------------------------------------------------- #
def test_load_corpus_raises_when_a_kline_row_lies_outside_the_frozen_window(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    symbol = scope.UNIVERSE[0]
    out_of_window_ms = scope.WINDOW_START_MS - 60_000  # one minute BEFORE the window
    bad_rows = [_kline(out_of_window_ms, symbol=symbol)]
    bad_kline_manifest = _kline_manifest_from_written_shard(
        tmp_path, symbol, bad_rows, gap_ranges=()
    )
    tampered_klines = tuple(
        bad_kline_manifest if k.symbol == symbol else k for k in manifest.klines
    )
    tampered = dataclasses.replace(manifest, klines=tampered_klines)
    with pytest.raises(loader.ShardWindowViolationError):
        loader.load_corpus(tampered, tmp_path)


def test_load_corpus_raises_when_a_funding_row_lies_outside_the_frozen_window(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    symbol = scope.UNIVERSE[0]
    out_of_window_ms = scope.WINDOW_END_MS + 3_600_000  # one hour AFTER the window
    bad_rows = [_funding(out_of_window_ms)]
    bad_funding_manifest = _funding_manifest_from_written_shard(
        tmp_path, symbol, bad_rows
    )
    tampered_funding = tuple(
        bad_funding_manifest if f.symbol == symbol else f for f in manifest.funding
    )
    tampered = dataclasses.replace(manifest, funding=tampered_funding)
    with pytest.raises(loader.ShardWindowViolationError):
        loader.load_corpus(tampered, tmp_path)


# --------------------------------------------------------------------------- #
# corpus-level (captain review follow-up): gap_ranges recomputation
# --------------------------------------------------------------------------- #
def test_load_corpus_raises_when_declared_gap_ranges_do_not_match_recomputed(tmp_path):
    manifest = _full_corpus_manifest(tmp_path)
    symbol = scope.UNIVERSE[0]
    wrong_gap_kline = dataclasses.replace(
        next(k for k in manifest.klines if k.symbol == symbol), gap_ranges=()
    )
    tampered_klines = tuple(
        wrong_gap_kline if k.symbol == symbol else k for k in manifest.klines
    )
    tampered = dataclasses.replace(manifest, klines=tampered_klines)
    with pytest.raises(loader.ShardGapRangesMismatchError):
        loader.load_corpus(tampered, tmp_path)


def test_load_corpus_accepts_correctly_recomputed_gap_ranges_with_a_real_gap(tmp_path):
    # 5 bars with the 3rd minute skipped -> one genuine 1-minute gap
    symbol = "XRPUSDT"
    rows = [
        _kline(scope.WINDOW_START_MS + i * 60_000, symbol=symbol) for i in (0, 1, 3, 4)
    ]
    kline_manifest = _kline_manifest_from_written_shard(
        tmp_path, symbol, rows, gap_ranges=_kline_gap_ranges(rows)
    )
    assert kline_manifest.gap_ranges  # sanity: a real gap was recorded
    frows = [_funding(scope.WINDOW_START_MS)]
    funding_manifest = _funding_manifest_from_written_shard(tmp_path, symbol, frows)
    eligibility = tuple(
        SymbolEligibility(symbol=s, **scope.eligibility(s)) for s in scope.UNIVERSE
    )
    other_klines = []
    other_funding = []
    for other_symbol in scope.UNIVERSE:
        if other_symbol == symbol:
            continue
        orows = [_kline(scope.WINDOW_START_MS, symbol=other_symbol)]
        other_klines.append(
            _kline_manifest_from_written_shard(
                tmp_path, other_symbol, orows, gap_ranges=_kline_gap_ranges(orows)
            )
        )
        ofrows = [_funding(scope.WINDOW_START_MS)]
        other_funding.append(
            _funding_manifest_from_written_shard(tmp_path, other_symbol, ofrows)
        )
    manifest = CorpusManifest(
        window_start_iso=scope.WINDOW_START_ISO,
        window_end_iso=scope.WINDOW_END_ISO,
        universe=scope.UNIVERSE,
        eligibility=eligibility,
        klines=(kline_manifest, *other_klines),
        funding=(funding_manifest, *other_funding),
    )
    result = loader.load_corpus(manifest, tmp_path)
    assert len(result["klines"][symbol]) == 4


def test_offline_loader_module_never_imports_urllib_or_network_openers():
    import ast
    from pathlib import Path

    src = Path(loader.__file__).read_text()
    tree = ast.parse(src)
    forbidden = {"urllib", "requests", "httpx", "socket"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [n.name.split(".")[0] for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert not (set(names) & forbidden), (
            f"offline loader must never import network modules: {names}"
        )
