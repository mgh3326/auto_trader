"""ROB-551: market valuation snapshot upsert chunking for Toss symbol master.

Toss symbol-master commits can produce thousands of market_cap-only valuation
rows. These pure unit tests pin statement chunking without touching Postgres or
calling external market-data providers.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql

from app.services.market_valuation_snapshots.repository import (
    _MAX_BIND_PARAMS,
    MarketValuationSnapshotsRepository,
    MarketValuationSnapshotUpsert,
    _chunk_rows_for_columns,
    _normalize_payload,
)


class _SpySession:
    """Minimal AsyncSession stand-in capturing executed statements."""

    def __init__(self, rowcounts: list[int] | None = None) -> None:
        self.statements: list = []
        self._rowcounts = list(rowcounts) if rowcounts is not None else None

    async def execute(self, stmt):  # noqa: ANN001 - test double
        idx = len(self.statements)
        self.statements.append(stmt)
        rowcount = self._rowcounts[idx] if self._rowcounts is not None else 0

        class _Result:
            rowcount: int

        result = _Result()
        result.rowcount = rowcount
        return result


def _repo(session: _SpySession) -> MarketValuationSnapshotsRepository:
    return MarketValuationSnapshotsRepository(cast(Any, session))


def _row(symbol: str) -> MarketValuationSnapshotUpsert:
    return MarketValuationSnapshotUpsert(
        market="kr",
        symbol=symbol,
        snapshot_date=dt.date(2026, 6, 14),
        source="toss_openapi",
        market_cap=Decimal("1234567890"),
        raw_payload={
            "source": "toss_openapi",
            "sharesOutstanding": "1000",
            "lastPrice": "1234567.89",
            "currency": "KRW",
        },
    )


def _bind_param_count(stmt) -> int:
    return len(stmt.compile(dialect=postgresql.dialect()).params)


def test_max_bind_params_below_asyncpg_ceiling():
    # asyncpg caps bound arguments per statement at 32767 (signed int16).
    assert _MAX_BIND_PARAMS <= 32767


@pytest.mark.parametrize("column_count", [1, 13, 25, 40, 120])
def test_chunk_rows_keeps_statement_under_bind_limit(column_count):
    chunk_rows = _chunk_rows_for_columns(column_count)
    assert chunk_rows >= 1
    assert chunk_rows * column_count <= _MAX_BIND_PARAMS


@pytest.mark.asyncio
async def test_empty_payload_returns_zero_without_execute():
    session = _SpySession()
    repo = _repo(session)
    assert await repo.upsert([]) == 0
    assert session.statements == []


@pytest.mark.asyncio
async def test_small_payload_executes_single_statement():
    session = _SpySession()
    repo = _repo(session)
    rows = [_row(f"S{i:04d}") for i in range(5)]

    await repo.upsert(rows)

    assert len(session.statements) == 1
    assert _bind_param_count(session.statements[0]) <= _MAX_BIND_PARAMS


@pytest.mark.asyncio
async def test_large_payload_chunked_into_bounded_statements():
    session = _SpySession()
    repo = _repo(session)
    n_rows = 3000
    rows = [_row(f"S{i:05d}") for i in range(n_rows)]

    await repo.upsert(rows)

    column_count = len(_normalize_payload(rows[0]))
    chunk_rows = _chunk_rows_for_columns(column_count)
    expected_chunks = -(-n_rows // chunk_rows)  # ceil division

    assert len(session.statements) == expected_chunks
    assert len(session.statements) > 1  # genuinely chunked, not one oversized stmt

    expected_sizes: list[int] = []
    remaining = n_rows
    while remaining > 0:
        take = min(chunk_rows, remaining)
        expected_sizes.append(take)
        remaining -= take
    for stmt, size in zip(session.statements, expected_sizes, strict=True):
        assert _bind_param_count(stmt) == column_count * size
        assert _bind_param_count(stmt) <= _MAX_BIND_PARAMS


@pytest.mark.asyncio
async def test_upsert_returns_summed_rowcount_across_chunks():
    n_rows = 3000
    rows = [_row(f"S{i:05d}") for i in range(n_rows)]
    column_count = len(_normalize_payload(rows[0]))
    chunk_rows = _chunk_rows_for_columns(column_count)

    rowcounts: list[int] = []
    remaining = n_rows
    while remaining > 0:
        take = min(chunk_rows, remaining)
        rowcounts.append(take)
        remaining -= take

    session = _SpySession(rowcounts=rowcounts)
    repo = _repo(session)
    total = await repo.upsert(rows)

    assert total == n_rows
