"""ROB-941 (AC2/AC3/AC6) — the immutable corpus manifest.

Covers: all 4 symbols share one frozen window, BTC's fixed eligibility record,
tamper detection (``validate_frozen_scope``), content-hash determinism (same
inputs -> same hash; any field change -> different hash), and a save/load
round-trip that reproduces an identical hash (JSONB-style round-trip safety,
same discipline as ``research_contracts.canonical_hash``).
"""

import pytest
import rob941_archive_fetch as af
import rob941_frozen_scope as scope
from rob941_manifest import (
    ArchiveProvenance,
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)


def test_archive_provenance_is_the_single_canonical_class_not_a_duplicate():
    # regression guard: a live full-corpus run once broke here because
    # rob941_manifest defined its OWN ArchiveProvenance (no .to_dict()) while
    # rob941_corpus_builder actually produces rob941_archive_fetch.ArchiveProvenance
    # instances -> SymbolKlineManifest.to_dict() blew up on real (non-fixture)
    # objects even though every isolated unit test passed. Re-export, don't
    # redefine, so this class of bug is structurally impossible.
    assert ArchiveProvenance is af.ArchiveProvenance


def _prov(n=1):
    return tuple(
        ArchiveProvenance(
            url=f"https://data.binance.vision/data/futures/um/monthly/klines/X/1m/X-1m-2025-0{i}.zip",
            checksum_url=f"https://data.binance.vision/data/futures/um/monthly/klines/X/1m/X-1m-2025-0{i}.zip.CHECKSUM",
            checksum_sha256="a" * 64,
        )
        for i in range(1, n + 1)
    )


def _kline_manifest(symbol: str) -> SymbolKlineManifest:
    return SymbolKlineManifest(
        symbol=symbol,
        interval="1m",
        archives=_prov(),
        normalized_shard_sha256="b" * 64,
        row_count=44640,
        min_open_time_ms=scope.WINDOW_START_MS,
        max_open_time_ms=scope.WINDOW_END_MS - 60_000,
        gap_ranges=(),
    )


def _funding_manifest(symbol: str) -> SymbolFundingManifest:
    return SymbolFundingManifest(
        symbol=symbol,
        archives=_prov(),
        normalized_shard_sha256="c" * 64,
        row_count=90,
        min_calc_time_ms=scope.WINDOW_START_MS,
        max_calc_time_ms=scope.WINDOW_END_MS - 1000,
    )


def _eligibility_records():
    return tuple(
        SymbolEligibility(symbol=s, **scope.eligibility(s)) for s in scope.UNIVERSE
    )


def _manifest() -> CorpusManifest:
    return CorpusManifest(
        window_start_iso=scope.WINDOW_START_ISO,
        window_end_iso=scope.WINDOW_END_ISO,
        universe=scope.UNIVERSE,
        eligibility=_eligibility_records(),
        klines=tuple(_kline_manifest(s) for s in scope.UNIVERSE),
        funding=tuple(_funding_manifest(s) for s in scope.UNIVERSE),
    )


def test_manifest_covers_exactly_the_frozen_four_symbol_universe():
    m = _manifest()
    assert m.universe == ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")
    assert {k.symbol for k in m.klines} == set(m.universe)
    assert {f.symbol for f in m.funding} == set(m.universe)


def test_manifest_all_symbols_share_one_frozen_window():
    m = _manifest()
    # window is a single top-level field (not per-symbol) -> structurally one window
    assert m.window_start_iso == "2025-07-01T00:00:00Z"
    assert m.window_end_iso == "2026-07-01T00:00:00Z"


def test_manifest_btc_eligibility_is_exactly_the_frozen_record():
    m = _manifest()
    btc = next(e for e in m.eligibility if e.symbol == "BTCUSDT")
    assert btc.historical_only is True
    assert btc.demo_execution_eligible is False
    assert btc.reason == "min_notional_50_exceeds_demo_cap_10"


def test_manifest_non_btc_eligibility_is_demo_ready():
    m = _manifest()
    for e in m.eligibility:
        if e.symbol == "BTCUSDT":
            continue
        assert e.historical_only is False
        assert e.demo_execution_eligible is True
        assert e.reason is None


def test_validate_frozen_scope_accepts_conforming_manifest():
    _manifest().validate_frozen_scope()  # must not raise


def test_validate_frozen_scope_rejects_tampered_window():
    m = _manifest()
    tampered = CorpusManifest(
        window_start_iso="2020-01-01T00:00:00Z",
        window_end_iso=m.window_end_iso,
        universe=m.universe,
        eligibility=m.eligibility,
        klines=m.klines,
        funding=m.funding,
    )
    with pytest.raises(ValueError, match="window"):
        tampered.validate_frozen_scope()


def test_validate_frozen_scope_rejects_duplicate_kline_symbol():
    m = _manifest()
    tampered = CorpusManifest(
        window_start_iso=m.window_start_iso,
        window_end_iso=m.window_end_iso,
        universe=m.universe,
        eligibility=m.eligibility,
        klines=m.klines + (m.klines[0],),  # duplicate first symbol
        funding=m.funding,
    )
    with pytest.raises(ValueError, match="klines"):
        tampered.validate_frozen_scope()


def test_validate_frozen_scope_rejects_missing_kline_symbol():
    m = _manifest()
    tampered = CorpusManifest(
        window_start_iso=m.window_start_iso,
        window_end_iso=m.window_end_iso,
        universe=m.universe,
        eligibility=m.eligibility,
        klines=m.klines[:-1],  # drop the last symbol's kline manifest
        funding=m.funding,
    )
    with pytest.raises(ValueError, match="klines"):
        tampered.validate_frozen_scope()


def test_validate_frozen_scope_rejects_missing_funding_symbol():
    m = _manifest()
    tampered = CorpusManifest(
        window_start_iso=m.window_start_iso,
        window_end_iso=m.window_end_iso,
        universe=m.universe,
        eligibility=m.eligibility,
        klines=m.klines,
        funding=m.funding[:-1],
    )
    with pytest.raises(ValueError, match="funding"):
        tampered.validate_frozen_scope()


def test_validate_frozen_scope_rejects_tampered_btc_eligibility():
    m = _manifest()
    bad_eligibility = tuple(
        SymbolEligibility(
            symbol="BTCUSDT",
            historical_only=False,
            demo_execution_eligible=True,
            reason=None,
        )
        if e.symbol == "BTCUSDT"
        else e
        for e in m.eligibility
    )
    tampered = CorpusManifest(
        window_start_iso=m.window_start_iso,
        window_end_iso=m.window_end_iso,
        universe=m.universe,
        eligibility=bad_eligibility,
        klines=m.klines,
        funding=m.funding,
    )
    with pytest.raises(ValueError, match="BTCUSDT"):
        tampered.validate_frozen_scope()


# --------------------------------------------------------------------------- #
# content hash: deterministic identity, changes on any field mutation
# --------------------------------------------------------------------------- #
def test_content_hash_is_deterministic_across_rebuilds():
    assert _manifest().content_hash() == _manifest().content_hash()


def test_content_hash_changes_when_a_gap_range_is_added():
    m1 = _manifest()
    m2_klines = tuple(
        SymbolKlineManifest(
            symbol=k.symbol,
            interval=k.interval,
            archives=k.archives,
            normalized_shard_sha256=k.normalized_shard_sha256,
            row_count=k.row_count,
            min_open_time_ms=k.min_open_time_ms,
            max_open_time_ms=k.max_open_time_ms,
            gap_ranges=((scope.WINDOW_START_MS, scope.WINDOW_START_MS + 60_000),)
            if k.symbol == "BTCUSDT"
            else k.gap_ranges,
        )
        for k in m1.klines
    )
    m2 = CorpusManifest(
        window_start_iso=m1.window_start_iso,
        window_end_iso=m1.window_end_iso,
        universe=m1.universe,
        eligibility=m1.eligibility,
        klines=m2_klines,
        funding=m1.funding,
    )
    assert m1.content_hash() != m2.content_hash()


def test_manifest_save_load_round_trip_preserves_content_hash(tmp_path):
    m = _manifest()
    path = tmp_path / "manifest.json"
    m.save(path)
    loaded = CorpusManifest.load(path)
    assert loaded.content_hash() == m.content_hash()
    loaded.validate_frozen_scope()


# --------------------------------------------------------------------------- #
# R1 I1 remediation: persisted-shard fields (offline load requires these)
# --------------------------------------------------------------------------- #
def test_archive_provenance_local_path_defaults_to_none_and_round_trips():
    p = ArchiveProvenance(
        url="https://data.binance.vision/x.zip",
        checksum_url="https://data.binance.vision/x.zip.CHECKSUM",
        checksum_sha256="a" * 64,
    )
    assert p.local_path is None
    assert ArchiveProvenance.from_dict(p.to_dict()).local_path is None

    p2 = ArchiveProvenance(
        url="https://data.binance.vision/x.zip",
        checksum_url="https://data.binance.vision/x.zip.CHECKSUM",
        checksum_sha256="a" * 64,
        local_path="rob941/raw/klines/X/x.zip",
    )
    assert (
        ArchiveProvenance.from_dict(p2.to_dict()).local_path
        == "rob941/raw/klines/X/x.zip"
    )


def test_archive_provenance_from_dict_defaults_missing_local_path_to_none():
    # backward compat: an older persisted manifest without local_path must load
    d = {
        "url": "https://data.binance.vision/x.zip",
        "checksum_url": "https://data.binance.vision/x.zip.CHECKSUM",
        "checksum_sha256": "a" * 64,
    }
    assert ArchiveProvenance.from_dict(d).local_path is None


def test_symbol_kline_manifest_shard_fields_default_to_none_and_round_trip():
    k = _kline_manifest("XRPUSDT")
    assert k.shard_path is None
    assert k.shard_file_sha256 is None
    assert SymbolKlineManifest.from_dict(k.to_dict()).shard_path is None

    k2 = SymbolKlineManifest(
        symbol="XRPUSDT",
        interval="1m",
        archives=_prov(),
        normalized_shard_sha256="b" * 64,
        shard_path="rob941/shards/klines/XRPUSDT-1m.parquet",
        shard_file_sha256="d" * 64,
        row_count=44640,
        min_open_time_ms=scope.WINDOW_START_MS,
        max_open_time_ms=scope.WINDOW_END_MS - 60_000,
        gap_ranges=(),
    )
    reloaded = SymbolKlineManifest.from_dict(k2.to_dict())
    assert reloaded.shard_path == "rob941/shards/klines/XRPUSDT-1m.parquet"
    assert reloaded.shard_file_sha256 == "d" * 64


def test_symbol_funding_manifest_shard_fields_default_to_none_and_round_trip():
    f = _funding_manifest("XRPUSDT")
    assert f.shard_path is None
    assert f.shard_file_sha256 is None

    f2 = SymbolFundingManifest(
        symbol="XRPUSDT",
        archives=_prov(),
        normalized_shard_sha256="c" * 64,
        shard_path="rob941/shards/funding/XRPUSDT-fundingRate.parquet",
        shard_file_sha256="e" * 64,
        row_count=90,
        min_calc_time_ms=scope.WINDOW_START_MS,
        max_calc_time_ms=scope.WINDOW_END_MS - 1000,
    )
    reloaded = SymbolFundingManifest.from_dict(f2.to_dict())
    assert reloaded.shard_path == "rob941/shards/funding/XRPUSDT-fundingRate.parquet"
    assert reloaded.shard_file_sha256 == "e" * 64


def test_content_hash_changes_when_shard_path_is_added():
    m1 = _manifest()
    m2_klines = tuple(
        SymbolKlineManifest(
            symbol=k.symbol,
            interval=k.interval,
            archives=k.archives,
            normalized_shard_sha256=k.normalized_shard_sha256,
            shard_path="rob941/shards/klines/foo.parquet"
            if k.symbol == "BTCUSDT"
            else k.shard_path,
            shard_file_sha256=k.shard_file_sha256,
            row_count=k.row_count,
            min_open_time_ms=k.min_open_time_ms,
            max_open_time_ms=k.max_open_time_ms,
            gap_ranges=k.gap_ranges,
        )
        for k in m1.klines
    )
    m2 = CorpusManifest(
        window_start_iso=m1.window_start_iso,
        window_end_iso=m1.window_end_iso,
        universe=m1.universe,
        eligibility=m1.eligibility,
        klines=m2_klines,
        funding=m1.funding,
    )
    assert m1.content_hash() != m2.content_hash()
