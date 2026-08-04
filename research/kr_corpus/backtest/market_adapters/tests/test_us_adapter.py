"""US adapter golden tests: XNYS sessions, absent trading_value, costs."""

from __future__ import annotations

from datetime import date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loader import ManifestEntry, sha256_bytes
from market_adapters.us import (
    US_ADAPTER,
    US_DEFAULT_COST,
    US_SLIPPAGE_SENSITIVITY_COST,
    US_TRADING_VALUE_RESOLUTION,
    USBar,
    USCalendarError,
    is_xnys_early_close,
    is_xnys_session,
)
from pit import LookaheadViolation, assert_no_lookahead
from schema_contract import SchemaMismatchError


def _row(
    *,
    session_date: date | datetime = date(2024, 7, 3),
    symbol: str = "ABC",
) -> dict:
    if type(session_date) is date:
        session_ts = datetime(session_date.year, session_date.month, session_date.day)
    else:
        session_ts = session_date
    return {
        "symbol": symbol,
        "session_date": session_ts,
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 1000,
    }


def _table(rows: list[dict]) -> pa.Table:
    table = pa.Table.from_pylist(
        rows,
        schema=US_ADAPTER.corpus.arrow_schema_for("ohlcv"),
    )
    # US contract table_load_policy requires sealed survivorship label.
    meta = dict(table.schema.metadata or {})
    meta[b"SURVIVORSHIP_BIASED"] = b"TRUE"
    meta[b"corpus_id"] = b"us-corpus-v1"
    meta[b"price_mode"] = b"adjusted"
    return table.replace_schema_metadata(meta)


def test_us_trading_value_resolution_is_absent_declared():
    assert US_TRADING_VALUE_RESOLUTION == "ABSENT_DECLARED"
    assert "trading_value" not in USBar.__dataclass_fields__


def test_us_session_date_from_sealed_timestamp_ms():
    bars = US_ADAPTER.bars_from_table(_table([_row()]))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.session_date == date(2024, 7, 3)
    assert bar.market == "US"
    assert bar.volume == 1000


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
    assert bars[0].session_date == date(2024, 7, 3)


def test_us_schema_rejects_trading_value_column():
    """Forbidden: inventing/carrying trading_value on US tables."""
    # Build table then append forbidden column.
    base = _table([_row()])
    bad = base.append_column("trading_value", pa.array([101_000.0]))
    with pytest.raises(SchemaMismatchError) as exc_info:
        US_ADAPTER.corpus.validate_table_schema(bad, "ohlcv")
    assert "trading_value" in str(exc_info.value)


def test_us_contract_refuses_unlabeled_table_on_unwrapped_load(tmp_path):
    """US structural label gate: bare ContractBackedCorpusAdapter refuses strip."""
    from market_adapters.common import ContractBackedCorpusAdapter
    from market_adapters.us import US_HOLDOUT_POLICY
    from schema_contract import ContractTablePolicyError

    from research.us_corpus.labeling import UnlabeledCorpusError  # noqa: F401

    rel = "ohlcv/US/2024/u.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    stripped = _table([_row()]).replace_schema_metadata(None)
    pq.write_table(stripped, path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(path.read_bytes()),
        row_count=1,
        dataset="ohlcv",
        market="US",
        year=2024,
    )
    from schema_contract import CorpusKind

    bare = ContractBackedCorpusAdapter(
        corpus=CorpusKind.US_V1,
        holdout_policy=US_HOLDOUT_POLICY,
    )
    with pytest.raises(ContractTablePolicyError):
        bare.load_shard(tmp_path, entry)


def test_us_xnys_holiday_and_half_day_are_calendar_backed():
    assert not is_xnys_session(date(2024, 7, 4))  # Independence Day
    assert is_xnys_session(date(2024, 11, 29))  # Black Friday
    assert is_xnys_early_close(date(2024, 11, 29))
    assert not is_xnys_early_close(date(2024, 7, 4))


def test_us_rejects_non_xnys_session_row():
    table = _table([_row(session_date=date(2024, 7, 4))])
    with pytest.raises(USCalendarError):
        US_ADAPTER.bars_from_table(table)


def test_us_lookahead_goes_red_on_future_row():
    bars = US_ADAPTER.bars_from_table(
        _table(
            [
                _row(session_date=date(2024, 7, 2)),
                _row(session_date=date(2024, 7, 3)),
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
    assert "ABC" not in residual
    assert "STILL" in residual
    assert events[0].symbol == "ABC"
    assert events[0].last_close == 87.5
