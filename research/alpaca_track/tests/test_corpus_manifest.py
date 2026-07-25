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
    with pytest.raises(ValueError, match="canonical lexicographic order"):
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
    with pytest.raises(ValueError, match="per_symbol coverage"):
        cm.CorpusManifest(
            window_start_ms=0,
            window_end_ms=600_000,
            symbols=("BTCUSDC", "ETHUSDC"),
            per_symbol=(_symbol_manifest("BTCUSDC", "b" * 64),),  # missing ETHUSDC
        )


def test_duplicate_symbol_is_rejected():
    with pytest.raises(ValueError, match="duplicate symbol in manifest"):
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
    with pytest.raises(ValueError, match="must NOT carry a checksum"):
        cm.ShardSource(
            source="backfill_rest",
            year=2024,
            month=6,
            day=1,
            url="https://data-api.binance.vision/",
            checksum_sha256="deadbeef",
        )


def test_archive_source_must_carry_checksum():
    with pytest.raises(ValueError, match="must carry a verified checksum"):
        cm.ShardSource(
            source="archive_daily",
            year=2024,
            month=6,
            day=1,
            url="https://data.binance.vision/",
            checksum_sha256=None,
        )


def test_shard_source_rejects_a_source_value_outside_the_literal_set():
    # CodeRabbit fix: previously `source` itself was never validated -- any
    # string outside ("archive_monthly", "archive_daily") and outside
    # "backfill_rest" satisfied NEITHER checksum branch and was silently
    # accepted with NO checksum constraint enforced at all (fail-open), even
    # WITH a checksum present (so the old branches alone can't catch this).
    with pytest.raises(ValueError, match="unknown ShardSource.source"):
        cm.ShardSource(
            source="totally_bogus_source",  # not in SourceLiteral
            year=2024,
            month=6,
            day=1,
            url="https://data.binance.vision/",
            checksum_sha256="a" * 64,  # present -- old code had NO check to fail
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


# --------------------------------------------------------------------------- #
# S5 remediation: type discipline (bool/int-subclass rejection) matching
# daily_bars.py/pit_universe_alpaca.py's _int/_float, previously entirely
# absent from this module.
# --------------------------------------------------------------------------- #
def test_symbol_corpus_manifest_rejects_bool_row_count():
    with pytest.raises(TypeError):
        cm.SymbolCorpusManifest(
            symbol="BTCUSDC",
            quote_mode="USDC",
            sources=(),
            row_count=True,
            expected_count=10,
            missing_open_times_ms=(),
            normalized_content_sha256="a" * 64,
        )


def test_symbol_corpus_manifest_rejects_bool_expected_count():
    with pytest.raises(TypeError):
        cm.SymbolCorpusManifest(
            symbol="BTCUSDC",
            quote_mode="USDC",
            sources=(),
            row_count=10,
            expected_count=False,
            missing_open_times_ms=(),
            normalized_content_sha256="a" * 64,
        )


def test_corpus_manifest_rejects_bool_window_start_ms():
    with pytest.raises(TypeError):
        cm.CorpusManifest(
            window_start_ms=False,
            window_end_ms=600_000,
            symbols=("BTCUSDC",),
            per_symbol=(_symbol_manifest("BTCUSDC", "b" * 64),),
        )


def test_symbol_corpus_manifest_rejects_inconsistent_row_and_missing_counts():
    # CodeRabbit fix: this manifest is the canonical, hashed identity
    # `persistence.load_symbol_shard`'s `expected_row_count` relies on -- a
    # hand-crafted/corrupted manifest whose `row_count` + missing-minute count
    # doesn't add up to `expected_count` must never construct silently.
    with pytest.raises(
        ValueError, match=r"row_count \+ len\(missing_open_times_ms\)"
    ):
        cm.SymbolCorpusManifest(
            symbol="BTCUSDC",
            quote_mode="USDC",
            sources=(),
            row_count=8,
            expected_count=10,
            missing_open_times_ms=(),  # should carry 2 entries, has 0
            normalized_content_sha256="a" * 64,
        )


def test_corpus_manifest_rejects_bool_window_end_ms():
    with pytest.raises(TypeError):
        cm.CorpusManifest(
            window_start_ms=0,
            window_end_ms=True,
            symbols=("BTCUSDC",),
            per_symbol=(_symbol_manifest("BTCUSDC", "b" * 64),),
        )


# --------------------------------------------------------------------------- #
# S1/AC7 remediation: usdcusdt_basis_drift_flags field round-trip + canonical
# order + hash sensitivity.
# --------------------------------------------------------------------------- #
def test_usdcusdt_basis_drift_flags_default_empty_and_round_trips(tmp_path):
    manifest = _symbol_manifest("BATUSDT", "a" * 64)
    assert manifest.usdcusdt_basis_drift_flags == ()
    d = manifest.to_dict()
    assert d["usdcusdt_basis_drift_flags"] == []
    assert cm.SymbolCorpusManifest.from_dict(d) == manifest


def test_usdcusdt_basis_drift_flags_round_trip_and_order_enforced():
    manifest = cm.SymbolCorpusManifest(
        symbol="BATUSDT",
        quote_mode="USDT_PROXY",
        sources=(),
        row_count=2,
        expected_count=2,
        missing_open_times_ms=(),
        normalized_content_sha256="a" * 64,
        usdcusdt_basis_drift_flags=(("2024-06-01", False), ("2024-06-02", True)),
    )
    d = manifest.to_dict()
    assert d["usdcusdt_basis_drift_flags"] == [
        ["2024-06-01", False],
        ["2024-06-02", True],
    ]
    loaded = cm.SymbolCorpusManifest.from_dict(d)
    assert loaded == manifest

    with pytest.raises(ValueError, match="canonical ascending-by-date order"):
        cm.SymbolCorpusManifest(
            symbol="BATUSDT",
            quote_mode="USDT_PROXY",
            sources=(),
            row_count=2,
            expected_count=2,
            missing_open_times_ms=(),
            normalized_content_sha256="a" * 64,
            usdcusdt_basis_drift_flags=(
                ("2024-06-02", True),
                ("2024-06-01", False),
            ),  # out of order
        )
    with pytest.raises(TypeError):
        cm.SymbolCorpusManifest(
            symbol="BATUSDT",
            quote_mode="USDT_PROXY",
            sources=(),
            row_count=2,
            expected_count=2,
            missing_open_times_ms=(),
            normalized_content_sha256="a" * 64,
            usdcusdt_basis_drift_flags=(("2024-06-01", 1),),  # int, not bool
        )


def test_flipping_a_basis_drift_flag_changes_the_manifest_hash():
    base_symbol = cm.SymbolCorpusManifest(
        symbol="BATUSDT",
        quote_mode="USDT_PROXY",
        sources=(),
        row_count=2,
        expected_count=2,
        missing_open_times_ms=(),
        normalized_content_sha256="a" * 64,
        usdcusdt_basis_drift_flags=(("2024-06-01", False),),
    )
    flipped_symbol = cm.SymbolCorpusManifest(
        symbol="BATUSDT",
        quote_mode="USDT_PROXY",
        sources=(),
        row_count=2,
        expected_count=2,
        missing_open_times_ms=(),
        normalized_content_sha256="a" * 64,
        usdcusdt_basis_drift_flags=(("2024-06-01", True),),
    )
    baseline = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("BATUSDT",),
        per_symbol=(base_symbol,),
    )
    mutated = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("BATUSDT",),
        per_symbol=(flipped_symbol,),
    )
    assert baseline.content_hash() != mutated.content_hash()


def test_generator_version_and_schema_version_are_part_of_the_hash_input():
    # AC21 remediation: a manifest that varies ONLY generator_version (or
    # ONLY schema_version) must change the content_hash -- both fields must
    # be part of the hashed identity, never silently dropped from
    # to_dict()/content_hash(). No prior test ever varied these fields (every
    # _manifest() call used the defaults), so this mutation previously
    # survived undetected.
    baseline = _manifest()
    mutated_generator = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("BTCUSDC", "ETHUSDC"),
        per_symbol=(
            _symbol_manifest("BTCUSDC", "b" * 64),
            _symbol_manifest("ETHUSDC", "c" * 64),
        ),
        generator_version="different_generator_version",
    )
    assert baseline.content_hash() != mutated_generator.content_hash()

    mutated_schema = cm.CorpusManifest(
        window_start_ms=0,
        window_end_ms=600_000,
        symbols=("BTCUSDC", "ETHUSDC"),
        per_symbol=(
            _symbol_manifest("BTCUSDC", "b" * 64),
            _symbol_manifest("ETHUSDC", "c" * 64),
        ),
        schema_version="different_schema_version",
    )
    assert baseline.content_hash() != mutated_schema.content_hash()


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
