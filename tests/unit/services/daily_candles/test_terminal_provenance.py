"""ROB-1038 typed provenance for terminal-close daily rows."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.daily_candles.converters import frame_to_rows
from app.services.daily_candles.provenance import (
    DAILY_SOURCE_CONTRACTS,
    daily_source_row_id,
)
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

pytestmark = pytest.mark.unit


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": date(2026, 6, 5),
                "open": 128.0,
                "high": 135.0,
                "low": 125.0,
                "close": 129.0,
                "adj_close": 128.5,
                "volume": 1000.0,
                "value": 129000.0,
            }
        ]
    )


@pytest.mark.parametrize(
    ("source", "expected_version", "expected_basis"),
    [
        ("kis", "kis-adjusted-daily-v1", "provider_adjusted"),
        ("toss", "toss-adjusted-daily-v1", "provider_adjusted"),
        ("toss_fallback", "toss-adjusted-daily-v1", "provider_adjusted"),
        ("yahoo", "yahoo-raw-daily-v1", "raw"),
        ("yahoo_fallback", "yahoo-raw-daily-v1", "raw"),
    ],
)
def test_frame_writer_stamps_exact_source_contract(
    source: str,
    expected_version: str,
    expected_basis: str,
):
    [row] = frame_to_rows(
        _frame(),
        symbol="SMCI",
        partition="NASD",
        source=source,
        final_through_date=date(2026, 6, 5),
    )

    assert row.is_final is True
    assert row.session_scope == "regular"
    assert row.source_row_version == expected_version
    assert row.price_basis == expected_basis
    assert row.source_row_id == daily_source_row_id(row)
    assert DAILY_SOURCE_CONTRACTS[source].price_basis == expected_basis


def test_writer_marks_forming_or_calendar_unknown_row_non_final():
    [forming] = frame_to_rows(
        _frame(),
        symbol="SMCI",
        partition="NASD",
        source="kis",
        final_through_date=date(2026, 6, 4),
    )
    [calendar_unknown] = frame_to_rows(
        _frame(),
        symbol="SMCI",
        partition="NASD",
        source="kis",
        final_through_date=None,
    )

    assert forming.is_final is False
    assert calendar_unknown.is_final is False
    assert forming.source_row_id == daily_source_row_id(forming)
    assert calendar_unknown.source_row_version == "kis-adjusted-daily-v1"


def test_content_correction_changes_source_row_identity():
    [original] = frame_to_rows(
        _frame(),
        symbol="SMCI",
        partition="NASD",
        source="kis",
        final_through_date=date(2026, 6, 5),
    )
    corrected = replace(original, close=131.0, value=131000.0)

    assert daily_source_row_id(corrected) != original.source_row_id


@pytest.mark.asyncio
async def test_repository_writes_and_reads_all_provenance_fields():
    session = MagicMock()
    write_result = MagicMock()
    write_result.rowcount = 1
    session.execute = AsyncMock(return_value=write_result)
    repo = DailyCandlesRepository(session=session)
    [row] = frame_to_rows(
        _frame(),
        symbol="SMCI",
        partition="NASD",
        source="kis",
        final_through_date=date(2026, 6, 5),
    )

    await repo.upsert_rows(market=MarketKey.US, rows=[row])
    statement, payload = session.execute.await_args.args

    assert "is_final" in str(statement)
    assert payload[0]["is_final"] is True
    assert payload[0]["session_scope"] == "regular"
    assert payload[0]["source_row_id"] == row.source_row_id
    assert payload[0]["source_row_version"] == "kis-adjusted-daily-v1"
    assert payload[0]["price_basis"] == "provider_adjusted"

    mapping_result = MagicMock()
    mapping_result.mappings.return_value.all.return_value = [
        {
            "time": row.time_utc,
            "symbol": row.symbol,
            "partition": row.partition,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "adj_close": row.adj_close,
            "volume": row.volume,
            "value": row.value,
            "source": row.source,
            "is_final": row.is_final,
            "session_scope": row.session_scope,
            "source_row_id": row.source_row_id,
            "source_row_version": row.source_row_version,
            "price_basis": row.price_basis,
            "ingested_at": datetime(2026, 6, 5, 23, tzinfo=UTC),
        }
    ]
    session.execute = AsyncMock(return_value=mapping_result)
    [fetched] = await repo.fetch_range(
        market=MarketKey.US,
        symbol="SMCI",
        partition="NASD",
        start=datetime(2026, 6, 5, tzinfo=UTC),
        end=datetime(2026, 6, 6, tzinfo=UTC),
        for_share=True,
    )
    read_statement = session.execute.await_args.args[0]

    assert "FOR SHARE" in str(read_statement)
    assert fetched.is_final is True
    assert fetched.session_scope == "regular"
    assert fetched.source_row_id == row.source_row_id
    assert fetched.source_row_version == "kis-adjusted-daily-v1"
    assert fetched.price_basis == "provider_adjusted"
    assert fetched.ingested_at == datetime(2026, 6, 5, 23, tzinfo=UTC)
