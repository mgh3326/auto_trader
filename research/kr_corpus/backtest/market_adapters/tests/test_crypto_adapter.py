"""Crypto golden tests: UTC 24/7, venue isolation, costs, and delisting."""

from __future__ import annotations

from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loader import ManifestEntry, sha256_bytes
from market_adapters.crypto import (
    BINANCE_USDT_COST,
    UPBIT_KRW_COST,
    CryptoPrelistingBarsUnavailable,
    CryptoSessionDateMismatchError,
    CryptoVenueAdapter,
    CryptoVenueMixError,
)
from pit import LookaheadViolation, assert_no_lookahead


def _row(
    *,
    venue: str = "upbit_krw",
    timestamp_utc: str = "2024-06-01T00:00:00+00:00",
    session_date: str = "2024-06-01",
    symbol: str = "KRW-NEW",
    close: float = 100.0,
) -> dict:
    return {
        "symbol": symbol,
        "venue": venue,
        "timestamp_utc": timestamp_utc,
        "session_date": session_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
        "trading_value": close * 1000.0,
    }


def _table(adapter: CryptoVenueAdapter, rows: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(
        rows,
        schema=adapter.corpus.arrow_schema_for("ohlcv"),
    )


def test_crypto_uses_24_7_utc_calendar_including_weekend():
    adapter = CryptoVenueAdapter("upbit_krw")
    view = adapter.view_from_table(_table(adapter, [_row()]))  # Saturday 2024-06-01
    assert view.bars[0].session_date == date(2024, 6, 1)
    assert view.bars[0].timestamp_utc.date() == date(2024, 6, 1)


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


def test_crypto_rejects_non_utc_calendar_date_declaration():
    adapter = CryptoVenueAdapter("upbit_krw")
    table = _table(adapter, [_row(session_date="2024-05-31")])
    with pytest.raises(CryptoSessionDateMismatchError):
        adapter.view_from_table(table)


def test_crypto_lookahead_goes_red_on_future_row():
    adapter = CryptoVenueAdapter("upbit_krw")
    view = adapter.view_from_table(
        _table(
            adapter,
            [
                _row(
                    timestamp_utc="2024-06-01T00:00:00+00:00", session_date="2024-06-01"
                ),
                _row(
                    timestamp_utc="2024-06-02T00:00:00+00:00", session_date="2024-06-02"
                ),
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
        [_row(venue="upbit_krw"), _row(venue="binance_usdt", symbol="BTCUSDT")],
    )
    with pytest.raises(CryptoVenueMixError) as exc_info:
        adapter.view_from_table(table)
    assert "cannot enter" in str(exc_info.value)


def test_crypto_manifest_entry_from_other_venue_is_rejected_before_read(tmp_path):
    adapter = CryptoVenueAdapter("upbit_krw")
    entry = ManifestEntry(
        relative_path="ohlcv/binance_usdt/2024/bars.parquet",
        file_sha256="0" * 64,
        row_count=0,
        dataset="ohlcv",
        market="binance_usdt",
        year=2024,
    )
    with pytest.raises(CryptoVenueMixError):
        adapter.load_shard(tmp_path, entry)


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
                    timestamp_utc="2024-06-02T00:00:00+00:00",
                    session_date="2024-06-02",
                    close=101.0,
                ),
                _row(
                    timestamp_utc="2024-06-03T00:00:00+00:00",
                    session_date="2024-06-03",
                    close=123.45,
                ),
            ],
        )
    )
    assert view.bars_available_at("KRW-NEW", date(2024, 6, 1)) == ()
    with pytest.raises(CryptoPrelistingBarsUnavailable):
        view.require_last_valid_bar("KRW-NEW", date(2024, 6, 1))

    residual, events = view.liquidate_delisted(
        session_date=date(2024, 6, 4),
        held_symbols={"KRW-NEW", "KRW-OTHER"},
        delisted_as_of=frozenset({"KRW-NEW"}),
    )
    assert residual == {"KRW-OTHER"}
    assert len(events) == 1
    assert events[0].symbol == "KRW-NEW"
    assert events[0].last_close == 123.45
