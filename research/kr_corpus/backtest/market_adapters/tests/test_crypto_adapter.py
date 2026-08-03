"""Crypto golden tests: UTC 24/7, venue isolation, units, and delisting."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loader import ManifestEntry, sha256_bytes
from loader import load_shard as bare_load_shard
from market_adapters.common import ContractBackedCorpusAdapter
from market_adapters.crypto import (
    BINANCE_USDT_COST,
    CRYPTO_ADAPTER_FREQUENCY,
    CRYPTO_HOLDOUT_POLICY,
    CRYPTO_SCHEMA_CONTRACT_PATH,
    CRYPTO_STRUCTURAL_GATE_ATTACHMENT,
    QUOTE_CURRENCY_BY_VENUE,
    UPBIT_KRW_COST,
    CryptoBar,
    CryptoFrequencyMismatchError,
    CryptoPrelistingBarsUnavailable,
    CryptoVenueAdapter,
    CryptoVenueMixError,
)
from pit import LookaheadViolation, assert_no_lookahead
from schema_contract import ContractTablePolicyError

from research.crypto_corpus.policy import UnlabeledParquetError, label_table_for_venue


def _row(
    *,
    venue: str = "upbit_krw",
    open_time_utc: datetime | None = None,
    symbol: str = "KRW-NEW",
    close: float = 100.0,
    base_volume: float = 1000.0,
    quote_volume: float | None = None,
    frequency: str = "1d",
) -> dict:
    from datetime import timedelta

    open_t = open_time_utc or datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    # exclusive end: 1d → +1 day; 1h → +1 hour (for adversarial rows)
    delta = timedelta(days=1) if frequency == "1d" else timedelta(hours=1)
    close_t = open_t + delta
    return {
        "venue": venue,
        "symbol": symbol,
        "frequency": frequency,
        "bucket_timezone": "UTC",
        "open_time_utc": open_t,
        "close_time_utc": close_t,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "base_volume": base_volume,
        "quote_volume": close * base_volume if quote_volume is None else quote_volume,
        "trade_count": None,
        "source_candle_date_time_utc": open_t.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_candle_date_time_kst": None,
        "source_open_time_ms": None,
        "source_close_time_ms": None,
        "source_timestamp_ms": None,
    }


def _table(adapter: CryptoVenueAdapter, rows: list[dict]) -> pa.Table:
    """Build a labeled sealed-style table (metadata required by label gate)."""
    table = pa.Table.from_pylist(
        rows,
        schema=adapter.corpus.arrow_schema_for("ohlcv"),
    )
    return label_table_for_venue(table, adapter.venue)


def test_crypto_uses_24_7_utc_calendar_including_weekend():
    adapter = CryptoVenueAdapter("upbit_krw")
    view = adapter.view_from_table(_table(adapter, [_row()]))  # Saturday 2024-06-01
    assert view.bars[0].session_date == date(2024, 6, 1)
    assert view.bars[0].open_time_utc.date() == date(2024, 6, 1)
    assert view.bars[0].timestamp_utc == view.bars[0].open_time_utc


def test_crypto_loads_single_venue_synthetic_fixture_through_shared_sha_loader(
    tmp_path,
):
    adapter = CryptoVenueAdapter("upbit_krw")
    rel = "ohlcv/upbit_krw/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    pq.write_table(_table(adapter, [_row()]), path)
    data = path.read_bytes()
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(data),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )

    view = adapter.load_shard(tmp_path, entry)
    assert view.venue == "upbit_krw"
    assert view.bars[0].symbol == "KRW-NEW"


def test_crypto_bar_time_is_open_time_not_exclusive_close():
    adapter = CryptoVenueAdapter("upbit_krw")
    open_t = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    view = adapter.view_from_table(_table(adapter, [_row(open_time_utc=open_t)]))
    bar = view.bars[0]
    assert bar.session_date == date(2024, 1, 1)
    assert bar.close_time_utc.date() == date(2024, 1, 2)


def test_crypto_lookahead_goes_red_on_future_row():
    adapter = CryptoVenueAdapter("upbit_krw")
    view = adapter.view_from_table(
        _table(
            adapter,
            [
                _row(open_time_utc=datetime(2024, 6, 1, tzinfo=UTC)),
                _row(open_time_utc=datetime(2024, 6, 2, tzinfo=UTC)),
            ],
        )
    )
    with pytest.raises(LookaheadViolation) as exc_info:
        assert_no_lookahead(view.bars, "2024-06-01")
    assert "2024-06-02" in str(exc_info.value)


def test_crypto_venue_mix_is_exception_not_documentation_only():
    adapter = CryptoVenueAdapter("upbit_krw")
    table = _table(
        adapter,
        [
            _row(venue="upbit_krw"),
            _row(venue="binance_usdt_spot", symbol="BTCUSDT"),
        ],
    )
    with pytest.raises(CryptoVenueMixError) as exc_info:
        adapter.view_from_table(table)
    assert "cannot enter" in str(exc_info.value)


def test_crypto_manifest_entry_from_other_venue_is_rejected_before_read(tmp_path):
    adapter = CryptoVenueAdapter("upbit_krw")
    entry = ManifestEntry(
        relative_path="ohlcv/binance_usdt_spot/2024/bars.parquet",
        file_sha256="0" * 64,
        row_count=0,
        dataset="ohlcv",
        market="binance_usdt_spot",
        year=2024,
    )
    with pytest.raises(CryptoVenueMixError):
        adapter.load_shard(tmp_path, entry)


def test_crypto_short_binance_usdt_alias_is_rejected():
    """Sealed venue is binance_usdt_spot; shortened alias must not silently work."""
    with pytest.raises(CryptoVenueMixError):
        CryptoVenueAdapter("binance_usdt")  # type: ignore[arg-type]


def test_crypto_units_and_volume_mapping():
    upbit = CryptoVenueAdapter("upbit_krw")
    view = upbit.view_from_table(
        _table(upbit, [_row(base_volume=10.0, quote_volume=6500.0)])
    )
    bar = view.bars[0]
    assert bar.volume == 10.0 == bar.base_volume
    assert bar.quote_volume == 6500.0
    assert bar.quote_currency == "KRW"
    assert bar.frequency == CRYPTO_ADAPTER_FREQUENCY == "1d"
    assert (
        not hasattr(bar, "trading_value")
        or "trading_value" not in CryptoBar.__dataclass_fields__
    )
    assert QUOTE_CURRENCY_BY_VENUE["binance_usdt_spot"] == "USDT"


def test_crypto_hourly_frequency_is_exception_not_silent_accept():
    """BLOCKER-1: 1h rows must not enter the daily adapter as same-session bars."""
    adapter = CryptoVenueAdapter("upbit_krw")
    table = _table(
        adapter,
        [
            _row(
                frequency="1h",
                open_time_utc=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            ),
            _row(
                frequency="1h",
                open_time_utc=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
            ),
        ],
    )
    with pytest.raises(CryptoFrequencyMismatchError) as exc_info:
        adapter.view_from_table(table)
    assert "1h" in str(exc_info.value)
    # No empty success: exception is mandatory.
    assert "refusing" in str(exc_info.value).lower() or "not adapter" in str(
        exc_info.value
    )


def test_crypto_unlabeled_table_is_exception_on_view_from_table():
    """BLOCKER-2 (view path): stripped metadata cannot produce bars."""
    adapter = CryptoVenueAdapter("upbit_krw")
    labeled = _table(adapter, [_row()])
    stripped = labeled.replace_schema_metadata(None)
    with pytest.raises(UnlabeledParquetError):
        adapter.view_from_table(stripped)


def test_crypto_unlabeled_load_shard_is_exception(tmp_path):
    """BLOCKER-2 (load_shard path): unlabeled temp copy fails closed."""
    adapter = CryptoVenueAdapter("upbit_krw")
    rel = "ohlcv/upbit_krw/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    labeled = _table(adapter, [_row()])
    stripped = labeled.replace_schema_metadata(None)
    pq.write_table(stripped, path)
    data = path.read_bytes()
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(data),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )
    with pytest.raises(UnlabeledParquetError):
        adapter.load_shard(tmp_path, entry)


def test_crypto_corpus_load_shard_also_enforces_label_and_frequency(tmp_path):
    """Public residual corpus.load_shard cannot bypass gates either."""
    adapter = CryptoVenueAdapter("upbit_krw")
    rel = "ohlcv/upbit_krw/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)

    # unlabeled
    stripped = _table(adapter, [_row()]).replace_schema_metadata(None)
    pq.write_table(stripped, path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(path.read_bytes()),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )
    with pytest.raises(UnlabeledParquetError):
        adapter.corpus.load_shard(tmp_path, entry)

    # labeled but hourly
    hourly = _table(adapter, [_row(frequency="1h")])
    pq.write_table(hourly, path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(path.read_bytes()),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )
    with pytest.raises(ContractTablePolicyError):
        adapter.corpus.load_shard(tmp_path, entry)


def test_crypto_structural_gate_attachment_documented():
    """Gates attach to contract validation — not an exhaustive wrapper list."""
    assert any("validate_table_schema" in s for s in CRYPTO_STRUCTURAL_GATE_ATTACHMENT)
    assert any("loader.load_shard" in s for s in CRYPTO_STRUCTURAL_GATE_ATTACHMENT)
    assert any(
        "ContractBackedCorpusAdapter" in s for s in CRYPTO_STRUCTURAL_GATE_ATTACHMENT
    )


def test_r3_unwrapped_contract_adapter_refuses_unlabeled_1h(tmp_path):
    """BLOCKER-R3-1: bare ContractBackedCorpusAdapter + crypto contract must gate."""
    adapter = CryptoVenueAdapter("upbit_krw")
    rel = "ohlcv/upbit_krw/2024/attack.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    # labeled then strip — unlabeled 1h
    hourly = _table(adapter, [_row(frequency="1h")]).replace_schema_metadata(None)
    pq.write_table(hourly, path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(path.read_bytes()),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )
    bare = ContractBackedCorpusAdapter(
        contract_path=CRYPTO_SCHEMA_CONTRACT_PATH,
        holdout_policy=CRYPTO_HOLDOUT_POLICY,
    )
    with pytest.raises(UnlabeledParquetError):
        bare.load_shard(tmp_path, entry)


def test_r3_bare_loader_load_shard_refuses_unlabeled_1h(tmp_path):
    """BLOCKER-R3-1: loader.load_shard with crypto contract must also gate."""
    adapter = CryptoVenueAdapter("upbit_krw")
    rel = "ohlcv/upbit_krw/2024/attack.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    hourly = _table(adapter, [_row(frequency="1h")]).replace_schema_metadata(None)
    pq.write_table(hourly, path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(path.read_bytes()),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )
    with pytest.raises(UnlabeledParquetError):
        bare_load_shard(
            tmp_path,
            entry,
            contract_path=CRYPTO_SCHEMA_CONTRACT_PATH,
            holdout_policy=CRYPTO_HOLDOUT_POLICY,
        )


def test_r3_bare_paths_refuse_labeled_hourly(tmp_path):
    """Labeled 1h still fails frequency policy on unwrapped paths."""
    adapter = CryptoVenueAdapter("upbit_krw")
    rel = "ohlcv/upbit_krw/2024/h1.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    pq.write_table(_table(adapter, [_row(frequency="1h")]), path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(path.read_bytes()),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )
    bare = ContractBackedCorpusAdapter(
        contract_path=CRYPTO_SCHEMA_CONTRACT_PATH,
        holdout_policy=CRYPTO_HOLDOUT_POLICY,
    )
    with pytest.raises(ContractTablePolicyError):
        bare.load_shard(tmp_path, entry)
    with pytest.raises(ContractTablePolicyError):
        bare_load_shard(
            tmp_path,
            entry,
            contract_path=CRYPTO_SCHEMA_CONTRACT_PATH,
            holdout_policy=CRYPTO_HOLDOUT_POLICY,
        )


def test_crypto_costs_are_venue_specific_and_bidirectional():
    notional_minor = 1_000_000
    assert UPBIT_KRW_COST.fee_bp == 5
    assert UPBIT_KRW_COST.slippage_bp_per_side == 10
    assert UPBIT_KRW_COST.side_cost_minor_units(notional_minor, side="buy") == 1_500
    assert UPBIT_KRW_COST.side_cost_minor_units(notional_minor, side="sell") == 1_500
    assert UPBIT_KRW_COST.round_trip_cost_minor_units(notional_minor) == 3_000
    assert BINANCE_USDT_COST.fee_bp == 10
    assert BINANCE_USDT_COST.slippage_bp_per_side == 10
    assert BINANCE_USDT_COST.round_trip_cost_minor_units(notional_minor) == 4_000


def test_crypto_prelisting_has_no_synthetic_bar_and_delist_uses_last_valid_close():
    adapter = CryptoVenueAdapter("upbit_krw")
    view = adapter.view_from_table(
        _table(
            adapter,
            [
                _row(
                    open_time_utc=datetime(2024, 6, 2, tzinfo=UTC),
                    close=42.0,
                )
            ],
        )
    )
    # Pre-listing: no bar on/before listing day.
    with pytest.raises(CryptoPrelistingBarsUnavailable):
        view.require_last_valid_bar("KRW-NEW", date(2024, 6, 1))
    last = view.require_last_valid_bar("KRW-NEW", date(2024, 6, 2))
    assert last.close == 42.0
    residual, events = view.liquidate_delisted(
        session_date=date(2024, 6, 2),
        held_symbols={"KRW-NEW"},
        delisted_as_of=frozenset({"KRW-NEW"}),
    )
    assert "KRW-NEW" not in residual
    assert events[0].last_close == 42.0
