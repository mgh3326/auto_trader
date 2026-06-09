"""ROB-442: bulk-upsert chunking + payload-key dedupe for KR DART fundamentals.

These are pure unit tests (no DB, no DART fetch) so chunking is provable in CI
without a Postgres backend or any DART budget consumption.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app.services.financial_fundamentals_snapshots.repository import (
    _MAX_BIND_PARAMS,
    FinancialFundamentalsSnapshotsRepository,
    FinancialFundamentalsUpsert,
    _chunk_rows_for_columns,
    _dedupe_payload,
    _normalize_payload,
)

_COLLECTED = dt.datetime(2026, 6, 6, 0, 0, tzinfo=dt.UTC)


def _row(
    symbol: str,
    *,
    fiscal_period: str = "2025A",
    net_income: int = 100,
    source: str = "dart",
    source_collected_at: dt.datetime = _COLLECTED,
) -> FinancialFundamentalsUpsert:
    return FinancialFundamentalsUpsert(
        market="kr",
        symbol=symbol,
        fiscal_period=fiscal_period,
        period_type="annual",
        period_end_date=dt.date(int(fiscal_period[:4]), 12, 31),
        source=source,
        source_collected_at=source_collected_at,
        net_income=Decimal(net_income),
    )


class _SpySession:
    """Minimal AsyncSession stand-in capturing executed statements.

    Avoids AsyncMock so ``result.rowcount`` is a real int (not a child mock),
    which the summed-rowcount assertion depends on.
    """

    def __init__(self, rowcounts: list[int] | None = None) -> None:
        self.statements: list = []
        self._rowcounts = list(rowcounts) if rowcounts is not None else None

    async def execute(self, stmt):  # noqa: ANN001 - test double
        idx = len(self.statements)
        self.statements.append(stmt)
        rowcount = self._rowcounts[idx] if self._rowcounts is not None else 0

        class _Result:
            pass

        result = _Result()
        result.rowcount = rowcount
        return result


def _bind_param_count(stmt) -> int:
    return len(stmt.compile(dialect=postgresql.dialect()).params)


# --------------------------------------------------------------------------- #
# chunk sizing helper
# --------------------------------------------------------------------------- #


def test_max_bind_params_below_asyncpg_ceiling():
    # asyncpg caps bound arguments per statement at 32767 (signed int16).
    assert _MAX_BIND_PARAMS <= 32767


@pytest.mark.parametrize("column_count", [1, 22, 25, 40, 120])
def test_chunk_rows_keeps_statement_under_bind_limit(column_count):
    chunk_rows = _chunk_rows_for_columns(column_count)
    assert chunk_rows >= 1
    assert chunk_rows * column_count <= _MAX_BIND_PARAMS


# --------------------------------------------------------------------------- #
# payload-key dedupe (key = market, symbol, fiscal_period, source)
# --------------------------------------------------------------------------- #


def test_dedupe_keeps_distinct_keys_untouched():
    rows = [
        _normalize_payload(_row("005930", fiscal_period="2024A")),
        _normalize_payload(_row("005930", fiscal_period="2025A")),
    ]
    deduped, dropped = _dedupe_payload(rows)
    assert dropped == 0
    assert len(deduped) == 2


def test_dedupe_distinguishes_by_source():
    rows = [
        _normalize_payload(_row("005930", source="dart")),
        _normalize_payload(_row("005930", source="naver")),
    ]
    deduped, dropped = _dedupe_payload(rows)
    assert dropped == 0
    assert {r["source"] for r in deduped} == {"dart", "naver"}


def test_dedupe_keeps_latest_by_source_collected_at():
    early = _normalize_payload(
        _row(
            "005930",
            net_income=100,
            source_collected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        )
    )
    late = _normalize_payload(
        _row(
            "005930",
            net_income=999,
            source_collected_at=dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
        )
    )
    deduped, dropped = _dedupe_payload([early, late])
    assert dropped == 1
    assert len(deduped) == 1
    assert deduped[0]["net_income"] == Decimal(999)


def test_dedupe_keeps_latest_regardless_of_input_order():
    early = _normalize_payload(
        _row(
            "005930",
            net_income=100,
            source_collected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        )
    )
    late = _normalize_payload(
        _row(
            "005930",
            net_income=999,
            source_collected_at=dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
        )
    )
    deduped, dropped = _dedupe_payload([late, early])
    assert dropped == 1
    assert deduped[0]["net_income"] == Decimal(999)


def test_dedupe_tie_breaks_last_write_wins_in_both_orders():
    # Equal source_collected_at is the common case (one job run shares a single
    # collected_at). The contract is last-write-wins within the payload:
    # deterministic for a given order, and the order is the builder's stable
    # per-symbol parse order. Pin both directions so the behaviour is explicit.
    a = _normalize_payload(
        _row("005930", net_income=100, source_collected_at=_COLLECTED)
    )
    b = _normalize_payload(
        _row("005930", net_income=222, source_collected_at=_COLLECTED)
    )

    deduped_ab, dropped_ab = _dedupe_payload([a, b])
    assert dropped_ab == 1
    assert deduped_ab[0]["net_income"] == Decimal(222)  # b is last → b wins

    deduped_ba, dropped_ba = _dedupe_payload([b, a])
    assert dropped_ba == 1
    assert deduped_ba[0]["net_income"] == Decimal(100)  # a is last → a wins


# --------------------------------------------------------------------------- #
# upsert chunking behaviour (mock session)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_payload_returns_zero_without_execute():
    session = _SpySession()
    repo = FinancialFundamentalsSnapshotsRepository(session)
    assert await repo.upsert([]) == 0
    assert session.statements == []


@pytest.mark.asyncio
async def test_small_payload_executes_single_statement():
    session = _SpySession()
    repo = FinancialFundamentalsSnapshotsRepository(session)
    rows = [_row(f"S{i:04d}") for i in range(5)]
    await repo.upsert(rows)
    assert len(session.statements) == 1
    assert _bind_param_count(session.statements[0]) <= _MAX_BIND_PARAMS


@pytest.mark.asyncio
async def test_large_payload_chunked_into_bounded_statements():
    session = _SpySession()
    repo = FinancialFundamentalsSnapshotsRepository(session)
    n_rows = 3000
    rows = [_row(f"S{i:05d}") for i in range(n_rows)]

    await repo.upsert(rows)

    column_count = len(_normalize_payload(rows[0]))
    chunk_rows = _chunk_rows_for_columns(column_count)
    expected_chunks = -(-n_rows // chunk_rows)  # ceil division

    assert len(session.statements) == expected_chunks
    assert len(session.statements) > 1  # genuinely chunked, not one oversized stmt

    # Each statement carries exactly column_count * rows_in_chunk binds, and
    # stays under the ceiling — proving the chunk math (not just an upper bound).
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

    # each chunk reports its own row count as rowcount
    rowcounts: list[int] = []
    remaining = n_rows
    while remaining > 0:
        take = min(chunk_rows, remaining)
        rowcounts.append(take)
        remaining -= take

    session = _SpySession(rowcounts=rowcounts)
    repo = FinancialFundamentalsSnapshotsRepository(session)
    total = await repo.upsert(rows)
    assert total == n_rows


@pytest.mark.asyncio
async def test_duplicate_keys_collapsed_before_chunking():
    # Same conflict key repeated would otherwise trigger Postgres
    # "ON CONFLICT DO UPDATE command cannot affect row a second time". This
    # unit test pins the collapse logic; the real-DB regression that would fail
    # on the old single-statement code is
    # test_upsert_dedupes_duplicate_keys_in_one_call_keeping_latest (repository).
    session = _SpySession()
    repo = FinancialFundamentalsSnapshotsRepository(session)
    rows = [
        _row(
            "005930",
            net_income=100,
            source_collected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        ),
        _row(
            "005930",
            net_income=999,
            source_collected_at=dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
        ),
    ]
    await repo.upsert(rows)
    assert len(session.statements) == 1
    # one row in the statement (the latest), not two
    assert _bind_param_count(session.statements[0]) == len(_normalize_payload(rows[0]))
