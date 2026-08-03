"""US adapter golden tests: ET session dates, XNYS, costs, and terminals."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loader import ManifestEntry, sha256_bytes
from market_adapters.us import (
    US_ADAPTER,
    US_DEFAULT_COST,
    US_SLIPPAGE_SENSITIVITY_COST,
    USCalendarError,
    USSessionDateMismatchError,
    is_xnys_early_close,
    is_xnys_session,
)
from pit import LookaheadViolation, assert_no_lookahead


def _row(
    *,
    timestamp_utc: str = "2024-07-03T20:00:00+00:00",
    session_date: str = "2024-07-03",
    symbol: str = "ABC",
) -> dict:
    return {
        "symbol": symbol,
        "timestamp_utc": timestamp_utc,
        "session_date": session_date,
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 1000.0,
        "trading_value": 101_000.0,
        "market": "US",
    }


def _table(rows: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(
        rows,
        schema=US_ADAPTER.corpus.arrow_schema_for("ohlcv"),
    )


def test_us_keeps_raw_utc_and_derives_et_session_date():
    bars = US_ADAPTER.bars_from_table(_table([_row()]))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timestamp_utc == datetime(2024, 7, 3, 20, 0, tzinfo=UTC)
    assert bar.session_date == date(2024, 7, 3)


def test_us_loads_synthetic_fixture_through_shared_sha_loader(tmp_path):
    rel = "ohlcv/US/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    pq.write_table(_table([_row()]), path)
    data = path.read_bytes()
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(data),
        row_count=1,
        dataset="ohlcv",
        market="US",
        year=2024,
    )

    bars = US_ADAPTER.load_shard(tmp_path, entry)
    assert bars[0].timestamp_utc == datetime(2024, 7, 3, 20, 0, tzinfo=UTC)


def test_us_rejects_kst_anchor_style_session_date_shift():
    """00:00 UTC is Jul-03 ET, so a Jul-04 declared day cannot pass silently."""
    table = _table(
        [
            _row(
                timestamp_utc="2024-07-04T00:00:00+00:00",
                session_date="2024-07-04",
            )
        ]
    )
    with pytest.raises(USSessionDateMismatchError) as exc_info:
        US_ADAPTER.bars_from_table(table)
    assert "derived ET date 2024-07-03" in str(exc_info.value)


def test_us_xnys_holiday_and_half_day_are_calendar_backed():
    assert not is_xnys_session(date(2024, 7, 4))  # Independence Day
    assert is_xnys_session(date(2024, 11, 29))  # Black Friday
    assert is_xnys_early_close(date(2024, 11, 29))
    assert not is_xnys_early_close(date(2024, 7, 4))


def test_us_rejects_non_xnys_session_row():
    table = _table(
        [
            _row(
                timestamp_utc="2024-07-04T20:00:00+00:00",
                session_date="2024-07-04",
            )
        ]
    )
    with pytest.raises(USCalendarError):
        US_ADAPTER.bars_from_table(table)


def test_us_lookahead_goes_red_on_future_row():
    bars = US_ADAPTER.bars_from_table(
        _table(
            [
                _row(
                    timestamp_utc="2024-07-02T20:00:00+00:00", session_date="2024-07-02"
                ),
                _row(
                    timestamp_utc="2024-07-03T20:00:00+00:00", session_date="2024-07-03"
                ),
            ]
        )
    )
    with pytest.raises(LookaheadViolation) as exc_info:
        assert_no_lookahead(bars, "2024-07-02")
    assert "2024-07-03" in str(exc_info.value)


def test_us_costs_are_per_side_integer_rounded_and_bidirectional():
    notional_minor = 1_000_000
    assert US_DEFAULT_COST.fee_bp == 0
    assert US_DEFAULT_COST.slippage_bp_per_side == 10
    assert US_DEFAULT_COST.side_cost_minor_units(notional_minor, side="buy") == 1_000
    assert US_DEFAULT_COST.side_cost_minor_units(notional_minor, side="sell") == 1_000
    assert US_DEFAULT_COST.round_trip_cost_minor_units(notional_minor) == 2_000
    assert US_SLIPPAGE_SENSITIVITY_COST.slippage_bp_per_side == 5
    assert (
        US_SLIPPAGE_SENSITIVITY_COST.round_trip_cost_minor_units(notional_minor)
        == 1_000
    )
    assert US_DEFAULT_COST.round_trip_cost_minor_units(1) == 2  # ceil each leg


def test_us_delist_terminal_event_is_explicit_when_present():
    residual, events = US_ADAPTER.terminalize_delisted(
        session_date=date(2024, 7, 3),
        held_symbols={"ABC", "STILL"},
        delisted_as_of=frozenset({"ABC"}),
        last_close_by_symbol={"ABC": 87.5},
    )
    assert residual == {"STILL"}
    assert len(events) == 1
    assert events[0].symbol == "ABC"
    assert events[0].reason == "delisted"
    assert events[0].last_close == 87.5
