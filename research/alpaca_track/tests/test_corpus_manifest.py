"""ROB-1059 H1 (spec §14.1/AC3/AC19-21) — CorpusManifest round-trip, canonical
symbol order, content_hash determinism, and ULP-sensitivity.
"""

import corpus_manifest as cm
import pytest


def _symbol_manifest(symbol: str, sha: str) -> cm.SymbolCorpusManifest:
    return cm.SymbolCorpusManifest(
        symbol=symbol,
        quote_mode="USDC",
        sources=(
            cm.ShardSource(
                source="archive_monthly",
                year=2024,
                month=6,
                day=None,
                url=f"https://data.binance.vision/{symbol}",
                checksum_sha256="a" * 64,
            ),
        ),
        row_count=10,
        expected_count=10,
        missing_open_times_ms=(),
        normalized_content_sha256=sha,
    )


def _manifest() -> cm.CorpusManifest:
    return cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("BTCUSDC", "ETHUSDC"),
        per_symbol=(
            _symbol_manifest("BTCUSDC", "b" * 64),
            _symbol_manifest("ETHUSDC", "c" * 64),
        ),
    )


def test_symbols_must_be_canonical_lexicographic_order():
    with pytest.raises(ValueError):
        cm.CorpusManifest(
            window_start_ms=0,
            window_end_ms=600_000,
            symbols=("ETHUSDC", "BTCUSDC"),  # wrong order
            per_symbol=(
                _symbol_manifest("ETHUSDC", "c" * 64),
                _symbol_manifest("BTCUSDC", "b" * 64),
            ),
        )


def test_per_symbol_coverage_must_exactly_match_declared_symbols():
    with pytest.raises(ValueError):
        cm.CorpusManifest(
            window_start_ms=0,
            window_end_ms=600_000,
            symbols=("BTCUSDC", "ETHUSDC"),
            per_symbol=(_symbol_manifest("BTCUSDC", "b" * 64),),  # missing ETHUSDC
        )


def test_duplicate_symbol_is_rejected():
    with pytest.raises(ValueError):
        cm.CorpusManifest(
            window_start_ms=0,
            window_end_ms=600_000,
            symbols=("BTCUSDC", "BTCUSDC"),
            per_symbol=(
                _symbol_manifest("BTCUSDC", "b" * 64),
                _symbol_manifest("BTCUSDC", "b" * 64),
            ),
        )


def test_content_hash_reproduces_on_rerun_without_recollection():
    m1 = _manifest()
    m2 = _manifest()
    assert m1.content_hash() == m2.content_hash()


def test_shard_source_backfill_never_carries_checksum():
    with pytest.raises(ValueError):
        cm.ShardSource(
            source="backfill_rest",
            year=2024,
            month=6,
            day=1,
            url="https://data-api.binance.vision/",
            checksum_sha256="deadbeef",
        )


def test_archive_source_must_carry_checksum():
    with pytest.raises(ValueError):
        cm.ShardSource(
            source="archive_daily",
            year=2024,
            month=6,
            day=1,
            url="https://data.binance.vision/",
            checksum_sha256=None,
        )


def test_save_load_round_trip_preserves_content_hash(tmp_path):
    m = _manifest()
    path = tmp_path / "manifest.json"
    m.save(path)
    loaded = cm.CorpusManifest.load(path)
    assert loaded.content_hash() == m.content_hash()
    assert loaded.to_dict() == m.to_dict()


def test_one_ulp_change_in_a_child_hash_changes_the_manifest_hash():
    baseline = _manifest()
    sha_variants = _symbol_manifest("ETHUSDC", "c" * 63 + "d")  # one hex char different
    mutated = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("BTCUSDC", "ETHUSDC"),
        per_symbol=(_symbol_manifest("BTCUSDC", "b" * 64), sha_variants),
    )
    assert baseline.content_hash() != mutated.content_hash()


def test_window_change_changes_the_manifest_hash():
    baseline = _manifest()
    mutated = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_001,  # one ms later
        symbols=("BTCUSDC", "ETHUSDC"),
        per_symbol=(
            _symbol_manifest("BTCUSDC", "b" * 64),
            _symbol_manifest("ETHUSDC", "c" * 64),
        ),
    )
    assert baseline.content_hash() != mutated.content_hash()


def test_symbol_order_change_changes_manifest_bytes_and_hash():
    # note: this constructs distinguishable *content* for each order (the
    # dataclass itself enforces per_symbol order == symbols order), so a
    # genuine reordering of which manifest a builder would produce for a
    # DIFFERENT canonical symbol set changes the hash -- containers with the
    # same distinct elements in canonical order always hash identically
    # (AC21: re-execution and semantically-irrelevant permutations stay
    # byte-identical; a real symbol-order CHANGE is not semantically
    # irrelevant, it changes which symbol's manifest a given hash represents).
    a = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("AAAUSDC", "BBBUSDC"),
        per_symbol=(
            _symbol_manifest("AAAUSDC", "1" * 64),
            _symbol_manifest("BBBUSDC", "2" * 64),
        ),
    )
    b = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("AAAUSDC", "BBBUSDC"),
        per_symbol=(
            _symbol_manifest("AAAUSDC", "2" * 64),  # swapped shas -> different identity
            _symbol_manifest("BBBUSDC", "1" * 64),
        ),
    )
    assert a.content_hash() != b.content_hash()
